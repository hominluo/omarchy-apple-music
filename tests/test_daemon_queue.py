"""Daemon queue, build-state and socket bookkeeping: the build map (C1),
failures that advance, prefetch, toggle/previous during a build, shuffle,
concurrent requests, the socket opening before the account check, quit.

The builder is a stand-in and the player's process methods are mocked: no
mpv, no network, every engine path in a temp dir.
"""

import asyncio
import copy
import json
import logging
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import config, daemon as daemon_module  # noqa: E402
from engine.apple import ApiError  # noqa: E402
from engine.daemon import FAILURE_LIMIT, Daemon  # noqa: E402
from engine.stream import StreamError  # noqa: E402


# The engine logs every failure these tests provoke on purpose.
logging.getLogger("apple-music").addHandler(logging.NullHandler())
logging.getLogger("apple-music").propagate = False


def song(song_id) -> dict:
    # albumId/artistId present, so the footer lookup stays out of the way.
    return {"id": str(song_id), "title": f"t{song_id}", "albumId": "a", "artistId": "r"}


class FakeBuilder:
    """build() returns a file at once, or waits on a gate, or raises."""

    def __init__(self, daemon: Daemon, root: Path):
        self.notify = daemon._build_notice
        self.root = root
        self.calls: list[str] = []
        self.gates: dict[str, asyncio.Event] = {}
        self.fail: dict[str, BaseException] = {}
        self.cleared: list = []

    def cached_path(self, song_id: str) -> Path:
        return self.root / f"{song_id}.m4a"

    async def build(self, track: dict, codec: str = "aac-web", force: bool = False) -> Path:
        song_id = str(track["id"])
        self.calls.append(song_id)
        gate = self.gates.get(song_id)
        if gate is not None:
            await self.notify("build", {"songId": song_id, "stage": "downloading"})
            await gate.wait()
        if song_id in self.fail:
            raise self.fail[song_id]
        path = self.cached_path(song_id)
        path.write_bytes(b"audio")
        if gate is not None:
            await self.notify("build", {"songId": song_id, "stage": "ready"})
        return path

    def clear_cache(self, keep=()):
        self.cleared.append(set(keep))
        return {"removed": 0}


class QueueCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="am-test-")
        self.root = Path(self.tmp.name)
        for name in ("run", "data", "cache", "files"):
            (self.root / name).mkdir()
        self.env = mock.patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(self.root / "run"),
            "XDG_DATA_HOME": str(self.root / "data"),
            "XDG_CONFIG_HOME": str(self.root / "config"),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            # The shell's config lives under $HOME, not XDG_CONFIG_HOME.
            "HOME": str(self.root / "home"),
        })
        self.env.start()
        (config.data_dir() / "settings.json").write_text(json.dumps({"prefetch": 0}), "utf-8")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def daemon(self, prefetch: int = 0) -> Daemon:
        d = Daemon()
        d.settings["prefetch"] = prefetch
        d.api = mock.Mock(media_user_token="token")
        d.builder = FakeBuilder(d, self.root / "files")
        self.sent: list[tuple[str, object]] = []

        async def broadcast(event, data):
            self.sent.append((event, copy.deepcopy(data)))
        d.broadcast = broadcast

        player = d.player
        self.loaded: list[str] = []
        self.starts: list[float] = []

        async def load(path, song_id, start=0.0):
            self.loaded.append(str(song_id))
            self.starts.append(start)
            player.state.update({"songId": str(song_id), "idle": False, "paused": False,
                                 "loading": False, "pos": 0.0})

        async def set_paused(paused):
            player.state["paused"] = bool(paused)

        async def stop():
            player.state.update({"idle": True, "paused": True, "loading": False, "pos": 0.0})
        player.load = mock.AsyncMock(side_effect=load)
        player.set_paused = mock.AsyncMock(side_effect=set_paused)
        player.stop = mock.AsyncMock(side_effect=stop)
        player.seek = mock.AsyncMock(return_value=True)
        player.toggle = mock.AsyncMock()
        player.terminate = mock.AsyncMock()
        player.start = mock.AsyncMock()
        player.set_volume = mock.AsyncMock(side_effect=lambda value: value)
        return d

    def builds(self) -> list:
        return [data for event, data in self.sent if event == "build"]

    async def settle(self, d: Daemon, rounds: int = 50):
        """Let every build/prefetch task run as far as it can."""
        for _ in range(rounds):
            await asyncio.sleep(0)
        pending = [t for t in (d._build_task, d._prefetch_task) if t and not t.done()]
        if pending:
            await asyncio.wait(pending, timeout=0.05)
        for _ in range(rounds):
            await asyncio.sleep(0)


class BuildMap(QueueCase):
    async def test_every_build_event_is_the_whole_map(self):
        """F3/C1: a live event carried one flat entry and the UI replaced the
        map with it, so build[id] was undefined for every row."""
        d = self.daemon()
        gate = d.builder.gates["1"] = asyncio.Event()
        await d._set_queue([song(1), song(2)], 0, {})
        await self.settle(d)
        self.assertEqual(d.build_state, {"1": {"songId": "1", "stage": "downloading"}})
        for data in self.builds():
            for key, entry in data.items():
                self.assertEqual(entry["songId"], key)
        self.assertEqual(self.builds()[-1], d.snapshot()["build"])
        gate.set()
        await self.settle(d)
        self.assertEqual(d.build_state, {})
        self.assertEqual(self.builds()[-1], {})

    async def test_cache_hit_leaves_no_entry(self):
        """F10: the 'queued' entry of a cached track stayed forever."""
        d = self.daemon()
        await d._set_queue([song(1)], 0, {})
        await self.settle(d)
        self.assertEqual(self.loaded, ["1"])
        self.assertEqual(d.build_state, {})
        self.assertEqual(self.builds()[-1], {})

    async def test_cancelled_build_is_cleared(self):
        """F10: skipping a track mid-build left it 'downloading'."""
        d = self.daemon()
        d.builder.gates["1"] = asyncio.Event()  # never opens
        await d._set_queue([song(1), song(2)], 0, {})
        await self.settle(d)
        self.assertIn("1", d.build_state)
        await d.cmd_jump({"index": 1})
        await self.settle(d)
        self.assertEqual(self.loaded, ["2"])
        self.assertEqual(d.build_state, {})

    async def test_any_failure_marks_the_track_and_moves_on(self):
        """F11: a non-ApiError failure only sent a notice; the queue stalled
        and the row kept spinning."""
        d = self.daemon()
        d.builder.fail["1"] = ConnectionError("the CDN hung up")
        await d._set_queue([song(1), song(2)], 0, {})
        await self.settle(d)
        self.assertEqual(d.index, 1)
        self.assertEqual(self.loaded, ["2"])
        self.assertEqual(d.build_state["1"]["stage"], "failed")
        self.assertIn("CDN", d.build_state["1"]["message"])
        self.assertIn(("notice", {"level": "error", "message": "the CDN hung up"}), self.sent)
        self.assertEqual(d._failures, 0)
        self.assertFalse(d.player.state["loading"])

    async def test_failed_entry_goes_on_retry_or_new_queue(self):
        d = self.daemon()
        d.builder.fail["1"] = StreamError("Region locked")
        await d._set_queue([song(1)], 0, {})
        await self.settle(d)
        self.assertEqual(d.build_state["1"]["stage"], "failed")
        # Tried again: the failed mark gives way to the new attempt.
        del d.builder.fail["1"]
        await d.cmd_jump({"index": 0})
        await self.settle(d)
        self.assertEqual(d.build_state, {})
        # A new queue drops failed marks of the old one.
        d.builder.fail["9"] = StreamError("Region locked")
        await d._set_queue([song(9), song(1)], 0, {})
        await self.settle(d)
        self.assertEqual(d.build_state["9"]["stage"], "failed")
        await d._set_queue([song(1)], 0, {})
        await self.settle(d)
        self.assertNotIn("9", d.build_state)

    async def test_failure_limit_stops_instead_of_walking_the_queue(self):
        d = self.daemon()
        tracks = [song(i) for i in range(1, 6)]
        for track in tracks:
            d.builder.fail[track["id"]] = StreamError("gone")
        await d._set_queue(tracks, 0, {})
        await self.settle(d)
        self.assertEqual(d.builder.calls, [str(i) for i in range(1, FAILURE_LIMIT + 1)])
        self.assertTrue(d.player.state["idle"])
        self.assertEqual({k for k, v in d.build_state.items() if v["stage"] == "failed"},
                         {"1", "2", "3"})

    async def test_not_signed_in_is_a_failed_entry(self):
        d = self.daemon()
        d.api.media_user_token = ""
        await d._set_queue([song(1)], 0, {})
        await self.settle(d)
        self.assertEqual(d.build_state["1"]["stage"], "failed")
        self.assertEqual(d.builder.calls, [])


class Prefetch(QueueCase):
    async def test_prefetch_failures_mark_only_real_ones(self):
        d = self.daemon(prefetch=2)
        d.builder.fail["2"] = ApiError("Network error", code="network")
        d.builder.fail["3"] = StreamError("No stream URL", code="unavailable")
        await d._set_queue([song(1), song(2), song(3)], 0, {})
        await self.settle(d)
        self.assertEqual(d.builder.calls, ["1", "2", "3"])
        self.assertNotIn("2", d.build_state)
        self.assertEqual(d.build_state["3"]["stage"], "failed")

    async def test_new_queue_cancels_the_old_prefetch(self):
        """F37: the old queue's prefetch kept downloading next to the new track."""
        d = self.daemon(prefetch=1)
        d.builder.gates["2"] = asyncio.Event()  # never opens
        await d._set_queue([song(1), song(2)], 0, {})
        await self.settle(d)
        old = d._prefetch_task
        self.assertEqual(d._prefetching, "2")
        await d._set_queue([song(7)], 0, {})
        await self.settle(d)
        self.assertTrue(old.cancelled())
        self.assertNotIn("2", d.build_state)
        self.assertEqual(self.loaded, ["1", "7"])

    async def test_prefetch_of_the_wanted_track_is_kept(self):
        """Next while the next track is being prefetched: the download goes on
        and the new build takes its file instead of starting over."""
        d = self.daemon(prefetch=1)
        gate = d.builder.gates["2"] = asyncio.Event()
        await d._set_queue([song(1), song(2), song(3)], 0, {})
        await self.settle(d)
        prefetch = d._prefetch_task
        await d.cmd_next({})
        await self.settle(d)
        self.assertFalse(prefetch.done())
        self.assertEqual(d.build_state["2"]["stage"], "downloading")
        gate.set()
        await self.settle(d)
        self.assertEqual(self.loaded, ["1", "2"])
        self.assertFalse(prefetch.cancelled())
        self.assertNotIn("2", d.build_state)

    async def test_prefetch_reads_its_songs_once(self):
        d = self.daemon(prefetch=2)
        d.queue = [song(1), song(2), song(3)]
        d.index = 0
        gate = d.builder.gates["2"] = asyncio.Event()
        task = asyncio.create_task(d._prefetch())
        await self.settle(d)
        # The position moves on (as _play_current does): the stale prefetch
        # finishes the song it is on and stops.
        d.queue = [song(8), song(9)]
        d._generation += 1
        gate.set()
        await task
        self.assertEqual(d.builder.calls, ["2"])


class DuringBuild(QueueCase):
    async def test_toggle_during_build_keeps_the_build_and_the_pause(self):
        """F33: toggle restarted the build (mpv is idle meanwhile) and the
        track then played regardless."""
        d = self.daemon()
        gate = d.builder.gates["1"] = asyncio.Event()
        await d._set_queue([song(1)], 0, {})
        await self.settle(d)
        task = d._build_task
        await d.cmd_toggle({})
        await d.cmd_toggle({})
        await d.cmd_toggle({})
        self.assertIs(d._build_task, task)
        self.assertEqual(d.builder.calls, ["1"])
        gate.set()
        await self.settle(d)
        self.assertEqual(self.loaded, ["1"])
        self.assertTrue(d.player.state["paused"])
        self.assertFalse(d._pending_pause)

    async def test_resume_during_build_cancels_a_pending_pause(self):
        d = self.daemon()
        gate = d.builder.gates["1"] = asyncio.Event()
        await d._set_queue([song(1)], 0, {})
        await self.settle(d)
        await d.cmd_pause({})
        await d.cmd_resume({})
        gate.set()
        await self.settle(d)
        self.assertFalse(d.player.state["paused"])
        self.assertEqual(d.builder.calls, ["1"])

    async def test_pause_during_build_silences_the_old_track(self):
        d = self.daemon()
        d.player.state.update({"idle": False, "paused": False, "songId": "0"})
        d.builder.gates["1"] = asyncio.Event()
        await d._set_queue([song(1)], 0, {})
        await self.settle(d)
        await d.cmd_pause({})
        d.player.set_paused.assert_awaited_with(True)
        self.assertTrue(d._pending_pause)

    async def test_seek_while_preparing_leaves_the_old_track(self):
        d = self.daemon()
        d.player.state["loading"] = True
        await d.cmd_seek({"pos": 30})
        d.player.seek.assert_not_called()


class Previous(QueueCase):
    async def test_previous_on_the_first_track_restarts_it(self):
        """F34: it stopped the player instead."""
        d = self.daemon()
        d.queue, d.index = [song(1), song(2)], 0
        d.player.state.update({"idle": False, "pos": 1.0, "songId": "1"})
        result = await d.cmd_previous({})
        self.assertEqual(result, {"index": 0})
        d.player.seek.assert_awaited_once_with(0)
        d.player.stop.assert_not_called()

    async def test_previous_on_the_first_track_with_another_file_loaded_plays_it(self):
        d = self.daemon()
        d.queue, d.index = [song(1), song(2)], 0
        d.player.state.update({"idle": False, "pos": 1.0, "songId": "9"})
        await d.cmd_previous({})
        await self.settle(d)
        d.player.seek.assert_not_called()
        self.assertEqual(self.loaded, ["1"])

    async def test_previous_on_an_idle_first_track_plays_it(self):
        d = self.daemon()
        d.queue, d.index = [song(1), song(2)], 0
        d.player.state.update({"idle": True, "pos": 0.0})
        await d.cmd_previous({})
        await self.settle(d)
        self.assertEqual(self.loaded, ["1"])
        d.player.stop.assert_not_called()

    async def test_previous_on_a_building_first_track_leaves_it_alone(self):
        d = self.daemon()
        d.builder.gates["1"] = asyncio.Event()
        await d._set_queue([song(1), song(2)], 0, {})
        await self.settle(d)
        task = d._build_task
        await d.cmd_previous({})
        self.assertIs(d._build_task, task)
        d.player.stop.assert_not_called()

    async def test_previous_while_preparing_goes_back_not_rewinds(self):
        """pos then belongs to the old file."""
        d = self.daemon()
        d.player.state.update({"idle": False, "pos": 100.0})
        d.builder.gates["2"] = asyncio.Event()
        await d._set_queue([song(1), song(2)], 1, {})
        await self.settle(d)
        await d.cmd_previous({})
        self.assertEqual(d.index, 0)
        d.player.seek.assert_not_called()

    async def test_previous_mid_queue_still_steps_back(self):
        d = self.daemon()
        d.queue, d.index = [song(1), song(2)], 1
        d.player.state.update({"idle": False, "pos": 1.0})
        await d.cmd_previous({})
        self.assertEqual(d.index, 0)


class Shuffle(QueueCase):
    async def test_shuffled_play_starts_with_the_chosen_track_then_all_others(self):
        """F35: tracks before the clicked one were never played."""
        d = self.daemon()
        d.shuffle = True
        tracks = [song(i) for i in range(12)]
        await d._set_queue(tracks, 3, {})
        self.assertEqual(d.index, 0)
        self.assertEqual(d.queue[0]["id"], "3")
        self.assertEqual(sorted(t["id"] for t in d.queue), sorted(t["id"] for t in tracks))

    async def test_shuffle_mid_queue_keeps_what_played(self):
        d = self.daemon()
        d.queue, d.index = [song(i) for i in range(6)], 2
        await d.cmd_shuffle({"on": True})
        self.assertEqual([t["id"] for t in d.queue[:3]], ["0", "1", "2"])


class Socket(QueueCase):
    async def open(self, d: Daemon):
        path = str(self.root / "run" / "t.sock")
        server = await asyncio.start_unix_server(d.handle, path=path)
        reader, writer = await asyncio.open_unix_connection(path)
        self.addAsyncCleanup(self._close, server, writer)
        return reader, writer

    async def _close(self, server, writer):
        writer.close()
        server.close()

    async def send(self, writer, request_id, cmd, **params):
        writer.write((json.dumps({"id": request_id, "cmd": cmd, "params": params}) + "\n").encode())
        await writer.drain()

    async def replies(self, reader, count: int, timeout: float = 3.0) -> list:
        got = []
        while len(got) < count:
            message = json.loads(await asyncio.wait_for(reader.readline(), timeout))
            if "id" in message:
                got.append(message)
        return got

    async def test_transport_is_not_stuck_behind_a_slow_request(self):
        """F12: a toggle waited for every earlier search or page load."""
        d = self.daemon()
        gate = asyncio.Event()

        async def slow_home(params):
            await gate.wait()
            return {"home": True}
        d.cmd_home = slow_home
        d.queue, d.index = [song(1)], 0
        d.player.state.update({"idle": False})
        reader, writer = await self.open(d)
        await self.send(writer, 1, "home")
        await self.send(writer, 2, "toggle")
        first = (await self.replies(reader, 1))[0]
        self.assertEqual(first["id"], 2)
        gate.set()
        second = (await self.replies(reader, 1))[0]
        self.assertEqual(second, {"id": 1, "ok": True, "result": {"home": True}})

    async def test_state_commands_keep_their_order(self):
        d = self.daemon()
        order = []

        async def advance(direction, automatic=False):
            await asyncio.sleep(0.01 if direction > 0 else 0)
            order.append(direction)
        d._advance = advance
        d.queue, d.index = [song(1), song(2), song(3)], 1
        reader, writer = await self.open(d)
        await self.send(writer, 1, "next")
        await self.send(writer, 2, "next")
        await self.send(writer, 3, "previous")
        await self.replies(reader, 3)
        self.assertEqual(order, [1, 1, -1])

    async def test_reads_are_cancelled_when_the_client_goes(self):
        d = self.daemon()
        cancelled = asyncio.Event()

        async def slow_search(params):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
        d.cmd_search = slow_search
        reader, writer = await self.open(d)
        await self.send(writer, 1, "search", term="x")
        await asyncio.sleep(0.05)
        writer.close()
        await asyncio.wait_for(cancelled.wait(), 3)

    async def test_clear_cache_keeps_the_file_mpv_has(self):
        d = self.daemon()
        d.player.state["songId"] = "5"
        await d.cmd_clearCache({})
        self.assertEqual(d.builder.cleared, [{d.builder.cached_path("5")}])


class Serve(QueueCase):
    """serve() for real on a temp runtime dir, with mpv and the account
    check stood in for."""

    async def test_socket_opens_before_the_account_check_and_quit_exits(self):
        d = self.daemon()
        d.api = None
        d.builder = None
        checking = asyncio.Event()
        finish = asyncio.Event()

        async def refresh():
            checking.set()
            await finish.wait()
            d.api = mock.Mock(media_user_token="token")
            d.api.home = mock.AsyncMock(return_value={"rows": []})
            return True
        d._refresh_api = refresh
        stop = asyncio.Event()
        serving = asyncio.create_task(d.serve(stop))
        await asyncio.wait_for(checking.wait(), 3)
        path = config.socket_path()
        self.assertTrue(path.exists(), "the socket must not wait for the network")
        reader, writer = await asyncio.open_unix_connection(str(path))
        hello = json.dumps({"id": 1, "cmd": "hello", "params": {}}) + "\n"
        writer.write(hello.encode())
        writer.write((json.dumps({"id": 2, "cmd": "home", "params": {}}) + "\n").encode())
        writer.write((json.dumps({"id": 3, "cmd": "volume", "params": {"value": 40}}) + "\n").encode())
        await writer.drain()
        got = {}
        while 3 not in got:
            message = json.loads(await asyncio.wait_for(reader.readline(), 3))
            if "id" in message:
                got[message["id"]] = message
        self.assertFalse(got[1]["result"]["snapshot"]["engine"]["ready"])
        self.assertNotIn(2, got)  # home waits for the account check
        finish.set()
        while 2 not in got:
            message = json.loads(await asyncio.wait_for(reader.readline(), 3))
            if "id" in message:
                got[message["id"]] = message
        self.assertEqual(got[2]["result"], {"rows": []})
        self.assertTrue(d.ready)

        # C2: quit answers, then the daemon leaves serve with the shell
        # still connected (it used to wait for the connection forever).
        writer.write((json.dumps({"id": 4, "cmd": "quit", "params": {}}) + "\n").encode())
        await writer.drain()
        while 4 not in got:
            line = await asyncio.wait_for(reader.readline(), 3)
            if not line:
                break
            message = json.loads(line)
            if "id" in message:
                got[message["id"]] = message
        self.assertEqual(got[4]["result"], {"quitting": True})
        await asyncio.wait_for(serving, 5)
        self.assertEqual(await asyncio.wait_for(reader.read(), 3), b"")
        await d.shutdown()
        d.player.terminate.assert_awaited_once()
        self.assertFalse(path.exists())
        self.assertFalse((path.parent / "daemon.pid").exists())
        writer.close()

    async def test_losing_launcher_leaves_the_owners_files(self):
        d = self.daemon()
        owner = d._take_lock(config.socket_path())
        try:
            path = config.socket_path()
            path.write_text("")
            (path.parent / "daemon.pid").write_text("1\n")
            other = self.daemon()
            await other.serve(asyncio.Event())
            await other.shutdown()
            self.assertTrue(path.exists())
            self.assertTrue((path.parent / "daemon.pid").exists())
        finally:
            owner.close()

    async def test_commands_answer_cleanly_if_startup_never_finishes(self):
        d = self.daemon()
        d.api = None
        d._session_task = asyncio.create_task(asyncio.sleep(30))
        with mock.patch.object(daemon_module, "STARTUP_WAIT", 0.05):
            with self.assertRaises(ApiError) as caught:
                await d.cmd_home({})
        self.assertEqual(caught.exception.code, "starting")
        d._session_task.cancel()

    async def test_play_during_startup_waits_instead_of_saying_signed_out(self):
        d = self.daemon()
        real_api = d.api
        d.api = None
        finish = asyncio.Event()

        async def session():
            await finish.wait()
            d.api = real_api
        d._session_task = asyncio.create_task(session())
        await d._set_queue([song(1)], 0, {})
        await asyncio.sleep(0.02)
        self.assertEqual(self.loaded, [])
        finish.set()
        await self.settle(d)
        self.assertEqual(self.loaded, ["1"])
        self.assertEqual(d.build_state, {})



class SameTrack(QueueCase):
    """G3: a second click on the row being prepared restarted its download."""

    async def test_playing_the_track_being_built_keeps_the_build(self):
        d = self.daemon()
        gate = d.builder.gates["1"] = asyncio.Event()
        await d.cmd_play({"tracks": [song(1), song(2)], "index": 0})
        await self.settle(d)
        task = d._build_task
        await d.cmd_play({"tracks": [song(1), song(2)], "index": 0})
        await d.cmd_jump({"index": 0})
        await self.settle(d)
        self.assertIs(d._build_task, task)
        self.assertFalse(task.done())
        self.assertEqual(d.builder.calls, ["1"])
        gate.set()
        await self.settle(d)
        self.assertEqual(self.loaded, ["1"])

    async def test_a_play_still_wins_over_a_pending_pause(self):
        d = self.daemon()
        gate = d.builder.gates["1"] = asyncio.Event()
        await d.cmd_play({"tracks": [song(1)], "index": 0})
        await self.settle(d)
        await d.cmd_pause({})
        await d.cmd_play({"tracks": [song(1)], "index": 0})
        gate.set()
        await self.settle(d)
        self.assertFalse(d.player.state["paused"])
        self.assertEqual(d.builder.calls, ["1"])

    async def test_a_track_already_in_mpv_restarts(self):
        d = self.daemon()
        await d.cmd_play({"tracks": [song(1)], "index": 0})
        await self.settle(d)
        await d.cmd_play({"tracks": [song(1)], "index": 0})
        await self.settle(d)
        self.assertEqual(d.builder.calls, ["1", "1"])
        self.assertEqual(self.loaded, ["1", "1"])

    async def test_another_track_still_cancels_the_build(self):
        d = self.daemon()
        d.builder.gates["1"] = asyncio.Event()  # never opens
        await d.cmd_play({"tracks": [song(1), song(2)], "index": 0})
        await self.settle(d)
        first = d._build_task
        await d.cmd_jump({"index": 1})
        await self.settle(d)
        self.assertTrue(first.cancelled())
        self.assertEqual(self.loaded, ["2"])


class PauseAcrossFailure(QueueCase):
    async def test_pause_during_a_failing_build_carries_to_the_next_track(self):
        """G4: the automatic advance after a failed build unpaused."""
        d = self.daemon()
        d.builder.gates["1"] = gate = asyncio.Event()
        d.builder.fail["1"] = StreamError("Region locked")
        await d._set_queue([song(1), song(2)], 0, {})
        await self.settle(d)
        await d.cmd_pause({})
        gate.set()
        await self.settle(d)
        self.assertEqual(self.loaded, ["2"])
        self.assertTrue(d.player.state["paused"])
        self.assertFalse(d._pending_pause)

    async def test_a_user_skip_still_drops_the_pause(self):
        d = self.daemon()
        d.builder.gates["1"] = asyncio.Event()  # never opens
        await d._set_queue([song(1), song(2)], 0, {})
        await self.settle(d)
        await d.cmd_pause({})
        await d.cmd_next({})
        await self.settle(d)
        self.assertEqual(self.loaded, ["2"])
        self.assertFalse(d.player.state["paused"])


class StopAtTheEnd(QueueCase):
    async def test_next_on_the_last_track_while_it_builds_stays_stopped(self):
        """G16: the build went on after the stop and the track started."""
        d = self.daemon(prefetch=1)
        gate = d.builder.gates["2"] = asyncio.Event()
        await d._set_queue([song(1), song(2)], 1, {})
        await self.settle(d)
        task = d._build_task
        await d.cmd_next({})
        gate.set()
        await self.settle(d)
        self.assertTrue(task.cancelled())
        self.assertEqual(self.loaded, [])
        self.assertTrue(d.player.state["idle"])
        self.assertNotIn("2", d.build_state)


class InUse(QueueCase):
    async def test_songs_in_use_are_the_player_the_position_and_the_window(self):
        """G7: prune is told what is really in use, not the last dozen builds."""
        d = self.daemon(prefetch=2)
        d.queue = [song(i) for i in range(1, 7)]
        d.index = 1
        d.player.state["songId"] = "9"
        d._prefetching = "4"
        self.assertEqual(d._songs_in_use(), {"9", "2", "3", "4"})
        d.settings["prefetch"] = 0
        self.assertEqual(d._songs_in_use(), {"9", "2", "4"})

    async def test_the_builder_made_at_sign_in_asks_the_daemon(self):
        d = Daemon()
        d.account_record = {"mediaUserToken": "", "signedOut": True}
        await d._refresh_api()
        self.assertEqual(d.builder.in_use, d._songs_in_use)


class PlayerGone(QueueCase):
    async def test_mpv_going_away_mid_handover_is_not_a_track_failure(self):
        """G15: a crash (or restart) during load marked the track failed and
        skipped to the next one."""
        from engine.player import PlayerReset

        d = self.daemon()
        d.player.load = mock.AsyncMock(side_effect=PlayerReset("gone"))
        await d._set_queue([song(1), song(2)], 0, {})
        await self.settle(d)
        self.assertEqual(d.index, 0)
        self.assertEqual(d.builder.calls, ["1"])
        self.assertNotIn("1", d.build_state)
        self.assertEqual(d._failures, 0)
        self.assertFalse([data for event, data in self.sent if event == "notice"])
        self.assertFalse(d.player.state["loading"])

    async def test_restart_player_during_a_build_replays_the_same_track(self):
        d = self.daemon()
        gate = d.builder.gates["1"] = asyncio.Event()
        await d._set_queue([song(1), song(2)], 0, {})
        await self.settle(d)
        first = d._build_task
        await d.cmd_restartPlayer({})
        await self.settle(d)
        self.assertTrue(first.cancelled())
        gate.set()
        await self.settle(d)
        self.assertEqual(d.index, 0)
        self.assertEqual(self.loaded, ["1"])
        self.assertNotIn("1", d.build_state)
        d.player.terminate.assert_awaited_once()

    async def test_restart_player_keeps_the_position(self):
        d = self.daemon()
        await d._set_queue([song(1)], 0, {})
        await self.settle(d)
        d.player.state["pos"] = 42.0
        await d.cmd_restartPlayer({})
        await self.settle(d)
        self.assertEqual(self.loaded, ["1", "1"])
        self.assertEqual(self.starts[-1], 42.0)


class AfterACrash(QueueCase):
    async def test_play_after_a_crash_picks_up_where_it_was(self):
        """G0: play after mpv died restarted the track from 0:00."""
        d = self.daemon()
        await d._set_queue([song(1), song(2)], 0, {})
        await self.settle(d)
        d.player.state.update({"idle": True, "paused": True, "pos": 0.0})
        d.player._resume = ("1", 90.0)
        await d.cmd_toggle({})
        await self.settle(d)
        self.assertEqual(self.loaded, ["1", "1"])
        self.assertEqual(self.starts[-1], 90.0)

    async def test_a_crash_position_belongs_to_its_own_song(self):
        d = self.daemon()
        await d._set_queue([song(1), song(2)], 0, {})
        await self.settle(d)
        d.player.state.update({"idle": True, "paused": True})
        d.player._resume = ("2", 90.0)
        await d.cmd_resume({})
        await self.settle(d)
        self.assertEqual(self.starts[-1], 0.0)


def shell_config(*, layout=None, plugins=None, disabled=None) -> dict:
    data = {"bar": {"layout": layout or {"right": [{"id": "omarchy.clock"}]}},
            "plugins": plugins or []}
    if disabled is not None:
        data["disabledPlugins"] = disabled
    return data


ENABLED = shell_config(layout={"right": [{"id": config.PLUGIN_ID}]})
DISABLED = shell_config(layout={"right": [{"id": config.PLUGIN_ID}]}, disabled=[config.PLUGIN_ID])


class EnabledRule(QueueCase):
    """The daemon runs only while the shell's config enables the plugin. It
    replaces the quit-requested marker, `call quit --wait` and the checks
    serve() made for them, which raced each other in every order tried."""

    def write_config(self, data) -> Path:
        path = config.shell_config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, dict) and "version" not in data:
            data = {"version": 1, **data}
        text = data if isinstance(data, str) else json.dumps(data)
        # A different size and a later mtime each time, as a real save has.
        temp = path.with_name("shell.json.tmp")
        temp.write_text(text, "utf-8")
        os.replace(temp, path)
        return path

    def setUp(self):
        super().setUp()
        poll = mock.patch.object(daemon_module, "ENABLED_POLL_SECONDS", 0.05)
        poll.start()
        self.addCleanup(poll.stop)

    async def start_serving(self, d):
        d._refresh_api = mock.AsyncMock(return_value=False)
        stop = asyncio.Event()
        serving = asyncio.create_task(d.serve(stop))
        for _ in range(100):
            if config.socket_path().exists() or serving.done():
                break
            await asyncio.sleep(0.02)
        return stop, serving

    async def finish(self, d, stop, serving):
        if not serving.done():
            stop.set()
        await asyncio.wait_for(serving, 3)
        await d.shutdown()

    async def test_disabled_at_start_leaves_before_mpv_and_touches_nothing(self):
        self.write_config(DISABLED)
        path = config.socket_path()
        # What a daemon that is running would own.
        path.write_text("")
        config.pid_path().write_text("4242\n")
        d = self.daemon()
        await asyncio.wait_for(d.serve(asyncio.Event()), 2)
        await d.shutdown()
        d.player.start.assert_not_called()
        self.assertTrue(path.exists())
        self.assertEqual(config.pid_path().read_text(), "4242\n")
        self.assertFalse(Path(str(path) + ".lock").exists(), "not even the lock was taken")

    async def test_no_config_or_a_broken_one_means_enabled(self):
        for data in (None, "{\"bar\": {\"layout\":", "[]"):
            if data is not None:
                self.write_config(data)
            d = self.daemon()
            stop, serving = await self.start_serving(d)
            self.assertFalse(serving.done(), f"stopped on {data!r}")
            d.player.start.assert_awaited()
            await self.finish(d, stop, serving)

    async def test_being_disabled_while_serving_ends_the_daemon(self):
        self.write_config(ENABLED)
        d = self.daemon()
        stop, serving = await self.start_serving(d)
        self.assertFalse(serving.done())
        reader, writer = await asyncio.open_unix_connection(str(config.socket_path()))
        self.write_config(DISABLED)
        await asyncio.wait_for(serving, 3)
        # The shell's connection is closed, as quit closes it.
        self.assertEqual(await asyncio.wait_for(reader.read(), 3), b"")
        writer.close()
        await d.shutdown()
        d.player.terminate.assert_awaited_once()
        self.assertFalse(config.socket_path().exists())
        self.assertFalse(config.pid_path().exists())

    async def test_a_deleted_plugin_directory_ends_the_daemon(self):
        # Deleting the plugin (or its dev symlink) leaves the id in the
        # config; the Service goes with the manifest, so the daemon must see
        # it for itself. One missing look is not enough, two are.
        plugin = self.root / "plugin"
        plugin.mkdir()
        (plugin / "manifest.json").write_text("{}", "utf-8")
        env = mock.patch.dict(os.environ, {"APPLE_MUSIC_PLUGIN_ROOT": str(plugin)})
        env.start()
        self.addCleanup(env.stop)
        self.write_config(ENABLED)
        d = self.daemon()
        stop, serving = await self.start_serving(d)
        await asyncio.sleep(0.2)
        self.assertFalse(serving.done())
        (plugin / "manifest.json").unlink()
        await asyncio.wait_for(serving, 3)
        await d.shutdown()
        d.player.terminate.assert_awaited_once()

    async def test_a_manifest_missing_for_one_look_does_not_stop_it(self):
        plugin = self.root / "plugin"
        plugin.mkdir()
        manifest = plugin / "manifest.json"
        manifest.write_text("{}", "utf-8")
        env = mock.patch.dict(os.environ, {"APPLE_MUSIC_PLUGIN_ROOT": str(plugin)})
        env.start()
        self.addCleanup(env.stop)
        self.write_config(ENABLED)
        d = self.daemon()
        stop, serving = await self.start_serving(d)
        manifest.unlink()
        await asyncio.sleep(0.03)  # under one poll interval: at most one missing look
        manifest.write_text("{}", "utf-8")
        await asyncio.sleep(0.3)
        self.assertFalse(serving.done(), "an update moving the directory aside must not stop it")
        await self.finish(d, stop, serving)

    async def test_taken_out_of_the_layout_counts_as_disabled(self):
        self.write_config(ENABLED)
        d = self.daemon()
        stop, serving = await self.start_serving(d)
        self.write_config(shell_config())
        await asyncio.wait_for(serving, 3)
        await d.shutdown()

    async def test_a_half_written_save_and_other_edits_do_not_stop_it(self):
        self.write_config(ENABLED)
        d = self.daemon()
        stop, serving = await self.start_serving(d)
        self.write_config('{"bar": {"layout": {"right": [')
        await asyncio.sleep(0.25)
        self.write_config(shell_config(layout={"left": [{"id": "omarchy.menu"}],
                                               "right": [config.PLUGIN_ID]}))
        await asyncio.sleep(0.25)
        os.unlink(config.shell_config_path())
        await asyncio.sleep(0.25)
        self.assertFalse(serving.done())
        await self.finish(d, stop, serving)

    async def test_a_failing_read_is_no_answer_not_a_disable(self):
        self.write_config(ENABLED)
        d = self.daemon()
        stop, serving = await self.start_serving(d)
        with mock.patch.object(config, "plugin_enabled", side_effect=RuntimeError("odd")), \
                self.assertLogs("apple-music.daemon", "WARNING"):
            self.write_config(DISABLED)
            await asyncio.sleep(0.25)
        self.assertFalse(serving.done())
        # Read again on the next look, and this time it answers.
        await asyncio.wait_for(serving, 3)
        await d.shutdown()

    async def test_disabled_while_mpv_starts_ends_it_once_listening(self):
        self.write_config(ENABLED)
        d = self.daemon()
        real_startup = d.startup

        async def startup():
            await real_startup()
            self.write_config(DISABLED)
            await asyncio.sleep(0.2)  # the watcher has seen it by now
        d.startup = startup
        d._refresh_api = mock.AsyncMock(return_value=False)
        await asyncio.wait_for(d.serve(asyncio.Event()), 3)
        await d.shutdown()
        d._refresh_api.assert_not_called()
        self.assertFalse(config.socket_path().exists())

    async def test_shutdown_stops_the_watch(self):
        self.write_config(ENABLED)
        d = self.daemon()
        stop, serving = await self.start_serving(d)
        await self.finish(d, stop, serving)
        await asyncio.sleep(0)
        watching = [task for task in asyncio.all_tasks() if not task.done()
                    and getattr(task.get_coro(), "__qualname__", "") == "Daemon._watch_enabled"]
        self.assertEqual(watching, [])

    async def test_hello_says_which_process_answers(self):
        """H6/H11: a Service that retired an older daemon tells the new one
        from the old one still closing by its pid."""
        d = self.daemon()
        writer = mock.Mock()
        await d._dispatch({"id": 1, "cmd": "hello"}, writer)
        reply = json.loads(writer.write.call_args[0][0])
        self.assertEqual(reply["result"]["pid"], os.getpid())
        self.assertEqual(reply["result"]["protocol"], daemon_module.PROTOCOL)


class CacheWithoutASession(QueueCase):
    """H2: the builder was made by the account check, so a sign-out that
    cancelled the check left clearCache and the cache stats with nothing."""

    async def test_the_builder_exists_before_any_session(self):
        d = Daemon()
        self.assertIsNotNone(d.builder)
        self.assertEqual(d.builder.in_use, d._songs_in_use)
        self.assertIs(d.builder.api, d.api)
        self.assertEqual(d.api.media_user_token, "")
        self.assertEqual((await d.cmd_cache({}))["tracks"], 0)

    async def test_the_session_check_moves_the_builder_to_its_api(self):
        d = Daemon()
        builder = d.builder
        d.account_record = {"mediaUserToken": "", "signedOut": True}
        await d._refresh_api()
        self.assertIs(d.builder, builder)
        self.assertIs(d.builder.api, d.api)

    async def test_cache_answers_while_the_check_hangs_and_after_it_is_cancelled(self):
        d = Daemon()
        d._session_task = asyncio.create_task(asyncio.sleep(30))
        stats = await asyncio.wait_for(d.cmd_cache({}), 1)
        self.assertEqual(stats["limitMb"], 1024)
        d._session_task.cancel()
        await asyncio.wait({d._session_task}, timeout=1)
        audio = config.audio_dir() / "1.m4a"
        audio.write_bytes(b"x")
        self.assertEqual((await d.cmd_cache({}))["tracks"], 1)
        self.assertEqual((await d.cmd_clearCache({}))["removed"], 1)
        self.assertFalse(audio.exists())


class Protocol(unittest.TestCase):
    def test_protocol_and_version(self):
        """G22: the Service replaces a daemon whose protocol or version
        differs from its own; both must move with the code."""
        from engine import __version__

        manifest = json.loads((ROOT / "manifest.json").read_text("utf-8"))
        self.assertEqual(daemon_module.PROTOCOL, 2)
        self.assertEqual(__version__, manifest["version"])
        self.assertEqual(daemon_module.ENGINE_VERSION, manifest["version"])


if __name__ == "__main__":
    unittest.main()
