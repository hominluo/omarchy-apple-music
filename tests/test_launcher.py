"""apple-musicd.py: the venv bootstrap lock, stale-venv detection and the
startup-error file the Service reads.

Everything runs against temp XDG roots; no venv is created and nothing is
installed (the subprocess steps are mocked).
"""

import fcntl
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

LAUNCHER = Path(__file__).resolve().parent.parent / "apple-musicd.py"


def load_launcher(env: dict):
    """Import apple-musicd.py fresh with the given environment: it reads the
    XDG roots at import time."""
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location(f"apple_musicd_{id(env)}", LAUNCHER)
        module = importlib.util.module_from_spec(spec)
        saved_prefix = sys.pycache_prefix
        try:
            spec.loader.exec_module(module)
        finally:
            sys.pycache_prefix = saved_prefix
    return module


class LauncherTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = {
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_RUNTIME_DIR": str(root / "run"),
        }
        (root / "run").mkdir()
        self.env_patch = mock.patch.dict(os.environ, self.env)
        self.env_patch.start()
        os.environ.pop("APPLE_MUSIC_VENV", None)
        # The launcher narrates to stderr for the detached process's sake.
        self.stderr_patch = mock.patch("sys.stderr", new_callable=io.StringIO)
        self.stderr_patch.start()
        self.launcher = load_launcher(self.env)
        self.venv = Path(self.launcher._venv_dir())
        self.python = Path(self.launcher._venv_python())

    def tearDown(self):
        self.stderr_patch.stop()
        self.env_patch.stop()
        self.tmp.cleanup()

    def make_venv(self, version: str = None):
        version = version or "%d.%d.%d" % sys.version_info[:3]
        self.python.parent.mkdir(parents=True, exist_ok=True)
        self.python.write_text("")
        (self.venv / "pyvenv.cfg").write_text(f"home = /usr/bin\nversion = {version}\n")


class StaleVenvTest(LauncherTestCase):
    def test_missing_venv_is_stale(self):
        self.assertEqual(self.launcher._stale_reason(str(self.venv), str(self.python)), "missing")

    def test_cfg_minor_reads_version_line(self):
        self.make_venv("3.15.2")
        self.assertEqual(self.launcher._cfg_minor(str(self.venv)), "3.15")

    def test_cfg_minor_reads_version_info_line(self):
        self.python.parent.mkdir(parents=True)
        (self.venv / "pyvenv.cfg").write_text("version_info = 3.13.1.final.0\n")
        self.assertEqual(self.launcher._cfg_minor(str(self.venv)), "3.13")

    def test_broken_interpreter_is_stale(self):
        self.make_venv()
        with mock.patch.object(self.launcher, "_python_minor", return_value=None):
            reason = self.launcher._stale_reason(str(self.venv), str(self.python))
        self.assertIn("does not start", reason)

    def test_healthy_venv_of_another_minor_than_the_caller_is_usable(self):
        """G13: a terminal's python3 of another minor rebuilt the shell's
        venv (and the next shell spawn rebuilt it back), under the daemon."""
        other = "3.1" if self.launcher._running_minor() != "3.1" else "3.2"
        self.make_venv(f"{other}.4")
        with mock.patch.object(self.launcher, "_python_minor", return_value=other):
            self.assertEqual(self.launcher._stale_reason(str(self.venv), str(self.python)), "")

    def test_interpreter_disagreeing_with_its_cfg_is_stale(self):
        self.make_venv("3.15.2")
        with mock.patch.object(self.launcher, "_python_minor", return_value="3.16"):
            reason = self.launcher._stale_reason(str(self.venv), str(self.python))
        self.assertIn("built for Python 3.15", reason)

    def test_symlink_moved_under_venv_is_stale(self):
        # mise's `latest` moved on: bin/python now runs the new minor, which
        # matches the launcher, but the venv's lib/ is still the old one.
        running = self.launcher._running_minor()
        self.make_venv("3.1.0")
        with mock.patch.object(self.launcher, "_python_minor", return_value=running):
            reason = self.launcher._stale_reason(str(self.venv), str(self.python))
        self.assertIn("built for Python 3.1", reason)

    def test_matching_venv_is_usable(self):
        self.make_venv()
        with mock.patch.object(self.launcher, "_python_minor", return_value=self.launcher._running_minor()):
            self.assertEqual(self.launcher._stale_reason(str(self.venv), str(self.python)), "")


class EnsureVenvTest(LauncherTestCase):
    def test_stale_venv_is_rebuilt_with_clear_then_installed(self):
        self.make_venv("3.1.0")
        calls = []
        installed = {"done": False}

        def fake_run(command, failure, timeout):
            calls.append(command)
            if "pip" in command:
                installed["done"] = True

        with mock.patch.object(self.launcher, "_python_minor", return_value=self.launcher._running_minor()), \
             mock.patch.object(self.launcher, "_has_pywidevine", side_effect=lambda p: installed["done"]), \
             mock.patch.object(self.launcher, "_has_pip", return_value=True), \
             mock.patch.object(self.launcher, "_run", side_effect=fake_run):
            python = self.launcher._ensure_venv()

        self.assertEqual(python, str(self.python))
        self.assertEqual(calls[0][1:4], ["-m", "venv", "--clear"])
        self.assertEqual(calls[0][0], sys.executable)
        self.assertIn("pywidevine", calls[1])
        self.assertTrue(Path(self.launcher._lock_path()).exists())
        self.assertEqual(Path(self.launcher._lock_path()).parent, self.venv.parent)

    def test_half_made_venv_without_pip_is_rebuilt(self):
        self.make_venv()
        calls = []
        with mock.patch.object(self.launcher, "_python_minor", return_value=self.launcher._running_minor()), \
             mock.patch.object(self.launcher, "_has_pywidevine", return_value=False), \
             mock.patch.object(self.launcher, "_has_pip", return_value=False), \
             mock.patch.object(self.launcher, "_run", side_effect=lambda c, f, timeout: calls.append(c)):
            self.launcher._ensure_venv()
        self.assertIn("--clear", calls[0])

    def test_venv_of_another_minor_is_not_cleared_by_another_caller(self):
        other = "3.1" if self.launcher._running_minor() != "3.1" else "3.2"
        self.make_venv(f"{other}.0")
        with mock.patch.object(self.launcher, "_python_minor", return_value=other), \
             mock.patch.object(self.launcher, "_has_pywidevine", return_value=True), \
             mock.patch.object(self.launcher, "_run") as run:
            self.assertEqual(self.launcher._ensure_venv(), str(self.python))
        run.assert_not_called()

    def test_healthy_venv_is_left_alone(self):
        self.make_venv()
        with mock.patch.object(self.launcher, "_python_minor", return_value=self.launcher._running_minor()), \
             mock.patch.object(self.launcher, "_has_pywidevine", return_value=True), \
             mock.patch.object(self.launcher, "_run") as run:
            self.launcher._ensure_venv()
        run.assert_not_called()

    def test_concurrent_launcher_waits_for_the_bootstrap_lock(self):
        self.make_venv()
        Path(self.launcher._lock_path()).parent.mkdir(parents=True, exist_ok=True)
        holder = open(self.launcher._lock_path(), "a+")
        fcntl.flock(holder, fcntl.LOCK_EX)
        finished = threading.Event()

        def second_launcher():
            with mock.patch.object(self.launcher, "_python_minor", return_value=self.launcher._running_minor()), \
                 mock.patch.object(self.launcher, "_has_pywidevine", return_value=True):
                self.launcher._ensure_venv()
            finished.set()

        thread = threading.Thread(target=second_launcher, daemon=True)
        thread.start()
        try:
            self.assertFalse(finished.wait(0.4), "a second launcher ran while the bootstrap lock was held")
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            holder.close()
        self.assertTrue(finished.wait(5), "the waiting launcher never continued")

    def test_pip_failure_becomes_one_bootstrap_line(self):
        fake = mock.Mock(returncode=1, stdout="", stderr="Collecting pywidevine\nERROR: No matching distribution found\n")
        with mock.patch.object(self.launcher.subprocess, "run", return_value=fake):
            with self.assertRaises(self.launcher.BootstrapError) as caught:
                self.launcher._run(["pip"], "pip install pywidevine failed", timeout=5)
        self.assertEqual(str(caught.exception), "pip install pywidevine failed: ERROR: No matching distribution found")


class StartupErrorTest(LauncherTestCase):
    def error_file(self) -> Path:
        return Path(self.env["XDG_RUNTIME_DIR"]) / "omarchy-apple-music" / "startup-error"

    def test_report_failure_writes_json_and_log(self):
        before = int(time.time())
        self.launcher._report_failure("pip install pywidevine failed:\n  network unreachable")
        data = json.loads(self.error_file().read_text())
        self.assertEqual(data["message"], "pip install pywidevine failed: network unreachable")
        self.assertGreaterEqual(data["at"], before)
        self.assertEqual(stat.S_IMODE(self.error_file().stat().st_mode), 0o600)
        log = Path(self.env["XDG_CACHE_HOME"]) / "omarchy" / "apple-music" / "engine.log"
        self.assertIn("launcher: pip install pywidevine failed", log.read_text())

    def test_tokens_are_redacted_but_paths_survive(self):
        token = "Ag" + "Xy9Kq2Lm8Np4Rs7Tu1Vw3Za6Bc5De0Fg" * 3
        line = self.launcher._one_line(f"bad token {token} in /home/user/.local/share/omarchy-apple-music/venv")
        self.assertNotIn(token, line)
        self.assertIn("/home/user/.local/share/omarchy-apple-music/venv", line)

    def test_main_reports_bootstrap_failure_when_serving(self):
        with mock.patch.object(sys, "argv", ["apple-musicd.py", "serve"]), \
             mock.patch.object(self.launcher.importlib.util, "find_spec", return_value=None), \
             mock.patch.object(self.launcher, "_ensure_venv",
                               side_effect=self.launcher.BootstrapError("could not create the private venv: boom")):
            self.assertEqual(self.launcher.main(), 1)
        self.assertIn("could not create the private venv", json.loads(self.error_file().read_text())["message"])

    def test_terminal_verbs_do_not_touch_the_startup_error(self):
        with mock.patch.object(sys, "argv", ["apple-musicd.py", "probe", "123"]), \
             mock.patch.object(self.launcher.importlib.util, "find_spec", return_value=None), \
             mock.patch.object(self.launcher, "_ensure_venv",
                               side_effect=self.launcher.BootstrapError("boom")):
            self.assertEqual(self.launcher.main(), 1)
        self.assertFalse(self.error_file().exists())

    def test_serving_detection(self):
        self.assertTrue(self.launcher._serving([]))
        self.assertTrue(self.launcher._serving(["serve"]))
        self.assertTrue(self.launcher._serving(["-v", "serve"]))
        self.assertTrue(self.launcher._serving(["--timeout", "5"]))
        self.assertFalse(self.launcher._serving(["call", "quit"]))
        self.assertFalse(self.launcher._serving(["status"]))

    def test_verb_skips_global_options(self):
        self.assertEqual(self.launcher._verb(["--timeout", "5", "call", "volume", "{\"value\": 40}"]), "call")
        self.assertEqual(self.launcher._verb(["-v", "token"]), "token")
        self.assertEqual(self.launcher._verb([]), "serve")


class ClientVerbTest(LauncherTestCase):
    """G6/G13: call, status and token run on the caller's python3 with the
    standard library only, never through the venv bootstrap."""

    def run_main(self, *argv):
        import engine.cli

        with mock.patch.object(sys, "argv", ["apple-musicd.py", *argv]), \
             mock.patch.object(self.launcher.importlib.util, "find_spec", return_value=None), \
             mock.patch.object(self.launcher, "_ensure_venv",
                               side_effect=AssertionError("bootstrapped")) as ensure, \
             mock.patch.object(self.launcher.os, "execv",
                               side_effect=AssertionError("re-executed")) as execv, \
             mock.patch.object(engine.cli, "main", return_value=0) as cli_main:
            code = self.launcher.main()
        return code, ensure, execv, cli_main

    def test_call_quit_skips_the_bootstrap(self):
        code, ensure, execv, cli_main = self.run_main("call", "quit")
        self.assertEqual(code, 0)
        ensure.assert_not_called()
        execv.assert_not_called()
        cli_main.assert_called_once_with(["call", "quit"])

    def test_status_and_token_skip_it_too(self):
        for verb in ("status", "token"):
            code, ensure, _execv, cli_main = self.run_main(verb)
            self.assertEqual(code, 0)
            ensure.assert_not_called()
            cli_main.assert_called_once_with([verb])

    def test_engine_verbs_still_bootstrap(self):
        os.environ.pop("APPLE_MUSIC_LAUNCHED_AT", None)
        with mock.patch.object(sys, "argv", ["apple-musicd.py", "probe", "1"]), \
             mock.patch.object(self.launcher.importlib.util, "find_spec", return_value=None), \
             mock.patch.object(self.launcher, "_ensure_venv", return_value="/venv/bin/python") as ensure, \
             mock.patch.object(self.launcher.os, "execv", side_effect=RuntimeError("exec")) as execv:
            with self.assertRaises(RuntimeError):
                self.launcher.main()
        ensure.assert_called_once()
        self.assertEqual(execv.call_args[0][0], "/venv/bin/python")
        # The quit marker's launch stamp went with the marker.
        self.assertNotIn("APPLE_MUSIC_LAUNCHED_AT", os.environ)


class Exec(BaseException):
    """Stands in for os.execv: main() reports any Exception as a startup
    failure, which would write the very file these tests look at."""


class StaleStartupErrorTest(LauncherTestCase):
    """H3: a startup-error left by an earlier launch marked a fresh start
    unavailable until that start reached the socket."""

    def error_file(self) -> Path:
        path = Path(self.env["XDG_RUNTIME_DIR"]) / "omarchy-apple-music" / "startup-error"
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def leave_error(self, age: float) -> Path:
        path = self.error_file()
        path.write_text(json.dumps({"message": "mpv is not installed", "at": 0}) + "\n")
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def run_main(self, *argv, venv: bool = False):
        """main() up to the exec into the venv, which is where it stops."""
        env = {"APPLE_MUSIC_VENV": "1"} if venv else {}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(sys, "argv", ["apple-musicd.py", *argv]), \
             mock.patch.object(self.launcher.importlib.util, "find_spec", return_value=None), \
             mock.patch.object(self.launcher, "_has_pywidevine", return_value=True), \
             mock.patch.object(self.launcher, "_ensure_venv", return_value="/venv/bin/python"), \
             mock.patch.object(self.launcher.os, "execv", side_effect=Exec()), \
             mock.patch("engine.cli.main", return_value=0):
            try:
                return self.launcher.main()
            except Exec:
                return None

    def test_the_outermost_serve_launcher_removes_an_older_error(self):
        path = self.leave_error(age=30)
        self.run_main("serve")
        self.assertFalse(path.exists())

    def test_the_default_verb_counts_as_serve(self):
        path = self.leave_error(age=30)
        self.run_main()
        self.assertFalse(path.exists())

    def test_an_error_newer_than_the_launch_stays(self):
        path = self.leave_error(age=-60)  # written after this launch began
        self.run_main("serve")
        self.assertTrue(path.exists())

    def test_the_stage_inside_the_venv_leaves_it(self):
        """By then a failure another launcher reported while this one
        waited on the bootstrap lock is current."""
        path = self.leave_error(age=30)
        self.run_main("serve", venv=True)
        self.assertTrue(path.exists())

    def test_terminal_verbs_leave_it(self):
        path = self.leave_error(age=30)
        for argv in (("status",), ("call", "quit"), ("probe", "1")):
            self.run_main(*argv)
        self.assertTrue(path.exists())

    def test_a_failure_of_this_launch_is_written_after_the_old_one_went(self):
        path = self.leave_error(age=30)
        with mock.patch.object(sys, "argv", ["apple-musicd.py", "serve"]), \
             mock.patch.object(self.launcher.importlib.util, "find_spec", return_value=None), \
             mock.patch.object(self.launcher, "_ensure_venv",
                               side_effect=self.launcher.BootstrapError("pip install pywidevine failed: offline")):
            self.assertEqual(self.launcher.main(), 1)
        self.assertIn("offline", json.loads(path.read_text())["message"])


if __name__ == "__main__":
    unittest.main()
