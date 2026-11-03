"""The mpv process and its IPC socket.

mpv is the whole audio pipeline on purpose: one small process, real gapless
playback, and an MPRIS bridge that every Omarchy surface already knows how to
read (mpv-mpris is installed with Omarchy). The daemon owns the playlist and
hands mpv one absolute file at a time, so a track that is still being
downloaded cannot stall the one that is playing.
"""

import asyncio
import ctypes
import json
import logging
import os
import random
import shutil
import signal
import time
from pathlib import Path

from . import config

log = logging.getLogger("apple-music.player")

PR_SET_PDEATHSIG = 1
# mpv is kept alive while a queue exists so the next track starts instantly.
# With nothing queued it is just 40 MB of idle process, so it is released.
IDLE_EXIT_SECONDS = 300

try:
    _LIBC = ctypes.CDLL("libc.so.6", use_errno=True)
except OSError:  # pragma: no cover - glibc is the target, musl names it libc.so
    _LIBC = None

MPRIS_PATHS = (
    "/usr/lib/mpv-mpris/mpris.so",
    "/usr/lib64/mpv-mpris/mpris.so",
    "/etc/mpv/scripts/mpris.so",
    os.path.expanduser("~/.config/mpv/scripts/mpris.so"),
)


def mpris_script() -> str | None:
    for path in MPRIS_PATHS:
        if os.path.exists(path):
            return path
    return None


def _die_with_parent() -> None:
    """Run in the child before exec: SIGTERM mpv when the daemon dies.

    Without this, a daemon that is killed (SIGTERM from a shell restart, or a
    crash) leaves its idle mpv behind, reparented to systemd --user, holding
    memory, an MPRIS player and the IPC socket for the next daemon to trip on.
    """
    try:
        if _LIBC is not None:
            _LIBC.prctl(PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)
    except (OSError, AttributeError):
        pass


def _stale_players(socket_path: str) -> list[int]:
    """PIDs of mpv processes pointed at our socket but not started by us."""
    needle = f"--input-ipc-server={socket_path}".encode()
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            cmdline = Path(f"/proc/{entry}/cmdline").read_bytes()
        except OSError:
            continue
        if needle in cmdline:
            pids.append(int(entry))
    return pids


def _reap_stale_players(socket_path: str) -> None:
    """A daemon restart used to leave the previous mpv running forever."""
    pids = _stale_players(socket_path)
    if not pids:
        return
    log.info("stopping %d orphaned mpv process(es)", len(pids))
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    for _ in range(20):
        if not _stale_players(socket_path):
            return
        time.sleep(0.05)
    for pid in _stale_players(socket_path):
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


class Player:
    def __init__(self, on_change=None, on_finished=None):
        self.on_change = on_change or (lambda *a, **k: None)
        self.on_finished = on_finished or (lambda *a, **k: None)
        self.proc = None
        self.socket_path = ""
        self.reader = None
        self.writer = None
        self.read_task = None
        self.poll_task = None
        self.request_id = 1
        self._pending = {}
        self.state = {
            "songId": None,
            "pos": 0.0,
            "duration": 0.0,
            "paused": True,
            "volume": 100,
            "idle": True,
            "loading": False,
            "buffering": False,
        }
        self._loaded = None
        self._had_media = False
        self._volume = 100
        self._starting = None
        self.idle_exit_seconds = IDLE_EXIT_SECONDS
        self._idle_since = None

    # -- lifecycle --------------------------------------------------------
    async def start(self):
        if self.proc and self.proc.returncode is None:
            return
        mpv = shutil.which("mpv")
        if not mpv:
            raise RuntimeError("mpv is not installed")
        self.socket_path = str(config.runtime_dir() / "mpv.sock")
        _reap_stale_players(self.socket_path)
        try:
            os.unlink(self.socket_path)
        except OSError:
            pass
        args = [
            mpv,
            "--idle=yes",
            "--no-video",
            "--force-window=no",
            "--no-terminal",
            "--really-quiet",
            "--audio-client-name=AppleMusic",
            # The daemon hands mpv local decrypted files, so there is nothing
            # to buffer: mpv's default 64 MiB demuxer cache is pure RSS here.
            "--cache=no",
            "--audio-display=no",
            f"--input-ipc-server={self.socket_path}",
            f"--volume={self._volume}",
        ]
        script = mpris_script()
        if script:
            args.append(f"--script={script}")
        log.info("starting mpv on %s", self.socket_path)
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=_die_with_parent,
        )
        for _ in range(200):
            if os.path.exists(self.socket_path):
                try:
                    self.reader, self.writer = await asyncio.open_unix_connection(self.socket_path)
                    break
                except OSError:
                    pass
            await asyncio.sleep(0.05)
        else:
            raise RuntimeError("mpv did not open its IPC socket")
        self.read_task = asyncio.create_task(self._read_loop())
        self.poll_task = asyncio.create_task(self._poll_loop())
        for prop in ("pause", "idle-active", "duration", "volume", "media-title"):
            await self._command(["observe_property", abs(hash(prop)) % 1000, prop])
        await self._command(["set_property", "pause", True])
        self.state["idle"] = True
        await self._emit()

    async def stop(self):
        for task in (self.poll_task, self.read_task):
            if task:
                task.cancel()
        self.poll_task = self.read_task = None
        if self.writer:
            try:
                self.writer.close()
            except OSError:
                pass
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), 3)
            except (asyncio.TimeoutError, ProcessLookupError):
                try:
                    self.proc.kill()
                except ProcessLookupError:
                    pass
        self.proc = None

    async def ensure(self):
        if not self.proc or self.proc.returncode is not None:
            await self.start()
        if not self.writer or self.writer.is_closing():
            await self.start()

    # -- ipc --------------------------------------------------------------
    async def _command(self, command: list, timeout: float = 5.0):
        if not self.writer:
            return None
        request_id = self.request_id
        self.request_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            self.writer.write((json.dumps({
                "command": command, "request_id": request_id,
            }) + "\n").encode())
            await self.writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            self._pending.pop(request_id, None)
            return None
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            return None

    async def _read_loop(self):
        while self.reader:
            try:
                line = await self.reader.readline()
            except (ConnectionResetError, asyncio.CancelledError):
                break
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "request_id" in message:
                future = self._pending.pop(message["request_id"], None)
                if future and not future.done():
                    future.set_result(message)
            elif message.get("event") == "property-change":
                await self._on_property(str(message.get("name")), message.get("data"))
            elif message.get("event") == "end-file":
                log.debug("mpv end-file: %s", message.get("reason"))

    async def _on_property(self, name: str, value):
        if name == "pause":
            self.state["paused"] = bool(value)
        elif name == "idle-active":
            idle = bool(value)
            if idle and not self.state["idle"] and self._had_media:
                self._had_media = False
                asyncio.create_task(self._finish())
            self.state["idle"] = idle
        elif name == "duration":
            self.state["duration"] = float(value or 0)
        elif name == "volume":
            self.state["volume"] = round(float(value or 0))
        elif name == "media-title":
            self.state["title"] = str(value or "")
        await self._emit()

    async def _poll_loop(self):
        while True:
            try:
                await asyncio.sleep(0.5)
                if self.state["idle"]:
                    # Nothing is queued. Give the idle process back after a
                    # while; ensure() starts a fresh mpv for the next track.
                    if self._idle_since is None:
                        self._idle_since = time.monotonic()
                    elif time.monotonic() - self._idle_since >= self.idle_exit_seconds:
                        await self._release()
                    continue
                self._idle_since = None
                if not self.writer:
                    continue
                response = await self._command(["get_property", "time-pos"], timeout=2)
                if response and response.get("error") == "success":
                    self.state["pos"] = max(0.0, float(response.get("data") or 0))
                paused = self.state["paused"]
                # A second tick while paused is wasted work; the UI still
                # gets one event per volume/seek change.
                if not paused:
                    await self._emit()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - polling must survive
                log.debug("poll failed: %s", error)

    async def _release(self):
        """Stop the idle mpv. `ensure` starts another one when it is needed."""
        if not self.proc or self.proc.returncode is not None:
            return
        self._idle_since = time.monotonic()
        log.info("mpv idle for %ds; releasing it", self.idle_exit_seconds)
        if self.read_task:
            self.read_task.cancel()
            self.read_task = None
        if self.writer:
            try:
                self.writer.close()
            except OSError:
                pass
        self.reader = self.writer = None
        proc, self.proc = self.proc, None
        try:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), 3)
        except (asyncio.TimeoutError, ProcessLookupError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    async def _finish(self):
        self.state["pos"] = 0
        self.state["paused"] = True
        await self._emit()
        result = self.on_finished()
        if asyncio.iscoroutine(result):
            await result

    async def _emit(self):
        result = self.on_change(self.snapshot())
        if asyncio.iscoroutine(result):
            await result

    # -- playback commands ------------------------------------------------
    def snapshot(self) -> dict:
        return dict(self.state)

    async def load(self, path: str, song_id: str, start: float = 0.0):
        await self.ensure()
        self._had_media = True
        self.state.update({
            "songId": song_id, "pos": start, "duration": 0.0,
            "paused": False, "idle": False, "loading": False,
        })
        await self._command(["loadfile", path, "replace"])
        await self._command(["set_property", "pause", False])
        if start and start > 1:
            await self.seek(start)
        await self._emit()

    async def set_paused(self, paused: bool):
        self.state["paused"] = bool(paused)
        await self._command(["set_property", "pause", bool(paused)])
        await self._emit()

    async def toggle(self):
        await self.set_paused(not self.state["paused"])

    async def seek(self, position: float):
        await self._command(["seek", max(0.0, float(position)), "absolute+exact"])
        self.state["pos"] = max(0.0, float(position))
        await self._emit()

    async def set_volume(self, value: int):
        self._volume = max(0, min(130, int(value)))
        self.state["volume"] = self._volume
        await self._command(["set_property", "volume", self._volume])
        await self._emit()

    async def stop(self):
        self._had_media = False
        self.state.update({"songId": None, "pos": 0.0, "duration": 0.0,
                           "paused": True, "idle": True})
        await self._command(["stop"])
        await self._emit()
