"""A stand-in for the sign-in Chrome: the profile lock, DevToolsActivePort
and just enough of the DevTools HTTP + websocket protocol for cdp.py.

Behaviour comes from the environment, so a test can shape it:
    FAKE_CHROME_DELAY         seconds before the lock and port appear (a cold start)
    FAKE_CHROME_TOKEN_FILE    the page's token is read from here on every evaluate
    FAKE_CHROME_IGNORE_CLOSE  set: Browser.close is answered but ignored
    FAKE_CHROME_LOG           commands received are appended here, one per line

It binds 127.0.0.1 only and exits (removing its lock, like Chrome) on
Browser.close or SIGTERM.
"""

import base64
import hashlib
import json
import os
import signal
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
PROFILE = Path(next(a.split("=", 1)[1] for a in sys.argv[1:] if a.startswith("--user-data-dir=")))
LOCK = PROFILE / "SingletonLock"
TOKEN_FILE = os.environ.get("FAKE_CHROME_TOKEN_FILE", "")
LOG = os.environ.get("FAKE_CHROME_LOG", "")
PORT = 0


def log(line: str) -> None:
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


def leave(*_args) -> None:
    try:
        LOCK.unlink()
    except OSError:
        pass
    os._exit(0)


def page_token() -> str:
    try:
        return Path(TOKEN_FILE).read_text("utf-8").strip()
    except OSError:
        return ""


def read_frame(stream) -> dict | None:
    head = stream.read(2)
    if len(head) < 2:
        return None
    opcode, length = head[0] & 0x0F, head[1] & 0x7F
    if length == 126:
        length = struct.unpack(">H", stream.read(2))[0]
    elif length == 127:
        length = struct.unpack(">Q", stream.read(8))[0]
    mask = stream.read(4) if head[1] & 0x80 else b"\0\0\0\0"
    data = bytes(b ^ mask[i % 4] for i, b in enumerate(stream.read(length)))
    if opcode == 0x8:
        return None
    return json.loads(data.decode("utf-8"))


def send_frame(stream, payload: dict) -> None:
    data = json.dumps(payload).encode()
    header = bytearray([0x81])
    if len(data) < 126:
        header.append(len(data))
    elif len(data) < 65536:
        header.append(126)
        header += struct.pack(">H", len(data))
    else:
        header.append(127)
        header += struct.pack(">Q", len(data))
    stream.write(bytes(header) + data)
    stream.flush()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _json(self, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            return self._websocket()
        if self.path == "/json":
            return self._json([
                {"type": "page", "url": "https://music.apple.com/us/new",
                 "webSocketDebuggerUrl": f"ws://127.0.0.1:{PORT}/devtools/page/P1"},
            ])
        if self.path == "/json/version":
            return self._json({"webSocketDebuggerUrl": f"ws://127.0.0.1:{PORT}/devtools/browser/B1"})
        self.send_error(404)

    def _websocket(self):
        key = self.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        self.wfile.write(
            ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
             f"Connection: Upgrade\r\nSec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        self.wfile.flush()
        while True:
            message = read_frame(self.rfile)
            if message is None:
                return
            method = message.get("method")
            log(f"{method} {self.path}")
            if method == "Runtime.evaluate":
                send_frame(self.wfile, {"id": message["id"],
                                        "result": {"result": {"type": "string", "value": page_token()}}})
            elif method == "Browser.close":
                send_frame(self.wfile, {"id": message["id"], "result": {}})
                if not os.environ.get("FAKE_CHROME_IGNORE_CLOSE"):
                    threading.Timer(0.05, leave).start()
            else:
                send_frame(self.wfile, {"id": message.get("id"), "result": {}})


def main() -> None:
    global PORT
    signal.signal(signal.SIGTERM, leave)
    time.sleep(float(os.environ.get("FAKE_CHROME_DELAY") or 0))
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    PORT = server.server_address[1]
    PROFILE.mkdir(parents=True, exist_ok=True)
    try:
        LOCK.unlink()
    except OSError:
        pass
    os.symlink(f"{socket.gethostname()}-{os.getpid()}", LOCK)
    (PROFILE / "DevToolsActivePort").write_text(f"{PORT}\n/devtools/browser/B1\n", "utf-8")
    log(f"started {PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
