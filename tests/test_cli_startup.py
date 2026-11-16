"""engine/cli.py `serve`: a daemon that fails on the way up leaves one line
in the runtime dir for the Service, and a daemon that comes up removes it.

The real Daemon is never imported; a stand-in module takes its place, and
every path points into a temp dir.
"""

import asyncio
import io
import json
import logging
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import cli, config  # noqa: E402


class CliCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="am-cli-")
        root = Path(self.tmp.name)
        for name in ("run", "data", "cache"):
            (root / name).mkdir()
        self.env = mock.patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(root / "run"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"),
        })
        self.env.start()

    def tearDown(self):
        root_logger = logging.getLogger()
        for handler in list(root_logger.handlers):
            handler.close()
            root_logger.removeHandler(handler)
        self.env.stop()
        self.tmp.cleanup()

    def error_file(self) -> Path:
        return config.runtime_dir() / "startup-error"

    def fake_daemon_module(self, daemon_class):
        module = types.ModuleType("engine.daemon")
        module.Daemon = daemon_class
        return mock.patch.dict(sys.modules, {"engine.daemon": module})


class DescribeFailureTest(CliCase):
    def test_one_line_with_type(self):
        line = cli.describe_failure(RuntimeError("mpv is not installed\n(see PATH)"))
        self.assertEqual(line, "RuntimeError: mpv is not installed (see PATH)")

    def test_tokens_are_redacted(self):
        token = "eyJhbGciOiJFUzI1NiIsInR5cCI6IkpXVCJ9" + "Abc123Def456" * 4
        line = cli.describe_failure(ValueError(f"Authorization: Bearer {token}"))
        self.assertNotIn(token, line)
        self.assertIn("Bearer", line)

    def test_long_messages_are_cut(self):
        line = cli.describe_failure(OSError("x " * 500))
        self.assertLessEqual(len(line), 300)


class StartupErrorFileTest(CliCase):
    def test_write_then_clear(self):
        cli.write_startup_error("RuntimeError: boom")
        data = json.loads(self.error_file().read_text())
        self.assertEqual(data["message"], "RuntimeError: boom")
        self.assertIsInstance(data["at"], int)
        self.assertEqual(stat.S_IMODE(self.error_file().stat().st_mode), 0o600)
        cli.clear_startup_error()
        self.assertFalse(self.error_file().exists())
        cli.clear_startup_error()  # already gone: no error

    def test_listening_needs_a_socket_newer_than_us(self):
        path = config.socket_path()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        try:
            self.assertTrue(cli._listening_since(time.time() - 5))
            # A socket left by a daemon that died before we started.
            self.assertFalse(cli._listening_since(time.time() + 60))
        finally:
            server.close()

    def test_no_socket_is_not_listening(self):
        self.assertFalse(cli._listening_since(time.time()))


class ServeTest(CliCase):
    def args(self):
        return types.SimpleNamespace(verbose=False)

    def test_startup_exception_is_written_and_logged(self):
        class Failing:
            async def serve(self, stop):
                raise RuntimeError("mpv is not installed")

            async def shutdown(self):
                pass

        with self.fake_daemon_module(Failing):
            self.assertEqual(cli.cmd_serve(self.args()), 1)
        data = json.loads(self.error_file().read_text())
        self.assertEqual(data["message"], "RuntimeError: mpv is not installed")
        log = (Path(os.environ["XDG_CACHE_HOME"]) / "omarchy" / "apple-music" / "engine.log").read_text()
        self.assertIn("mpv is not installed", log)

    def test_constructor_failure_is_written(self):
        class Broken:
            def __init__(self):
                raise ImportError("No module named 'pywidevine'")

        with self.fake_daemon_module(Broken):
            self.assertEqual(cli.cmd_serve(self.args()), 1)
        self.assertIn("pywidevine", json.loads(self.error_file().read_text())["message"])

    def test_listening_daemon_removes_the_previous_error(self):
        cli.write_startup_error("RuntimeError: from the last attempt")
        seen = {}

        class Listening:
            async def serve(self, stop):
                path = config.socket_path()
                server = await asyncio.start_unix_server(lambda r, w: None, path=str(path))
                async with server:
                    for _ in range(50):
                        if not self_error_file.exists():
                            break
                        await asyncio.sleep(0.05)
                    seen["cleared"] = not self_error_file.exists()

            async def shutdown(self):
                pass

        self_error_file = self.error_file()
        with self.fake_daemon_module(Listening):
            self.assertEqual(cli.cmd_serve(self.args()), 0)
        self.assertTrue(seen.get("cleared"), "the startup error outlived a listening daemon")

    def test_second_daemon_that_finds_the_lock_taken_leaves_the_file(self):
        # serve() returns at once when another daemon owns the socket; that is
        # not a failure, and the file (if any) belongs to whoever wrote it.
        class AlreadyRunning:
            async def serve(self, stop):
                return

            async def shutdown(self):
                pass

        with self.fake_daemon_module(AlreadyRunning):
            self.assertEqual(cli.cmd_serve(self.args()), 0)
        self.assertFalse(self.error_file().exists())


class RequestTest(CliCase):
    def test_no_daemon_is_an_answer_not_a_traceback(self):
        answer = cli._request({"cmd": "quit"}, timeout=1)
        self.assertFalse(answer["ok"])
        self.assertEqual(answer["error"]["code"], "disconnected")


def serve_one_answer(path: Path, delay: float, received: list) -> threading.Thread:
    """A daemon stand-in that opens its socket after `delay` and answers one
    request the way the daemon does (an event first, then the reply)."""
    def run():
        time.sleep(delay)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        server.listen(1)
        server.settimeout(5)
        try:
            connection, _ = server.accept()
            with connection:
                line = connection.makefile().readline()
                received.append(json.loads(line))
                connection.sendall(b'{"event": "player", "data": {}}\n'
                                   b'{"id": 1, "ok": true, "result": {"quitting": true}}\n')
        finally:
            server.close()
    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


class CallTest(CliCase):
    """`call` sends one command and reports what came back. The quit
    machinery it used to carry for daemons still starting (`--wait`, the
    quit-requested marker) is gone: the daemon ends itself once the plugin
    is disabled in the shell's config."""

    def call(self, command="quit"):
        args = types.SimpleNamespace(command=command, params=None, timeout=5.0)
        with mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            code = cli.cmd_call(args)
        return code, json.loads(out.getvalue())

    def test_quit_reaches_a_listening_daemon(self):
        received = []
        server = serve_one_answer(config.socket_path(), 0.0, received)
        for _ in range(100):
            if config.socket_path().exists():
                break
            time.sleep(0.02)
        code, answer = self.call()
        server.join(5)
        self.assertEqual(code, 0)
        self.assertEqual(answer["result"], {"quitting": True})
        self.assertEqual(received[0]["cmd"], "quit")

    def test_no_daemon_is_one_attempt_and_leaves_nothing(self):
        started = time.monotonic()
        code, answer = self.call()
        self.assertEqual(code, 1)
        self.assertEqual(answer["error"]["code"], "disconnected")
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(sorted(p.name for p in config.runtime_dir().iterdir()), [])
        self.assertFalse(hasattr(cli, "request_quit_later"))
        self.assertFalse(hasattr(config, "quit_marker_path"))

    def test_wait_is_no_longer_an_option(self):
        with mock.patch("sys.stderr", new_callable=io.StringIO), \
                self.assertRaises(SystemExit):
            cli.main(["call", "quit", "--wait", "30"])


class SignInTest(CliCase):
    """G11: the terminal sign-in re-saved a revoked session's cookie."""

    def run_signin(self, tokens, verdicts, running=True):
        from engine import auth

        calls = []
        queue = list(tokens)

        def wait_for_token(timeout, **kwargs):
            calls.append(dict(kwargs, reject=set(kwargs.get("reject") or ())))
            return queue.pop(0) if queue else None
        account = {"meta": {"subscription": {"storefront": "gb", "active": True}}}
        answers = {token: (verdict, account if verdict == "ok" else None)
                   for token, verdict in verdicts.items()}
        with mock.patch.object(auth, "launch_login", return_value={"running": True}), \
             mock.patch.object(auth, "wait_for_token", side_effect=wait_for_token), \
             mock.patch.object(auth, "login_running", return_value=running), \
             mock.patch.object(auth, "close_login", return_value=True) as close, \
             mock.patch.object(cli, "_check_token", side_effect=lambda t, s: answers[t]), \
             mock.patch("sys.stdout", new_callable=io.StringIO), \
             mock.patch("sys.stderr", new_callable=io.StringIO):
            code = cli.cmd_signin(types.SimpleNamespace(timeout=30.0))
        return code, calls, close

    def test_the_dead_token_is_refused_and_a_refused_capture_waited_past(self):
        from engine import auth

        auth._write(dict(auth.DEFAULT_ACCOUNT, mediaUserToken="DEAD", error="refused", expired=True))
        code, calls, close = self.run_signin(["STILL-BAD", "GOOD"],
                                             {"STILL-BAD": "refused", "GOOD": "ok"})
        self.assertEqual(code, 0)
        self.assertEqual(calls[0]["reject"], {"DEAD"})
        self.assertFalse(calls[0]["close"])
        self.assertEqual(calls[1]["reject"], {"DEAD", "STILL-BAD"})
        record = auth.load()
        self.assertEqual(record["mediaUserToken"], "GOOD")
        self.assertEqual(record["storefront"], "gb")
        close.assert_called()

    def test_a_good_stored_token_is_not_waited_past(self):
        from engine import auth

        auth.save("GOOD", storefront="us")
        code, calls, _close = self.run_signin(["GOOD"], {"GOOD": "ok"})
        self.assertEqual(code, 0)
        self.assertEqual(calls[0]["reject"], set())

    def test_refused_with_the_window_gone_is_a_failure(self):
        from engine import auth

        code, _calls, close = self.run_signin(["BAD"], {"BAD": "refused"}, running=False)
        self.assertEqual(code, 1)
        self.assertEqual(auth.load()["mediaUserToken"], "")
        close.assert_called()

    def test_unreachable_apple_still_keeps_the_token(self):
        from engine import auth

        code, _calls, _close = self.run_signin(["NEW"], {"NEW": "unreachable"})
        self.assertEqual(code, 0)
        self.assertEqual(auth.load()["mediaUserToken"], "NEW")


if __name__ == "__main__":
    unittest.main()
