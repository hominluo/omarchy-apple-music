"""The mpv process and its IPC socket.

mpv is the whole audio pipeline on purpose: one small process, real gapless
playback, and an MPRIS bridge that every Omarchy surface already knows how to
read (mpv-mpris is installed with Omarchy). The daemon owns the playlist and
hands mpv one absolute file at a time, so a track that is still being
downloaded cannot stall the one that is playing.

Lifecycle rules, because every one of them was once a bug:
- One mpv at a time, started and stopped under one lock, so two plays or a
  play racing the idle release cannot each spawn or kill a process.
- Each process gets its own read loop and poll loop, and both end with it.
  A loop that outlived its mpv used to release the next one mid-start.
- A start that is cancelled (the daemon cancels a track's task on every
  next/jump) kills what it spawned instead of leaving mpv without a socket.
- An mpv that dies on its own is noticed: the state says stopped and the
  daemon is told, so play restarts it rather than talking to a dead socket.
"""

import asyncio
import ctypes
import json
import logging
import os
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
# mpv itself accepts up to 1000 (volume-max permitting) and refuses to start
# outside -1..1000, so the range is enforced here rather than trusted.
VOLUME_MAX = 100
# mpv's property replies are small, but a long media-title must not end the
# read loop the way an overrun of asyncio's 64 KiB default would.
READ_LIMIT = 1024 * 1024

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


class PlayerReset(RuntimeError):
    """mpv went away while a track was being handed to it (a crash, or the
    daemon ending it). Says nothing about the track, unlike a refusal."""


def clamp_volume(value, default: int = 100) -> int:
    """A volume the player can use, from whatever settings.json or a caller
    holds. Only a missing or non-numeric value falls back: 0 is a real
    volume (muted), not "unset"."""
    try:
        number = round(float(value))
    except (TypeError, ValueError, OverflowError):
        return max(0, min(VOLUME_MAX, int(default)))
    return max(0, min(VOLUME_MAX, number))


def _ok(response) -> bool:
    return bool(response) and response.get("error") == "success"


def _kill(proc) -> None:
    """Synchronous on purpose: this runs in cancellation and error paths,
    where an await could be interrupted and leave the process behind. The
    event loop's child watcher reaps it."""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
    except ProcessLookupError:
        pass


async def _stop_process(proc) -> None:
    """SIGTERM first so mpv-mpris can drop its bus name, SIGKILL if it hangs
    or if the wait itself is cancelled (daemon shutdown)."""
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), 3)
    except ProcessLookupError:
        pass
    except asyncio.TimeoutError:
        _kill(proc)
    except BaseException:
        _kill(proc)
        raise


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
    def __init__(self, on_change=None, on_finished=None, on_exit=None, on_error=None):
        self.on_change = on_change or (lambda *a, **k: None)
        self.on_finished = on_finished or (lambda *a, **k: None)
        # mpv died without being asked to: on_exit(returncode).
        self.on_exit = on_exit or (lambda *a, **k: None)
        # mpv could not open a file it was handed: on_error(message).
        self.on_error = on_error or (lambda *a, **k: None)
        self.proc = None
        self.socket_path = ""
        self.reader = None
        self.writer = None
        self.read_task = None
        self.poll_task = None
        self.request_id = 1
        self._pending = {}
        self.state = {
            # The song whose file mpv has (or last had) loaded. It is not the
            # queue position: while the next track is still being prepared,
            # this stays on the old one and "loading" is true.
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
        # (songId, position) of the track mpv died under, so play can pick it
        # up where it was rather than from the top. Cleared by any load.
        self._resume: tuple[str, float] | None = None
        # Where a file handed over with a start position begins, until mpv
        # first reports a position for it. The state's pos says only what
        # mpv has said; this is what a crash or a restart in that gap uses.
        self._starting_at: float | None = None
        # Bumped whenever the current process's socket is let go, so a
        # handover can tell "mpv refused the file" from "mpv went away".
        self._epoch = 0
        self._volume = 100
        # Every spawn, release and terminate holds this, so a play cannot race
        # the idle release and two plays cannot each spawn an mpv.
        self._lock = asyncio.Lock()
        # Bumped each time the daemon starts preparing a track, so a stale
        # build finishing (or failing) cannot clear the newer one's flag.
        self._loading_token = 0
        self._tasks: set[asyncio.Task] = set()
        self.idle_exit_seconds = IDLE_EXIT_SECONDS

    @property
    def running(self) -> bool:
        return bool(self.proc is not None and self.proc.returncode is None)

    def _connected(self) -> bool:
        return self.running and self.writer is not None and not self.writer.is_closing()

    def _spawn_task(self, coro) -> None:
        try:
            task = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:  # the loop is gone (shutdown): nothing to tell
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- lifecycle --------------------------------------------------------
    async def start(self):
        async with self._lock:
            if self._connected():
                return
            await self._spawn()

    async def ensure(self):
        if not self._connected():
            await self.start()

    async def _spawn(self):
        mpv = shutil.which("mpv")
        if not mpv:
            raise RuntimeError("mpv is not installed")
        # Whatever is left of an earlier process goes first: a dead one, or a
        # live one whose socket is gone, is no use and would hold the path.
        old, self.proc = self.proc, None
        self._detach()
        _kill(old)
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
            f"--volume={clamp_volume(self._volume)}",
        ]
        script = mpris_script()
        if script:
            args.append(f"--script={script}")
        log.info("starting mpv on %s", self.socket_path)
        # A cancel inside create_subprocess_exec is handled by asyncio, which
        # kills the child; from here on it is ours to clean up.
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            preexec_fn=_die_with_parent,
        )
        self.proc = proc
        try:
            self.reader, self.writer = await self._connect(proc)
            self.read_task = asyncio.create_task(self._read_loop(proc, self.reader))
            self.poll_task = asyncio.create_task(self._poll_loop(proc))
            for prop in ("pause", "idle-active", "duration", "volume", "media-title"):
                await self._command(["observe_property", abs(hash(prop)) % 1000, prop])
            await self._command(["set_property", "pause", True])
            if self.proc is not proc:
                raise RuntimeError("mpv exited while starting")
        except BaseException:
            # Cancelled (next/jump during a start) or failed: never leave a
            # process behind without a socket, or ensure() would think it is
            # running and every command would go nowhere.
            if self.proc is proc:
                self.proc = None
                self._detach()
            _kill(proc)
            raise
        self._had_media = False
        self.state["idle"] = True
        await self._emit()

    async def _connect(self, proc):
        for _ in range(200):
            if proc.returncode is not None:
                raise RuntimeError(f"mpv exited with status {proc.returncode} while starting")
            if os.path.exists(self.socket_path):
                try:
                    return await asyncio.open_unix_connection(self.socket_path, limit=READ_LIMIT)
                except OSError:
                    pass
            await asyncio.sleep(0.05)
        raise RuntimeError("mpv did not open its IPC socket")

    def _detach(self):
        """Forget the current process's socket and loops without awaiting, so
        it is safe in cancellation paths. The caller deals with the process."""
        self._epoch += 1
        current = asyncio.current_task()
        for task in (self.poll_task, self.read_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
        self.poll_task = self.read_task = None
        if self.writer is not None:
            try:
                self.writer.close()
            except (OSError, RuntimeError):
                pass
        self.reader = self.writer = None
        # Anyone waiting on a reply from this mpv gets "no answer" now rather
        # than after the command timeout.
        pending, self._pending = self._pending, {}
        for future in pending.values():
            if not future.done():
                future.set_result(None)

    async def terminate(self):
        """The lifecycle stop: end mpv (restartPlayer, quit, daemon shutdown).

        Not to be confused with `stop`, the playback stop further down, which
        keeps mpv running and only unloads the file."""
        async with self._lock:
            proc, self.proc = self.proc, None
            self._detach()
            self._had_media = False
            self._resume = None
            self._starting_at = None
            self.state.update({"pos": 0.0, "paused": True, "idle": True})
            await _stop_process(proc)
        await self._emit()

    async def _release(self, proc):
        """Stop the idle mpv. `ensure` starts another one when it is needed."""
        async with self._lock:
            # Re-checked under the lock: a play may have started meanwhile.
            if self.proc is not proc or not self.state["idle"] or self.state["loading"]:
                return
            log.info("mpv idle for %ds; releasing it", self.idle_exit_seconds)
            self.proc = None
            self._detach()
            await _stop_process(proc)
        await self._emit()

    async def _on_exit(self, proc):
        """mpv's socket closed without us closing it: it crashed or was killed."""
        try:
            await asyncio.wait_for(proc.wait(), 2)
        except asyncio.TimeoutError:
            # The socket is gone but the process lingers; it cannot be talked
            # to any more, so it is no use.
            _kill(proc)
        if self.proc is not proc:
            return
        returncode = proc.returncode
        # Only a track that was loaded was interrupted. An idle mpv dying
        # (an OOM kill, a stray `pkill mpv`) cost nothing: the next play
        # starts another, so it is no error to show the user.
        interrupted = self._had_media
        if interrupted:
            log.warning("mpv exited unexpectedly (status %s)", returncode)
            song_id = self.state.get("songId")
            self._resume = (str(song_id), self.position()) if song_id else None
        else:
            log.info("idle mpv exited (status %s); the next play starts another", returncode)
        self.proc = None
        self._detach()
        self._had_media = False
        self._starting_at = None
        # songId stays, so the bar still shows what was playing; play starts
        # it again through a fresh mpv, from resume_point().
        self.state.update({"pos": 0.0, "paused": True, "idle": True})
        await self._emit()
        if not interrupted:
            return
        result = self.on_exit(returncode)
        if asyncio.iscoroutine(result):
            await result

    # -- ipc --------------------------------------------------------------
    async def _command(self, command: list, timeout: float = 5.0):
        writer = self.writer
        if writer is None or writer.is_closing():
            return None
        request_id = self.request_id
        self.request_id += 1
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            writer.write((json.dumps({
                "command": command, "request_id": request_id,
            }) + "\n").encode())
            await writer.drain()
        except (OSError, RuntimeError):
            self._pending.pop(request_id, None)
            return None
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            return None

    async def _read_loop(self, proc, reader):
        try:
            while True:
                line = await reader.readline()
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
                    if message.get("reason") == "error":
                        await self._on_file_error(message.get("file_error"))
        except asyncio.CancelledError:
            raise
        except (OSError, ValueError) as error:  # reset, or a line past the limit
            log.debug("mpv socket read failed: %s", error)
        # Our own release/terminate cancels this task before closing the
        # socket; reaching here for the current process means mpv went away.
        if self.proc is proc:
            await self._on_exit(proc)

    async def _on_property(self, name: str, value):
        if name == "pause":
            self.state["paused"] = bool(value)
        elif name == "idle-active":
            idle = bool(value)
            if idle and not self.state["idle"] and self._had_media:
                self._had_media = False
                self._spawn_task(self._finish())
            self.state["idle"] = idle
        elif name == "duration":
            self.state["duration"] = float(value or 0)
        elif name == "volume":
            self.state["volume"] = round(float(value or 0))
        elif name == "media-title":
            self.state["title"] = str(value or "")
        await self._emit()

    async def _on_file_error(self, detail):
        """mpv could not open the file it was handed. It stays idle without
        an idle-active change, so without this the state would say playing."""
        if not self._had_media:
            return
        self._had_media = False
        self._starting_at = None
        self.state.update({"pos": 0.0, "paused": True, "idle": True})
        await self._emit()
        result = self.on_error(f"mpv could not play the track ({detail or 'unknown error'})")
        if asyncio.iscoroutine(result):
            await result

    async def _poll_loop(self, proc):
        idle_since = None
        while self.proc is proc:
            try:
                await asyncio.sleep(0.5)
                if self.proc is not proc:
                    return
                if self.state["idle"]:
                    # Nothing is loaded. Give the idle process back after a
                    # while; ensure() starts a fresh mpv for the next track.
                    # Not while a track is being prepared: it is about to be
                    # handed one.
                    if self.state["loading"]:
                        idle_since = None
                    elif idle_since is None:
                        idle_since = time.monotonic()
                    elif time.monotonic() - idle_since >= self.idle_exit_seconds:
                        idle_since = None
                        await self._release(proc)
                    continue
                idle_since = None
                if not self._connected():
                    continue
                response = await self._command(["get_property", "time-pos"], timeout=2)
                if _ok(response):
                    self.state["pos"] = max(0.0, float(response.get("data") or 0))
                    self._starting_at = None
                # A second tick while paused is wasted work; the UI still
                # gets one event per volume/seek change.
                if not self.state["paused"]:
                    await self._emit()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - polling must survive
                log.debug("poll failed: %s", error)

    async def _finish(self):
        self._starting_at = None
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

    # -- preparing --------------------------------------------------------
    def begin_loading(self) -> int:
        """The daemon moved to a new track and is fetching its file. Until
        load() hands it to mpv, "loading" is true and songId still names
        whatever mpv has. Returns the token end_loading() needs.

        Synchronous so the daemon can cancel the old build and start the new
        one without an await in between for another command to slip into."""
        self._loading_token += 1
        self.state["loading"] = True
        self._spawn_task(self._emit())
        return self._loading_token

    def end_loading(self, token: int) -> None:
        """The preparation behind `token` ended without load() (it failed or
        was cancelled). A newer preparation keeps its flag."""
        if token != self._loading_token or not self.state["loading"]:
            return
        self.state["loading"] = False
        self._spawn_task(self._emit())

    # -- playback commands ------------------------------------------------
    def snapshot(self) -> dict:
        return dict(self.state)

    def position(self) -> float:
        """Where the loaded file is: mpv's last reported position, or the
        start it was handed with while mpv has not reported one yet."""
        if self._starting_at is not None:
            return self._starting_at
        return float(self.state.get("pos") or 0)

    def resume_point(self, song_id: str) -> float:
        """Where `song_id` was when mpv died under it; 0 for any other song,
        or when nothing died."""
        if self._resume and self._resume[0] == str(song_id):
            return self._resume[1]
        return 0.0

    async def load(self, path: str, song_id: str, start: float = 0.0):
        if not os.path.isfile(path):
            raise RuntimeError("The audio file is missing; play the track again")
        token = self._loading_token
        await self.ensure()
        # Once loadfile is on the wire, mpv plays this file whether or not the
        # caller is still waiting, so the bookkeeping must finish even if the
        # caller is cancelled; otherwise songId would name the wrong file.
        handover = asyncio.ensure_future(self._hand_over(path, str(song_id), start, token))
        # If the caller was cancelled nobody awaits the result; reading the
        # exception here keeps asyncio from logging it as never retrieved.
        handover.add_done_callback(lambda task: task.cancelled() or task.exception())
        await asyncio.shield(handover)

    async def _hand_over(self, path: str, song_id: str, start: float, token: int):
        epoch = self._epoch
        start = max(0.0, float(start or 0))
        # Unpause first and write the state before loadfile: what mpv says
        # next (the file ended at once, or could not be opened) may arrive
        # before the loadfile reply is read, and must not be overwritten.
        # pos is not set to the start: mpv says where it is once it plays.
        await self._command(["set_property", "pause", False])
        previous = self.state["songId"]
        self._had_media = True
        self._starting_at = None
        self.state.update({
            "songId": song_id, "pos": 0.0, "duration": 0.0,
            "paused": False, "idle": False,
        })
        response = await self._command(self._loadfile(path, start, legacy=False))
        if (not _ok(response) and start > 1 and self._epoch == epoch
                and self._connected()):
            # An mpv older than 0.38 has no index argument and reads the -1
            # as the options; it takes them one place earlier.
            response = await self._command(self._loadfile(path, start, legacy=True))
        if not _ok(response):
            self._had_media = False
            self.state.update({"songId": previous, "paused": True, "idle": True})
            if self._epoch != epoch or not self._connected():
                raise PlayerReset("mpv went away while the track was being handed over")
            raise RuntimeError("mpv did not accept the track")
        self._loaded = (path, song_id)
        self._resume = None
        if start > 1 and self._had_media:
            self._starting_at = start
        if token == self._loading_token:
            self.state["loading"] = False
        await self._emit()

    @staticmethod
    def _loadfile(path: str, start: float, legacy: bool) -> list:
        """The loadfile for `path`, starting `start` seconds in. The start
        goes in the load itself: mpv refuses a seek until the file has
        opened, which is after loadfile has already answered, so the seek
        that used to follow it failed and the track played from 0:00."""
        command = ["loadfile", path, "replace"]
        if start > 1:
            option = f"start={start:.3f}"
            command += [option] if legacy else [-1, option]
        return command

    async def replay(self, song_id: str) -> bool:
        """Play the file loaded last again, from the start. Repeat-one needs
        this: by the time mpv reports the end, it has already unloaded the
        file (--idle=yes without keep-open), so there is nothing to seek in."""
        if not self._loaded or self._loaded[1] != str(song_id):
            return False
        path = self._loaded[0]
        if not os.path.isfile(path):  # pruned from the cache meanwhile
            return False
        try:
            await self.load(path, str(song_id))
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the caller falls back to a rebuild
            log.warning("replay failed: %s", error)
            return False
        return True

    async def set_paused(self, paused: bool):
        # The state follows mpv's answer: writing it regardless is how a dead
        # or idle mpv used to show "playing" with nothing playing.
        response = await self._command(["set_property", "pause", bool(paused)])
        if _ok(response):
            self.state["paused"] = bool(paused)
        await self._emit()

    async def toggle(self):
        await self.set_paused(not self.state["paused"])

    async def seek(self, position: float) -> bool:
        position = max(0.0, float(position))
        response = await self._command(["seek", position, "absolute+exact"])
        if not _ok(response):  # idle mpv, or no mpv: nothing to seek in
            return False
        self._starting_at = None
        self.state["pos"] = position
        await self._emit()
        return True

    async def set_volume(self, value) -> int:
        self._volume = clamp_volume(value, self._volume)
        self.state["volume"] = self._volume
        await self._command(["set_property", "volume", self._volume])
        await self._emit()
        return self._volume

    async def stop(self):
        """The playback stop: unload the file, keep mpv (see `terminate`).

        songId is kept on purpose: the queue position does not move, and a
        cleared songId would read as "still preparing" to the UI."""
        self._had_media = False
        self._resume = None
        self._starting_at = None
        self._loading_token += 1
        self.state.update({"pos": 0.0, "duration": 0.0, "paused": True,
                           "idle": True, "loading": False})
        await self._command(["stop"])
        await self._emit()
