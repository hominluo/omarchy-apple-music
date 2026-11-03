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
"""

import importlib.util
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

_cache_home = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
_data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
sys.pycache_prefix = os.path.join(_cache_home, "omarchy", "apple-music", "pycache")

if HERE not in sys.path:
    sys.path.insert(0, HERE)


def _venv_python() -> str:
    return os.path.join(_data_home, "omarchy-apple-music", "venv", "bin", "python")


def _has_pywidevine(python: str) -> bool:
    result = subprocess.run(
        [python, "-c", "import pywidevine"], capture_output=True, timeout=60, check=False,
    )
    return result.returncode == 0


def _ensure_venv() -> str:
    python = _venv_python()
    if not os.path.exists(python):
        venv = os.path.dirname(os.path.dirname(python))
        print(f"apple-musicd: creating {venv}", file=sys.stderr)
        subprocess.run([sys.executable, "-m", "venv", venv], check=True)
    if not _has_pywidevine(python):
        print("apple-musicd: installing pywidevine", file=sys.stderr)
        subprocess.run(
            [python, "-m", "pip", "install", "--disable-pip-version-check", "-q", "pywidevine"],
            check=True,
        )
    return python


def main() -> int:
    if importlib.util.find_spec("pywidevine") is None:
        if os.environ.get("APPLE_MUSIC_VENV") == "1" and not _has_pywidevine(sys.executable):
            raise SystemExit("apple-musicd: pywidevine is missing from the private venv")
        python = _ensure_venv()
        os.environ["APPLE_MUSIC_VENV"] = "1"
        os.execv(python, [python, os.path.abspath(__file__), *sys.argv[1:]])
    from engine.cli import main as cli_main

    return cli_main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
