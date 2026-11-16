"""Daemon transport paths that sit on the player: volume and mute, repeat-one,
resume while idle, the preparing flag, restart and shutdown.

The player is never started here (its process methods are mocked) and every
engine path points into a temp dir.
"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import config  # noqa: E402
from engine.daemon import Daemon  # noqa: E402


class DaemonCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="am-test-")
        root = Path(self.tmp.name)
        for name in ("run", "data", "cache"):
            (root / name).mkdir()
        self.env = mock.patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(root / "run"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            # The shell's config lives under $HOME, not XDG_CONFIG_HOME.
            "HOME": str(root / "home"),
        })
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def write_settings(self, **values):
        (config.data_dir() / "settings.json").write_text(json.dumps(values), "utf-8")

    def saved(self) -> dict:
        return json.loads((config.data_dir() / "settings.json").read_text("utf-8"))

    def daemon(self) -> Daemon:
        daemon = Daemon()
        daemon.queue = [{"id": "1"}, {"id": "2"}]
        daemon.index = 0
        return daemon


class VolumeSettings(DaemonCase):
    def test_saved_zero_stays_muted(self):
        """F19: `volume or 100` turned a muted player into a full-volume one."""
        self.write_settings(volume=0)
        self.assertEqual(Daemon().volume, 0)

    def test_out_of_range_saved_volume_is_clamped(self):
        self.write_settings(volume=-5)
        self.assertEqual(Daemon().volume, 0)
        self.write_settings(volume=2000)
        self.assertEqual(Daemon().volume, 100)
        self.write_settings(volume="loud")
        self.assertEqual(Daemon().volume, 100)

    async def test_cmd_volume_saves_the_clamped_value(self):
        daemon = self.daemon()
        result = await daemon.cmd_volume({"value": 2000})
        self.assertEqual(result, {"volume": 100})
        self.assertEqual(self.saved()["volume"], 100)
        self.assertEqual(daemon.player.state["volume"], 100)
        await daemon.cmd_volume({"value": -5})
        self.assertEqual(self.saved()["volume"], 0)

    async def test_mute_remembers_and_restores_across_restarts(self):
        daemon = self.daemon()
        await daemon.cmd_volume({"value": 42})
        result = await daemon.cmd_mute({})
        self.assertEqual(result, {"volume": 0, "muted": True})
        self.assertEqual(self.saved()["volume"], 0)
        self.assertEqual(self.saved()["muteRestore"], 42)
        # A new daemon (shell restart) comes back muted and unmutes to 42.
        again = self.daemon()
        self.assertEqual(again.volume, 0)
        again.player.state["volume"] = again.volume
        result = await again.cmd_mute({})
        self.assertEqual(result, {"volume": 42, "muted": False})
        self.assertEqual(self.saved()["volume"], 42)

    async def test_unmute_without_memory_goes_to_70(self):
        daemon = self.daemon()
        await daemon.cmd_volume({"value": 0})
        result = await daemon.cmd_mute({})
        self.assertEqual(result["volume"], 70)

    async def test_prefetch_zero_turns_it_off(self):
        self.write_settings(prefetch=0)
        daemon = self.daemon()
        daemon.builder = mock.Mock()
        daemon.builder.build = mock.AsyncMock()
        await daemon._prefetch()
        daemon.builder.build.assert_not_called()


class RepeatOne(DaemonCase):
    async def test_repeat_one_replays_instead_of_seeking(self):
        """F16: the seek went to an mpv that had already unloaded the file."""
        daemon = self.daemon()
        daemon.repeat = "one"
        daemon.player.replay = mock.AsyncMock(return_value=True)
        daemon.player.seek = mock.AsyncMock()
        daemon._play_current = mock.AsyncMock()
        await daemon._track_finished()
        daemon.player.replay.assert_awaited_once_with("1")
        daemon.player.seek.assert_not_called()
        daemon._play_current.assert_not_called()
        self.assertEqual(daemon.index, 0)

    async def test_repeat_one_rebuilds_when_the_file_is_gone(self):
        daemon = self.daemon()
        daemon.repeat = "one"
        daemon.player.replay = mock.AsyncMock(return_value=False)
        daemon._play_current = mock.AsyncMock()
        await daemon._track_finished()
        daemon._play_current.assert_awaited_once()
        self.assertEqual(daemon.index, 0)

    async def test_finish_while_the_next_track_is_preparing_does_not_skip_it(self):
        daemon = self.daemon()
        daemon.player.state["loading"] = True
        daemon._advance = mock.AsyncMock()
        await daemon._track_finished()
        daemon._advance.assert_not_called()


class Transport(DaemonCase):
    async def test_resume_while_idle_restarts_the_track(self):
        daemon = self.daemon()
        daemon.player.state["idle"] = True
        daemon._play_current = mock.AsyncMock()
        daemon.player.set_paused = mock.AsyncMock()
        await daemon.cmd_resume({})
        daemon._play_current.assert_awaited_once()
        daemon.player.set_paused.assert_not_called()

    async def test_resume_while_playing_unpauses(self):
        daemon = self.daemon()
        daemon.player.state["idle"] = False
        daemon._play_current = mock.AsyncMock()
        daemon.player.set_paused = mock.AsyncMock()
        await daemon.cmd_resume({})
        daemon.player.set_paused.assert_awaited_once_with(False)
        daemon._play_current.assert_not_called()

    async def test_restart_player_terminates_then_starts(self):
        """F15: restartPlayer called the playback stop, so nothing restarted."""
        daemon = self.daemon()
        calls = []
        daemon.player.terminate = mock.AsyncMock(side_effect=lambda: calls.append("terminate"))
        daemon.player.start = mock.AsyncMock(side_effect=lambda: calls.append("start"))
        daemon.player.stop = mock.AsyncMock(side_effect=lambda: calls.append("stop"))
        daemon._play_current = mock.AsyncMock()
        await daemon.cmd_restartPlayer({})
        self.assertEqual(calls, ["terminate", "start"])
        daemon._play_current.assert_awaited_once()

    async def test_shutdown_terminates_mpv(self):
        daemon = self.daemon()
        daemon.player.terminate = mock.AsyncMock()
        await daemon.shutdown()
        daemon.player.terminate.assert_awaited_once()

    async def test_engine_slice_follows_the_process(self):
        daemon = self.daemon()
        self.assertEqual(daemon.snapshot()["engine"]["mpv"], "stopped")
        daemon.player.proc = mock.Mock(returncode=None)
        self.assertEqual(daemon.snapshot()["engine"]["mpv"], "running")
        daemon.player.proc = mock.Mock(returncode=-9)  # crashed, not yet noticed
        self.assertEqual(daemon.snapshot()["engine"]["mpv"], "stopped")

    async def test_engine_event_on_mpv_status_change(self):
        daemon = self.daemon()
        sent = []

        async def broadcast(event, data):
            sent.append(event)
        daemon.broadcast = broadcast
        daemon.player.proc = mock.Mock(returncode=None)
        await daemon._player_changed(daemon.player.snapshot())
        await daemon._player_changed(daemon.player.snapshot())
        self.assertEqual(sent, ["player", "engine", "player"])


class Preparing(DaemonCase):
    async def test_loading_is_set_and_cleared_when_the_build_fails(self):
        daemon = self.daemon()
        started = asyncio.Event()

        async def failing(song, start=0.0):
            started.set()
            raise RuntimeError("boom")
        daemon._start_track = failing
        await daemon._play_current()
        self.assertTrue(daemon.player.state["loading"])
        task = daemon._build_task
        with self.assertRaises(RuntimeError):
            await task
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertFalse(daemon.player.state["loading"])

    async def test_cancelled_build_keeps_the_newer_tracks_flag(self):
        daemon = self.daemon()
        gate = asyncio.Event()

        async def slow(song, start=0.0):
            await gate.wait()
        daemon._start_track = slow
        await daemon._play_current()
        first = daemon._build_task
        daemon.index = 1
        await daemon._play_current()  # cancels the first build
        with self.assertRaises(asyncio.CancelledError):
            await first
        await asyncio.sleep(0)
        self.assertTrue(daemon.player.state["loading"])
        daemon._build_task.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertFalse(daemon.player.state["loading"])


if __name__ == "__main__":
    unittest.main()
