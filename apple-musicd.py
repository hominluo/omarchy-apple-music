#!/usr/bin/env python3
"""Launcher for the Omarchy Apple Music engine.

The shell runs ``apple-musicd.py serve`` and expects the daemon to appear on
the socket. Two arrangements have to happen before the engine is imported:

  * Bytecode goes to the cache directory, never to a __pycache__ inside the
    plugin folder. The shell watches that folder with inotify and reloads the
    plugin on every write, so a first import dropping .pyc files next to the
    sources would reload the bar mid-setup.

  * The plugin folder is on sys.path, because Quickshell spawns us with no
    working directory we can rely on.

The engine's only third-party dependency is pywidevine. If the system Python
does not have it, a private venv is created once under
$XDG_DATA_HOME/omarchy-apple-music/venv and this file re-executes inside it.
The client verbs (call, status, token) need only the standard library and run
on whatever python3 started them, without touching the venv.

The shell spawns this file detached and only ever learns whether the socket
came up, so a launcher that fails says why in
$XDG_RUNTIME_DIR/omarchy-apple-music/startup-error (and engine.log); the
Service reads it while it cannot connect. engine/cli.py writes the same file
for failures past this point. A launch first removes a file left by an earlier
one, so a failure that has since been fixed does not mark this start failed.
"""

# Annotations stay strings so this file still runs, and reports, on a Python
# too old for the engine's `X | None` syntax.
from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import re
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))

_cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
_data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
sys.pycache_prefix = os.path.join(_cache_home, "omarchy", "apple-music", "pycache")

if HERE not in sys.path:
    sys.path.insert(0, HERE)

APP_ID = "omarchy-apple-music"


class BootstrapError(Exception):
    """A failure the user can act on, phrased for the startup-error line."""


def _app_dir() -> str:
    return os.path.join(_data_home, APP_ID)


def _venv_dir() -> str:
    return os.path.join(_app_dir(), "venv")


def _venv_python() -> str:
    return os.path.join(_venv_dir(), "bin", "python")


def _lock_path() -> str:
    # Next to the venv, not inside it: `venv --clear` empties the venv folder,
    # and a lock file that vanishes mid-bootstrap would let a second launcher in.
    return os.path.join(_app_dir(), "venv.lock")


# ---- failure reporting ------------------------------------------------------
# This file cannot import engine/cli.py's copy of these: the failure being
# reported may be that the engine does not import on this Python at all.

_TOKENISH = re.compile(r"[A-Za-z0-9+/=_-]{40,}")


def _redact(text: str) -> str:
    """Drop anything shaped like a token. Paths survive: they rarely mix
    upper case, lower case and digits in one 40-character run."""
    def scrub(match):
        run = match.group(0)
        mixed = (any(c.isupper() for c in run) and any(c.islower() for c in run)
                 and any(c.isdigit() for c in run))
        return "…" if mixed else run
    return _TOKENISH.sub(scrub, text)


def _one_line(text: str, limit: int = 300) -> str:
    line = " ".join(str(text or "").split())
    line = _redact(line)
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _startup_error_path() -> str:
    runtime = (os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    return os.path.join(runtime, APP_ID, "startup-error") if runtime else ""


def _clear_stale_startup_error(launched: float) -> None:
    """Remove a startup-error written before this launch began.

    The file outlives the launch that wrote it, and the Service shows it as
    the reason the engine is unavailable while nothing answers; a fixed
    failure (mpv installed since, the network back) used to go on marking
    every later start failed until one of them reached the socket. One that
    a concurrent launcher writes after this point is news and stays."""
    path = _startup_error_path()
    if not path:
        return
    try:
        if os.stat(path).st_mtime <= launched:
            os.unlink(path)
    except OSError:
        pass


def _report_failure(message: str) -> None:
    message = _one_line(message)
    print(f"apple-musicd: {message}", file=sys.stderr)
    try:
        log_dir = os.path.join(_cache_home, "omarchy", "apple-music")
        os.makedirs(log_dir, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(log_dir, "engine.log"), "a", encoding="utf-8") as log:
            log.write(f"{stamp},000 ERROR   launcher: {message}\n")
    except OSError:
        pass
    path = _startup_error_path()
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        temp = f"{path}.{os.getpid()}.tmp"
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump({"message": message, "at": int(time.time())}, handle)
            handle.write("\n")
        os.chmod(temp, 0o600)
        os.replace(temp, path)
    except OSError:
        pass


# Verbs that only read account.json or talk to the daemon's socket. They need
# nothing but the standard library, so they skip the venv: a command must not
# queue behind a first-run pip install that holds the bootstrap lock, and a
# terminal's python3 has no business rebuilding the venv a live daemon uses.
CLIENT_VERBS = frozenset({"call", "status", "token"})


def _verb(argv: list[str]) -> str:
    """The verb engine/cli.py will run: the first positional argument, past
    the global options (--timeout takes a value), or `serve` by default."""
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg == "--timeout":
            skip = True
            continue
        if arg.startswith("-"):
            continue
        return arg
    return "serve"


def _serving(argv: list[str]) -> bool:
    """Only `serve` (the default verb) reports to the shell; a failing
    terminal verb already prints to the terminal that ran it."""
    return _verb(argv) == "serve"


# ---- the private venv -------------------------------------------------------

def _probe(python: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run([python, *args], capture_output=True, text=True,
                              timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None


def _has_pywidevine(python: str) -> bool:
    result = _probe(python, "-c", "import pywidevine")
    return result is not None and result.returncode == 0


def _has_pip(python: str) -> bool:
    result = _probe(python, "-m", "pip", "--version")
    return result is not None and result.returncode == 0


def _python_minor(python: str) -> str | None:
    result = _probe(python, "-c", "import sys; print('%d.%d' % sys.version_info[:2])", timeout=30)
    if result is None or result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _running_minor() -> str:
    return "%d.%d" % sys.version_info[:2]


def _cfg_minor(venv: str) -> str | None:
    """The major.minor the venv was built for, from pyvenv.cfg."""
    try:
        with open(os.path.join(venv, "pyvenv.cfg"), encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    for line in lines:
        key, _, value = line.partition("=")
        if key.strip() in ("version", "version_info"):
            parts = value.strip().split(".")
            if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                return f"{parts[0]}.{parts[1]}"
    return None


def _stale_reason(venv: str, python: str) -> str:
    """Why the venv has to be rebuilt, or "" when it is usable.

    A venv's bin/python is a symlink to whatever interpreter built it. When
    that interpreter moves on a minor version (mise's `latest`, an Arch Python
    upgrade), the link still resolves but looks in a lib/pythonX.Y that does
    not exist, so pywidevine and pip both vanish and nothing ever repairs it.

    Only the venv's own state counts. The launcher execs into the venv's
    interpreter whatever python3 started it, so a healthy venv of another
    minor is fine; comparing it with the caller's minor made the shell's
    python3 and a terminal's python3 wipe and rebuild it in turn, under the
    running daemon.
    """
    if not os.path.exists(python):
        return "missing"
    minor = _python_minor(python)
    if minor is None:
        return "its interpreter does not start"
    built = _cfg_minor(venv)
    if built and built != minor:
        return f"it was built for Python {built}, its interpreter is now {minor}"
    return ""


def _run(command: list[str], failure: str, timeout: float) -> None:
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise BootstrapError(f"{failure}: timed out after {int(timeout)} s") from None
    except OSError as error:
        raise BootstrapError(f"{failure}: {error}") from None
    if result.returncode != 0:
        tail = [line for line in (result.stderr or result.stdout or "").splitlines() if line.strip()]
        detail = tail[-1].strip() if tail else f"exit status {result.returncode}"
        raise BootstrapError(f"{failure}: {detail}")


def _ensure_venv() -> str:
    """Make the private venv usable and return its interpreter.

    The shell respawns a launcher every few seconds while the socket is
    missing, and a first-run pip install takes far longer than that. Without
    the lock, a second launcher finds bin/python already there, pywidevine
    not yet installed, and starts its own pip into the same venv. With it,
    later launchers wait, then find the work done and only check it.
    """
    os.makedirs(_app_dir(), exist_ok=True)
    venv, python = _venv_dir(), _venv_python()
    with open(_lock_path(), "a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        reason = _stale_reason(venv, python)
        # An interrupted first run can leave bin/python with no pip in it.
        if not reason and not _has_pywidevine(python) and not _has_pip(python):
            reason = "it has neither pywidevine nor pip"
        if reason:
            note = "" if reason == "missing" else f" ({reason})"
            print(f"apple-musicd: creating {venv}{note}", file=sys.stderr)
            _run([sys.executable, "-m", "venv", "--clear", venv],
                 "could not create the private venv", timeout=300)
        if not _has_pywidevine(python):
            print("apple-musicd: installing pywidevine", file=sys.stderr)
            _run([python, "-m", "pip", "install", "--disable-pip-version-check", "-q", "pywidevine"],
                 "pip install pywidevine failed", timeout=900)
    return python


MIN_PYTHON = (3, 11)


def main() -> int:
    argv = sys.argv[1:]
    launched = time.time()
    try:
        if _serving(argv) and os.environ.get("APPLE_MUSIC_VENV") != "1":
            # Only the outermost stage: past the exec below, a failure that
            # another launcher reported while this one waited on the
            # bootstrap lock is current, not stale.
            _clear_stale_startup_error(launched)
        if sys.version_info < MIN_PYTHON:
            raise BootstrapError("Python %d.%d or newer is required, %s found at %s" % (
                *MIN_PYTHON, _running_minor(), sys.executable))
        if _verb(argv) not in CLIENT_VERBS and importlib.util.find_spec("pywidevine") is None:
            if os.environ.get("APPLE_MUSIC_VENV") == "1" and not _has_pywidevine(sys.executable):
                raise BootstrapError("pywidevine is missing from the private venv")
            python = _ensure_venv()
            os.environ["APPLE_MUSIC_VENV"] = "1"
            os.execv(python, [python, os.path.abspath(__file__), *argv])
        from engine.cli import main as cli_main
    except BootstrapError as error:
        if _serving(argv):
            _report_failure(str(error))
        else:
            print(f"apple-musicd: {error}", file=sys.stderr)
        return 1
    except Exception as error:  # noqa: BLE001 - anything here is a startup failure
        if not _serving(argv):
            raise
        _report_failure(f"{type(error).__name__}: {error}")
        return 1

    return cli_main(argv)


if __name__ == "__main__":
    sys.exit(main())
