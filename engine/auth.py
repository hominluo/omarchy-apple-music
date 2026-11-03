"""The account: where the music-user token comes from and where it is kept.

Apple only issues a ``media-user-token`` to a signed-in browser session, so
the only honest way to get one is to let the user sign in inside a real
Chrome/Chromium window once. That window is a throwaway profile owned by the
plugin (``data/chrome``), launched with ``--password-store=basic`` so the
cookie is encrypted with Chrome's well-known Linux key instead of whatever
keyring happens to be running; after that we read the cookie straight out of
Chrome's SQLite database. No CDP, no remote debugging port, no JavaScript.

The token is stored in ``account.json`` with mode 0600 and is the only secret
the daemon holds.
"""

import json
import logging
import os
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from . import config

log = logging.getLogger("apple-music.auth")

COOKIE_NAME = "media-user-token"
COOKIE_HOSTS = (".music.apple.com", "music.apple.com")


# ---- Chrome cookie database ----------------------------------------------

def _cookie_db(profile: Path) -> Path | None:
    """The cookie database of a profile, or of the user-data-dir holding it.

    Callers hand us either a profile directory (``.../Default``) or a Chrome
    user-data-dir (the plugin's own profile); the database lives one level
    deeper in the second case, and newer Chromes moved it into ``Network``.
    """
    candidates = [
        profile / "Cookies",
        profile / "Network" / "Cookies",
        profile / "Default" / "Cookies",
        profile / "Default" / "Network" / "Cookies",
    ]
    found = [path for path in candidates if path.is_file()]
    if not found:
        return None
    return max(found, key=lambda path: path.stat().st_mtime)


def _aes():
    """AES-CBC from pycryptodome, which pywidevine already depends on."""
    try:
        from Crypto.Cipher import AES

        return AES
    except ImportError:  # pycryptodomex installs the same API as Cryptodome
        from Cryptodome.Cipher import AES

        return AES


def _chrome_key(password: bytes) -> bytes:
    try:
        from Crypto.Hash import SHA1
        from Crypto.Protocol.KDF import PBKDF2
    except ImportError:
        from Cryptodome.Hash import SHA1
        from Cryptodome.Protocol.KDF import PBKDF2

    return PBKDF2(password, b"saltysalt", 16, count=1, hmac_hash_module=SHA1)


def _keyring_password() -> bytes | None:
    """Chrome's keyring secret, when the profile was created with one."""
    if not shutil.which("secret-tool"):
        return None
    for attributes in (
        ["application", "chrome"],
        ["xdg:schema", "chrome_libsecret_os_crypt_password_v2", "application", "chrome"],
    ):
        try:
            result = subprocess.run(
                ["secret-tool", "lookup", *attributes],
                capture_output=True, timeout=5, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


def _strip_host_digest(plain: bytes, host: str) -> bytes:
    """Drop the SHA-256 domain binding Chrome >= 130 puts in front.

    The digest is the hash of the cookie's domain, with or without the
    leading dot, so both are tried instead of guessing from the bytes.
    """
    if len(plain) <= 32:
        return plain
    import hashlib

    for name in (host, host.lstrip("."), "." + host.lstrip(".")):
        if hashlib.sha256(name.encode()).digest() == plain[:32]:
            return plain[32:]
    return plain


def _decrypt_value(encrypted: bytes, password: bytes, host: str = "") -> str | None:
    if not encrypted:
        return None
    if encrypted[:3] not in (b"v10", b"v11"):
        # Chrome stores small values in the clear, with a legacy prefix.
        return encrypted.decode("utf-8", "ignore")
    AES = _aes()

    payload = encrypted[3:]
    cipher = AES.new(_chrome_key(password), AES.MODE_CBC, iv=b" " * 16)
    plain = cipher.decrypt(payload)
    if not plain:
        return None
    padding = plain[-1]
    if 0 < padding <= 16:
        plain = plain[:-padding]
    plain = _strip_host_digest(plain, host)
    try:
        return plain.decode("utf-8").strip("\x00").strip()
    except UnicodeDecodeError:
        return None


def _read_cookie_db(db: Path) -> list[tuple[str, bytes, bytes]]:
    """Rows for the music-user-token, read from a copy of the live database.

    Chrome keeps the database open in WAL mode while it runs, so copying just
    the main file can miss a cookie written seconds ago. Copy the -wal too.
    """
    rows: list[tuple[str, bytes, bytes]] = []
    with tempfile.TemporaryDirectory(prefix="apple-music-cookie-") as tmp:
        copy = Path(tmp) / "Cookies"
        try:
            shutil.copy2(db, copy)
        except OSError:
            return rows
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(db) + suffix)
            if sidecar.exists():
                try:
                    shutil.copy2(sidecar, Path(str(copy) + suffix))
                except OSError:
                    pass
        try:
            with sqlite3.connect(str(copy)) as connection:
                cursor = connection.execute(
                    "SELECT host_key, value, encrypted_value FROM cookies "
                    "WHERE name = ? ORDER BY last_access_utc DESC",
                    (COOKIE_NAME,),
                )
                rows = [(str(host or ""), value or b"", encrypted or b"")
                        for host, value, encrypted in cursor]
        except sqlite3.DatabaseError as error:
            log.debug("cookie database unreadable: %s", error)
    return rows


def _decode_rows(rows: list[tuple[str, bytes, bytes]]) -> str | None:
    if not rows:
        return None
    # A plaintext value wins: it needs no key at all.
    for _host, value, _encrypted in rows:
        text = (value or b"").decode("utf-8", "ignore").strip() if isinstance(value, bytes) else str(value)
        if text:
            return text
    passwords = [b"peanuts"]
    keyring = _keyring_password()
    if keyring and keyring not in passwords:
        passwords.append(keyring)
    for host, _value, encrypted in rows:
        for password in passwords:
            try:
                token = _decrypt_value(encrypted, password, host)
            except Exception as error:  # noqa: BLE001 - a wrong key is not fatal
                log.debug("cookie decrypt failed: %s", error)
                continue
            if token:
                return token
    return None


def read_profile_token(profile: Path) -> str | None:
    db = _cookie_db(profile)
    if not db:
        return None
    return _decode_rows(_read_cookie_db(db))


def discover() -> tuple[Path, str] | None:
    """Any profile on this machine that already holds an Apple Music session.

    The plugin's own profile is checked first, then the daily browsers, so a
    user who has signed in anywhere does not have to do it twice.
    """
    candidates = [config.login_profile()]
    for root in config.CHROME_PROFILE_ROOTS:
        base = Path.home() / root
        if not base.is_dir():
            continue
        for name in ("Default", "Profile 1", "Profile 2", "Profile 3"):
            if (base / name).is_dir():
                candidates.append(base / name)
    for profile in candidates:
        try:
            token = read_profile_token(profile)
        except Exception as error:  # noqa: BLE001
            log.debug("profile %s: %s", profile, error)
            continue
        if token:
            return profile, token
    return None


# ---- the plugin's sign-in window ----------------------------------------

def _display() -> dict:
    env = dict(os.environ)
    if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        env.setdefault("WAYLAND_DISPLAY", "wayland-1")
    return env


def login_running() -> bool:
    """True while a Chrome we launched still has the profile open."""
    lock = config.login_profile() / "SingletonLock"
    try:
        target = os.readlink(lock)
    except OSError:
        return False
    # Chrome's lock is "hostname-pid".
    _, _, pid = target.rpartition("-")
    try:
        os.kill(int(pid), 0)
    except (ValueError, OSError):
        return False
    return True


def launch_login(storefront: str = "us") -> dict:
    chrome = config.find_chrome()
    if not chrome:
        raise RuntimeError("No Chrome, Chromium, Brave or Edge found on this system")
    profile = config.login_profile()
    if login_running():
        return {"running": True, "profile": str(profile)}
    url = f"https://music.apple.com/{storefront or 'us'}/new"
    subprocess.Popen(
        [
            chrome,
            f"--user-data-dir={profile}",
            "--password-store=basic",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            # Port 0: Chrome picks one and records it in DevToolsActivePort.
            # The daemon only reads the page with it (see cdp.py); nothing
            # listens on a fixed port that another instance could take.
            "--remote-debugging-port=0",
            # A normal window, not --app: Apple's sign-in opens its own popup
            # for the Apple ID form, and app-mode windows refuse popups.
            "--new-window",
            f"--window-size=1180,960",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=_display(),
    )
    return {"running": True, "profile": str(profile), "url": url}


def token_from_window() -> str | None:
    """Ask the sign-in window itself: faster and more reliable than the disk.

    Chrome batches cookie writes, so a session can be minutes old before the
    database shows it, while the page knows the token immediately.
    """
    from . import cdp

    return cdp.page_token(config.login_profile())


def current_token() -> str | None:
    token = token_from_window()
    if token:
        return token
    return read_profile_token(config.login_profile())


def wait_for_token(timeout: float = 300.0, interval: float = 1.0) -> str | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        token = current_token()
        if token:
            return token
        if not login_running():
            # The window closed without a session; stop waiting.
            return read_profile_token(config.login_profile())
        time.sleep(interval)
    return None


# ---- persistence ---------------------------------------------------------

DEFAULT_ACCOUNT = {
    "mediaUserToken": "",
    "storefront": "",
    "savedAt": 0,
    "source": "",
    "account": None,
}


def load() -> dict:
    account = dict(DEFAULT_ACCOUNT)
    try:
        stored = json.loads(config.auth_path().read_text("utf-8"))
        if isinstance(stored, dict):
            account.update(stored)
    except (OSError, ValueError):
        pass
    return account


def save(token: str, source: str = "", storefront: str = "", account: dict | None = None) -> dict:
    record = {
        "mediaUserToken": token,
        "storefront": storefront,
        "savedAt": int(time.time()),
        "source": source,
        "account": account,
    }
    path = config.auth_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n", "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return record


def clear() -> None:
    try:
        config.auth_path().unlink()
    except OSError:
        pass


def machine_id() -> str:
    return socket.gethostname()
