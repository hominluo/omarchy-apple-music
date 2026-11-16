"""The account: where the music-user token comes from and where it is kept.

Apple only issues a ``media-user-token`` to a signed-in browser session, so
the only honest way to get one is to let the user sign in inside a real
Chrome/Chromium window once. That window is a throwaway profile owned by the
plugin (``data/chrome``), launched with ``--password-store=basic`` so the
cookie is encrypted with Chrome's well-known Linux key instead of whatever
keyring happens to be running.

While the window is open the token is read from the page itself over the
DevTools port (see cdp.py; Chrome batches cookie writes, so the database can
lag the page by half a minute), and from the cookie database once Chrome has
flushed it. The window is closed as soon as the token is in hand, or when the
sign-in is cancelled or times out, so the unauthenticated DevTools endpoint
lives only as long as the sign-in does.

The token is stored in ``account.json`` with mode 0600 and is the only secret
the daemon holds. Signing out keeps that file, with no token and a
``signedOut`` flag, so the next start does not find the old session again and
sign back in by itself; only an explicit sign-in clears the flag.
"""

import json
import logging
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import cdp, config

log = logging.getLogger("apple-music.auth")

COOKIE_NAME = "media-user-token"
COOKIE_HOSTS = (".music.apple.com", "music.apple.com")
# Chrome counts cookie times in microseconds since 1601-01-01.
_CHROME_EPOCH_OFFSET = 11644473600
# How long a just-launched browser may take to show up (take the profile
# lock) before a missing lock means it is not coming. A cold, headed start
# through the /usr/bin wrapper scripts takes well over the first poll.
LOGIN_STARTUP_GRACE = 20.0
# How long Browser.close may take before the window is sent SIGTERM.
CLOSE_WAIT = 5.0


# ---- Chrome cookie database ----------------------------------------------

def _cookie_dbs(profile: Path) -> list[Path]:
    """Every cookie database of a profile, or of the user-data-dir holding it.

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
    return [path for path in candidates if path.is_file()]


def _cookie_db(profile: Path) -> Path | None:
    found = _cookie_dbs(profile)
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


def _keyring_password(applications: tuple[str, ...] = ("chrome",)) -> list[bytes]:
    """The browser's keyring secret, when its profile was created with one.

    Each browser files its secret under its own name (chrome, chromium,
    brave, ...), in the old schema and in the v2 one; all are tried.
    """
    if not applications or not shutil.which("secret-tool"):
        return []
    found: list[bytes] = []
    for application in applications:
        for attributes in (
            ["application", application],
            ["xdg:schema", "chrome_libsecret_os_crypt_password_v2", "application", application],
        ):
            try:
                result = subprocess.run(
                    ["secret-tool", "lookup", *attributes],
                    capture_output=True, timeout=5, check=False,
                )
            except (OSError, subprocess.SubprocessError):
                continue
            secret = result.stdout.strip()
            if result.returncode == 0 and secret and secret not in found:
                found.append(secret)
    return found


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


def _chrome_now() -> int:
    return int((time.time() + _CHROME_EPOCH_OFFSET) * 1_000_000)


def _read_cookie_db(db: Path) -> list[tuple[str, bytes, bytes]]:
    """Rows for the music-user-token, read from a copy of the live database.

    Chrome keeps the database open in WAL mode while it runs, so copying just
    the main file can miss a cookie written seconds ago. Copy the -wal too.
    Expired rows are left out: Chrome only purges them while it runs, and a
    dead cookie on disk is exactly what a re-sign-in must not pick up.
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
                    "WHERE name = ? AND (expires_utc = 0 OR expires_utc > ?) "
                    "ORDER BY last_access_utc DESC",
                    (COOKIE_NAME, _chrome_now()),
                )
                rows = [(str(host or ""), value or b"", encrypted or b"")
                        for host, value, encrypted in cursor]
        except sqlite3.DatabaseError as error:
            log.debug("cookie database unreadable: %s", error)
    return rows


def _decode_rows(rows: list[tuple[str, bytes, bytes]],
                 keyring: tuple[str, ...] = ()) -> list[str]:
    """Every token the rows decrypt to, most recently used first."""
    tokens: list[str] = []
    if not rows:
        return tokens
    # A plaintext value needs no key at all.
    for _host, value, _encrypted in rows:
        text = (value or b"").decode("utf-8", "ignore").strip() if isinstance(value, bytes) else str(value)
        if text and text not in tokens:
            tokens.append(text)
    encrypted_rows = [row for row in rows if row[2]]
    if not encrypted_rows:
        return tokens
    passwords = [b"peanuts"]
    secrets: list[bytes] | None = None
    for host, _value, encrypted in encrypted_rows:
        token = None
        for password in passwords:
            token = _try_decrypt(encrypted, password, host)
            if token:
                break
        if not token and encrypted[:3] == b"v11" and secrets is None:
            # Only asked for when the basic key fails: a keyring lookup can
            # pop an unlock prompt.
            secrets = _keyring_password(keyring)
            passwords.extend(s for s in secrets if s not in passwords)
            for password in secrets:
                token = _try_decrypt(encrypted, password, host)
                if token:
                    break
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _try_decrypt(encrypted: bytes, password: bytes, host: str) -> str | None:
    try:
        return _decrypt_value(encrypted, password, host)
    except Exception as error:  # noqa: BLE001 - a wrong key is not fatal
        log.debug("cookie decrypt failed: %s", error)
        return None


def read_profile_token(profile: Path, reject=(), keyring: tuple[str, ...] = ()) -> str | None:
    """The newest live token in a profile that is not in `reject`."""
    db = _cookie_db(profile)
    if not db:
        return None
    for token in _decode_rows(_read_cookie_db(db), keyring):
        if token not in reject:
            return token
    return None


def _daily_profiles() -> list[tuple[Path, tuple[str, ...]]]:
    """Default and every "Profile N" of each daily browser, with the keyring
    names its cookies may be encrypted under."""
    found: list[tuple[Path, tuple[str, ...]]] = []
    for root, keyring in config.CHROME_PROFILE_ROOTS:
        base = Path.home() / root
        if not base.is_dir():
            continue
        names = ["Default"] + sorted(
            (path.name for path in base.glob("Profile *") if path.is_dir()),
            key=lambda name: (len(name), name),
        )
        for name in names:
            if (base / name).is_dir():
                found.append((base / name, tuple(keyring)))
    return found


def discover() -> tuple[Path, str] | None:
    """Any profile on this machine that already holds an Apple Music session.

    The plugin's own profile is checked first, then the daily browsers
    (Chrome, Chromium, Brave, Edge), so a user who has signed in anywhere
    does not have to do it twice. The daemon does not call this after an
    explicit sign-out.
    """
    candidates = [(config.login_profile(), ())] + _daily_profiles()
    for profile, keyring in candidates:
        try:
            token = read_profile_token(profile, keyring=keyring)
        except Exception as error:  # noqa: BLE001
            log.debug("profile %s: %s", profile, error)
            continue
        if token:
            return profile, token
    return None


# ---- the plugin's sign-in window ----------------------------------------

# The browser launch_login started last. Kept so a window that has not taken
# the profile lock yet still counts as starting, and so its exit is reaped.
_launched: subprocess.Popen | None = None
_launched_at = 0.0
_launch_lock = threading.Lock()


def _display() -> dict:
    env = dict(os.environ)
    if not env.get("DISPLAY") and not env.get("WAYLAND_DISPLAY"):
        env.setdefault("WAYLAND_DISPLAY", "wayland-1")
    return env


def login_running() -> bool:
    """True while a Chrome still has the plugin's profile open."""
    return cdp.browser_pid(config.login_profile()) is not None


def _launched_alive() -> bool:
    """The browser we launched has not exited (polling also reaps it)."""
    with _launch_lock:
        proc = _launched
    return proc is not None and proc.poll() is None


def _current_launch() -> subprocess.Popen | None:
    with _launch_lock:
        return _launched


def _clean_profile_first() -> None:
    """Finish a sign-out that could not remove the Apple session (its window
    would not close, or the cookie database would not open). Without this the
    next window opens signed in to the old account and is captured at once,
    with no chance to choose another."""
    record = load()
    if not (record.get("profileDirty") and signed_out(record)):
        return
    if login_running():
        close_login()
        if login_running():
            raise RuntimeError("the previous sign-in window is still open; close it and try again")
    if not forget_profile_session():
        raise RuntimeError("the old Apple session could not be removed from the sign-in profile")
    _write({key: value for key, value in record.items() if key != "profileDirty"})


def launch_login(storefront: str = "us") -> dict:
    global _launched, _launched_at
    chrome = config.find_chrome()
    if not chrome:
        raise RuntimeError("No Chrome, Chromium, Brave or Edge found on this system")
    profile = config.login_profile()
    _clean_profile_first()
    if login_running():
        return {"running": True, "profile": str(profile)}
    # A port file from an earlier run would be probed until the new window
    # writes its own; whatever listens on that port now is not ours.
    cdp.forget_port(profile)
    url = f"https://music.apple.com/{storefront or 'us'}/new"
    proc = subprocess.Popen(
        [
            chrome,
            f"--user-data-dir={profile}",
            "--password-store=basic",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-features=Translate",
            # Port 0: Chrome picks one and records it in DevToolsActivePort.
            # The daemon only reads the page with it (see cdp.py); nothing
            # listens on a fixed port that another instance could take. The
            # listener is loopback (Chrome's default, stated anyway) and
            # goes when the window is closed after sign-in.
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            # A normal window, not --app: Apple's sign-in opens its own popup
            # for the Apple ID form, and app-mode windows refuse popups.
            "--new-window",
            "--window-size=1180,960",
            url,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=_display(),
    )
    with _launch_lock:
        _launched, _launched_at = proc, time.monotonic()
    return {"running": True, "profile": str(profile), "url": url}


def close_login(wait: float = CLOSE_WAIT) -> bool:
    """Close the sign-in window; True once nothing holds the profile.

    Browser.close first, which flushes cookies like a normal quit; SIGTERM
    (Chrome's own clean-exit signal) only if that did not take. Never
    SIGKILL: a killed Chrome leaves the profile to be repaired next time.
    """
    profile = config.login_profile()
    if not login_running() and _launched_alive():
        _settle_launch(wait)
    if login_running():
        try:
            cdp.close_browser(profile)
        except (cdp.CdpError, OSError) as error:
            # A failed close must not end the caller (a sign-in that has
            # already saved its token): SIGTERM below still closes it.
            log.debug("Browser.close: %s", error)
        if not _wait_closed(wait):
            pid = cdp.browser_pid(profile)
            if pid is not None:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
                _wait_closed(wait)
    _launched_alive()  # reap it if it has gone
    cdp.forget_port(profile)
    return not login_running()


def _settle_launch(wait: float) -> None:
    """The browser launch_login started is alive but does not hold the
    profile yet: a sign-in cancelled (quit, sign-out) in the first second of
    a cold start. Left alone it would open its window and DevTools port with
    nothing watching or closing it. It gets the moment it needs to take the
    lock, so the clean close works; one that still has not is ended."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if login_running() or not _launched_alive():
            return
        time.sleep(0.1)
    proc = _current_launch()
    if proc is None or proc.poll() is not None:
        return
    log.info("the sign-in browser never took its profile; ending it")
    try:
        # It leads its own session (start_new_session), so the group is
        # the browser and its helpers only. SIGTERM is Chrome's clean exit.
        os.killpg(proc.pid, signal.SIGTERM)
    except OSError:
        try:
            proc.terminate()
        except OSError:
            pass
    try:
        proc.wait(wait)
    except subprocess.TimeoutExpired:
        pass


def _wait_closed(wait: float) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if not login_running():
            return True
        time.sleep(0.1)
    return not login_running()


def token_from_window() -> str | None:
    """Ask the sign-in window itself: faster and more reliable than the disk.

    Chrome batches cookie writes, so a session can be minutes old before the
    database shows it, while the page knows the token immediately.
    """
    return cdp.page_token(config.login_profile())


def current_token(reject=(), disk: bool = True) -> str | None:
    token = token_from_window()
    if token and token not in reject:
        return token
    if not disk:
        return None
    return read_profile_token(config.login_profile(), reject)


def wait_for_token(timeout: float = 300.0, interval: float = 1.0, *, reject=(),
                   cancel: threading.Event | None = None, close: bool = True) -> str | None:
    """Wait for the sign-in window to hold a session; None if it never does.

    `reject` holds tokens that must not count (the one being replaced: it is
    still on disk, and was accepted the moment the browser was launched).
    The window counts as closed only once it has been seen running and is
    gone, or when nothing has shown up within LOGIN_STARTUP_GRACE: the first
    poll comes milliseconds after launch, long before Chrome takes the lock.
    The disk is only read once the window has been seen, so Chrome has had
    the chance to answer from the page first.

    The window is closed on a timeout or `cancel`, and after a capture when
    `close` is set (a caller that still wants to check the token first
    closes it itself, with close_login).
    """
    reject = set(reject or ())
    started = time.monotonic()
    deadline = started + timeout
    seen = False
    token = None
    launched = _current_launch()
    try:
        while time.monotonic() < deadline:
            if cancel is not None and cancel.is_set():
                return None
            token = current_token(reject, disk=seen)
            if token:
                return token
            running = login_running()
            seen = seen or running
            if not running and not _launched_alive() and (
                    seen or time.monotonic() - started > LOGIN_STARTUP_GRACE):
                if _current_launch() is not launched:
                    # Sign in was clicked again just as the old window went:
                    # the new one is this wait's to watch, from its own start.
                    launched, started, seen = _current_launch(), time.monotonic(), False
                    continue
                # The window closed; Chrome flushed its cookies on the way out.
                token = read_profile_token(config.login_profile(), reject)
                return token
            if cancel is not None:
                if cancel.wait(interval):
                    return None
            else:
                time.sleep(interval)
        return None
    finally:
        # A window launched after this wait gave up belongs to the sign-in
        # that launched it, not to this cleanup.
        relaunched = _current_launch() is not launched and _launched_alive()
        if (close or not token) and not relaunched:
            close_login()


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


def _write(record: dict) -> dict:
    path = config.auth_path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2) + "\n", "utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(path)
    return record


def save(token: str, source: str = "", storefront: str = "", account: dict | None = None) -> dict:
    """Store a token. A fresh record: it drops any error and the signed-out
    flag, so only a token being saved (a sign-in) clears that flag."""
    return _write({
        "mediaUserToken": token,
        "storefront": storefront,
        "savedAt": int(time.time()),
        "source": source,
        "account": account,
    })


def signed_out(record: dict | None = None) -> bool:
    record = load() if record is None else record
    return bool(record.get("signedOut")) and not record.get("mediaUserToken")


def forget_profile_session(profile: Path | None = None) -> bool:
    """Remove the Apple session from the plugin's own profile.

    Deleting only account.json left the media-user-token cookie behind, and
    the next start found it and signed straight back in. The Apple ID
    cookies go with it (the profile is the plugin's and holds nothing else),
    and so does the page's local storage, where MusicKit keeps its own copy
    of the token: otherwise "sign in" after a sign-out would come back as the
    same account without asking. Chrome must not be running; True when
    nothing of the session is left.
    """
    profile = profile or config.login_profile()
    if cdp.browser_pid(profile) is not None:
        return False
    clean = True
    for db in _cookie_dbs(profile):
        try:
            with sqlite3.connect(str(db), timeout=2) as connection:
                connection.execute(
                    "DELETE FROM cookies WHERE name = ? OR host_key = 'apple.com' "
                    "OR host_key LIKE '%.apple.com'",
                    (COOKIE_NAME,),
                )
        except sqlite3.DatabaseError as error:
            log.warning("could not clear the sign-in profile's cookies: %s", error)
            clean = False
    for sub in ("Default/Local Storage", "Default/Session Storage"):
        shutil.rmtree(profile / sub, ignore_errors=True)
    return clean and read_profile_token(profile) is None


def sign_out() -> dict:
    """Forget the account: the stored token, the profile's session, and a
    flag so startup discovery leaves the user signed out."""
    close_login()
    record = dict(DEFAULT_ACCOUNT, savedAt=int(time.time()), signedOut=True)
    if not forget_profile_session():
        log.warning("the sign-in profile still holds an Apple session")
        # Recorded, so the daemon can say so and launch_login finishes the
        # job before the next window opens (Chrome is gone by then).
        record["profileDirty"] = True
    try:
        return _write(record)
    except OSError as error:
        log.warning("could not record the sign-out: %s", error)
        return record


def clear() -> None:
    try:
        config.auth_path().unlink()
    except OSError:
        pass


def machine_id() -> str:
    return socket.gethostname()
