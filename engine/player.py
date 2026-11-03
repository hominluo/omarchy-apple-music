"""The mpv process and its IPC socket.

mpv is the whole audio pipeline on purpose: one small process, real gapless
playback, and an MPRIS bridge that every Omarchy surface already knows how to
read (mpv-mpris is installed with Omarchy). The daemon owns the playlist and
hands mpv one absolute file at a time, so a track that is still being
downloaded cannot stall the one that is playing.
"""

import asyncio
import json
import logging
import os
import random
import shutil
import time

from . import config

log = logging.getLogger("apple-music.player")

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

    # -- lifecycle --------------------------------------------------------
    async def start(self):
        if self.proc and self.proc.returncode is None:
            return
        mpv = shutil.which("mpv")
        if not mpv:
            raise RuntimeError("mpv is not installed")
        self.socket_path = str(config.runtime_dir() / "mpv.sock")
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
            f"--input-ipc-server={self.socket_path}",
            f"--volume={self._volume}",
        ]
        script = mpris_script()
        if script:
            args.append(f"--script={script}")
        log.info("starting mpv on %s", self.socket_path)
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
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
                if self.state["idle"] or not self.writer:
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
