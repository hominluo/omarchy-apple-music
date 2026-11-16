"""Sign-in, sign-out and the cookie readers (F2, F20, F21, F38, F39).

The browser is tests/fake_chrome.py behind a wrapper script, so the real
launch_login, wait_for_token, cdp client and close path run against a
process that behaves like Chrome on the parts they touch: a profile lock that
appears late, DevToolsActivePort, /json, a page websocket and Browser.close.
Every path points into a temp dir; no real browser, keyring or Apple.
"""

import os
import sqlite3
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import auth, cdp, config  # noqa: E402

FAKE_CHROME = Path(__file__).resolve().parent / "fake_chrome.py"
_CHROME_EPOCH = 11644473600


def chrome_time(offset_s: float) -> int:
    return int((time.time() + offset_s + _CHROME_EPOCH) * 1_000_000)


def make_cookie_db(path: Path, rows) -> None:
    """rows: (host, name, plaintext value, expires_utc)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(path)) as connection:
        connection.execute(
            "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
            "encrypted_value BLOB, expires_utc INTEGER, last_access_utc INTEGER)")
        for index, (host, name, value, expires) in enumerate(rows):
            connection.execute(
                "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?)",
                (host, name, value, b"", expires, index))


class AuthCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="am-auth-")
        root = Path(self.tmp.name)
        for name in ("run", "data", "cache", "home"):
            (root / name).mkdir()
        self.root = root
        self.env = mock.patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(root / "run"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "HOME": str(root / "home"),
            "FAKE_CHROME_TOKEN_FILE": str(root / "page-token"),
            "FAKE_CHROME_LOG": str(root / "chrome.log"),
        })
        self.env.start()
        wrapper = root / "chrome"
        # Like /usr/bin/google-chrome-stable: a script that execs the browser.
        wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE_CHROME} \"$@\"\n", "utf-8")
        wrapper.chmod(0o755)
        self.find = mock.patch.object(config, "find_chrome", return_value=str(wrapper))
        self.find.start()
        self.profile = config.login_profile()

    def tearDown(self):
        # Never leave a fake browser behind.
        pid = cdp.browser_pid(self.profile)
        if pid:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
        proc = auth._launched
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(5)
        auth._launched = None
        self.find.stop()
        self.env.stop()
        self.tmp.cleanup()

    def page(self, token: str) -> None:
        (self.root / "page-token").write_text(token, "utf-8")

    def chrome_log(self) -> str:
        try:
            return (self.root / "chrome.log").read_text("utf-8")
        except OSError:
            return ""


class SlowBrowserStart(AuthCase):
    def test_first_signin_survives_a_cold_start(self):
        """F2: the first poll used to find no lock and report the window
        closed while it was still opening."""
        with mock.patch.dict(os.environ, {"FAKE_CHROME_DELAY": "1.0"}):
            auth.launch_login("gb")
        threading.Timer(1.6, self.page, args=("NEWTOKEN",)).start()
        started = time.monotonic()
        token = auth.wait_for_token(timeout=15, interval=0.1)
        self.assertEqual(token, "NEWTOKEN")
        self.assertGreater(time.monotonic() - started, 1.0)
        # F39: closed once the token is in hand, its port file with it.
        self.assertFalse(auth.login_running())
        self.assertIn("Browser.close /devtools/browser/B1", self.chrome_log())
        self.assertFalse((self.profile / "DevToolsActivePort").exists())

    def test_a_browser_that_never_appears_is_given_up_on(self):
        with mock.patch.object(auth, "LOGIN_STARTUP_GRACE", 0.5), \
                mock.patch.object(config, "find_chrome", return_value="/bin/true"):
            auth.launch_login()
            started = time.monotonic()
            self.assertIsNone(auth.wait_for_token(timeout=10, interval=0.1))
        self.assertLess(time.monotonic() - started, 5)

    def test_closing_the_window_returns_what_it_flushed(self):
        auth.launch_login()
        deadline = time.monotonic() + 5
        while not auth.login_running() and time.monotonic() < deadline:
            time.sleep(0.05)

        def user_closes():
            make_cookie_db(self.profile / "Default" / "Cookies",
                           [(".music.apple.com", auth.COOKIE_NAME, "FLUSHED", 0)])
            os.kill(cdp.browser_pid(self.profile), 15)
        threading.Timer(0.5, user_closes).start()
        self.assertEqual(auth.wait_for_token(timeout=10, interval=0.1), "FLUSHED")


class StaleTokens(AuthCase):
    def test_the_token_being_replaced_is_not_taken_again(self):
        """F20: the dead cookie on disk (and still in the page) was saved the
        instant the browser launched."""
        make_cookie_db(self.profile / "Default" / "Cookies",
                       [(".music.apple.com", auth.COOKIE_NAME, "OLD", chrome_time(86400))])
        self.page("OLD")
        auth.launch_login()
        threading.Timer(1.0, self.page, args=("FRESH",)).start()
        self.assertEqual(auth.wait_for_token(timeout=10, interval=0.1, reject={"OLD"}), "FRESH")

    def test_expired_cookies_are_ignored(self):
        make_cookie_db(self.profile / "Default" / "Cookies", [
            (".music.apple.com", auth.COOKIE_NAME, "EXPIRED", chrome_time(-60)),
        ])
        self.assertIsNone(auth.read_profile_token(self.profile))
        make_cookie_db(self.profile / "Network" / "Cookies", [
            (".music.apple.com", auth.COOKIE_NAME, "SESSION", 0),
        ])
        os.utime(self.profile / "Network" / "Cookies", (time.time() + 5, time.time() + 5))
        self.assertEqual(auth.read_profile_token(self.profile), "SESSION")


class WindowLifetime(AuthCase):
    def wait_running(self):
        deadline = time.monotonic() + 5
        while not auth.login_running() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(auth.login_running())

    def test_cancel_closes_the_window(self):
        auth.launch_login()
        cancel = threading.Event()
        result = {}
        waiter = threading.Thread(target=lambda: result.update(
            token=auth.wait_for_token(timeout=30, interval=0.1, cancel=cancel)))
        waiter.start()
        self.wait_running()
        cancel.set()
        waiter.join(10)
        self.assertFalse(waiter.is_alive())
        self.assertIsNone(result["token"])
        self.assertFalse(auth.login_running())

    def test_close_false_leaves_it_to_the_caller(self):
        self.page("TOKEN")
        auth.launch_login()
        self.assertEqual(auth.wait_for_token(timeout=10, interval=0.1, close=False), "TOKEN")
        self.assertTrue(auth.login_running())
        self.assertTrue(auth.close_login())

    def test_a_window_ignoring_browser_close_gets_sigterm(self):
        with mock.patch.dict(os.environ, {"FAKE_CHROME_IGNORE_CLOSE": "1"}):
            auth.launch_login()
        self.wait_running()
        self.assertTrue(auth.close_login(wait=0.5))
        self.assertFalse(auth.login_running())

    def test_a_stale_port_file_is_not_probed(self):
        """F39: DevToolsActivePort outlives Chrome; its port may be anyone's."""
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        (self.profile / "DevToolsActivePort").write_text(
            f"{listener.getsockname()[1]}\n/devtools/browser/X\n", "utf-8")
        self.assertIsNone(cdp._port(self.profile))  # no live lock
        self.assertIsNone(cdp.page_token(self.profile))
        # Older than the live lock: from an earlier run.
        os.symlink(f"{socket.gethostname()}-{os.getpid()}", self.profile / "SingletonLock")
        old = time.time() - 60
        os.utime(self.profile / "DevToolsActivePort", (old, old))
        self.assertIsNone(cdp._port(self.profile))
        os.unlink(self.profile / "SingletonLock")
        cdp.forget_port(self.profile)
        self.assertFalse((self.profile / "DevToolsActivePort").exists())

    def test_another_hosts_lock_is_not_a_running_window(self):
        os.symlink(f"some-other-host-{os.getpid()}", self.profile / "SingletonLock")
        self.assertFalse(auth.login_running())

    def test_closing_a_browser_that_has_not_taken_the_profile_yet(self):
        """G10: close_login did nothing until the lock existed, so a sign-in
        cancelled in the first second left a window nobody watched."""
        with mock.patch.dict(os.environ, {"FAKE_CHROME_DELAY": "5"}):
            auth.launch_login()
        proc = auth._launched
        started = time.monotonic()
        self.assertTrue(auth.close_login(wait=0.5))
        self.assertLess(time.monotonic() - started, 4)
        self.assertIsNotNone(proc.poll())
        self.assertFalse(auth.login_running())

    def test_a_browser_about_to_take_the_profile_is_closed_cleanly(self):
        with mock.patch.dict(os.environ, {"FAKE_CHROME_DELAY": "0.4"}):
            auth.launch_login()
        self.assertTrue(auth.close_login(wait=3))
        self.assertIn("Browser.close", self.chrome_log())
        self.assertFalse(auth.login_running())

    def test_a_relaunched_window_is_not_closed_by_the_old_wait(self):
        """G10, second click: the wait that was finishing closed the window
        the user had just opened."""
        wrapper = config.find_chrome()
        with mock.patch.object(config, "find_chrome", return_value="/bin/true"):
            auth.launch_login()
        cancel = threading.Event()
        waiter = threading.Thread(target=auth.wait_for_token,
                                  kwargs={"timeout": 30, "interval": 0.05, "cancel": cancel})
        with mock.patch.object(auth, "close_login") as close:
            waiter.start()
            time.sleep(0.1)
            with mock.patch.object(config, "find_chrome", return_value=wrapper), \
                    mock.patch.dict(os.environ, {"FAKE_CHROME_DELAY": "5"}):
                auth.launch_login()
            cancel.set()
            waiter.join(10)
        self.assertFalse(waiter.is_alive())
        close.assert_not_called()
        self.assertIsNone(auth._launched.poll())

    def test_a_relaunch_as_the_old_window_goes_is_watched_from_its_start(self):
        with mock.patch.object(auth, "LOGIN_STARTUP_GRACE", 0.5), \
                mock.patch.object(config, "find_chrome", return_value="/bin/true"):
            auth.launch_login()
            started = time.monotonic()
            result = {}
            waiter = threading.Thread(target=lambda: result.update(
                token=auth.wait_for_token(timeout=10, interval=0.05)))
            waiter.start()
            time.sleep(0.2)
            auth.launch_login()  # the second click, its browser not up yet
            waiter.join(10)
        self.assertIsNone(result["token"])
        # Given up on only one grace period after the second launch was seen.
        self.assertGreaterEqual(time.monotonic() - started, 0.9)

    def test_a_dropped_devtools_socket_does_not_escape_close(self):
        class Broken:
            def __init__(self, *args, **kwargs):
                pass

            def send(self, payload):
                raise BrokenPipeError("gone")

            def close(self):
                pass
        with mock.patch.object(cdp, "_active_port", return_value=(1, "/devtools/browser/B1")), \
                mock.patch.object(cdp, "_WebSocket", Broken):
            self.assertIsNone(cdp.close_browser(self.profile))


class SignOut(AuthCase):
    def test_sign_out_clears_the_profile_session_and_records_the_flag(self):
        """F21: the profile kept the cookie and the next start signed back in."""
        auth.save("TOKEN", source="x", storefront="gb")
        make_cookie_db(self.profile / "Default" / "Cookies", [
            (".music.apple.com", auth.COOKIE_NAME, "TOKEN", 0),
            (".apple.com", "myacinfo", "session", 0),
            ("example.org", "keep", "1", 0),
        ])
        storage = self.profile / "Default" / "Local Storage" / "leveldb"
        storage.mkdir(parents=True)
        (storage / "000001.log").write_text("music user token", "utf-8")
        record = auth.sign_out()
        self.assertTrue(record["signedOut"])
        self.assertEqual(record["mediaUserToken"], "")
        self.assertTrue(auth.signed_out())
        self.assertIsNone(auth.read_profile_token(self.profile))
        with sqlite3.connect(str(self.profile / "Default" / "Cookies")) as connection:
            left = [row[0] for row in connection.execute("SELECT name FROM cookies")]
        self.assertEqual(left, ["keep"])
        self.assertFalse(storage.exists())
        self.assertEqual(oct(config.auth_path().stat().st_mode & 0o777), "0o600")
        # A sign-in (a token saved) clears the flag.
        auth.save("NEW")
        self.assertFalse(auth.signed_out())

    def test_sign_out_closes_an_open_window_first(self):
        make_cookie_db(self.profile / "Default" / "Cookies",
                       [(".music.apple.com", auth.COOKIE_NAME, "TOKEN", 0)])
        auth.launch_login()
        deadline = time.monotonic() + 5
        while not auth.login_running() and time.monotonic() < deadline:
            time.sleep(0.05)
        auth.sign_out()
        self.assertFalse(auth.login_running())
        self.assertIsNone(auth.read_profile_token(self.profile))

    def test_a_session_left_by_sign_out_is_removed_before_the_next_window(self):
        """G12: sign-out said done with the session still in the profile,
        and the next window signed straight back in to it."""
        with mock.patch.object(auth, "forget_profile_session", return_value=False):
            record = auth.sign_out()
        self.assertTrue(record["profileDirty"])
        self.assertTrue(auth.load()["profileDirty"])
        with mock.patch.object(auth, "forget_profile_session", return_value=True) as forget:
            auth.launch_login()
        forget.assert_called_once()
        self.assertNotIn("profileDirty", auth.load())
        self.assertTrue(auth.signed_out())

    def test_no_window_while_the_old_session_cannot_be_removed(self):
        with mock.patch.object(auth, "forget_profile_session", return_value=False):
            auth.sign_out()
            with self.assertRaises(RuntimeError):
                auth.launch_login()
        self.assertIsNone(auth._launched)


class DailyBrowsers(AuthCase):
    def test_brave_profiles_are_found_with_their_keyring_name(self):
        """F38: Brave lives under BraveSoftware/, and every Profile N counts."""
        home = Path(os.environ["HOME"])
        brave = home / ".config" / "BraveSoftware" / "Brave-Browser"
        for name in ("Default", "Profile 1", "Profile 7"):
            (brave / name).mkdir(parents=True)
        (home / ".config" / "brave-browser" / "Default").mkdir(parents=True)  # the old, wrong root
        found = auth._daily_profiles()
        self.assertEqual([p.name for p, _ in found], ["Default", "Profile 1", "Profile 7"])
        self.assertTrue(all(keyring == ("brave",) for _, keyring in found))

    def test_v11_cookies_ask_their_own_browsers_keyring(self):
        rows = [(".music.apple.com", b"", b"v11" + b"\0" * 32)]
        with mock.patch.object(auth, "_keyring_password", return_value=[]) as keyring:
            self.assertEqual(auth._decode_rows(rows, ("chromium",)), [])
        keyring.assert_called_once_with(("chromium",))

    def test_keyring_lookup_uses_each_application_name(self):
        calls = []

        def run(args, **_kwargs):
            calls.append(args)
            return mock.Mock(returncode=0 if "chromium" in args else 1,
                             stdout=b"secret\n" if "chromium" in args else b"")
        with mock.patch.object(auth.shutil, "which", return_value="/usr/bin/secret-tool"), \
                mock.patch.object(auth.subprocess, "run", side_effect=run):
            self.assertEqual(auth._keyring_password(("chromium",)), [b"secret"])
        self.assertTrue(all(args[-1] == "chromium" for args in calls))

    def test_brave_binary_names(self):
        def which(name):
            return "/usr/bin/brave" if name == "brave" else None
        self.find.stop()
        try:
            with mock.patch.object(config.shutil, "which", side_effect=which):
                self.assertEqual(config.find_chrome(), "/usr/bin/brave")
        finally:
            self.find.start()

    def test_discover_reads_the_plugins_own_profile_by_default(self):
        make_cookie_db(config.login_profile() / "Default" / "Cookies",
                       [(".music.apple.com", auth.COOKIE_NAME, "OWN", 0)])
        profile, token = auth.discover()
        self.assertEqual(token, "OWN")
        self.assertEqual(profile, config.login_profile())

    def test_discover_ignores_daily_browsers_by_default(self):
        home = Path(os.environ["HOME"])
        make_cookie_db(home / ".config" / "BraveSoftware" / "Brave-Browser" / "Default" / "Cookies",
                       [(".music.apple.com", auth.COOKIE_NAME, "BRAVE", 0)])
        self.assertIsNone(auth.discover())

    def test_discover_opt_in_reads_a_brave_profile(self):
        home = Path(os.environ["HOME"])
        make_cookie_db(home / ".config" / "BraveSoftware" / "Brave-Browser" / "Default" / "Cookies",
                       [(".music.apple.com", auth.COOKIE_NAME, "BRAVE", 0)])
        profile, token = auth.discover(browsers=True)
        self.assertEqual(token, "BRAVE")
        self.assertIn("BraveSoftware", str(profile))


if __name__ == "__main__":
    unittest.main()
