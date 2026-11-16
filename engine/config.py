"""Paths, constants and persisted settings for the Apple Music engine.

Everything the daemon writes lives under two XDG roots:

    $XDG_DATA_HOME/omarchy-apple-music/     the account, the Chrome profile, the venv
    $XDG_CACHE_HOME/omarchy/apple-music/   decrypted audio, artwork, logs, pycache

Nothing is written inside the plugin folder: the shell watches it with inotify
and reloads the plugin on every write. The shell's own config
(~/.config/omarchy/shell.json) is only ever read: it says whether the
plugin is enabled, and the daemon runs only while it is.
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
# Arch's brave-bin and brave-origin-bin install `brave` and `brave-origin`;
# `brave-browser` is the name Brave's own deb/rpm packages use.
CHROME_BINARIES = (
    "google-chrome-stable",
    "google-chrome",
    "chromium",
    "chromium-browser",
    "brave",
    "brave-browser",
    "brave-origin",
    "microsoft-edge-stable",
    "microsoft-edge",
)
# Each browser's user-data-dir and the libsecret `application` its cookie key
# is stored under. A profile is only readable with its own browser's secret:
# Omarchy runs Chromium with --password-store=gnome-libsecret, and Chromium's
# secret is filed as "chromium", not "chrome".
CHROME_PROFILE_ROOTS = (
    (".config/google-chrome", ("chrome",)),
    (".config/chromium", ("chromium",)),
    (".config/BraveSoftware/Brave-Browser", ("brave",)),
    (".config/BraveSoftware/Brave-Origin", ("brave", "brave-origin")),
    (".config/microsoft-edge", ("microsoft-edge", "msedge")),
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


def pid_path() -> Path:
    return runtime_dir() / "daemon.pid"


# ---- the shell's config -----------------------------------------------------
# The daemon runs only while the plugin is enabled in the shell. It is spawned
# detached, so a plugin disabled while a launcher was still on its way up (a
# first-run pip install takes minutes) used to leave a daemon nobody would
# ever stop, and every attempt to hand that launcher a quit raced something.
# The daemon reads the shell's own answer instead, at start and whenever the
# file changes.

PLUGIN_ID = "io.github.hominluo.apple-music"


def shell_config_path() -> Path:
    # Deliberately not XDG_CONFIG_HOME: the shell (shell.qml userConfigPath)
    # and the omarchy-plugin scripts hard-code $HOME/.config, and the shell is
    # the only writer. Following XDG would watch a file nobody writes.
    return Path(os.environ.get("HOME") or Path.home()) / ".config" / "omarchy" / "shell.json"


def plugin_root() -> Path:
    """The plugin directory as the shell scans it. Not resolved: with a dev
    symlink the checkout outlives the plugin, and the link is what counts."""
    override = os.environ.get("APPLE_MUSIC_PLUGIN_ROOT")
    if override:
        return Path(override)
    return Path(os.path.abspath(__file__)).parents[1]


def plugin_installed() -> bool:
    return (plugin_root() / "manifest.json").exists()


def _names_plugin(entry, plugin_id: str) -> bool:
    """A bar layout entry is the id itself or {id, ...settings}. Entries that
    hold entries of their own are searched too: being generous here costs a
    daemon that stays up, being strict would stop someone's music."""
    if isinstance(entry, str):
        return entry == plugin_id
    if isinstance(entry, dict):
        if str(entry.get("id") or "") == plugin_id:
            return True
        return any(_names_plugin(value, plugin_id) for value in entry.values()
                   if isinstance(value, (list, dict)))
    if isinstance(entry, list):
        return any(_names_plugin(item, plugin_id) for item in entry)
    return False


def plugin_enabled_in(shell_config, plugin_id: str = PLUGIN_ID) -> bool:
    """The shell's rule for a third-party plugin (PluginRegistry.isEnabled
    and findEntryLocation): not in disabledPlugins, and named by the bar, a
    bar layout entry in any section, or an entry of plugins[]. Anything that
    is not a JSON object is no answer, and no answer means enabled. So is a
    file without version 1: the shell does not use it either (applyShellConfig
    falls back to its defaults and keeps changes in memory), so its content
    says nothing about what the shell has loaded."""
    if not isinstance(shell_config, dict):
        return True
    version = shell_config.get("version")
    if isinstance(version, bool) or version != 1:
        return True
    disabled = shell_config.get("disabledPlugins")
    if isinstance(disabled, list) and plugin_id in disabled:
        return False
    bar = shell_config.get("bar")
    if isinstance(bar, dict):
        if str(bar.get("id") or "") == plugin_id:
            return True
        layout = bar.get("layout")
        if isinstance(layout, dict) and any(
                isinstance(section, list) and _names_plugin(section, plugin_id)
                for section in layout.values()):
            return True
    plugins = shell_config.get("plugins")
    if isinstance(plugins, list):
        for entry in plugins:
            if isinstance(entry, dict) and str(entry.get("id") or "") == plugin_id:
                return True
    return False


def shell_config_stamp():
    """What changes when shell.json is rewritten (mtime, size and inode, so
    an atomic replace inside one clock tick still counts), or None when there
    is no file."""
    try:
        info = shell_config_path().stat()
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size, info.st_ino)


def plugin_enabled() -> tuple[object, bool]:
    """(stamp, enabled) for shell.json as it is now. The stamp is taken before
    the read, so a write landing in between shows up as a changed stamp on the
    next look rather than being missed. A missing, unreadable or half-written
    file says enabled: music must never stop on a file mid-save."""
    stamp = shell_config_stamp()
    try:
        text = shell_config_path().read_text("utf-8")
    except (OSError, UnicodeDecodeError):
        return stamp, True
    try:
        return stamp, plugin_enabled_in(json.loads(text))
    except (ValueError, RecursionError):  # garbage, or nesting past the stack
        return stamp, True


def auth_path() -> Path:
    return data_dir() / "account.json"


def devtoken_path() -> Path:
    return cache_dir() / "devtoken.json"


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
    # Off: startup only looks in the plugin's own sign-in profile. On: it
    # also reads the media-user-token from Chrome, Chromium, Brave and Edge.
    "importBrowserCookies": False,
    "cacheLimitMb": 1024,
    "volume": 100,
    "muteRestore": 70,         # the level unmute returns to; written by mute
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
