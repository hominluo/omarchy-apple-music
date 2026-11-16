"""Player lifecycle: start/release/terminate races, crashes, repeat-one,
volume and the preparing flag.

mpv is replaced by tests/fake_mpv.py on PATH, and every path the engine
touches (runtime dir, data dir, cache) points into a temp dir, so nothing here
talks to the live daemon, a real mpv or Apple.
"""

import asyncio
import json
import logging
import os
import signal
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import player as player_module  # noqa: E402
from engine.player import Player, clamp_volume  # noqa: E402

FAKE_MPV = Path(__file__).resolve().parent / "fake_mpv.py"
# The crashes below are provoked on purpose; their warnings are not news.
logging.getLogger("apple-music").addHandler(logging.NullHandler())


async def wait_until(condition, timeout: float = 5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if condition():
            return True
        await asyncio.sleep(0.02)
    return condition()


async def wait_until_true(attempt, timeout: float = 5.0) -> bool:
    """Retry an async check until it says True."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await attempt():
            return True
        await asyncio.sleep(0.02)
    return False


def poll_loops() -> int:
    return sum(1 for task in asyncio.all_tasks()
               if not task.done()
               and getattr(task.get_coro(), "__qualname__", "") == "Player._poll_loop")


class FakeMpvCase(unittest.IsolatedAsyncioTestCase):
    """Each test gets its own runtime dir and a fake mpv first on PATH."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="am-test-")
        root = Path(self.tmp.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        mpv = bin_dir / "mpv"
        mpv.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I "{FAKE_MPV}" "$@"\n', "utf-8")
        mpv.chmod(0o755)
        self.command_log = root / "commands.log"
        for name in ("run", "data", "cache"):
            (root / name).mkdir()
        self.env = mock.patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(root / "run"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_MPV_LOG": str(self.command_log),
            "FAKE_MPV_DELAY": "0",
        })
        self.env.start()
        # The MPRIS script is irrelevant to the fake and would be a real path.
        self.mpris = mock.patch.object(player_module, "mpris_script", return_value=None)
        self.mpris.start()
        self.audio = root / "audio"
        self.audio.mkdir()

    async def asyncSetUp(self):
        self.events = []
        self.finished = 0
        self.exits = []
        self.errors = []
        self.player = Player(on_change=self.events.append, on_finished=self._finished,
                             on_exit=self.exits.append, on_error=self.errors.append)

    def _finished(self):
        self.finished += 1

    async def asyncTearDown(self):
        await self.player.terminate()
        socket_path = str(Path(os.environ["XDG_RUNTIME_DIR"]) / "omarchy-apple-music" / "mpv.sock")
        for pid in player_module._stale_players(socket_path):
            os.kill(pid, signal.SIGKILL)

    def tearDown(self):
        self.mpris.stop()
        self.env.stop()
        self.tmp.cleanup()

    def track(self, name: str = "song.m4a") -> str:
        path = self.audio / name
        path.write_bytes(b"\0")
        return str(path)

    def commands(self, name: str) -> list:
        if not self.command_log.exists():
            return []
        lines = [json.loads(line) for line in self.command_log.read_text("utf-8").splitlines()]
        return [command for command in lines if command and command[0] == name]


class StartAndRelease(FakeMpvCase):
    async def test_release_ends_its_poll_loop_and_never_kills_the_next_mpv(self):
        """F0: the released process's loop used to keep ticking and terminate
        the next mpv inside its start window, and each cycle leaked a loop."""
        self.player.idle_exit_seconds = 0.6
        await self.player.start()
        first = self.player.proc
        self.assertTrue(await wait_until(lambda: self.player.proc is None, 5))
        await asyncio.wait_for(first.wait(), 5)
        # Well past another idle window: a leftover loop would now fire on
        # every tick.
        await asyncio.sleep(1.5)
        self.assertEqual(poll_loops(), 0)
        await self.player.load(self.track(), "a")
        self.assertTrue(self.player.running)
        self.assertEqual(poll_loops(), 1)
        await asyncio.sleep(1.0)
        self.assertTrue(self.player.running, "the new mpv must survive the old timer")
        self.assertFalse(self.player.state["idle"])

    async def test_no_release_while_a_track_is_being_prepared(self):
        self.player.idle_exit_seconds = 0.3
        await self.player.start()
        self.player.begin_loading()
        await asyncio.sleep(1.0)
        self.assertTrue(self.player.running)

    async def test_concurrent_starts_spawn_one_mpv(self):
        await asyncio.gather(self.player.start(), self.player.start(), self.player.ensure())
        self.assertTrue(self.player.running)
        self.assertEqual(poll_loops(), 1)
        socket_path = self.player.socket_path
        self.assertEqual(len(player_module._stale_players(socket_path)), 1)

    async def test_cancel_during_start_leaves_nothing_behind(self):
        """F1: a next/jump cancelling the track task inside start() used to
        leave mpv running without a socket, wedging every later command."""
        os.environ["FAKE_MPV_DELAY"] = "0.4"
        task = asyncio.ensure_future(self.player.load(self.track(), "a"))
        self.assertTrue(await wait_until(lambda: self.player.proc is not None, 3))
        spawned = self.player.proc
        await asyncio.sleep(0.1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertIsNone(self.player.proc)
        self.assertIsNone(self.player.writer)
        await asyncio.wait_for(spawned.wait(), 3)
        self.assertEqual(poll_loops(), 0)
        os.environ["FAKE_MPV_DELAY"] = "0"
        await self.player.load(self.track(), "b")
        self.assertTrue(self.player.running)
        self.assertEqual(self.player.state["songId"], "b")
        self.assertEqual(len(self.commands("loadfile")), 1)

    async def test_mpv_that_exits_at_start_fails_fast(self):
        self.player._volume = 5000  # bypasses the clamp on purpose
        with mock.patch.object(player_module, "clamp_volume", lambda value, default=100: value):
            with self.assertRaises(RuntimeError) as caught:
                await asyncio.wait_for(self.player.start(), 5)
        self.assertIn("exited", str(caught.exception))
        self.assertIsNone(self.player.proc)


class TerminateAndStop(FakeMpvCase):
    async def test_terminate_is_the_lifecycle_stop(self):
        """F15: the lifecycle stop was shadowed by the playback stop, so
        restartPlayer and shutdown never ended mpv."""
        await self.player.load(self.track(), "a")
        proc = self.player.proc
        await self.player.terminate()
        self.assertIsNone(self.player.proc)
        self.assertIsNotNone(proc.returncode)
        self.assertIsNone(self.player.poll_task)
        self.assertIsNone(self.player.read_task)
        await asyncio.sleep(0.05)
        self.assertEqual(poll_loops(), 0)
        self.assertEqual(self.exits, [], "a requested stop is not a crash")
        await self.player.start()
        self.assertTrue(self.player.running)
        self.assertIsNot(self.player.proc, proc)

    async def test_playback_stop_keeps_mpv(self):
        await self.player.load(self.track(), "a")
        proc = self.player.proc
        await self.player.stop()
        self.assertIs(self.player.proc, proc)
        self.assertTrue(self.player.running)
        self.assertTrue(self.player.state["idle"])
        self.assertEqual(self.commands("stop"), [["stop"]])


class Crash(FakeMpvCase):
    async def test_crash_is_noticed_and_play_recovers(self):
        """F17: a dead mpv left the state saying playing and the transport
        talking to a closed socket."""
        await self.player.load(self.track(), "a")
        self.assertFalse(self.player.state["idle"])
        proc = self.player.proc
        os.kill(proc.pid, signal.SIGKILL)
        self.assertTrue(await wait_until(lambda: self.exits, 5))
        self.assertEqual(self.exits, [-signal.SIGKILL])
        self.assertFalse(self.player.running)
        self.assertIsNone(self.player.proc)
        self.assertTrue(self.player.state["idle"])
        self.assertTrue(self.player.state["paused"])
        self.assertEqual(self.player.state["songId"], "a")
        self.assertEqual(poll_loops(), 0)
        # The transport no longer pretends.
        self.assertFalse(await self.player.seek(30))
        await self.player.set_paused(False)
        self.assertTrue(self.player.state["paused"])
        # Play restarts mpv.
        await self.player.load(self.track(), "a")
        self.assertTrue(self.player.running)
        self.assertFalse(self.player.state["idle"])
        self.assertEqual(self.finished, 0, "a crash is not the end of the track")

    async def test_an_idle_mpv_dying_is_not_an_error(self):
        """G1: an idle mpv killed with nothing loaded sent 'press play to
        start the track again' when nothing was playing."""
        await self.player.start()
        os.kill(self.player.proc.pid, signal.SIGKILL)
        self.assertTrue(await wait_until(lambda: not self.player.running, 5))
        await asyncio.sleep(0.1)
        self.assertEqual(self.exits, [])
        self.assertTrue(self.player.state["idle"])
        self.assertEqual(self.player.resume_point("a"), 0.0)
        # The next play starts another mpv as before.
        await self.player.load(self.track(), "a")
        self.assertTrue(self.player.running)

    async def test_a_crash_keeps_the_position_for_the_next_play(self):
        """G0: the position was thrown away with the process."""
        await self.player.load(self.track(), "a")
        # mpv takes a seek only once the file has opened.
        self.assertTrue(await wait_until_true(lambda: self.player.seek(90)))
        os.kill(self.player.proc.pid, signal.SIGKILL)
        self.assertTrue(await wait_until(lambda: self.exits, 5))
        self.assertEqual(self.player.state["pos"], 0.0)
        self.assertEqual(self.player.resume_point("a"), 90.0)
        self.assertEqual(self.player.resume_point("b"), 0.0)
        await self.player.load(self.track(), "a", start=90.0)
        self.assertEqual(self.player.resume_point("a"), 0.0)
        # H0: where mpv really is, once it says so: the start went in the load.
        self.assertTrue(await wait_until(lambda: self.player.state["pos"] == 90.0, 3))

    async def test_a_crash_before_mpv_reports_keeps_the_start(self):
        """A restart's own crash in the gap before mpv reported a position
        must not throw the resume point away again."""
        os.environ["FAKE_MPV_OPEN_DELAY"] = "5"
        await self.player.load(self.track(), "a", start=75.0)
        self.assertEqual(self.player.position(), 75.0)
        os.kill(self.player.proc.pid, signal.SIGKILL)
        self.assertTrue(await wait_until(lambda: self.exits, 5))
        self.assertEqual(self.player.resume_point("a"), 75.0)

    async def test_mpv_going_away_mid_handover_is_a_reset_not_a_refusal(self):
        """G15: a handover cut off by a crash or a restart read as 'mpv did
        not accept the track', and the daemon skipped the track."""
        await self.player.start()
        real = self.player._command

        async def vanishing(command, timeout=5.0):
            if command and command[0] == "loadfile":
                self.player._detach()  # what _on_exit / terminate do meanwhile
                return None
            return await real(command, timeout)
        self.player._command = vanishing
        with self.assertRaises(player_module.PlayerReset):
            await self.player.load(self.track(), "a")

    async def test_a_refusal_from_a_live_mpv_is_still_a_failure(self):
        await self.player.start()
        real = self.player._command

        async def refusing(command, timeout=5.0):
            if command and command[0] == "loadfile":
                return {"error": "loading failed"}
            return await real(command, timeout)
        self.player._command = refusing
        with self.assertRaises(RuntimeError) as caught:
            await self.player.load(self.track(), "a")
        self.assertNotIsInstance(caught.exception, player_module.PlayerReset)

    async def test_file_mpv_cannot_open_is_reported(self):
        await self.player.load(self.track("corrupt.m4a"), "a")
        self.assertTrue(await wait_until(lambda: self.errors, 3))
        self.assertTrue(self.player.state["idle"])
        self.assertTrue(self.player.state["paused"])
        self.assertEqual(self.finished, 0)

    async def test_missing_file_is_refused(self):
        with self.assertRaises(RuntimeError):
            await self.player.load(str(self.audio / "gone.m4a"), "a")


class StartPosition(FakeMpvCase):
    """H0: crash resume and restartPlayer handed mpv the file, then seeked;
    mpv refuses a seek until the file has opened, so the track started from
    0:00 while the state said otherwise."""

    async def test_the_start_goes_in_the_load_and_nothing_seeks(self):
        await self.player.load(self.track(), "a", start=90.0)
        self.assertEqual(self.commands("loadfile")[-1][2:], ["replace", -1, "start=90.000"])
        self.assertEqual(self.commands("seek"), [])
        # Nothing claims 90 before mpv does; then mpv's own word is used.
        self.assertEqual(self.player.state["pos"], 0.0)
        self.assertEqual(self.player.position(), 90.0)
        self.assertTrue(await wait_until(lambda: self.player.state["pos"] == 90.0, 3))
        self.assertEqual(self.player.position(), 90.0)
        self.assertFalse(self.player.state["idle"])

    async def test_an_older_mpv_gets_the_options_one_place_earlier(self):
        os.environ["FAKE_MPV_LEGACY"] = "1"
        await self.player.load(self.track(), "a", start=42.5)
        loads = self.commands("loadfile")
        self.assertEqual([command[2:] for command in loads],
                         [["replace", -1, "start=42.500"], ["replace", "start=42.500"]])
        self.assertTrue(await wait_until(lambda: self.player.state["pos"] == 42.5, 3))

    async def test_a_refused_retry_is_still_a_refusal(self):
        await self.player.start()
        real = self.player._command
        sent = []

        async def refusing(command, timeout=5.0):
            if command and command[0] == "loadfile":
                sent.append(command)
                return {"error": "loading failed"}
            return await real(command, timeout)
        self.player._command = refusing
        with self.assertRaises(RuntimeError) as caught:
            await self.player.load(self.track(), "a", start=30.0)
        self.assertNotIsInstance(caught.exception, player_module.PlayerReset)
        self.assertEqual(len(sent), 2)
        self.assertIsNone(self.player._starting_at)

    async def test_no_retry_once_mpv_has_gone(self):
        await self.player.start()
        real = self.player._command
        sent = []

        async def vanishing(command, timeout=5.0):
            if command and command[0] == "loadfile":
                sent.append(command)
                self.player._detach()
                return None
            return await real(command, timeout)
        self.player._command = vanishing
        with self.assertRaises(player_module.PlayerReset):
            await self.player.load(self.track(), "a", start=30.0)
        self.assertEqual(len(sent), 1)

    async def test_from_the_top_is_a_plain_load(self):
        await self.player.load(self.track(), "a")
        await self.player.load(self.track(), "a", start=0.6)
        self.assertEqual([command[2:] for command in self.commands("loadfile")],
                         [["replace"], ["replace"]])
        self.assertEqual(self.player.position(), self.player.state["pos"])

    async def test_a_seek_or_a_stop_drops_the_pending_start(self):
        os.environ["FAKE_MPV_OPEN_DELAY"] = "0.3"
        await self.player.load(self.track(), "a", start=60.0)
        self.assertFalse(await self.player.seek(10), "mpv has not opened the file yet")
        self.assertEqual(self.player.position(), 60.0)
        self.assertTrue(await wait_until_true(lambda: self.player.seek(10)))
        self.assertEqual(self.player.position(), 10.0)
        await self.player.load(self.track(), "b", start=60.0)
        await self.player.stop()
        self.assertEqual(self.player.position(), 0.0)


class Replay(FakeMpvCase):
    async def test_replay_reloads_the_file_after_mpv_went_idle(self):
        """F16: repeat-one used to seek in an mpv that had unloaded the file."""
        path = self.track("short.m4a")
        await self.player.load(path, "a")
        self.assertTrue(await wait_until(lambda: self.finished == 1, 3))
        self.assertTrue(self.player.state["idle"])
        self.assertFalse(await self.player.seek(0), "the old fix had nothing to seek in")
        self.assertTrue(await self.player.replay("a"))
        self.assertEqual(len(self.commands("loadfile")), 2)
        self.assertFalse(self.player.state["idle"])
        self.assertFalse(self.player.state["paused"])
        # The flag that detects the end is armed again.
        self.assertTrue(await wait_until(lambda: self.finished == 2, 3))

    async def test_replay_declines_another_song_or_a_pruned_file(self):
        path = self.track()
        await self.player.load(path, "a")
        self.assertFalse(await self.player.replay("b"))
        os.unlink(path)
        self.assertFalse(await self.player.replay("a"))


class Loading(FakeMpvCase):
    async def test_load_clears_only_its_own_preparation(self):
        token_a = self.player.begin_loading()
        self.assertTrue(self.player.state["loading"])
        token_b = self.player.begin_loading()
        self.player.end_loading(token_a)  # a cancelled older build
        self.assertTrue(self.player.state["loading"])
        await self.player.load(self.track(), "b")
        self.assertFalse(self.player.state["loading"])
        self.assertEqual(self.player.state["songId"], "b")
        self.player.end_loading(token_b)  # already done by load: no-op
        self.assertFalse(self.player.state["loading"])

    async def test_failed_build_clears_the_flag(self):
        token = self.player.begin_loading()
        self.player.end_loading(token)
        await asyncio.sleep(0)
        self.assertFalse(self.player.state["loading"])
        self.assertFalse(self.events[-1]["loading"])

    async def test_songid_names_the_file_mpv_has_while_the_next_prepares(self):
        await self.player.load(self.track(), "a")
        self.player.begin_loading()
        self.assertEqual(self.player.state["songId"], "a")
        self.assertTrue(self.player.state["loading"])


class Volume(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(clamp_volume(0), 0)
        self.assertEqual(clamp_volume(-5), 0)
        self.assertEqual(clamp_volume(2000), 100)
        self.assertEqual(clamp_volume("55"), 55)
        self.assertEqual(clamp_volume(None), 100)
        self.assertEqual(clamp_volume("loud", 40), 40)
        self.assertEqual(clamp_volume(float("nan"), 30), 30)


if __name__ == "__main__":
    unittest.main()
