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
import threading
import time
from pathlib import Path

from . import __version__, auth, config
from .apple import ApiError, AppleApi, storefront_from_account, subscription_end
from .player import Player, PlayerReset, clamp_volume
from .stream import StreamBuilder, sweep_work_dir

log = logging.getLogger("apple-music.daemon")

# Bumped whenever a command or a slice changes shape. The Service compares it
# (and the engine version) on hello and replaces a daemon left over from an
# older install, which would otherwise outlive any number of plugin updates.
PROTOCOL = 2
ENGINE_VERSION = __version__
# How many tracks may fail in a row before automatic advance gives up. Without
# this, a broken album (region lock, delisted tracks) would walk the whole queue.
FAILURE_LIMIT = 3
# How long a command that needs the API waits for the account check that runs
# after the socket opens. One request gives up after 30 s and a timeout is not
# retried, but a 5xx or a refused connection is (with backoff), and a 401
# re-scrapes the developer token first, so the check can outlast this. Whoever
# stops waiting does not act on top of it: the check's result is dropped once
# a sign-in or sign-out has happened (_session_gen).
STARTUP_WAIT = 60.0
# How often shell.json is looked at (a stat; it is read again only when it
# changed). The daemon runs only while the plugin is enabled there: disabled
# while a launcher was still starting, or with the Service gone before it
# could send quit, it ends itself within this long.
ENABLED_POLL_SECONDS = 5.0

# Commands that only read: each runs as its own task, so a slow Apple request
# never holds up a pause, and a reply nobody can receive any more is dropped
# when its connection closes.
READ_COMMANDS = frozenset({"status", "home", "search", "album", "playlist", "artist",
                           "library", "account", "cache"})
# Commands that take no turn: quit must not queue behind a hung command,
# sign-in waits on a browser launch and sign-out on the window closing.
FREE_COMMANDS = frozenset({"quit", "signin", "signOut", "signout"})
# How long a confirmed-good session goes unprobed after a stray refusal: a
# licence or one endpoint can refuse for its own reasons, and every request
# in flight would otherwise probe the account at once.
VERIFY_INTERVAL = 30.0
# play may look a song up first; it takes the command lock itself, after that.
SELF_ORDERED = frozenset({"play"})
# Build failures that say nothing about the track itself. A prefetch that hits
# one leaves the row alone instead of marking it unavailable.
TRANSIENT_CODES = frozenset({"network", "unauthorized", "starting"})


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
        # Token-less until the account check (_refresh_api) swaps in the
        # session's. The builder is made once, here, and follows the api:
        # it owns the cache, and clearing it or reading its size must not
        # depend on a session check that a sign-out may have cancelled.
        self.api: AppleApi | None = AppleApi(
            storefront=self.account_record.get("storefront") or self.settings.get("storefront") or "us",
            language=self.settings.get("language") or "en-US",
        )
        self.builder: StreamBuilder | None = StreamBuilder(
            self.api, notify=self._build_notice, in_use=self._songs_in_use)
        self.player = Player(on_change=self._player_changed, on_finished=self._track_finished,
                             on_exit=self._player_exited, on_error=self._player_failed)
        self.queue: list[dict] = []
        self.index = -1
        self.context: dict = {}
        self.shuffle = False
        self.repeat = "off"
        # A saved 0 is a muted player and stays 0; anything out of range is
        # pulled back, because mpv refuses to start with --volume outside
        # -1..1000 and the daemon would never come up.
        self.volume = clamp_volume(self.settings.get("volume"), 100)
        self._mpv_status = "stopped"
        self.clients: set[asyncio.StreamWriter] = set()
        self.tasks: set[asyncio.Task] = set()
        # {songId: {songId, stage, message?}}; the whole map goes out on every
        # change, the same shape as the snapshot slice.
        self.build_state: dict = {}
        # How many tasks are building each song right now: a cancelled one
        # clears the song's entry only if nobody else is still on it.
        self._build_refs: dict[str, int] = {}
        self._build_task: asyncio.Task | None = None
        # The song _build_task is preparing, while it has not reached mpv:
        # a play of that same song keeps the download instead of starting over.
        self._build_song: str | None = None
        self._prefetch_task: asyncio.Task | None = None
        self._prefetching: str | None = None
        # Bumped whenever the current track changes; a prefetch started for
        # an older position stops after the song it is on.
        self._generation = 0
        # A pause/play press while the current track is still being built.
        self._pending_pause = False
        self._command_lock = asyncio.Lock()
        self._session_task: asyncio.Task | None = None
        # Bumped by every sign-in and sign-out. An account check started
        # before one drops its result instead of saving over it.
        self._session_gen = 0
        self._stop: asyncio.Event | None = None
        self._shut_down = False
        self._lookup_task: asyncio.Task | None = None
        self._related: dict[str, dict] = {}
        self._auth_task: asyncio.Task | None = None
        # Set to stop the sign-in waiter's thread (and close its window).
        self._auth_cancel: threading.Event | None = None
        # The probe that decides whether a refused request means the session
        # is dead, and when the session was last confirmed good.
        self._verify_task: asyncio.Task | None = None
        self._verified_at: float | None = None
        self._advancing = False
        self._failures = 0
        self._lock = None
        self.ready = False

    # -- wiring -----------------------------------------------------------
    async def startup(self):
        """What has to work before the socket opens: mpv (fast and local, and
        a missing one is a startup error the bar shows). The account check
        is network and runs after, in _start_session."""
        self.player._volume = self.volume
        await self.player.start()
        await self.player.set_volume(self.volume)
        # One daemon at a time (the lock), so no build of ours is running yet
        # and whatever a killed daemon left in build/ can go.
        sweep_work_dir(min_age=0)

    def _begin_session(self) -> None:
        self._session_task = asyncio.create_task(self._start_session())
        self.tasks.add(self._session_task)
        self._session_task.add_done_callback(self.tasks.discard)

    async def _start_session(self) -> None:
        """The account check. On a captive or stalled network it takes up to
        requests' 30 s timeout, and the socket used to stay closed all that
        time, so even pausing a cached track did nothing."""
        try:
            await self._refresh_api()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - the daemon stays up signed out
            log.exception("account check failed")
        finally:
            self.ready = True
        await self._emit_state("auth", "engine")

    async def _cancel_session_check(self) -> None:
        """Stop the startup account check: a sign-in or sign-out has made its
        answer moot. Its finally still marks the engine ready, which the
        engine slice then reports (the check's own broadcast is skipped)."""
        task = self._session_task
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.wait({task}, timeout=1.0)
        await self._emit_state("engine")

    async def _wait_started(self) -> None:
        task = self._session_task
        if task is None or task.done():
            return
        done, _ = await asyncio.wait({task}, timeout=STARTUP_WAIT)
        if not done:
            raise ApiError("Apple Music is still starting; try again in a moment", code="starting")

    async def _refresh_api(self) -> bool:
        generation = self._session_gen
        token = self.account_record.get("mediaUserToken") or ""
        storefront = self.account_record.get("storefront") or self.settings.get("storefront") or "us"
        # After an explicit sign-out the session the user left behind (in
        # the plugin's profile or a daily browser) is not picked up again;
        # only a sign-in clears that.
        if not token and not auth.signed_out(self.account_record):
            found = await asyncio.to_thread(auth.discover)
            if self._session_gen != generation:
                return False  # signed in or out meanwhile: that record stands
            if found:
                profile, token = found
                log.info("found a session in %s", profile)
                self.account_record = auth.save(token, source=str(profile), storefront=storefront)
        # A new api object per session, so a refusal reported by the old one
        # is told apart (_session_refused); the builder stays and follows it,
        # with its per-song locks and its Widevine device.
        api = self.api = AppleApi(
            media_user_token=token,
            storefront=storefront,
            language=self.settings.get("language") or "en-US",
        )
        if self.builder is None:
            self.builder = StreamBuilder(api, notify=self._build_notice, in_use=self._songs_in_use)
        else:
            self.builder.api = api
        if not token:
            self._watch_session(api)
            return False
        try:
            account = await api.fetch_account()
            if self._session_gen != generation:
                # A sign-out (or a newer sign-in) landed while Apple was
                # answering: saving now would put the old token back.
                return False
            self.account_record = auth.save(
                token, source=self.account_record.get("source") or "stored",
                storefront=api.storefront, account=account,
            )
            self._verified_at = time.monotonic()
            return True
        except ApiError as error:
            log.warning("account check failed: %s", error.message)
            if error.code == "unauthorized" and self._session_gen == generation:
                # The token is dead (a 403, or a 401 that a fresh developer
                # token did not cure): say so instead of failing every request.
                self.account_record = dict(self.account_record, error=error.message,
                                           expired=True)
            return False
        finally:
            # Watched from here on: this check reports its own refusal.
            self._watch_session(api)

    # -- session health ---------------------------------------------------
    def _watch_session(self, api: AppleApi) -> None:
        """Have `api` report a refused user token. The report comes from a
        worker thread, so it is handed to the loop."""
        loop = asyncio.get_running_loop()

        def refused(source: AppleApi, token: str, _error: ApiError) -> None:
            try:
                loop.call_soon_threadsafe(self._session_refused, source, token)
            except RuntimeError:  # the loop has closed: the daemon is exiting
                pass

        api.on_unauthorized = refused

    def _session_refused(self, api: AppleApi, token: str) -> None:
        """A request was turned away for the user token. One refusal is not
        proof (an endpoint can refuse for its own reasons), so the account
        is asked once; only if that is refused too is the session dead."""
        if api is not self.api or token != api.media_user_token:
            return  # a session that has been replaced since
        if self.account_record.get("expired"):
            return
        if self._verify_task is not None and not self._verify_task.done():
            return
        if self._verified_at is not None and time.monotonic() - self._verified_at < VERIFY_INTERVAL:
            return
        self._verify_task = asyncio.create_task(self._verify_session(api, token))
        self.tasks.add(self._verify_task)
        self._verify_task.add_done_callback(self.tasks.discard)

    async def _verify_session(self, api: AppleApi, token: str) -> None:
        try:
            await api.fetch_account()
        except ApiError as error:
            if error.code == "unauthorized":
                await self._session_expired(api, token, error.message)
            return  # a network error says nothing about the token
        if api is self.api and token == api.media_user_token:
            self._verified_at = time.monotonic()

    async def _session_expired(self, api: AppleApi, token: str, message: str) -> None:
        if api is not self.api or token != api.media_user_token:
            return
        if self.account_record.get("expired"):
            return
        log.warning("Apple Music no longer accepts the stored token: %s", message)
        # The token stays on disk (a restart checks it again); the session is
        # marked so the UI offers sign-in instead of failing track after track.
        self.account_record = dict(self.account_record, error=message, expired=True)
        await self._emit_state("auth")
        await self.broadcast("notice", {
            "level": "error",
            "message": "Apple Music signed you out; sign in again to keep listening",
        })

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
            # A token is stored, usable or not: Sign out applies even to a
            # session Apple has turned away (signedIn is false then).
            "hasToken": bool(self.account_record.get("mediaUserToken")),
            "error": self.account_record.get("error") or "",
            # Apple turned the stored token away (at startup or mid-session).
            "expired": bool(self.account_record.get("expired")),
            "signedOut": auth.signed_out(self.account_record),
            "name": _account_name(account),
            # The catalogue follows the subscription's storefront, which
            # fetch_account reads; settings.storefront is only the fallback
            # before the account has been seen.
            "storefront": (storefront_from_account(account, "")
                           or self.account_record.get("storefront")
                           or self.settings.get("storefront") or "us"),
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
            # ready: the account check after startup has finished; until
            # then the catalogue commands wait for it.
            "engine": {"version": ENGINE_VERSION, "protocol": PROTOCOL,
                       "mpv": "running" if self.player.running else "stopped",
                       "ready": self.ready},
        }

    async def _emit_state(self, *slices: str) -> None:
        snapshot = self.snapshot()
        for name in slices:
            await self.broadcast(name, snapshot[name])

    async def _player_changed(self, state: dict) -> None:
        await self.broadcast("player", state)
        # mpv comes and goes (idle release, crash, restart); the engine slice
        # follows it so the UI never reports a dead player as running.
        status = "running" if self.player.running else "stopped"
        if status != self._mpv_status:
            self._mpv_status = status
            await self._emit_state("engine")

    async def _player_exited(self, returncode) -> None:
        await self.broadcast("notice", {
            "level": "error",
            "message": f"The audio player stopped unexpectedly (mpv exit status {returncode}); "
                       "press play to start the track again",
        })

    async def _player_failed(self, message: str) -> None:
        await self.broadcast("notice", {"level": "error", "message": message})

    # -- build state ------------------------------------------------------
    # One map, {songId: {songId, stage, message?}}, broadcast whole on every
    # change so the UI can replace rather than merge. An entry lives while a
    # song is being built; it goes when the build succeeds (a cache hit too)
    # or is cancelled. A failed entry stays until the song is tried again or
    # the queue is replaced, so the row can say it would not play.
    async def _publish_build(self) -> None:
        await self.broadcast("build", self.build_state)

    async def _build_notice(self, event: str, payload: dict) -> None:
        if event != "build":
            return
        song_id = str(payload.get("songId") or "")
        if not song_id:
            return
        if payload.get("stage") == "ready":
            self.build_state.pop(song_id, None)
        else:
            self.build_state[song_id] = {**payload, "songId": song_id}
        await self._publish_build()

    def _hold_build(self, song_id: str) -> None:
        self._build_refs[song_id] = self._build_refs.get(song_id, 0) + 1

    async def _release_build(self, song_id: str, succeeded: bool = False,
                             failed: str | None = None) -> None:
        """The end of one task's build of `song_id`, however it ended. The
        state is settled before the broadcast, so a cancel that lands during
        the broadcast cannot leave the entry behind."""
        count = self._build_refs.get(song_id, 0) - 1
        if count > 0:
            self._build_refs[song_id] = count
        else:
            self._build_refs.pop(song_id, None)
        entry = self.build_state.get(song_id)
        if failed is not None:
            self.build_state[song_id] = {"songId": song_id, "stage": "failed", "message": failed}
        elif entry is None:
            return
        elif succeeded or (count <= 0 and entry.get("stage") != "failed"):
            self.build_state.pop(song_id, None)
        else:
            return
        await self._publish_build()

    async def _drop_failed_builds(self) -> None:
        failed = [key for key, entry in self.build_state.items() if entry.get("stage") == "failed"]
        for key in failed:
            self.build_state.pop(key, None)
        if failed:
            await self._publish_build()

    def _building(self) -> bool:
        """The current track is still being built (and is not in mpv yet)."""
        return self._build_task is not None and not self._build_task.done()

    def _prefetch_depth(self) -> int:
        # 0 turns prefetch off (README); only a missing or broken value means 2.
        try:
            return max(0, min(10, int(self.settings.get("prefetch", 2))))
        except (TypeError, ValueError):
            return 2

    def _songs_in_use(self) -> set[str]:
        """The songs whose cached files the cache must keep: the file mpv
        has, the queue position, the prefetch window ahead of it and the song
        being prefetched. The builder asks on every prune, so this is the
        live answer, not a count of recent builds."""
        ids = {self.player.state.get("songId"), self._prefetching, self._build_song}
        if self.index >= 0:
            ids.update(track.get("id") for track in
                       self.queue[self.index: self.index + 1 + self._prefetch_depth()])
        return {str(song_id) for song_id in ids if song_id}

    # -- queue ------------------------------------------------------------
    async def _set_queue(self, tracks: list[dict], index: int, context: dict) -> None:
        self.queue = [t for t in tracks if t.get("id")]
        self.context = context or {}
        self.index = max(0, min(index, len(self.queue) - 1)) if self.queue else -1
        if self.shuffle and self.queue:
            # A fresh queue has nothing played yet: the chosen track first,
            # everything else shuffled after it. _shuffle_from keeps the head
            # in order, which is right only mid-queue (cmd_shuffle), where the
            # tracks before the cursor really have played.
            chosen = self.queue.pop(self.index)
            random.shuffle(self.queue)
            self.queue.insert(0, chosen)
            self.index = 0
        await self._drop_failed_builds()
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

    def _cancel_build(self) -> None:
        # A failed build advances from inside its own task: cancelling that
        # task here would cancel the caller, not an old build.
        if self._build_task and self._build_task is not asyncio.current_task():
            self._build_task.cancel()
        self._build_song = None

    def _abandon_preparation(self, wanted: str | None = None, keep_build: bool = False) -> None:
        """Stop working towards the old position: its build (unless
        `keep_build`) and its prefetch. Every way off a track comes through
        here, the stop at the end of the queue too, so a build nobody waits
        for any more cannot reach mpv after the queue has moved or stopped."""
        if not keep_build:
            self._cancel_build()
        self._generation += 1
        # The old position's prefetch would go on fetching (and licensing)
        # songs nobody is waiting for, next to the track that is. Unless it
        # is building exactly the track wanted now: the new build waits on
        # the per-song lock and takes the finished file instead of starting
        # the download over.
        if self._prefetch_task and not (wanted and self._prefetching == wanted):
            self._prefetch_task.cancel()
            self._prefetch_task = None

    async def _play_current(self, start: float = 0.0, keep_pause: bool = False) -> None:
        """Start the queue's current track (from `start` seconds). A pause
        pressed while a track was being prepared is dropped here, a new
        track being a new request, except across an automatic advance
        (`keep_pause`): a build that failed must not unpause the next one."""
        if not keep_pause:
            self._pending_pause = False
        song = self.now_playing()
        wanted = str(song.get("id")) if song else None
        if (wanted and self._building() and self._build_song == wanted
                and self._build_task is not asyncio.current_task()):
            # Already being prepared (a second click on the row while it
            # downloads, or a new queue that starts with it): restarting
            # threw the download away and fetched it again. The build
            # carries on and prefetches from the new position when it lands.
            self._abandon_preparation(wanted, keep_build=True)
            self._lookup(song)
            return
        self._abandon_preparation(wanted)
        if not song:
            self._pending_pause = False
            await self.player.stop()
            await self._emit_state("nowPlaying")
            return
        # From here until the file reaches mpv the player reports "loading",
        # so the UI shows the new title as preparing instead of the old
        # track's progress. The done callback clears it if the build fails or
        # is cancelled; a newer track's preparation keeps its own flag.
        loading = self.player.begin_loading()
        self._lookup(song)
        self._build_song = wanted
        self._build_task = asyncio.create_task(self._start_track(song, start))
        self.tasks.add(self._build_task)
        self._build_task.add_done_callback(self.tasks.discard)
        self._build_task.add_done_callback(lambda _task: self.player.end_loading(loading))

    # -- album / artist links ---------------------------------------------
    def _lookup(self, song: dict) -> None:
        """Find the album and artist the current song belongs to.

        The footer links the cover to the album and the artist line to the
        artist, so it needs both ids. A listing usually carries them already;
        a queue handed over by a view, or a cached track, may not, and then
        one lookup is worth it. The answer is cached per song for the session.
        """
        if not song or not self.api or not self.api.media_user_token:
            return
        if song.get("albumId") and song.get("artistId"):
            return
        song_id = str(song.get("id") or "")
        if not song_id:
            return
        known = self._related.get(song_id)
        if known is None:
            if self._lookup_task:
                self._lookup_task.cancel()
            self._lookup_task = asyncio.create_task(self._fetch_related(song))
            self.tasks.add(self._lookup_task)
            self._lookup_task.add_done_callback(self.tasks.discard)
            return
        if known:
            task = asyncio.create_task(self._patch_track(song_id, known))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)

    async def _fetch_related(self, song: dict) -> None:
        song_id = str(song.get("id") or "")
        try:
            info = await self.api.song(song_id, bool(song.get("library")))
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - the link is cosmetic
            log.debug("no album or artist for %s: %s", song_id, error)
            self._related[song_id] = {}
            return
        patch = {key: info[key] for key in
                 ("albumId", "albumLibrary", "artistId", "artistLibrary", "album", "artist")
                 if info.get(key)}
        if len(self._related) > 400:
            self._related.clear()
        self._related[song_id] = patch
        await self._patch_track(song_id, patch)

    async def _patch_track(self, song_id: str, patch: dict) -> None:
        changed = False
        for track in self.queue:
            if str(track.get("id")) == song_id:
                track.update(patch)
                changed = True
        if not changed:
            return
        current = self.now_playing()
        if current and str(current.get("id")) == song_id:
            await self._emit_state("nowPlaying")
        await self._emit_state("queue")

    async def _start_track(self, song: dict, start: float = 0.0) -> None:
        song_id = str(song["id"])
        try:
            # Opened before the account check finished: wait for it rather
            # than call a perfectly good session "not signed in".
            await self._wait_started()
        except ApiError as error:
            self.build_state[song_id] = {"songId": song_id, "stage": "failed",
                                         "message": error.message}
            await self._publish_build()
            await self.broadcast("notice", {"level": "error", "message": error.message})
            return
        if not self.api or not self.api.media_user_token:
            self.build_state[song_id] = {"songId": song_id, "stage": "failed",
                                         "message": "Not signed in"}
            await self.broadcast("notice", {
                "level": "error", "message": "Sign in to Apple Music to play this track",
            })
            await self._publish_build()
            return
        # A prefetch may be on this song already; its stage is the truer one.
        entry = self.build_state.get(song_id)
        queued = entry is None or entry.get("stage") == "failed"
        if queued:
            self.build_state[song_id] = {"songId": song_id, "stage": "queued"}
        # Held before the first await, so a cancel at any point below finds
        # the entry accounted for and clears it.
        self._hold_build(song_id)
        failure: BaseException | None = None
        try:
            if queued:
                await self._publish_build()
            path = await self.builder.build(song, codec=str(self.settings.get("codec") or "aac-web"))
            await self.player.load(str(path), song_id, start=start)
            if self._build_task is asyncio.current_task():
                # In mpv now: a click on its row restarts it again.
                self._build_song = None
            if self._pending_pause:
                # Paused while it was being prepared: honour the last press.
                await self.player.set_paused(True)
            self._pending_pause = False
            self._failures = 0
        except asyncio.CancelledError:
            await self._release_build(song_id)
            raise
        except PlayerReset:
            # mpv went away under the handover (it crashed, or was ended):
            # nothing is wrong with the track. The crash notice says to press
            # play, and play starts this same track again, so the queue
            # stays where it is rather than counting a failure and moving on.
            log.info("mpv went away while %s was being handed over", song_id)
            await self._release_build(song_id)
            return
        except Exception as error:  # noqa: BLE001 - every failure takes the path below
            failure = error
        if failure is None:
            await self._release_build(song_id, succeeded=True)
            await self._emit_state("nowPlaying")
            self._schedule_prefetch()
            return

        # Any failure, not just the ones the API explains: a network error
        # or a dead mpv used to leave the queue stalled and the row spinning.
        if self._build_task is asyncio.current_task():
            self._build_song = None  # a click on the row now tries it again
        if isinstance(failure, ApiError):
            message = failure.message
            log.warning("could not play %s: %s", song_id, message)
        else:
            message = str(failure) or type(failure).__name__
            log.warning("could not play %s", song_id, exc_info=failure)
        await self._release_build(song_id, failed=message)
        await self.broadcast("notice", {"level": "error", "message": message})
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

    def _schedule_prefetch(self) -> None:
        if self._prefetch_task:
            self._prefetch_task.cancel()
        self._prefetch_task = asyncio.create_task(self._prefetch())
        self.tasks.add(self._prefetch_task)
        self._prefetch_task.add_done_callback(self.tasks.discard)

    async def _prefetch(self) -> None:
        depth = self._prefetch_depth()
        if depth <= 0 or not self.builder:
            return
        # The songs ahead of the position this prefetch was started for. Read
        # once: re-reading the queue on each pass mixed positions of an old
        # queue with a new one.
        generation = self._generation
        songs = list(self.queue[self.index + 1: self.index + 1 + depth])
        for song in songs:
            if self._generation != generation:
                return
            song_id = str(song.get("id") or "")
            if not song_id:
                continue
            self._prefetching = song_id
            self._hold_build(song_id)
            try:
                await self.builder.build(song, codec=str(self.settings.get("codec") or "aac-web"))
            except asyncio.CancelledError:
                self._prefetching = None
                await self._release_build(song_id)
                raise
            except Exception as error:  # noqa: BLE001 - prefetch is best effort
                self._prefetching = None
                code = getattr(error, "code", "")
                message = getattr(error, "message", "") or str(error) or type(error).__name__
                log.info("prefetch %s failed: %s", song_id, message)
                # A network blip says nothing about the track; it is tried
                # again when its turn comes. Anything else marks the row.
                await self._release_build(
                    song_id, failed=None if code in TRANSIENT_CODES else message)
                continue
            self._prefetching = None
            await self._release_build(song_id, succeeded=True)

    async def _advance(self, direction: int, automatic: bool = False) -> None:
        target = self._step(direction, automatic)
        if target is None:
            # Next on the last track while it is still downloading: without
            # this the build finished after the stop and started playing.
            self._abandon_preparation()
            self._pending_pause = False
            await self.player.stop()
            await self._emit_state("player", "nowPlaying")
            return
        if automatic and self.repeat == "one" and target == self.index:
            # mpv runs with --idle=yes and no keep-open, so by the time the
            # end is reported the file is unloaded and a seek does nothing.
            # Hand it the same file again; rebuild (a cache hit) if it is gone.
            song = self.now_playing()
            if song and await self.player.replay(str(song.get("id"))):
                return
            await self._play_current(keep_pause=True)
            return
        self.index = target
        await self._emit_state("queue", "nowPlaying")
        await self._play_current(keep_pause=automatic)

    async def _track_finished(self) -> None:
        if self._advancing:
            return
        if self.player.state.get("loading"):
            # The old track ran out while the queue had already moved on and
            # the new one is still being prepared: it starts when it is ready.
            # Advancing here would skip it.
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
        storefront = self.auth_state()["storefront"]
        launch = asyncio.ensure_future(asyncio.to_thread(auth.launch_login, storefront))
        try:
            result = await asyncio.shield(launch)
        except asyncio.CancelledError:
            # Cancelled mid-launch (quit, shutdown): the launch runs on in
            # its thread and opens the window anyway, with no waiter to ever
            # close it or its DevTools port. Let it finish, then close it.
            try:
                await launch
            except BaseException:  # noqa: BLE001 - already on the way out
                pass
            if self._auth_task is None or self._auth_task.done():
                asyncio.get_running_loop().run_in_executor(None, auth.close_login)
            raise
        except (RuntimeError, OSError) as error:
            raise ApiError(f"Could not open a sign-in window: {error}", code="unavailable") from error
        if self._auth_task and not self._auth_task.done():
            return {**result, "waiting": True}
        # The token being replaced is still in the profile's cookie database
        # and was accepted the instant the browser launched, before the user
        # had done anything; a dead one never counts as the new session.
        reject = set()
        old = self.account_record.get("mediaUserToken") or ""
        if old and (self.account_record.get("error") or self.account_record.get("expired")):
            reject.add(old)
        self._auth_cancel = cancel = threading.Event()
        # Tracked like every other task, so shutdown cancels it (and its
        # cancel handler closes the window) and a failure is not lost.
        self._auth_task = task = asyncio.create_task(self._await_login(reject, cancel, storefront))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        task.add_done_callback(self._signin_ended)
        return {**result, "waiting": True}

    def _signin_ended(self, task: asyncio.Task) -> None:
        """A sign-in that failed outright (not cancelled, not refused) used
        to vanish as 'Task exception was never retrieved' at exit, with the
        UI still on the sign-in prompt. Say so, and close the window: a
        token may have been captured and saved before the failure."""
        if task.cancelled() or task.exception() is None:
            return
        log.error("sign-in failed", exc_info=task.exception())
        if self._shut_down:
            return
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, auth.close_login)

        async def report():
            await self._emit_state("auth")
            await self.broadcast("notice", {
                "level": "error", "message": f"Sign-in failed: {task.exception()}"})
        follow_up = loop.create_task(report())
        self.tasks.add(follow_up)
        follow_up.add_done_callback(self.tasks.discard)

    async def _await_login(self, reject: set, cancel: threading.Event, storefront: str) -> None:
        # True only while the waiter thread runs: it closes its own window
        # when `cancel` is set. At any other point nothing else would.
        waiting = False
        try:
            # The startup account check saves whatever it found when it
            # finishes; it must not land on top of this sign-in. Waited for,
            # because a quick answer is best; past STARTUP_WAIT it is
            # stopped instead (and its result would be dropped anyway).
            try:
                await self._wait_started()
            except ApiError:
                await self._cancel_session_check()
            while True:
                waiting = True
                token = await asyncio.to_thread(
                    auth.wait_for_token, 600.0, reject=reject, cancel=cancel, close=False)
                waiting = False
                if cancel.is_set():
                    return
                if not token:
                    # wait_for_token has closed the window if it was open.
                    await self.broadcast("notice", {
                        "level": "error", "message": "Sign-in window closed without a session"})
                    return
                self._session_gen += 1
                self.account_record = auth.save(token, source=str(config.login_profile()),
                                                storefront=storefront)
                ok = await self._refresh_api()
                if ok or not self.account_record.get("expired"):
                    # Accepted, or Apple could not be reached to check it:
                    # either way this is the session, and the window (with
                    # its DevTools port) has done its job. The UI hears
                    # first, so a failure closing the window cannot hide a
                    # saved session.
                    await self._emit_state("auth")
                    await asyncio.to_thread(auth.close_login)
                    await self.broadcast("notice", {
                        "level": "success" if ok else "info",
                        "message": ("Signed in to Apple Music" if ok else
                                    "Signed in; Apple Music could not be reached to check the account"),
                    })
                    return
                # Apple turned this token down. If the window is still open
                # the user may be mid-way through signing in: keep watching
                # it for a different token instead of walking away.
                reject.add(token)
                await self._emit_state("auth")
                if await asyncio.to_thread(auth.login_running):
                    await self.broadcast("notice", {
                        "level": "error",
                        "message": "Apple did not accept that session; finish signing in in the window",
                    })
                    continue
                await self.broadcast("notice", {
                    "level": "error",
                    "message": "Found a session but Apple rejected it (check the subscription)",
                })
                return
        except asyncio.CancelledError:
            # The waiter thread sees the event, stops and closes its window.
            # Cancelled anywhere else (still waiting for the startup check,
            # or checking a captured token) the window is open with nobody to
            # close it: close it without holding up the cancel. On shutdown
            # asyncio.run waits for the executor, so it is closed before exit.
            cancel.set()
            if not waiting:
                asyncio.get_running_loop().run_in_executor(None, auth.close_login)
            raise

    def _cancel_signin(self) -> None:
        if self._auth_cancel is not None:
            self._auth_cancel.set()
        if self._auth_task is not None and not self._auth_task.done():
            self._auth_task.cancel()

    async def cmd_signOut(self, params: dict) -> dict:
        """Sign out for good: the stored token, the session in the plugin's
        own browser profile, and a flag so the next start does not find that
        session (or a daily browser's) and sign straight back in."""
        # The startup account check would save its token over this; it is
        # stopped (and anything it is still doing is dropped), not waited
        # for: on a stalled network that wait was a minute.
        self._session_gen += 1
        await self._cancel_session_check()
        self._cancel_signin()
        if self._verify_task is not None:
            self._verify_task.cancel()
        self.account_record = await asyncio.to_thread(auth.sign_out)
        if self.api:
            self.api.media_user_token = ""
        await self._emit_state("auth")
        if self.account_record.get("profileDirty"):
            await self.broadcast("notice", {
                "level": "error",
                "message": "Signed out, but the sign-in window would not close; its Apple "
                           "session is removed before the next sign-in",
            })
        return self.auth_state()

    async def cmd_signout(self, params: dict) -> dict:
        # The name engines before 0.2.5 answered to; the shell falls back to it.
        return await self.cmd_signOut(params)

    async def cmd_account(self, params: dict) -> dict:
        api = self.api
        if api and api.media_user_token:
            token = api.media_user_token
            # This is the probe: a refusal here must not start a second one.
            if self._verify_task is None or self._verify_task.done():
                self._verify_task = asyncio.current_task()
            try:
                account = await api.fetch_account()
            except ApiError as error:
                if error.code == "unauthorized":
                    await self._session_expired(api, token, error.message)
                raise
            if api is self.api and token == api.media_user_token:
                # A fresh record: an earlier "expired" that was wrong goes.
                self.account_record = auth.save(
                    token, source=self.account_record.get("source") or "stored",
                    storefront=api.storefront, account=account)
                self._verified_at = time.monotonic()
                await self._emit_state("auth")
        return self.auth_state()

    async def _require_api(self) -> AppleApi:
        if not self.api or not self.api.media_user_token:
            raise ApiError("Sign in to Apple Music first", code="unauthorized")
        return self.api

    async def _catalogue_api(self) -> AppleApi:
        """The API for a catalogue command. Until the account check that
        runs after the socket opens has finished, wait for it (these run as
        their own tasks, so nothing else waits with them)."""
        await self._wait_started()
        return self.api or await self._require_api()

    async def cmd_home(self, params: dict) -> dict:
        api = await self._catalogue_api()
        return await api.home()

    async def cmd_search(self, params: dict) -> dict:
        api = await self._catalogue_api()
        return await api.search(str(params.get("term") or ""), limit=int(params.get("limit") or 20))

    async def cmd_album(self, params: dict) -> dict:
        api = await self._catalogue_api()
        return await api.album(str(params.get("id")), bool(params.get("library")))

    async def cmd_playlist(self, params: dict) -> dict:
        api = await self._catalogue_api()
        return await api.playlist(str(params.get("id")), bool(params.get("library")))

    async def cmd_artist(self, params: dict) -> dict:
        api = await self._catalogue_api()
        return await api.artist(str(params.get("id")), bool(params.get("library")))

    async def cmd_library(self, params: dict) -> dict:
        """{kind, offset} -> {items, next}: `next` is the offset of the
        following page, or None after the last (the API client decides)."""
        api = await self._catalogue_api()
        try:
            offset = max(0, int(params.get("offset") or 0))
        except (TypeError, ValueError):
            offset = 0
        return await api.library(str(params.get("kind") or "songs"),
                                 int(params.get("limit") or 100), offset)

    async def cmd_play(self, params: dict) -> dict:
        tracks = params.get("tracks") or []
        index = int(params.get("index") or 0)
        context = params.get("context") or {}
        if not tracks and params.get("id"):
            # The lookup runs outside the command lock, so a slow Apple answer
            # holds up nothing but this play.
            api = await self._catalogue_api()
            tracks = [await api.song(str(params["id"]), bool(params.get("library")))]
            index = 0
        if not tracks:
            raise ApiError("Nothing to play", code="bad-request")
        # With tracks given there is no await before this, so the lock is
        # taken in arrival order like every other ordered command.
        async with self._command_lock:
            await self._set_queue(tracks, index, context)
            return {"index": self.index, "count": len(self.queue)}

    async def _set_pending_pause(self, paused: bool) -> None:
        """A pause/play press while the current track is still being built.
        Restarting the build (what toggle used to do, because mpv is idle
        meanwhile) threw the download away and ignored the pause. The press
        is kept for when the file reaches mpv; a pause also silences the
        previous track if it is still playing meanwhile."""
        self._pending_pause = paused
        if paused and not self.player.state["idle"] and not self.player.state["paused"]:
            await self.player.set_paused(True)

    def _resume_position(self) -> float:
        """Where the current track is, for a restart of it: mpv's position
        while it plays it, the position mpv died at after a crash (so play
        does not throw the place away), 0 otherwise."""
        song = self.now_playing()
        if not song or self._building():
            return 0.0
        song_id = str(song.get("id"))
        state = self.player.state
        if not state["idle"] and not state.get("loading") and str(state.get("songId")) == song_id:
            return self.player.position()
        return self.player.resume_point(song_id)

    async def cmd_toggle(self, params: dict) -> dict:
        if self._building():
            await self._set_pending_pause(not self._pending_pause)
        elif self.player.state["idle"] and self.queue:
            await self._play_current(start=self._resume_position())
        else:
            await self.player.toggle()
        return self.player.snapshot()

    async def cmd_pause(self, params: dict) -> dict:
        if self._building():
            await self._set_pending_pause(True)
        else:
            await self.player.set_paused(True)
        return self.player.snapshot()

    async def cmd_resume(self, params: dict) -> dict:
        # An idle mpv (the track ended, the queue ran out, mpv was released
        # or crashed) has nothing to unpause: start the current track again,
        # the way toggle does. Not while it is being prepared already.
        if self._building():
            await self._set_pending_pause(False)
        elif (self.player.state["idle"] and self.now_playing()
                and not self.player.state.get("loading")):
            await self._play_current(start=self._resume_position())
        else:
            await self.player.set_paused(False)
        return self.player.snapshot()

    async def cmd_next(self, params: dict) -> dict:
        await self._advance(1)
        return {"index": self.index}

    async def cmd_previous(self, params: dict) -> dict:
        # A "previous" press mid-track restarts it, like every other player.
        # So does one on the first track: there is nothing before it, and
        # stopping there left the player idle. While the current track is
        # being built, pos belongs to the old file and says nothing.
        building = self._building()
        at_start = self.repeat != "all" and self.index <= 0
        current = self.now_playing()
        loaded = (current is not None and not self.player.state["idle"]
                  and str(self.player.state.get("songId")) == str(current.get("id")))
        if at_start or (not building and self.player.position() > 3):
            if building or not current:
                pass  # it starts from the top once it is ready
            elif not loaded or not await self.player.seek(0):
                await self._play_current()  # nothing of this track to rewind
            else:
                await self.player.set_paused(False)
        else:
            await self._advance(-1)
        return {"index": self.index}

    async def cmd_seek(self, params: dict) -> dict:
        # While the next track is being prepared mpv still has the old one;
        # a seek there would move a song the UI no longer shows.
        if not self.player.state.get("loading"):
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
        # Clamped before it is saved: an out-of-range value in settings.json
        # used to stop mpv, and with it the daemon, from starting at all.
        value = clamp_volume(params.get("value"), self.volume)
        await self._apply_volume(value)
        return {"volume": value}

    async def cmd_mute(self, params: dict) -> dict:
        """One mute for every surface. The level to come back to is saved, so
        unmuting after a restart (or from another monitor) restores it."""
        current = clamp_volume(self.player.state.get("volume"), self.volume)
        if current > 0:
            self.settings = config.save_settings({"muteRestore": current})
            value = 0
        else:
            value = clamp_volume(self.settings.get("muteRestore"), 70) or 70
        await self._apply_volume(value)
        return {"volume": value, "muted": value == 0}

    async def _apply_volume(self, value: int) -> None:
        value = await self.player.set_volume(value)
        self.volume = value
        self.settings = config.save_settings({"volume": value})
        await self._emit_state("settings")

    async def cmd_shuffle(self, params: dict) -> dict:
        self.shuffle = bool(params.get("on")) if params.get("on") is not None else not self.shuffle
        if self.shuffle:
            self._shuffle_from(self.index)
            # What was prefetched is no longer next; fetch what is. While the
            # current track is still building, its load schedules this.
            if self.queue and not self._building():
                self._generation += 1
                self._schedule_prefetch()
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
        # Local files only: nothing to wait for the account check over.
        return self.builder.cache_stats() if self.builder else {}

    async def cmd_clearCache(self, params: dict) -> dict:
        if not self.builder:
            return {"removed": 0}
        # The file mpv has stays, so repeat and previous do not need Apple.
        keep = {self.builder.cached_path(str(song_id))
                for song_id in (self.player.state.get("songId"),) if song_id}
        return self.builder.clear_cache(keep=keep)

    async def cmd_settings(self, params: dict) -> dict:
        changes = params.get("changes") or {}
        if changes:
            self.settings = config.save_settings(changes)
        return self.settings

    async def cmd_restartPlayer(self, params: dict) -> dict:
        resume = self._resume_position()
        # A build waiting in load() would see its handover fail as mpv goes,
        # mark the track unplayable and skip to the next one. Cancelled first,
        # it ends quietly, and _play_current below starts the same track
        # again (a cache hit), where it was.
        self._cancel_build()
        # terminate, not stop: the point is a fresh process, and the playback
        # stop would leave a hung mpv exactly where it was.
        await self.player.terminate()
        await self.player.start()
        song = self.now_playing()
        if song:
            await self._play_current(start=resume)
        return self.player.snapshot()

    async def cmd_quit(self, params: dict) -> dict:
        """Stop playback, end mpv and exit, removing the socket and pid file.
        The service sends this when the plugin is disabled or removed, which
        is quicker than waiting for _watch_enabled to notice; a shell restart
        does not, and playback carries on into the next service. The answer
        goes out first: the stop is set on the next loop pass, after
        _dispatch has written it."""
        log.info("quit requested")
        asyncio.get_running_loop().call_soon(self._request_stop)
        return {"quitting": True}

    def _request_stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    # -- socket -----------------------------------------------------------
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        """Every request runs as its own task; replies carry the request id,
        so they need not come back in order. One connection carries the whole
        shell, and serving it one request at a time put a pause behind
        whatever search or page load came first, up to Apple's 30 s timeout."""
        self.clients.add(writer)
        reads: set[asyncio.Task] = set()
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                task = asyncio.create_task(self._dispatch(message, writer))
                self.tasks.add(task)
                task.add_done_callback(self.tasks.discard)
                if str(message.get("cmd") or "") in READ_COMMANDS:
                    reads.add(task)
                    task.add_done_callback(reads.discard)
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self.clients.discard(writer)
            # Nobody is left to read these answers. Commands that change
            # state run on: a play that was sent must still happen.
            for task in list(reads):
                task.cancel()
            try:
                writer.close()
            except OSError:
                pass

    async def _dispatch(self, message: dict, writer: asyncio.StreamWriter) -> None:
        request_id = message.get("id")
        command = str(message.get("cmd") or "")
        params = message.get("params") or {}
        if not isinstance(params, dict):
            params = {}
        handler = getattr(self, f"cmd_{command}", None)
        if command == "hello":
            snapshot = self.snapshot()
            # pid: a Service that retired an older daemon can tell the new
            # one answering from the old one still closing its socket.
            response = {"id": request_id, "ok": True,
                        "result": {"engineVersion": ENGINE_VERSION, "protocol": PROTOCOL,
                                   "pid": os.getpid(), "snapshot": snapshot}}
            self._write(writer, response)
            return
        if not handler:
            self._write(writer, {"id": request_id, "ok": False,
                                 "error": {"code": "bad-request", "message": f"Unknown command: {command}"}})
            return
        try:
            if command in READ_COMMANDS or command in FREE_COMMANDS or command in SELF_ORDERED:
                result = await handler(params)
            else:
                # Commands that change the player or the queue keep their
                # order among themselves (next, next, previous must land as
                # sent). Nothing under this lock waits on the network. The
                # lock is taken before the task's first await, so tasks queue
                # on it in the order their lines arrived.
                async with self._command_lock:
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
        # Asked before anything is touched: a launcher that comes up after
        # the plugin was disabled (queued behind the venv bootstrap, or
        # spawned just before the Service went) leaves without starting mpv,
        # and the socket, pid file and lock of a daemon that may be running
        # stay as they are.
        stamp, enabled = config.plugin_enabled()
        if not enabled:
            log.info("the plugin is disabled in the shell's config; not starting")
            return
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
        config.pid_path().write_text(f"{os.getpid()}\n", "utf-8")
        # quit sets the same event the signal handlers do, and so does the
        # plugin being disabled (_watch_enabled), from here on: a disable
        # while mpv is coming up ends the daemon as soon as it listens.
        self._stop = stop = stop or asyncio.Event()
        watcher = asyncio.create_task(self._watch_enabled(stamp))
        self.tasks.add(watcher)
        watcher.add_done_callback(self.tasks.discard)
        await self.startup()
        # A play command carries the whole queue, artwork URLs included: a
        # 100-track handover is well past asyncio's 64 KiB readline default,
        # and that used to close the connection mid-request.
        server = await asyncio.start_unix_server(self.handle, path=str(path),
                                                 limit=8 * 1024 * 1024)
        os.chmod(path, 0o600)
        log.info("listening on %s", path)
        if not stop.is_set():
            # The socket is open before the network part of startup, so a
            # slow or captive network leaves the controls working meanwhile.
            self._begin_session()
            await self.broadcast("snapshot", self.snapshot())
        async with server:
            serving = asyncio.create_task(server.serve_forever())
            stopping = asyncio.create_task(stop.wait())
            try:
                await asyncio.wait({serving, stopping}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (serving, stopping):
                    task.cancel()
                # Leaving `async with server` waits for every connection to
                # finish, and the shell's never does on its own: close them,
                # or a quit would hang with the shell still connected.
                # close() sends what is already written (quit's answer) first.
                server.close()
                for writer in list(self.clients):
                    try:
                        writer.close()
                    except OSError:
                        pass

    async def _watch_enabled(self, stamp) -> None:
        """End the daemon once the shell's config no longer enables the
        plugin. The Service sends quit when it is disabled, but only if it is
        connected; this covers the rest, whatever order things happen in. A
        stat per look, and a read only when the file changed."""
        missing = 0
        while True:
            await asyncio.sleep(ENABLED_POLL_SECONDS)
            # A plugin directory deleted by hand (or a dev symlink removed)
            # leaves the id in the config, and the Service goes with the
            # manifest. Two looks in a row, so an update that moves the
            # directory aside and back, or a git checkout, does not count.
            if config.plugin_installed():
                missing = 0
            else:
                missing += 1
                if missing >= 2:
                    log.info("the plugin is no longer installed; quitting")
                    self._request_stop()
                    return
            current = config.shell_config_stamp()
            if current == stamp:
                continue
            try:
                stamp, enabled = config.plugin_enabled()
            except Exception:  # noqa: BLE001 - no answer is not a disable
                # The stamp stays, so the next look reads it again.
                log.warning("could not read the shell's config", exc_info=True)
                enabled = True
            if not enabled:
                log.info("the plugin was disabled in the shell's config; quitting")
                self._request_stop()
                return

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
        if self._shut_down:
            return
        self._shut_down = True
        for task in list(self.tasks):
            task.cancel()
        # The event too, not just the task: the waiter is a thread, and the
        # loop may be gone before the task runs its cancel handler; the
        # executor would then hold the exit for the waiter's ten minutes.
        self._cancel_signin()
        # End mpv here rather than leaning on PDEATHSIG alone: that is
        # best-effort, and a clean exit should not leave cleanup to chance.
        await self.player.terminate()
        if self._lock is None:
            # This launcher lost the race: the socket and pid file belong to
            # the daemon that holds the lock.
            return
        for leftover in (config.socket_path(), config.pid_path()):
            try:
                leftover.unlink()
            except OSError:
                pass
        try:
            self._lock.close()
        except OSError:
            pass
        self._lock = None
