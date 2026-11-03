"""A very small Chrome DevTools Protocol client, standard library only.

The sign-in window is the one place where the daemon needs to know what the
page thinks, rather than what Chrome has already flushed to disk: a fresh
session's ``media-user-token`` cookie can sit in memory for half a minute
before the cookie database sees it, and the web player itself knows the token
the moment the user finishes signing in.

Only what is needed is implemented: find the page over HTTP, open its
websocket, and evaluate one expression. Chrome's own port is read from
``DevToolsActivePort`` in the profile, so nothing is hard-coded.
"""

import base64
import json
import os
import socket
import struct
from pathlib import Path

from . import config


class CdpError(Exception):
    pass


def _port(profile: Path) -> int | None:
    try:
        lines = (profile / "DevToolsActivePort").read_text("utf-8").splitlines()
        return int(lines[0].strip())
    except (OSError, ValueError, IndexError):
        return None


def _pages(port: int) -> list[dict]:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as error:  # noqa: BLE001 - Chrome may be starting or gone
        raise CdpError(str(error)) from error


class _WebSocket:
    """Just enough RFC 6455 for DevTools: text frames, client masking."""

    def __init__(self, host: str, port: int, path: str, timeout: float = 5.0):
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
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
    url = page.get("webSocketDebuggerUrl", "")
    path = "/" + url.split("/", 3)[3] if url.count("/") >= 3 else "/"
    websocket = _WebSocket("127.0.0.1", port, path, timeout=timeout)
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


def page_token(profile: Path | None = None) -> str | None:
    """The token the signed-in page holds, when the window is still open."""
    try:
        value = evaluate(TOKEN_EXPRESSION, profile=profile)
    except CdpError:
        return None
    return str(value) if value else None
