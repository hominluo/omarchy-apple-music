"""The daemon's side of the account (F2, F20, F21, F22, F43; contract C5).

Apple is never called: AppleApi.fetch_account is replaced, and so are the
browser parts of auth (launch, wait, close). The rest of auth runs for real
on temp dirs, so the signed-out flag goes through account.json.
"""

import asyncio
import logging
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import auth, config, daemon as daemon_module  # noqa: E402
from engine.apple import ApiError, AppleApi  # noqa: E402
from engine.daemon import Daemon  # noqa: E402

_LOGGER = logging.getLogger("apple-music")
_LEVEL = _LOGGER.level


def setUpModule():
    # The refusals below are the point; their warnings are not news.
    _LOGGER.setLevel(logging.ERROR)


def tearDownModule():
    _LOGGER.setLevel(_LEVEL)


ACCOUNT_GB = {"data": [{"attributes": {"displayName": "Ann"}}],
              "meta": {"subscription": {"storefront": "gb", "active": True}}}


def refused(status=403):
    return ApiError("refused", status=status, code="unauthorized")


class AuthDaemonCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="am-dauth-")
        root = Path(self.tmp.name)
        for name in ("run", "data", "cache", "home"):
            (root / name).mkdir()
        self.env = mock.patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(root / "run"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "HOME": str(root / "home"),
        })
        self.env.start()
        # Nothing here may reach a real browser.
        self.close = mock.patch.object(auth, "close_login", return_value=True)
        self.close_login = self.close.start()
        self.events = []

    def tearDown(self):
        self.close.stop()
        self.env.stop()
        self.tmp.cleanup()

    def daemon(self) -> Daemon:
        d = Daemon()

        async def broadcast(event, data):
            self.events.append((event, data))
        d.broadcast = broadcast
        return d

    def auth_events(self):
        return [data for event, data in self.events if event == "auth"]

    def notices(self):
        return [data["message"] for event, data in self.events if event == "notice"]

    def fetch(self, answers: dict):
        """fetch_account by token: an account dict, or an exception."""
        async def fetch_account(api):
            answer = answers[api.media_user_token]
            if isinstance(answer, Exception):
                if getattr(answer, "code", "") == "unauthorized" and api.on_unauthorized:
                    api.on_unauthorized(api, api.media_user_token, answer)
                raise answer
            api.account = answer
            api.storefront = answer["meta"]["subscription"]["storefront"]
            return answer
        return mock.patch.object(AppleApi, "fetch_account", autospec=True, side_effect=fetch_account)


class Startup(AuthDaemonCase):
    async def test_signed_out_stays_signed_out(self):
        """F21: the next start found the profile's cookie and signed back in."""
        auth.sign_out()
        d = self.daemon()
        with mock.patch.object(auth, "discover", side_effect=AssertionError("discover ran")):
            self.assertFalse(await d._refresh_api())
        state = d.auth_state()
        self.assertFalse(state["signedIn"])
        self.assertTrue(state["signedOut"])

    async def test_without_the_flag_a_session_is_discovered(self):
        d = self.daemon()
        with mock.patch.object(auth, "discover", return_value=(Path("/p"), "FOUND")), \
                self.fetch({"FOUND": ACCOUNT_GB}):
            self.assertTrue(await d._refresh_api())
        self.assertTrue(d.auth_state()["signedIn"])
        self.assertEqual(auth.load()["mediaUserToken"], "FOUND")

    async def test_a_401_user_token_is_a_dead_session(self):
        """F22: only a 403 counted; a 401 surviving the re-scrape did not."""
        auth.save("DEAD", storefront="us")
        d = self.daemon()
        with self.fetch({"DEAD": refused(401)}):
            self.assertFalse(await d._refresh_api())
        state = d.auth_state()
        self.assertFalse(state["signedIn"])
        self.assertTrue(state["expired"])

    async def test_storefront_is_the_accounts(self):
        """F43 (data side): the label read settings' "us" for a GB account."""
        auth.save("TOKEN", storefront="")
        d = self.daemon()
        with self.fetch({"TOKEN": ACCOUNT_GB}):
            await d._refresh_api()
        self.assertEqual(d.settings["storefront"], "us")
        self.assertEqual(d.auth_state()["storefront"], "gb")
        self.assertEqual(d.api.storefront, "gb")


class MidSession(AuthDaemonCase):
    async def signed_in(self, answers):
        auth.save("TOKEN", storefront="gb")
        d = self.daemon()
        with self.fetch({"TOKEN": ACCOUNT_GB}):
            await d._refresh_api()
        d._verified_at = None  # as if the startup check was long ago
        self.events.clear()
        return d

    async def report(self, d, token="TOKEN", api=None):
        api = api or d.api
        # Reports come from requests' worker threads.
        await asyncio.to_thread(api.on_unauthorized, api, token, refused())
        for _ in range(20):
            await asyncio.sleep(0)
        if d._verify_task:
            await asyncio.wait({d._verify_task}, timeout=2)

    async def test_a_dead_token_mid_session_signs_out_the_ui(self):
        """F22: library/webPlayback 403s never touched the auth slice."""
        d = await self.signed_in({})
        with self.fetch({"TOKEN": refused()}):
            await self.report(d)
        state = d.auth_state()
        self.assertTrue(state["expired"])
        self.assertFalse(state["signedIn"])
        self.assertTrue(self.auth_events()[-1]["expired"])
        self.assertTrue(any("signed you out" in n for n in self.notices()))
        # The token is kept on disk for the next start to check again.
        self.assertEqual(auth.load()["mediaUserToken"], "TOKEN")

    async def test_one_refusal_with_a_good_account_changes_nothing(self):
        d = await self.signed_in({})
        with self.fetch({"TOKEN": ACCOUNT_GB}) as fetch:
            await self.report(d)
            # Confirmed good just now: the next refusal is not probed.
            await self.report(d)
        self.assertEqual(fetch.call_count, 1)
        self.assertFalse(d.auth_state()["expired"])
        self.assertEqual(self.auth_events(), [])

    async def test_a_network_failure_says_nothing_about_the_token(self):
        d = await self.signed_in({})
        with self.fetch({"TOKEN": ApiError("down", code="network")}):
            await self.report(d)
        self.assertFalse(d.auth_state()["expired"])

    async def test_a_replaced_session_is_not_probed(self):
        d = await self.signed_in({})
        old = d.api
        with self.fetch({"TOKEN": ACCOUNT_GB}):
            await d._refresh_api()
        with self.fetch({"TOKEN": refused()}) as fetch:
            await self.report(d, api=old)
        self.assertEqual(fetch.call_count, 0)

    async def test_cmd_account_is_the_probe(self):
        d = await self.signed_in({})
        with self.fetch({"TOKEN": refused()}) as fetch:
            with self.assertRaises(ApiError):
                await d.cmd_account({})
            for _ in range(20):
                await asyncio.sleep(0)
        self.assertEqual(fetch.call_count, 1)
        self.assertTrue(d.auth_state()["expired"])


class SignIn(AuthDaemonCase):
    def browser(self, tokens, running=True):
        """launch/wait/running stand-ins; wait hands out `tokens` in order."""
        calls = []
        queue = list(tokens)

        def wait_for_token(timeout, **kwargs):
            calls.append({k: (set(v) if k == "reject" else v) for k, v in kwargs.items()})
            return queue.pop(0) if queue else None
        patches = [
            mock.patch.object(auth, "launch_login", return_value={"running": True}),
            mock.patch.object(auth, "wait_for_token", side_effect=wait_for_token),
            mock.patch.object(auth, "login_running", return_value=running),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return calls

    async def finish(self, d):
        await asyncio.wait_for(d._auth_task, 5)

    async def test_signin_saves_checks_and_closes(self):
        auth.sign_out()
        d = self.daemon()
        calls = self.browser(["NEW"])
        with self.fetch({"NEW": ACCOUNT_GB}):
            result = await d.cmd_signin({})
            await self.finish(d)
        self.assertTrue(result["waiting"])
        self.assertFalse(calls[0]["close"])
        self.assertEqual(calls[0]["reject"], set())
        self.close_login.assert_called()
        state = d.auth_state()
        self.assertTrue(state["signedIn"])
        self.assertFalse(state["signedOut"])  # an explicit sign-in clears the flag
        self.assertEqual(state["storefront"], "gb")
        self.assertIn("Signed in to Apple Music", self.notices())

    async def test_the_dead_token_is_rejected_and_a_refused_one_is_waited_past(self):
        """F20: the old cookie was saved at once and the real sign-in lost."""
        auth.save("DEAD", storefront="us")
        d = self.daemon()
        with self.fetch({"DEAD": refused()}):
            await d._refresh_api()
        calls = self.browser(["STILL-BAD", "GOOD"], running=True)
        with self.fetch({"STILL-BAD": refused(), "GOOD": ACCOUNT_GB}):
            await d.cmd_signin({})
            await self.finish(d)
        self.assertEqual(calls[0]["reject"], {"DEAD"})
        self.assertEqual(calls[1]["reject"], {"DEAD", "STILL-BAD"})
        self.assertEqual(auth.load()["mediaUserToken"], "GOOD")
        self.assertTrue(d.auth_state()["signedIn"])

    async def test_a_closed_window_reports_it(self):
        d = self.daemon()
        self.browser([None])
        await d.cmd_signin({})
        await self.finish(d)
        self.assertIn("Sign-in window closed without a session", self.notices())

    async def test_signin_waits_for_the_startup_check(self):
        """The startup check's save used to be able to land on top of it."""
        d = self.daemon()
        gate = asyncio.Event()

        async def slow_session():
            await gate.wait()
        d._session_task = asyncio.create_task(slow_session())
        calls = self.browser(["NEW"])
        with self.fetch({"NEW": ACCOUNT_GB}):
            await d.cmd_signin({})
            await asyncio.sleep(0.05)
            self.assertEqual(calls, [])
            gate.set()
            await self.finish(d)
        self.assertEqual(len(calls), 1)

    async def test_signin_is_a_free_command(self):
        self.assertIn("signin", daemon_module.FREE_COMMANDS)
        self.assertIn("signOut", daemon_module.FREE_COMMANDS)


class SignOut(AuthDaemonCase):
    async def test_sign_out_cancels_a_sign_in_and_sticks(self):
        auth.save("TOKEN", storefront="gb")
        d = self.daemon()
        with self.fetch({"TOKEN": ACCOUNT_GB}):
            await d._refresh_api()
        release = threading.Event()

        def wait_for_token(timeout, cancel=None, **_kwargs):
            cancel.wait(5)
            release.set()
            return None
        with mock.patch.object(auth, "launch_login", return_value={"running": True}), \
                mock.patch.object(auth, "wait_for_token", side_effect=wait_for_token):
            await d.cmd_signin({})
            await asyncio.sleep(0.05)
            state = await d.cmd_signOut({})
            self.assertTrue(await asyncio.to_thread(release.wait, 5))
        self.assertFalse(state["signedIn"])
        self.assertTrue(state["signedOut"])
        self.assertEqual(d.api.media_user_token, "")
        self.assertTrue(self.auth_events()[-1]["signedOut"])
        self.assertTrue(auth.signed_out())
        # And the old command name still works.
        self.assertTrue((await d.cmd_signout({}))["signedOut"])

    async def test_shutdown_stops_the_waiter_thread(self):
        d = self.daemon()
        d._auth_cancel = threading.Event()
        with mock.patch.object(d.player, "terminate", mock.AsyncMock()):
            await d.shutdown()
        self.assertTrue(d._auth_cancel.is_set())


class LateStartupCheck(AuthDaemonCase):
    """G5/G17: the startup account check outlived STARTUP_WAIT and then
    saved its token over a sign-out, or marked a newer sign-in expired."""

    def gated_fetch(self, gate: asyncio.Event, answers: dict):
        async def fetch_account(api):
            token = api.media_user_token
            await gate.wait()
            answer = answers[token]
            if isinstance(answer, Exception):
                raise answer
            api.account = answer
            api.storefront = answer["meta"]["subscription"]["storefront"]
            return answer
        return mock.patch.object(AppleApi, "fetch_account", autospec=True, side_effect=fetch_account)

    async def test_a_late_check_does_not_undo_a_sign_out(self):
        auth.save("OLD", storefront="gb")
        d = self.daemon()
        gate = asyncio.Event()
        with self.gated_fetch(gate, {"OLD": ACCOUNT_GB}):
            d._begin_session()
            await asyncio.sleep(0.05)
            state = await asyncio.wait_for(d.cmd_signOut({}), 2)
            gate.set()
            await asyncio.sleep(0.05)
        self.assertTrue(state["signedOut"])
        record = auth.load()
        self.assertEqual(record["mediaUserToken"], "")
        self.assertTrue(record["signedOut"])
        self.assertTrue(d.ready)
        self.assertIn(("engine", d.snapshot()["engine"]), self.events)

    async def test_a_check_that_finishes_after_a_sign_out_drops_its_result(self):
        # The same race without the cancel: the generation alone holds.
        auth.save("OLD", storefront="gb")
        d = self.daemon()
        gate = asyncio.Event()
        with self.gated_fetch(gate, {"OLD": ACCOUNT_GB}):
            check = asyncio.create_task(d._refresh_api())
            await asyncio.sleep(0.05)
            d._session_gen += 1
            d.account_record = auth.sign_out()
            gate.set()
            self.assertFalse(await check)
        self.assertTrue(auth.signed_out())

    async def test_a_late_refusal_does_not_mark_a_newer_sign_in_expired(self):
        auth.save("OLD", storefront="gb")
        d = self.daemon()
        gate = asyncio.Event()
        with self.gated_fetch(gate, {"OLD": refused()}):
            check = asyncio.create_task(d._refresh_api())
            await asyncio.sleep(0.05)
            d._session_gen += 1  # what _await_login does before saving
            d.account_record = auth.save("NEW", storefront="gb")
            gate.set()
            self.assertFalse(await check)
        self.assertFalse(d.account_record.get("expired"))
        self.assertEqual(auth.load()["mediaUserToken"], "NEW")

    async def test_signin_saves_over_a_check_that_outlived_the_wait(self):
        auth.save("OLD", storefront="gb")
        d = self.daemon()
        gate = asyncio.Event()
        with mock.patch.object(daemon_module, "STARTUP_WAIT", 0.05), \
                mock.patch.object(auth, "launch_login", return_value={"running": True}), \
                mock.patch.object(auth, "wait_for_token", return_value="NEW"), \
                mock.patch.object(auth, "login_running", return_value=True), \
                self.gated_fetch(gate, {"OLD": ACCOUNT_GB, "NEW": ACCOUNT_GB}):
            d._begin_session()
            await asyncio.sleep(0.01)
            await d.cmd_signin({})
            await asyncio.sleep(0.2)
            gate.set()
            await asyncio.wait_for(d._auth_task, 2)
        self.assertEqual(auth.load()["mediaUserToken"], "NEW")
        self.assertTrue(d._session_task.cancelled())


class SignInLifetime(AuthDaemonCase):
    """G18: the sign-in task was untracked; a cancel before the waiter
    started left the window open, and a failure vanished."""

    async def test_cancel_during_the_startup_wait_closes_the_window(self):
        d = self.daemon()
        d._session_task = asyncio.create_task(asyncio.sleep(30))
        with mock.patch.object(auth, "launch_login", return_value={"running": True}), \
                mock.patch.object(auth, "wait_for_token", side_effect=AssertionError("waited")):
            await d.cmd_signin({})
            self.assertIn(d._auth_task, d.tasks)
            await asyncio.sleep(0.02)
            self.close_login.reset_mock()
            d._auth_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await d._auth_task
            for _ in range(50):
                if self.close_login.called:
                    break
                await asyncio.sleep(0.01)
        self.close_login.assert_called()
        d._session_task.cancel()

    async def test_shutdown_cancels_the_sign_in(self):
        d = self.daemon()
        d._session_task = asyncio.create_task(asyncio.sleep(30))
        with mock.patch.object(auth, "launch_login", return_value={"running": True}), \
                mock.patch.object(d.player, "terminate", mock.AsyncMock()):
            await d.cmd_signin({})
            task = d._auth_task
            await d.shutdown()
            await asyncio.wait({task}, timeout=2)
        self.assertTrue(task.cancelled())

    async def test_a_failed_sign_in_is_reported(self):
        d = self.daemon()
        with mock.patch.object(auth, "launch_login", return_value={"running": True}), \
                mock.patch.object(auth, "wait_for_token", side_effect=OSError("socket went away")), \
                self.assertLogs("apple-music.daemon", "ERROR"):
            await d.cmd_signin({})
            await asyncio.wait({d._auth_task}, timeout=2)
            for _ in range(50):
                if any("Sign-in failed" in n for n in self.notices()):
                    break
                await asyncio.sleep(0.01)
        self.assertTrue(any("socket went away" in n for n in self.notices()))
        self.assertTrue(self.auth_events())

    async def test_a_cancel_mid_launch_closes_what_it_opened(self):
        import threading as _threading

        d = self.daemon()
        launched = _threading.Event()
        release = _threading.Event()

        def slow_launch(storefront):
            launched.set()
            release.wait(5)
            return {"running": True}
        with mock.patch.object(auth, "launch_login", side_effect=slow_launch):
            call = asyncio.create_task(d.cmd_signin({}))
            await asyncio.to_thread(launched.wait, 5)
            call.cancel()
            await asyncio.sleep(0.05)
            self.close_login.reset_mock()
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await call
            for _ in range(50):
                if self.close_login.called:
                    break
                await asyncio.sleep(0.01)
        self.close_login.assert_called()
        self.assertIsNone(d._auth_task)


class AuthSlice(AuthDaemonCase):
    async def test_has_token_covers_an_expired_session(self):
        """G26: the UI offers Sign out whenever a token is stored."""
        auth.save("DEAD", storefront="us")
        d = self.daemon()
        with self.fetch({"DEAD": refused()}):
            await d._refresh_api()
        state = d.auth_state()
        self.assertFalse(state["signedIn"])
        self.assertTrue(state["expired"])
        self.assertTrue(state["hasToken"])
        auth.sign_out()
        self.assertFalse(self.daemon().auth_state()["hasToken"])

    async def test_a_sign_out_that_left_the_session_says_so(self):
        """G12: sign-out reported success with the session still in the profile."""
        d = self.daemon()
        dirty = dict(auth.DEFAULT_ACCOUNT, signedOut=True, profileDirty=True)
        with mock.patch.object(auth, "sign_out", return_value=dirty):
            state = await d.cmd_signOut({})
        self.assertTrue(state["signedOut"])
        self.assertTrue(any("would not close" in n for n in self.notices()))


if __name__ == "__main__":
    unittest.main()
