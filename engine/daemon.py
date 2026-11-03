"""The daemon: one socket, one queue, one mpv.

Everything the shell can do to the player arrives here as a newline-delimited
JSON request. State changes leave as events, so a bar widget, the browser
window and any future surface all read the same snapshot rather than polling.

Queue semantics are deliberately boring: the queue is the playback order.
Shuffling rewrites the part of the queue that has not played yet, so
"previous" is always the track that just played.
"""

import asyncio
import fcntl
import json
import logging
import os
import random
import time
from pathlib import Path

from . import __version__, auth, config
from .apple import ApiError, AppleApi, storefront_from_account, subscription_end
from .player import Player
from .stream import StreamBuilder

log = logging.getLogger("apple-music.daemon")

PROTOCOL = 1
ENGINE_VERSION = __version__
# How many tracks may fail in a row before automatic advance gives up. Without
# this, a broken album (region lock, delisted tracks) would walk the whole queue.
FAILURE_LIMIT = 3


def _account_name(account: dict | None) -> str:
    for record in ((account or {}).get("data") or []):
        attributes = record.get("attributes") or {}
        name = attributes.get("displayName") or attributes.get("firstName")
        if name:
            return str(name)
    return ""


def _subscription_state(account: dict | None) -> str:
    """/v1/me/account answers with a boolean ``active``; older shapes use
    a numeric ``subscriptionStatus``, so both are understood."""
    try:
        subscription = account["meta"]["subscription"]
    except (KeyError, TypeError):
        return "unknown"
    if "active" in subscription:
        return "subscribed" if subscription["active"] else "expired"
    status = subscription.get("subscriptionStatus")
    if status is None:
        return "unknown"
    return {"1": "subscribed", "2": "subscribed", "3": "expired"}.get(str(status), str(status))


class Daemon:
    def __init__(self):
        self.settings = config.load_settings()
        self.account_record = auth.load()
        self.api: AppleApi | None = None
        self.builder: StreamBuilder | None = None
        self.player = Player(on_change=self._player_changed, on_finished=self._track_finished)
        self.queue: list[dict] = []
        self.index = -1
        self.context: dict = {}
        self.shuffle = False
        self.repeat = "off"
        self.volume = int(self.settings.get("volume") or 100)
        self.clients: set[asyncio.StreamWriter] = set()
        self.tasks: set[asyncio.Task] = set()
        self.build_state: dict = {}
        self._build_task: asyncio.Task | None = None
        self._prefetch_task: asyncio.Task | None = None
        self._auth_task: asyncio.Task | None = None
        self._advancing = False
        self._failures = 0
        self._lock = None
        self.ready = False

    # -- wiring -----------------------------------------------------------
    async def startup(self):
        self.player._volume = self.volume
        await self.player.start()
        await self._refresh_api()
        await self.player.set_volume(self.volume)
        self.ready = True

    async def _refresh_api(self) -> bool:
        token = self.account_record.get("mediaUserToken") or ""
        storefront = self.account_record.get("storefront") or self.settings.get("storefront") or "us"
        if not token:
            found = await asyncio.to_thread(auth.discover)
            if found:
                profile, token = found
                log.info("found a session in %s", profile)
                self.account_record = auth.save(token, source=str(profile), storefront=storefront)
        self.api = AppleApi(
            media_user_token=token,
            storefront=storefront,
            language=self.settings.get("language") or "en-US",
        )
        self.builder = StreamBuilder(self.api, notify=self._build_notice)
        if not token:
            return False
        try:
            account = await self.api.fetch_account()
            self.account_record = auth.save(
                token, source=self.account_record.get("source") or "stored",
                storefront=self.api.storefront, account=account,
            )
            return True
        except ApiError as error:
            log.warning("account check failed: %s", error.message)
            if error.status == 403:
                # The token is dead: say so instead of failing every request.
                self.account_record = dict(self.account_record, error=error.message)
            return False

    # -- events -----------------------------------------------------------
    async def broadcast(self, event: str, data) -> None:
        if not self.clients:
            return
        line = (json.dumps({"event": event, "data": data}, ensure_ascii=False) + "\n").encode()
        dead = []
        for writer in list(self.clients):
            try:
                writer.write(line)
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError, RuntimeError):
                dead.append(writer)
        for writer in dead:
            self.clients.discard(writer)

    def auth_state(self) -> dict:
        account = self.account_record.get("account")
        return {
            "signedIn": bool(self.account_record.get("mediaUserToken")) and not self.account_record.get("error"),
            "error": self.account_record.get("error") or "",
            "name": _account_name(account),
            "storefront": (self.account_record.get("storefront")
                           or storefront_from_account(account, self.settings.get("storefront") or "us")),
            "status": _subscription_state(account),
            "endsAt": subscription_end(account),
            "source": self.account_record.get("source") or "",
        }

    def queue_state(self) -> dict:
        return {
            "index": self.index,
            "context": self.context,
            "shuffle": self.shuffle,
            "repeat": self.repeat,
            "items": self.queue,
        }

    def now_playing(self) -> dict | None:
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]
        return None

    def snapshot(self) -> dict:
        return {
            "player": self.player.snapshot(),
            "nowPlaying": self.now_playing(),
            "queue": self.queue_state(),
            "auth": self.auth_state(),
            "build": self.build_state,
            "settings": self.settings,
            "engine": {"version": ENGINE_VERSION, "protocol": PROTOCOL, "mpv": "running" if self.player.proc else "stopped"},
        }

    async def _emit_state(self, *slices: str) -> None:
        snapshot = self.snapshot()
        for name in slices:
            await self.broadcast(name, snapshot[name])

    async def _player_changed(self, state: dict) -> None:
        await self.broadcast("player", state)

    async def _build_notice(self, event: str, payload: dict) -> None:
        if event == "build":
            song_id = payload.get("songId")
            stage = payload.get("stage")
            if stage == "ready":
                self.build_state.pop(song_id, None)
            else:
                self.build_state[song_id] = payload
            await self.broadcast("build", {"songId": song_id, **payload})

    # -- queue ------------------------------------------------------------
    async def _set_queue(self, tracks: list[dict], index: int, context: dict) -> None:
        self.queue = [t for t in tracks if t.get("id")]
        self.context = context or {}
        self.index = max(0, min(index, len(self.queue) - 1)) if self.queue else -1
        if self.shuffle and self.queue:
            self._shuffle_from(self.index)
        await self._emit_state("queue", "nowPlaying")
        await self._play_current()

    def _shuffle_from(self, index: int) -> None:
        head = self.queue[: index + 1]
        tail = self.queue[index + 1:]
        random.shuffle(tail)
        self.queue = head + tail
        self.index = index

    def _ordered(self, start: int, step: int):
        order = range(start, len(self.queue)) if step > 0 else range(start, -1, -1)
        return order

    def _step(self, direction: int, automatic: bool = False) -> int | None:
        if not self.queue:
            return None
        if automatic and self.repeat == "one":
            return self.index
        target = self.index + direction
        if 0 <= target < len(self.queue):
            return target
        if self.repeat == "all":
            return 0 if direction > 0 else len(self.queue) - 1
        return None

    async def _play_current(self) -> None:
        if self._build_task:
            self._build_task.cancel()
        song = self.now_playing()
        if not song:
            await self.player.stop()
            await self._emit_state("nowPlaying")
            return
        self._build_task = asyncio.create_task(self._start_track(song))
        self.tasks.add(self._build_task)
        self._build_task.add_done_callback(self.tasks.discard)

    async def _start_track(self, song: dict) -> None:
        song_id = str(song["id"])
        if not self.api or not self.api.media_user_token:
            self.build_state.pop(song_id, None)
            await self.broadcast("notice", {
                "level": "error", "message": "Sign in to Apple Music to play this track",
            })
            await self.broadcast("build", {"songId": song_id, "stage": "failed",
                                           "message": "Not signed in"})
            return
        self.build_state[song_id] = {"songId": song_id, "stage": "queued"}
        await self.broadcast("build", self.build_state[song_id])
        try:
            path = await self.builder.build(song, codec=str(self.settings.get("codec") or "aac-web"))
            await self.player.load(str(path), song_id)
            self._failures = 0
            await self._emit_state("nowPlaying")
            self._schedule_prefetch()
        except asyncio.CancelledError:
            raise
        except ApiError as error:
            log.warning("could not play %s: %s", song_id, error.message)
            self.build_state.pop(song_id, None)
            await self.broadcast("build", {"songId": song_id, "stage": "failed",
                                           "message": error.message})
            await self.broadcast("notice", {"level": "error", "message": error.message})
            if self._advancing:
                return
            self._failures += 1
            if self._failures >= FAILURE_LIMIT:
                self._failures = 0
                await self.player.stop()
                await self._emit_state("player", "nowPlaying")
                await self.broadcast("notice", {
                    "level": "error",
                    "message": f"{FAILURE_LIMIT} tracks in a row would not play; stopped",
                })
                return
            self._advancing = True
            try:
                await self._advance(1, automatic=True)
            finally:
                self._advancing = False
        except Exception as error:  # noqa: BLE001
            log.exception("playback error for %s", song_id)
            await self.broadcast("notice", {"level": "error", "message": str(error)})

    def _schedule_prefetch(self) -> None:
        if self._prefetch_task:
            self._prefetch_task.cancel()
        self._prefetch_task = asyncio.create_task(self._prefetch())
        self.tasks.add(self._prefetch_task)
        self._prefetch_task.add_done_callback(self.tasks.discard)

    async def _prefetch(self) -> None:
        depth = int(self.settings.get("prefetch") or 2)
        if depth <= 0 or not self.builder:
            return
        for offset in range(1, depth + 1):
            position = self.index + offset
            if position >= len(self.queue):
                break
            song = self.queue[position]
            try:
                await self.builder.build(song, codec=str(self.settings.get("codec") or "aac-web"))
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - prefetch is best effort
                log.debug("prefetch %s failed: %s", song.get("id"), error)

    async def _advance(self, direction: int, automatic: bool = False) -> None:
        target = self._step(direction, automatic)
        if target is None:
            await self.player.stop()
            await self._emit_state("player", "nowPlaying")
            return
        if automatic and self.repeat == "one" and target == self.index:
            await self.player.seek(0)
            await self.player.set_paused(False)
            return
        self.index = target
        await self._emit_state("queue", "nowPlaying")
        await self._play_current()

    async def _track_finished(self) -> None:
        if self._advancing:
            return
        self._advancing = True
        try:
            await self._advance(1, automatic=True)
        finally:
            self._advancing = False

    # -- commands ---------------------------------------------------------
    async def cmd_status(self, params: dict) -> dict:
        return self.snapshot()

    async def cmd_signin(self, params: dict) -> dict:
        result = await asyncio.to_thread(auth.launch_login, self.settings.get("storefront") or "us")
        if self._auth_task and not self._auth_task.done():
            return {**result, "waiting": True}
        self._auth_task = asyncio.create_task(self._await_login())
        return {**result, "waiting": True}

    async def _await_login(self) -> None:
        token = await asyncio.to_thread(auth.wait_for_token, 600.0)
        if not token:
            await self.broadcast("notice", {"level": "error",
                                            "message": "Sign-in window closed without a session"})
            return
        self.account_record = auth.save(token, source=str(config.login_profile()),
                                        storefront=self.settings.get("storefront") or "us")
        ok = await self._refresh_api()
        await self._emit_state("auth")
        await self.broadcast("notice", {
            "level": "success" if ok else "error",
            "message": ("Signed in to Apple Music" if ok else
                        "Found a session but Apple rejected it (check the subscription)"),
        })

    async def cmd_signout(self, params: dict) -> dict:
        auth.clear()
        self.account_record = dict(auth.DEFAULT_ACCOUNT)
        if self.api:
            self.api.media_user_token = ""
        await self._emit_state("auth")
        return self.auth_state()

    async def cmd_account(self, params: dict) -> dict:
        if self.api and self.api.media_user_token:
            try:
                account = await self.api.fetch_account()
                self.account_record = auth.save(
                    self.api.media_user_token,
                    source=self.account_record.get("source") or "stored",
                    storefront=self.api.storefront, account=account)
                await self._emit_state("auth")
            except ApiError as error:
                raise
        return self.auth_state()

    async def _require_api(self) -> AppleApi:
        if not self.api or not self.api.media_user_token:
            raise ApiError("Sign in to Apple Music first", code="unauthorized")
        return self.api

    async def cmd_home(self, params: dict) -> dict:
        api = self.api or await self._require_api()
        return await api.home()

    async def cmd_search(self, params: dict) -> dict:
        api = self.api or await self._require_api()
        return await api.search(str(params.get("term") or ""), limit=int(params.get("limit") or 20))

    async def cmd_album(self, params: dict) -> dict:
        api = self.api or await self._require_api()
        return await api.album(str(params.get("id")), bool(params.get("library")))

    async def cmd_playlist(self, params: dict) -> dict:
        api = self.api or await self._require_api()
        return await api.playlist(str(params.get("id")), bool(params.get("library")))

    async def cmd_artist(self, params: dict) -> dict:
        api = self.api or await self._require_api()
        return await api.artist(str(params.get("id")), bool(params.get("library")))

    async def cmd_library(self, params: dict) -> dict:
        api = self.api or await self._require_api()
        return await api.library(str(params.get("kind") or "songs"),
                                 int(params.get("limit") or 100),
                                 int(params.get("offset") or 0))

    async def cmd_play(self, params: dict) -> dict:
        tracks = params.get("tracks") or []
        index = int(params.get("index") or 0)
        context = params.get("context") or {}
        if not tracks and params.get("id"):
            api = self.api or await self._require_api()
            tracks = [await api.song(str(params["id"]), bool(params.get("library")))]
            index = 0
        if not tracks:
            raise ApiError("Nothing to play", code="bad-request")
        await self._set_queue(tracks, index, context)
        return {"index": self.index, "count": len(self.queue)}

    async def cmd_toggle(self, params: dict) -> dict:
        if self.player.state["idle"] and self.queue:
            await self._play_current()
        else:
            await self.player.toggle()
        return self.player.snapshot()

    async def cmd_pause(self, params: dict) -> dict:
        await self.player.set_paused(True)
        return self.player.snapshot()

    async def cmd_resume(self, params: dict) -> dict:
        await self.player.set_paused(False)
        return self.player.snapshot()

    async def cmd_next(self, params: dict) -> dict:
        await self._advance(1)
        return {"index": self.index}

    async def cmd_previous(self, params: dict) -> dict:
        # A "previous" press mid-track restarts it, like every other player.
        if self.player.state["pos"] > 3:
            await self.player.seek(0)
            await self.player.set_paused(False)
        else:
            await self._advance(-1)
        return {"index": self.index}

    async def cmd_seek(self, params: dict) -> dict:
        await self.player.seek(float(params.get("pos") or 0))
        return self.player.snapshot()

    async def cmd_jump(self, params: dict) -> dict:
        if not self.queue:
            raise ApiError("Nothing is queued", code="bad-request")
        index = int(params.get("index") if params.get("index") is not None else 0)
        self.index = max(0, min(index, len(self.queue) - 1))
        await self._emit_state("queue", "nowPlaying")
        await self._play_current()
        return {"index": self.index}

    async def cmd_volume(self, params: dict) -> dict:
        value = int(params.get("value") if params.get("value") is not None else self.volume)
        await self.player.set_volume(value)
        self.volume = value
        self.settings = config.save_settings({"volume": value})
        return {"volume": value}

    async def cmd_shuffle(self, params: dict) -> dict:
        self.shuffle = bool(params.get("on")) if params.get("on") is not None else not self.shuffle
        if self.shuffle:
            self._shuffle_from(self.index)
        else:
            # Restoring the original order would need a second list; keeping
            # the played order is what every other player does too.
            pass
        await self._emit_state("queue")
        return {"shuffle": self.shuffle}

    async def cmd_repeat(self, params: dict) -> dict:
        mode = str(params.get("mode") or "")
        if mode not in ("off", "all", "one"):
            mode = {"off": "all", "all": "one", "one": "off"}.get(self.repeat, "off")
        self.repeat = mode
        await self._emit_state("queue")
        return {"repeat": self.repeat}

    async def cmd_cache(self, params: dict) -> dict:
        return self.builder.cache_stats() if self.builder else {}

    async def cmd_clearCache(self, params: dict) -> dict:
        return self.builder.clear_cache() if self.builder else {"removed": 0}

    async def cmd_settings(self, params: dict) -> dict:
        changes = params.get("changes") or {}
        if changes:
            self.settings = config.save_settings(changes)
        return self.settings

    async def cmd_restartPlayer(self, params: dict) -> dict:
        await self.player.stop()
        await self.player.start()
        song = self.now_playing()
        if song:
            await self._play_current()
        return self.player.snapshot()

    # -- socket -----------------------------------------------------------
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        self.clients.add(writer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                await self._dispatch(message, writer)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.clients.discard(writer)
            try:
                writer.close()
            except OSError:
                pass

    async def _dispatch(self, message: dict, writer: asyncio.StreamWriter) -> None:
        request_id = message.get("id")
        command = str(message.get("cmd") or "")
        params = message.get("params") or {}
        handler = getattr(self, f"cmd_{command}", None)
        if command == "hello":
            snapshot = self.snapshot()
            response = {"id": request_id, "ok": True,
                        "result": {"engineVersion": ENGINE_VERSION, "protocol": PROTOCOL,
                                   "snapshot": snapshot}}
            self._write(writer, response)
            return
        if not handler:
            self._write(writer, {"id": request_id, "ok": False,
                                 "error": {"code": "bad-request", "message": f"Unknown command: {command}"}})
            return
        try:
            result = await handler(params)
            self._write(writer, {"id": request_id, "ok": True, "result": result})
        except ApiError as error:
            self._write(writer, {"id": request_id, "ok": False,
                                 "error": {"code": error.code, "message": error.message}})
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001
            log.exception("command %s failed", command)
            self._write(writer, {"id": request_id, "ok": False,
                                 "error": {"code": "internal", "message": str(error)}})

    def _write(self, writer: asyncio.StreamWriter, payload: dict) -> None:
        try:
            writer.write((json.dumps(payload, ensure_ascii=False) + "\n").encode())
        except (ConnectionResetError, BrokenPipeError):
            pass

    async def serve(self, stop: asyncio.Event | None = None) -> None:
        path = config.socket_path()
        lock = self._take_lock(path)
        if lock is None:
            log.info("another daemon already owns %s", path)
            return
        self._lock = lock
        # Only the socket goes: the lock file is the inode another daemon's
        # flock is held on, so unlinking it would hand out a second lock.
        try:
            path.unlink()
        except OSError:
            pass
        pid_path = path.parent / "daemon.pid"
        pid_path.write_text(f"{os.getpid()}\n", "utf-8")
        await self.startup()
        server = await asyncio.start_unix_server(self.handle, path=str(path))
        os.chmod(path, 0o600)
        log.info("listening on %s", path)
        await self.broadcast("snapshot", self.snapshot())
        async with server:
            if stop is None:
                await server.serve_forever()
                return
            serving = asyncio.create_task(server.serve_forever())
            stopping = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (serving, stopping):
                    task.cancel()

    def _take_lock(self, path: Path):
        """One daemon per session: the shell can respawn while the old one is
        still closing, and two daemons mean two mpv processes and two MPRIS
        players both fighting over one socket path."""
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = Path(str(path) + ".lock")
        handle = open(lock_path, "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        return handle

    async def shutdown(self) -> None:
        for task in list(self.tasks):
            task.cancel()
        if self._auth_task:
            self._auth_task.cancel()
        await self.player.stop()
