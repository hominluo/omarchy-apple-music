"""Paths, constants and persisted settings for the Apple Music engine.

Everything the daemon writes lives under two XDG roots:

    $XDG_DATA_HOME/omarchy-apple-music/     the account, the Chrome profile, the venv
    $XDG_CACHE_HOME/omarchy/apple-music/   decrypted audio, artwork, logs, pycache

Nothing is written inside the plugin folder: the shell watches it with inotify
and reloads the plugin on every write.
"""

import json
import os
import re
import shutil
from pathlib import Path

APP_ID = "omarchy-apple-music"
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
# The Widevine device file is not redistributable, so it is gitignored; either
# drop one here or point APPLE_MUSIC_WVD at an existing .wvd.
DEVICE_PATH = Path(
    os.environ.get("APPLE_MUSIC_WVD") or Path(__file__).resolve().parent / "device.wvd"
).expanduser()
LAUNCHER = PLUGIN_ROOT / "apple-musicd.py"

# Chrome's own defaults, in the order we would rather find them. A user who
# already listens in their daily browser should not have to sign in again.
CHROME_BINARIES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "brave-browser",
    "microsoft-edge-stable",
)
CHROME_PROFILE_ROOTS = (
    ".config/google-chrome",
    ".config/chromium",
    ".config/brave-browser",
    ".config/microsoft-edge",
)


def _xdg(variable: str, fallback: str) -> Path:
    value = (os.environ.get(variable) or "").strip()
    root = Path(value) if value else Path.home() / fallback
    return root


def data_dir() -> Path:
    path = _xdg("XDG_DATA_HOME", ".local/share") / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    return path


def cache_dir() -> Path:
    path = _xdg("XDG_CACHE_HOME", ".cache") / "omarchy" / "apple-music"
    path.mkdir(parents=True, exist_ok=True)
    return path


def audio_dir() -> Path:
    path = cache_dir() / "audio"
    path.mkdir(parents=True, exist_ok=True)
    return path


def artwork_dir() -> Path:
    path = cache_dir() / "artwork"
    path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_dir() -> Path:
    """The private per-user runtime directory.

    A world-shared /tmp socket would let any local user drive the player, so
    the daemon refuses to run without XDG_RUNTIME_DIR rather than fall back.
    """
    value = (os.environ.get("XDG_RUNTIME_DIR") or "").strip()
    if not value:
        raise RuntimeError("XDG_RUNTIME_DIR is not set")
    path = Path(value) / APP_ID
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def socket_path() -> Path:
    return runtime_dir() / "daemon.sock"


def auth_path() -> Path:
    return data_dir() / "account.json"


def login_profile() -> Path:
    """The Chrome profile owned by this plugin, used only for sign-in."""
    path = data_dir() / "chrome"
    path.mkdir(parents=True, exist_ok=True)
    return path


def venv_python() -> Path:
    return data_dir() / "venv" / "bin" / "python"


# ---- settings -------------------------------------------------------------

DEFAULT_SETTINGS = {
    "storefront": "us",
    "language": "en-US",
    "codec": "aac-web",       # 256 kbps AAC, the web player's default asset
    "prefetch": 2,             # tracks resolved ahead of the queue position
    "cacheLimitMb": 1024,
    "volume": 100,
}


def load_settings() -> dict:
    path = data_dir() / "settings.json"
    settings = dict(DEFAULT_SETTINGS)
    try:
        settings.update(json.loads(path.read_text("utf-8")))
    except (OSError, ValueError):
        pass
    return settings


def save_settings(changes: dict) -> dict:
    settings = load_settings()
    for key, value in (changes or {}).items():
        if key in DEFAULT_SETTINGS:
            settings[key] = value
    path = data_dir() / "settings.json"
    path.write_text(json.dumps(settings, indent=2) + "\n", "utf-8")
    os.chmod(path, 0o600)
    return settings


def find_chrome() -> str | None:
    for name in CHROME_BINARIES:
        found = shutil.which(name)
        if found:
            return found
    return None


# Artwork URLs arrive as templates: .../{w}x{h}cc.jpg
_ARTWORK_TEMPLATE = re.compile(r"\{w\}x\{h\}([a-z]{2})\.jpg")


def artwork_url(template: str | None, size: int = 320) -> str:
    if not template:
        return ""
    return _ARTWORK_TEMPLATE.sub(lambda m: f"{size}x{size}{m.group(1)}.jpg", template)
