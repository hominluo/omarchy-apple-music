"""StreamBuilder bookkeeping: the build's work folder, failure translation,
prune/clear/stats. The DRM steps (resolve, download, licence, decrypt) are
replaced by stand-ins; nothing here touches the network or a .wvd.
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import config, stream  # noqa: E402
from engine.stream import StreamBuilder, StreamError  # noqa: E402

MB = 1024 * 1024
# The engine logs every failure these tests provoke on purpose.
logging.getLogger("apple-music").addHandler(logging.NullHandler())
logging.getLogger("apple-music").propagate = False



class CacheCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="am-test-")
        root = Path(self.tmp.name)
        for name in ("run", "data", "cache"):
            (root / name).mkdir()
        self.env = mock.patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(root / "run"),
            "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"),
        })
        self.env.start()
        stream._RECENT.clear()
        stream._ACTIVE_WORK.clear()
        self.events = []

    def tearDown(self):
        stream._RECENT.clear()
        stream._ACTIVE_WORK.clear()
        self.env.stop()
        self.tmp.cleanup()

    def settings(self, **values):
        (config.data_dir() / "settings.json").write_text(json.dumps(values), "utf-8")

    def track(self, name: str, size_mb: float, age: float) -> Path:
        path = config.audio_dir() / f"{name}.m4a"
        path.write_bytes(b"\0" * int(size_mb * MB))
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def builder(self) -> StreamBuilder:
        def notify(event, payload):
            self.events.append(dict(payload))
        builder = StreamBuilder(api=None, notify=notify)
        # Stand-ins for the network and DRM steps; build() only orchestrates.
        builder.resolve = mock.AsyncMock(return_value=mock.Mock(segments=[{}]))
        builder._download = mock.AsyncMock(return_value=[b"encrypted"])
        builder._key = mock.AsyncMock(return_value="00" * 16)
        builder._cover = mock.AsyncMock(return_value=None)
        return builder

    def work_entries(self) -> list:
        return list(stream.work_dir().iterdir())


class Limits(CacheCase):
    def test_cache_limit_is_read_as_written(self):
        """F19/F42: `or 1024` turned an explicit 0 into a gigabyte."""
        self.settings(cacheLimitMb=0)
        self.assertEqual(stream.cache_limit_mb(), 0)
        self.settings(cacheLimitMb=-5)
        self.assertEqual(stream.cache_limit_mb(), 0)
        self.settings(cacheLimitMb="lots")
        self.assertEqual(stream.cache_limit_mb(), 1024)
        self.settings(cacheLimitMb=None)
        self.assertEqual(stream.cache_limit_mb(), 1024)
        self.settings()
        self.assertEqual(stream.cache_limit_mb(), 1024)
        self.settings(cacheLimitMb=300)
        self.assertEqual(stream.cache_limit_mb(), 300)
        self.assertEqual(self.builder().cache_stats()["limitMb"], 300)


class Prune(CacheCase):
    def test_prune_never_deletes_the_tracks_in_use(self):
        """F42: a small limit deleted the playing track and the one just built."""
        playing = self.track("playing", 2, age=300)
        old = self.track("old", 2, age=600)
        fresh = self.track("fresh", 2, age=0)
        stream._remember(playing)
        self.builder().prune(limit_mb=0, keep={fresh})
        self.assertTrue(playing.exists())
        self.assertTrue(fresh.exists())
        self.assertFalse(old.exists())

    def test_prune_drops_oldest_first_until_it_fits(self):
        a = self.track("a", 2, age=300)
        b = self.track("b", 2, age=200)
        c = self.track("c", 2, age=100)
        self.builder().prune(limit_mb=4)
        self.assertFalse(a.exists())
        self.assertTrue(b.exists())
        self.assertTrue(c.exists())

    def test_prune_survives_a_file_vanishing(self):
        self.track("a", 1, age=10)
        (config.audio_dir() / "ghost.m4a").symlink_to(config.audio_dir() / "nowhere")
        self.builder().prune(limit_mb=0)  # must not raise
        stats = self.builder().cache_stats()
        self.assertEqual(stats["tracks"], 0)

    def test_prune_keeps_what_the_daemon_says_is_in_use(self):
        """G7: with prefetch 10 a second prefetch pass (shuffle turned on)
        pushed the playing track out of the recent-builds window."""
        playing = self.track("playing", 1, age=900)
        ahead = self.track("ahead", 1, age=800)
        old = self.track("old", 1, age=700)
        for index in range(25):
            stream._remember(self.track(f"prefetched{index}", 0.01, age=index))
        builder = self.builder()
        builder.in_use = lambda: {"playing", "ahead", None}
        builder.prune(limit_mb=0)
        self.assertTrue(playing.exists())
        self.assertTrue(ahead.exists())
        self.assertFalse(old.exists())

    def test_a_fresh_build_is_kept_only_for_its_minute(self):
        """H12: the recent-builds keep was a count, so cacheLimitMb 0 kept
        the last twelve tracks built, not "only the tracks in use"."""
        fresh = self.track("fresh", 1, age=0)
        stale = self.track("stale", 1, age=0)
        stream._remember(fresh)
        stream._remember(stale)
        stream._RECENT[stale] -= stream.RECENT_SECONDS + 1
        self.builder().prune(limit_mb=0)
        self.assertTrue(fresh.exists())
        self.assertFalse(stale.exists())
        self.assertNotIn(stale, stream._RECENT)

    def test_with_limit_zero_only_the_tracks_in_use_survive_once_fresh_ones_age(self):
        playing = self.track("playing", 1, age=900)
        built = [self.track(f"built{index}", 0.01, age=index) for index in range(20)]
        for path in built:
            stream._remember(path)
        builder = self.builder()
        builder.in_use = lambda: {"playing"}
        builder.prune(limit_mb=0)
        self.assertTrue(all(path.exists() for path in built), "fresh builds are kept")
        later = time.monotonic() + stream.RECENT_SECONDS + 1
        with mock.patch.object(stream.time, "monotonic", return_value=later):
            builder.prune(limit_mb=0)
        self.assertTrue(playing.exists())
        self.assertEqual([path for path in built if path.exists()], [])

    def test_a_failing_in_use_answer_does_not_fail_the_prune(self):
        old = self.track("old", 1, age=700)
        builder = self.builder()

        def broken():
            raise RuntimeError("no state")
        builder.in_use = broken
        with self.assertLogs("apple-music.stream", "WARNING"):
            builder.prune(limit_mb=0)
        self.assertFalse(old.exists())


class Clear(CacheCase):
    def test_clear_keeps_the_playing_file_and_sweeps_build_leftovers(self):
        playing = self.track("playing", 1, age=0)
        other = self.track("other", 1, age=0)
        leftover = stream.work_dir() / "123.enc"  # what older versions left
        leftover.write_bytes(b"x" * 100)
        result = self.builder().clear_cache(keep={playing})
        self.assertTrue(playing.exists())
        self.assertFalse(other.exists())
        self.assertFalse(leftover.exists())
        self.assertEqual(result["removed"], 2)

    def test_sweep_leaves_running_builds_alone(self):
        running = stream.work_dir() / "running-abc"
        running.mkdir()
        stream._ACTIVE_WORK.add(running)
        stale = stream.work_dir() / "stale-abc"
        stale.mkdir()
        (stale / "x.enc").write_bytes(b"x")
        self.assertEqual(stream.sweep_work_dir(min_age=0), 1)
        self.assertTrue(running.exists())
        self.assertFalse(stale.exists())

    def test_a_routine_sweep_leaves_young_folders_to_their_owner(self):
        """G8: `probe` in a second process swept the daemon's live build
        folders (its _ACTIVE_WORK knows only its own)."""
        young = stream.work_dir() / "daemon-build"
        young.mkdir()
        (young / "x.enc").write_bytes(b"x")
        old = stream.work_dir() / "killed-build"
        old.mkdir()
        stamp = time.time() - stream.SWEEP_MIN_AGE - 60
        os.utime(old, (stamp, stamp))
        self.assertEqual(stream.sweep_work_dir(), 1)
        self.assertTrue(young.exists())
        self.assertFalse(old.exists())
        # Daemon startup holds the daemon lock: everything there is a leftover.
        self.assertEqual(stream.sweep_work_dir(min_age=0), 1)
        self.assertFalse(young.exists())


class Build(CacheCase):
    async def test_cache_hit_returns_at_once_and_is_remembered(self):
        builder = self.builder()
        target = self.track("42", 1, age=100)
        path = await builder.build({"id": "42"})
        self.assertEqual(path, target)
        self.assertIn(target, stream._RECENT)
        builder.resolve.assert_not_called()
        self.assertEqual(self.events, [])

    async def test_success_moves_the_file_and_removes_the_work_folder(self):
        builder = self.builder()

        def decrypt(source, target, key, song, cover=None):
            target.write_bytes(source.read_bytes() + b"-clear")
        builder._decrypt = decrypt
        path = await builder.build({"id": "7"})
        self.assertEqual(path.read_bytes(), b"encrypted-clear")
        self.assertEqual(self.work_entries(), [])
        self.assertEqual(stream._ACTIVE_WORK, set())
        self.assertEqual([e["stage"] for e in self.events],
                         ["resolving", "downloading", "decrypting", "ready"])
        self.assertIn(path, stream._RECENT)

    async def test_the_catalogue_twin_reaches_the_playback_lookup(self):
        """0.3.2: a playlist-only library track streams only through it."""
        builder = self.builder()
        builder._decrypt = lambda source, target, key, song, cover=None: target.write_bytes(b"x")
        await builder.build({"id": "a.123", "library": True, "catalogId": "123"})
        builder.resolve.assert_awaited_once_with("a.123", "123", "aac-web")

        api = mock.Mock()
        api.webplayback = mock.AsyncMock(return_value={"songList": [{
            "songId": "123", "assets": [{"flavor": "28:ctrp256", "URL": "https://x/a.m3u8"}]}]})
        found = await StreamBuilder(api=api)._playback("a.123", "123", "aac-web")
        api.webplayback.assert_awaited_once_with("a.123", "123")
        self.assertEqual(found["trackId"], "123")

    async def test_failed_decrypt_leaves_nothing_behind(self):
        """F36: the .enc, the cover and the partial stayed in build/ forever."""
        builder = self.builder()

        def decrypt(source, target, key, song, cover=None):
            target.write_bytes(b"half")
            raise StreamError("Decryption failed: nope")
        builder._decrypt = decrypt
        with self.assertRaises(StreamError):
            await builder.build({"id": "7"})
        self.assertEqual(self.work_entries(), [])
        self.assertFalse(builder.cached_path("7").exists())

    async def test_cancel_during_decrypt_cleans_up_after_the_thread(self):
        """The ffmpeg thread cannot be stopped; its folder goes when it ends,
        not under it, and a retry gets a folder of its own."""
        builder = self.builder()
        started = threading.Event()
        release = threading.Event()
        seen = []

        def decrypt(source, target, key, song, cover=None):
            started.set()
            release.wait(5)
            seen.append(source.exists())
            target.write_bytes(b"late")
        builder._decrypt = decrypt
        task = asyncio.create_task(builder.build({"id": "7"}))
        while not started.is_set():
            await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.work_entries()), 1)  # still in use by the thread
        self.assertEqual(stream.sweep_work_dir(), 0)  # and a sweep leaves it alone
        release.set()
        for _ in range(200):
            if not self.work_entries():
                break
            await asyncio.sleep(0.01)
        self.assertEqual(seen, [True])
        self.assertEqual(self.work_entries(), [])
        self.assertFalse(builder.cached_path("7").exists())

    async def test_network_errors_become_stream_errors(self):
        """F11: a raw requests error skipped the daemon's failure path."""
        builder = self.builder()
        builder.resolve = mock.AsyncMock(side_effect=requests.ConnectionError("boom https://cdn/x?sig=SECRET"))
        with self.assertRaises(StreamError) as caught:
            await builder.build({"id": "7"})
        self.assertEqual(caught.exception.code, "network")
        self.assertNotIn("SECRET", caught.exception.message)

        response = requests.Response()
        response.status_code = 403
        builder.resolve = mock.AsyncMock(side_effect=requests.HTTPError(response=response))
        with self.assertRaises(StreamError) as caught:
            await builder.build({"id": "7"})
        self.assertIn("403", caught.exception.message)

    async def test_tool_failures_become_stream_errors(self):
        builder = self.builder()

        def decrypt(source, target, key, song, cover=None):
            raise subprocess.TimeoutExpired("ffmpeg", 180)
        builder._decrypt = decrypt
        with self.assertRaises(StreamError) as caught:
            await builder.build({"id": "7"})
        self.assertEqual(caught.exception.code, "io")

        def missing(source, target, key, song, cover=None):
            raise FileNotFoundError(2, "No such file or directory", "ffprobe")
        builder._decrypt = missing
        with self.assertRaises(StreamError) as caught:
            await builder.build({"id": "7"})
        self.assertIn("ffprobe", caught.exception.message)
        self.assertEqual(self.work_entries(), [])

    async def test_a_builder_outside_the_daemon_leaves_the_cache_alone(self):
        """G8: probe pruned the daemon's playing and prefetched files."""
        self.settings(cacheLimitMb=0)
        daemons = self.track("daemons", 1, age=600)
        builder = self.builder()
        builder.manage_cache = False
        builder._decrypt = lambda source, target, key, song, cover=None: target.write_bytes(b"ok")
        path = await builder.build({"id": "5"}, force=True)
        self.assertTrue(path.exists())
        self.assertTrue(daemons.exists())

    async def test_a_tiny_limit_does_not_delete_the_file_it_returns(self):
        """F42: with cacheLimitMb below one track the fresh file was pruned
        before mpv ever opened it."""
        self.settings(cacheLimitMb=0)
        self.track("old", 1, age=100)
        builder = self.builder()

        def decrypt(source, target, key, song, cover=None):
            target.write_bytes(b"\0" * MB)
        builder._decrypt = decrypt
        path = await builder.build({"id": "7"})
        self.assertTrue(path.exists())
        self.assertFalse((config.audio_dir() / "old.m4a").exists())


if __name__ == "__main__":
    unittest.main()
