"""A very small Chrome DevTools Protocol client, standard library only.

The sign-in window is the one place where the daemon needs to know what the
page thinks, rather than what Chrome has already flushed to disk: a fresh
session's ``media-user-token`` cookie can sit in memory for half a minute
before the cookie database sees it, and the web player itself knows the token
the moment the user finishes signing in.

Only what is needed is implemented: find the page over HTTP, open its
websocket, evaluate one expression, and close the browser once sign-in is
over. Chrome's own port is read from ``DevToolsActivePort`` in the profile, so
nothing is hard-coded.

The endpoint has no authentication, so it is kept small: Chrome binds it to
loopback, every connection here goes to 127.0.0.1 whatever Chrome reports,
and a port file is only believed while the Chrome that wrote it is alive
(otherwise whatever took the port since would be asked for a token).
"""

import base64
import json
import os
import socket
import struct
from pathlib import Path

from . import config

LOOPBACK = "127.0.0.1"


class CdpError(Exception):
    pass


def browser_pid(profile: Path) -> int | None:
    """The pid of the Chrome holding `profile` open, from its SingletonLock.

    The lock is a symlink to "hostname-pid". Another host's pid (a home
    directory shared over the network) means nothing here, so it is not one.
    """
    try:
        target = os.readlink(profile / "SingletonLock")
    except OSError:
        return None
    host, _, pid = target.rpartition("-")
    if host and host != socket.gethostname():
        return None
    try:
        pid_number = int(pid)
        os.kill(pid_number, 0)
    except (ValueError, OSError):
        return None
    return pid_number


def _active_port(profile: Path) -> tuple[int, str] | None:
    """(port, browser websocket path) from DevToolsActivePort, when it was
    written by the Chrome that holds the profile now.

    Chrome writes the file at startup and leaves it behind when it exits, so
    a file older than the current lock, or with no live lock at all, is from
    an earlier run and its port may belong to anything by now.
    """
    path = profile / "DevToolsActivePort"
    if browser_pid(profile) is None:
        return None
    try:
        lines = path.read_text("utf-8").splitlines()
        written = path.stat().st_mtime
        locked = os.lstat(profile / "SingletonLock").st_mtime
        port = int(lines[0].strip())
    except (OSError, ValueError, IndexError):
        return None
    if not 0 < port < 65536 or written + 2 < locked:
        return None
    browser_path = lines[1].strip() if len(lines) > 1 else ""
    return port, browser_path


def _port(profile: Path) -> int | None:
    active = _active_port(profile)
    return active[0] if active else None


def forget_port(profile: Path) -> None:
    """Remove a port file left by an earlier run, so nothing probes it."""
    if browser_pid(profile) is not None:
        return
    try:
        (profile / "DevToolsActivePort").unlink()
    except OSError:
        pass


def _http_json(port: int, path: str):
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://{LOOPBACK}:{port}{path}", timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as error:  # noqa: BLE001 - Chrome may be starting or gone
        raise CdpError(str(error)) from error


def _pages(port: int) -> list[dict]:
    pages = _http_json(port, "/json")
    return pages if isinstance(pages, list) else []


def _ws_path(url: str) -> str:
    """The path of a ws:// URL Chrome reported. Only the path is used: the
    host is always loopback, whatever the URL says."""
    return "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"


class _WebSocket:
    """Just enough RFC 6455 for DevTools: text frames, client masking."""

    def __init__(self, host: str, port: int, path: str, timeout: float = 5.0):
        try:
            self.sock = socket.create_connection((host, port), timeout=timeout)
        except OSError as error:  # Chrome went away between /json and here
            raise CdpError(str(error)) from error
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        try:
            self.sock.sendall(request.encode())
            buffered = b""
            while b"\r\n\r\n" not in buffered:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise CdpError("websocket closed during handshake")
                buffered += chunk
            head, _, self._rest = buffered.partition(b"\r\n\r\n")
            if b" 101 " not in head.split(b"\r\n")[0]:
                raise CdpError("websocket upgrade refused")
        except (OSError, CdpError) as error:
            self.close()
            if isinstance(error, CdpError):
                raise
            raise CdpError(str(error)) from error

    # -- framing ----------------------------------------------------------
    def send(self, payload: dict) -> None:
        data = json.dumps(payload).encode()
        header = bytearray([0x81])
        length = len(data)
        if length < 126:
            header.append(0x80 | length)
        elif length < (1 << 16):
            header.append(0x80 | 126)
            header += struct.pack(">H", length)
        else:
            header.append(0x80 | 127)
            header += struct.pack(">Q", length)
        mask = os.urandom(4)
        header += mask
        self.sock.sendall(bytes(header) + bytes(byte ^ mask[i % 4] for i, byte in enumerate(data)))

    def _read_exact(self, count: int) -> bytes:
        while len(self._rest) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CdpError("websocket closed")
            self._rest += chunk
        data, self._rest = self._rest[:count], self._rest[count:]
        return data

    def recv(self) -> dict:
        first, second = self._read_exact(2)
        opcode = first & 0x0F
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8))[0]
        data = self._read_exact(length)
        if opcode == 0x8:
            raise CdpError("websocket closed by peer")
        if opcode == 0x9:  # ping
            self.send({"pong": True})
            return self.recv()
        return json.loads(data.decode("utf-8"), strict=False)

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def evaluate(expression: str, profile: Path | None = None, timeout: float = 6.0,
             user_gesture: bool = False):
    """Evaluate one expression in the Apple Music page and return its value."""
    profile = profile or config.login_profile()
    port = _port(profile)
    if not port:
        raise CdpError("no DevToolsActivePort")
    pages = [p for p in _pages(port) if p.get("type") == "page"]
    page = next((p for p in pages if "music.apple.com" in p.get("url", "")), None)
    if page is None:
        raise CdpError("no Apple Music page")
    path = _ws_path(page.get("webSocketDebuggerUrl", ""))
    websocket = _WebSocket(LOOPBACK, port, path, timeout=timeout)
    try:
        websocket.send({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "returnByValue": True,
                       "awaitPromise": True, "userGesture": bool(user_gesture)},
        })
        for _ in range(20):
            message = websocket.recv()
            if message.get("id") == 1:
                result = message.get("result") or {}
                if result.get("exceptionDetails"):
                    raise CdpError(str(result["exceptionDetails"].get("text", "exception")))
                return (result.get("result") or {}).get("value")
        raise CdpError("no answer from the page")
    finally:
        websocket.close()


TOKEN_EXPRESSION = """(() => {
  const mk = (window.MusicKit && window.MusicKit.getInstance) ? window.MusicKit.getInstance() : null;
  if (mk && mk.musicUserToken) return mk.musicUserToken;
  if (mk && !mk.isAuthorized) return "";
  for (let i = 0; i < localStorage.length; i++) {
    const value = localStorage.getItem(localStorage.key(i));
    if (value && /^[A-Za-z0-9%._-]{200,}$/.test(value) && (value.includes('%') || value.length > 400)) return value;
  }
  return "";
})()"""


def close_browser(profile: Path | None = None, timeout: float = 5.0) -> None:
    """Ask the Chrome holding `profile` to quit, the way its own menu does.

    Browser.close is a clean shutdown: cookies are flushed and the profile
    lock is released. It goes to the browser target (DevToolsActivePort's
    second line, or /json/version), not a page, so it works whatever tab is
    in front. Chrome may drop the connection before it answers.
    """
    profile = profile or config.login_profile()
    active = _active_port(profile)
    if not active:
        raise CdpError("no DevToolsActivePort")
    port, path = active
    if not path.startswith("/devtools/browser/"):
        version = _http_json(port, "/json/version")
        path = _ws_path(str((version or {}).get("webSocketDebuggerUrl") or ""))
    websocket = _WebSocket(LOOPBACK, port, path, timeout=timeout)
    try:
        try:
            websocket.send({"id": 1, "method": "Browser.close"})
            for _ in range(20):
                if websocket.recv().get("id") == 1:
                    return
        except (CdpError, OSError):
            return  # closed under us: that is the answer
    finally:
        websocket.close()


def page_token(profile: Path | None = None) -> str | None:
    """The token the signed-in page holds, when the window is still open."""
    try:
        value = evaluate(TOKEN_EXPRESSION, profile=profile)
    except (CdpError, OSError, ValueError):  # a timeout mid-frame, a torn reply
        return None
    return str(value) if value else None
