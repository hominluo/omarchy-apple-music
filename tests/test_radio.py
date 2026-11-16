"""Radio and Apple's pages: the HLS proxy that decrypts live stations and
episodes, the shelf reader behind Home/New/Radio/genre/show pages, lyrics,
and the daemon's station queue (stream stations, track-station refills,
Autoplay, Play Next/Later, a dropped live stream).

No network, no mpv and no ffmpeg: Apple, the player and the transmux are
stand-ins.
"""

import asyncio
import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from engine import apple, catalog, config, radio  # noqa: E402
from engine.apple import ApiError, brief  # noqa: E402
from engine.daemon import Daemon, _airing, _ident  # noqa: E402
from engine.player import Player  # noqa: E402

# The engine logs every failure these tests provoke on purpose.
logging.getLogger("apple-music").addHandler(logging.NullHandler())
logging.getLogger("apple-music").propagate = False

LIVE_MASTER = """#EXTM3U
#EXT-X-STREAM-INF:CODECS="mp4a.40.2",BANDWIDTH=281600
https://a.example/cmaf/index_1.m3u8
#EXT-X-STREAM-INF:CODECS="mp4a.40.5",BANDWIDTH=70400
https://a.example/cmaf/index_2.m3u8
#EXT-X-STREAM-INF:CODECS="mp4a.40.2",BANDWIDTH=281600
https://b.example/cmaf/index_1.m3u8
"""

LIVE_MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:16
#EXT-X-MEDIA-SEQUENCE:100
#EXT-X-DISCONTINUITY-SEQUENCE:7
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="data:text/plain;base64,UFI=",KEYFORMAT="com.microsoft.playready"
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="data:text/plain;base64,V1Y=",KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://linear/abc",KEYFORMAT="com.apple.streamingkeydelivery"
#EXT-X-PROGRAM-DATE-TIME:2026-11-16T20:05:54.694Z
#EXT-X-MAP:URI="init_1.mp4"
#EXTINF:16.02,
seg_100.mp4
#EXTINF:16.02,
seg_101.mp4
#EXT-X-DISCONTINUITY
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="data:text/plain;base64,TkVX",KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
#EXT-X-MAP:URI="init_2.mp4"
#EXTINF:15.99,
seg_102.mp4
"""

VOD_MEDIA = """#EXTM3U
#EXT-X-TARGETDURATION:16
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-KEY:METHOD=SAMPLE-AES,URI="data:text/plain;base64,V1Y=",KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
#EXT-X-MAP:URI="fileSequence0.mp4"
#EXTINF:16.0,
fileSequence1.m4s
#EXTINF:12.5,
fileSequence2.m4s
#EXT-X-ENDLIST
"""


def tenc(version: int, pattern: int) -> bytes:
    body = bytes([version, 0, 0, 0, 0, pattern, 1, 0]) + b"\0" * 16
    return (8 + len(body)).to_bytes(4, "big") + b"tenc" + body


class Playlists(unittest.TestCase):
    def test_the_256k_aac_variant_and_its_backup(self):
        url, backups = radio.choose_variant(LIVE_MASTER, "https://a.example/gl/index.m3u8")
        self.assertEqual(url, "https://a.example/cmaf/index_1.m3u8")
        self.assertEqual(backups, ["https://b.example/cmaf/index_1.m3u8"])

    def test_a_media_playlist_is_its_own_variant(self):
        self.assertEqual(radio.choose_variant(VOD_MEDIA, "https://x/m.m3u8"),
                         ("https://x/m.m3u8", []))

    def test_live_segments_carry_their_own_key_and_init(self):
        parsed = radio.parse_media(LIVE_MEDIA, "https://a.example/cmaf/index_1.m3u8")
        self.assertFalse(parsed["ended"])
        self.assertEqual(parsed["sequence"], 100)
        segments = parsed["segments"]
        self.assertEqual([s.seq for s in segments], [100, 101, 102])
        # Only the Widevine key is kept; PlayReady and FairPlay lines are not ours.
        self.assertEqual(segments[0].key, "data:text/plain;base64,V1Y=")
        self.assertEqual(segments[2].key, "data:text/plain;base64,TkVX")
        self.assertEqual(segments[0].init, "https://a.example/cmaf/init_1.mp4")
        self.assertEqual(segments[2].init, "https://a.example/cmaf/init_2.mp4")
        self.assertTrue(segments[2].discontinuity)
        self.assertEqual(segments[1].url, "https://a.example/cmaf/seg_101.mp4")

    def test_the_local_playlist_names_only_local_segments(self):
        session = radio.RadioSession(mock.Mock(), "s", {}, "https://x/m.m3u8", [], "")
        parsed = radio.parse_media(LIVE_MEDIA, "https://a.example/cmaf/index_1.m3u8")
        text = session.local_playlist(parsed)
        self.assertNotIn("KEY", text)
        self.assertNotIn("https://", text)
        self.assertIn("#EXT-X-MEDIA-SEQUENCE:100", text)
        self.assertIn("#EXT-X-DISCONTINUITY-SEQUENCE:7", text)
        self.assertIn("100.ts", text)
        self.assertNotIn("ENDLIST", text)
        vod = session.local_playlist(radio.parse_media(VOD_MEDIA, "https://x/v/prog.m3u8"))
        self.assertIn("#EXT-X-ENDLIST", vod)
        self.assertIn("#EXT-X-PLAYLIST-TYPE:VOD", vod)

    def test_an_unpatterned_cbcs_box_gets_the_whole_sample_pattern(self):
        init = b"moov" + tenc(1, 0)
        self.assertEqual(radio.patch_pattern(init)[4 + 13], 0x10)
        # A declared pattern, and a version-0 box (no pattern field), stay.
        self.assertEqual(radio.patch_pattern(b"moov" + tenc(1, 0x19)), b"moov" + tenc(1, 0x19))
        self.assertEqual(radio.patch_pattern(b"moov" + tenc(0, 0)), b"moov" + tenc(0, 0))
        self.assertEqual(radio.patch_pattern(b"no box here"), b"no box here")


class FakeSession:
    def __init__(self):
        self.asked = []

    async def refresh(self, force=False):
        return radio.parse_media(LIVE_MEDIA, "https://a.example/cmaf/index_1.m3u8")

    def local_playlist(self, parsed):
        return "#EXTM3U\n"

    async def segment(self, seq):
        self.asked.append(seq)
        if seq == 404:
            raise KeyError(seq)
        if seq == 502:
            raise radio.RadioError("boom")
        return b"TS" * 10


class ProxyHttp(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.proxy = radio.RadioProxy(lambda: None, lambda: None)
        await self.proxy.start()
        self.session = FakeSession()
        self.proxy.sessions["abc123"] = self.session

    async def asyncTearDown(self):
        await self.proxy.close()

    async def get(self, path: str, method: str = "GET") -> tuple[int, bytes]:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.proxy.port)
        writer.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        await writer.drain()
        status = int((await reader.readline()).split()[1])
        length = 0
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b""):
                break
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        body = await reader.readexactly(length) if length else b""
        writer.close()
        return status, body

    async def test_it_listens_on_loopback_only(self):
        host = self.proxy.server.sockets[0].getsockname()[0]
        self.assertEqual(host, "127.0.0.1")

    async def test_the_token_is_required(self):
        token = self.proxy.token
        self.assertEqual((await self.get(f"/{token}/abc123/index.m3u8"))[0], 200)
        self.assertEqual((await self.get("/wrong-token/abc123/index.m3u8"))[0], 404)
        self.assertEqual((await self.get(f"/{token}/abc123/../index.m3u8"))[0], 404)
        self.assertEqual((await self.get(f"/{token}/ffffff/index.m3u8"))[0], 404)

    async def test_segments_and_failures(self):
        token = self.proxy.token
        self.assertEqual(await self.get(f"/{token}/abc123/101.ts"), (200, b"TS" * 10))
        self.assertEqual((await self.get(f"/{token}/abc123/404.ts"))[0], 404)
        self.assertEqual((await self.get(f"/{token}/abc123/502.ts"))[0], 502)
        self.assertEqual((await self.get(f"/{token}/abc123/101.ts", "POST"))[0], 405)


class Tuning(unittest.IsolatedAsyncioTestCase):
    async def test_a_broadcaster_goes_to_mpv_as_it_is(self):
        api = mock.Mock(media_user_token="t")
        api.play_assets = mock.AsyncMock(return_value={"url": "https://npr.example/live.m3u8"})
        proxy = radio.RadioProxy(lambda: api, lambda: None)
        url = await proxy.tune({"id": "ra.1", "playParams": {"id": "ra.1", "hasDrm": False}})
        self.assertEqual(url, "https://npr.example/live.m3u8")
        self.assertIsNone(proxy.server)  # nothing to proxy

    async def test_only_https_streams_reach_mpv(self):
        api = mock.Mock(media_user_token="t")
        api.play_assets = mock.AsyncMock(return_value={"url": "http://plain.example/live.m3u8"})
        proxy = radio.RadioProxy(lambda: api, lambda: None)
        with self.assertRaises(ApiError):
            await proxy.tune({"id": "ra.1", "playParams": {"id": "ra.1", "hasDrm": False}})

    async def test_signed_out_is_refused(self):
        proxy = radio.RadioProxy(lambda: mock.Mock(media_user_token=""), lambda: None)
        with self.assertRaises(ApiError) as caught:
            await proxy.tune({"id": "ra.1", "playParams": {"id": "ra.1"}})
        self.assertEqual(caught.exception.code, "unauthorized")

    async def test_a_drm_station_is_served_from_the_proxy(self):
        api = mock.Mock(media_user_token="t")
        api.play_assets = mock.AsyncMock(return_value={
            "url": "https://a.example/gl/index-cmaf.m3u8",
            "keyServerUrl": "https://linear.tv.apple.com/v1/radio/streaming-key-delivery"})
        proxy = radio.RadioProxy(lambda: api, lambda: None)
        answers = {"https://a.example/gl/index-cmaf.m3u8": LIVE_MASTER.encode(),
                   "https://a.example/cmaf/index_1.m3u8": LIVE_MEDIA.encode()}
        with mock.patch.object(radio, "_fetch_capped", side_effect=lambda url, *a: answers[url]):
            url = await proxy.tune({"id": "ra.9", "playParams": {"id": "ra.9", "hasDrm": True}})
        try:
            self.assertTrue(proxy.owns(url))
            self.assertTrue(url.endswith("/index.m3u8"))
            session = next(iter(proxy.sessions.values()))
            self.assertEqual(sorted(session.segments), [100, 101, 102])
        finally:
            await proxy.close()

    async def test_a_segment_is_fetched_keyed_and_transmuxed_once(self):
        proxy = radio.RadioProxy(lambda: None, lambda: None)
        proxy.license = mock.AsyncMock(return_value="00" * 16)
        proxy.transmux = mock.AsyncMock(return_value=b"TSDATA")
        session = radio.RadioSession(proxy, "s", {}, "https://a.example/cmaf/index_1.m3u8", [], "https://k.apple.com/")
        fetched = []

        def fetch(url, *args):
            fetched.append(url)
            return LIVE_MEDIA.encode() if url.endswith(".m3u8") else (b"moov" + tenc(1, 0) if "init" in url else b"moof")
        with mock.patch.object(radio, "_fetch_capped", side_effect=fetch):
            await session.refresh()
            first, again = await asyncio.gather(session.segment(100), session.segment(100))
            await asyncio.sleep(0.01)  # the prefetch of 101
        self.assertEqual((first, again), (b"TSDATA", b"TSDATA"))
        self.assertEqual(fetched.count("https://a.example/cmaf/seg_100.mp4"), 1)
        self.assertEqual(fetched.count("https://a.example/cmaf/init_1.mp4"), 1)
        proxy.license.assert_awaited_once()
        data, _key = proxy.transmux.await_args_list[0].args
        self.assertEqual(data[4 + 13], 0x10)  # the patched init went in front


class Shelves(unittest.TestCase):
    @staticmethod
    def element(eid, kind="326", name="", contents=(), children=(), room=None, links=None):
        record = {"id": eid, "type": "editorial-elements",
                  "attributes": {"editorialElementKind": kind}, "relationships": {}}
        if name:
            record["attributes"]["name"] = name
        if links:
            record["attributes"]["links"] = links
        if contents:
            record["relationships"]["contents"] = {"data": list(contents)}
        if children:
            record["relationships"]["children"] = {"data": list(children)}
        if room:
            record["relationships"]["room"] = {"data": [{"id": room, "type": "rooms"}]}
        return record

    @staticmethod
    def station(sid, live=True, stream=True, name="S"):
        return {"id": sid, "type": "stations", "attributes": {
            "name": name, "isLive": live, "stationProviderName": "Apple Music",
            "playParams": {"id": sid, "kind": "radioStation",
                           "format": "stream" if stream else "tracks", "hasDrm": True}}}

    def grouping(self, *children):
        tab = {"id": "t", "type": "editorial-elements", "attributes": {},
               "relationships": {"children": {"data": list(children)}}}
        return {"data": [{"id": "g", "type": "groupings", "attributes": {"name": "Radio"},
                          "relationships": {"tabs": {"data": [tab]}}}]}

    def test_a_radio_page(self):
        carousel = self.element("c", "316", children=[
            self.element("c1", "383", contents=[self.station("ra.1", name="Apple Music 1")]),
            self.element("c2", "320", contents=[self.station("ra.2")])])
        on_air = self.element("o", "488", children=[
            self.element("o1", "489", contents=[self.station("ra.1")]),
            self.element("o2", "489", contents=[self.station("ra.2")])])
        on_air["attributes"]["title"] = "On Air Now"
        genre = self.element("s", name="Top Stations", room="99",
                             contents=[self.station("ra.3", live=False, stream=False)])
        videos = self.element("v", name="Watch Interviews",
                              contents=[{"id": "1", "type": "music-videos", "attributes": {"name": "x"}}])
        links = self.element("l", "391", name="More to Explore", links=[
            {"label": "Pop", "url": "https://music.apple.com/WebObjects/MZStore.woa/wa/viewGrouping?cc=us&id=168"},
            {"label": "Concerts", "target": "external", "url": "https://music.apple.com/concerts"},
            {"label": "Moods", "url": "https://itunes.apple.com/us/collection/moods/id178644?fcId=645&mt=1"}])
        title, sections = catalog.grouping_sections(self.grouping(carousel, on_air, genre, videos, links))
        self.assertEqual(title, "Radio")
        self.assertEqual([s["title"] for s in sections], ["On Air Now", "Top Stations", "More to Explore"])
        hero, stations, explore = sections
        self.assertEqual(hero["kind"], "hero")
        self.assertEqual([i["id"] for i in hero["items"]], ["ra.1", "ra.2"])
        self.assertTrue(hero["items"][0]["live"])
        self.assertEqual(stations["seeAll"], {"view": "room", "args": {"id": "99", "title": "Top Stations"}})
        self.assertFalse(stations["items"][0]["stream"])
        self.assertEqual([i["target"] for i in explore["items"]], [
            {"view": "grouping", "args": {"id": "168", "title": "Pop"}},
            {"view": "room", "args": {"id": "645", "title": "Moods"}}])

    def test_station_kinds(self):
        live = brief(self.station("ra.1"))
        self.assertEqual((live["type"], live["live"], live["stream"], live["episode"]),
                         ("station", True, True, False))
        self.assertEqual(live["subtitle"], "LIVE · Apple Music")
        episode = self.station("ra.2", live=False)
        episode["attributes"]["durationInMillis"] = 7246000
        episode = brief(episode)
        self.assertTrue(episode["episode"])
        self.assertEqual(episode["subtitle"], "Episode · 2 hr 1 min")
        tracks = brief(self.station("ra.3", live=False, stream=False))
        self.assertEqual((tracks["stream"], tracks["subtitle"]), (False, "Station"))
        video = brief({"id": "5", "type": "uploaded-videos", "attributes": {"name": "v"}})
        self.assertFalse(video["playable"])

    def test_home_is_one_shelf_per_recommendation(self):
        data = {"data": [
            {"id": "r1", "type": "personal-recommendation",
             "attributes": {"title": {"stringForDisplay": "Top Picks for You"},
                            "display": {"kind": "MusicNotesHeroShelf"}},
             "relationships": {"contents": {"data": [
                 {"id": "pl.1", "type": "playlists", "attributes": {"name": "Chill"}}]}}},
            {"id": "r2", "type": "personal-recommendation",
             "attributes": {"title": {"stringForDisplay": "Recently Played"},
                            "display": {"kind": "MusicCoverShelf"}},
             "relationships": {"contents": {"data": [
                 {"id": "l.1", "type": "library-albums", "attributes": {"name": "A"}}]}}},
        ]}
        sections = catalog.recommendation_sections(data)
        self.assertEqual([(s["title"], s["kind"]) for s in sections],
                         [("Top Picks for You", "hero"), ("Recently Played", "cards")])
        self.assertTrue(sections[1]["items"][0]["library"])

    def test_links(self):
        self.assertEqual(catalog.link_target("https://music.apple.com/us/curator/apple-music-1/1234"),
                         {"view": "curator", "args": {"id": "1234", "title": "", "kind": "apple-curators"}})
        self.assertEqual(catalog.link_target("https://itunes.apple.com/WebObjects/MZStore.woa/wa/viewTop?genreId=34")["view"],
                         "charts")
        self.assertIsNone(catalog.link_target("https://example.com/whatever"))
        self.assertIsNone(catalog.link_target(""))


class Lyrics(unittest.TestCase):
    TTML = ('<tt xmlns="http://www.w3.org/ns/ttml" xmlns:itunes="http://music.apple.com/lyric-ttml-internal" '
            'itunes:timing="Line"><head><metadata><iTunesMetadata xmlns="http://music.apple.com/lyric-ttml-internal">'
            '<songwriters><songwriter>Bono</songwriter></songwriters></iTunesMetadata></metadata></head>'
            '<body><div><p begin="16.645" end="21.588">I can\'t deny</p>'
            '<p begin="1:02.5" end="1:05">this <span>passion</span></p></div></body></tt>')

    def test_lines_times_and_writers(self):
        parsed = catalog.parse_lyrics(self.TTML)
        self.assertTrue(parsed["synced"])
        self.assertEqual(parsed["writers"], ["Bono"])
        self.assertEqual(parsed["lines"][0], {"begin": 16.645, "end": 21.588, "text": "I can't deny"})
        self.assertEqual(parsed["lines"][1]["begin"], 62.5)
        self.assertEqual(parsed["lines"][1]["text"], "this passion")

    def test_unsynced(self):
        parsed = catalog.parse_lyrics(self.TTML.replace('itunes:timing="Line"', 'itunes:timing="None"'))
        self.assertFalse(parsed["synced"])

    def test_a_doctype_is_refused(self):
        with self.assertRaises(ValueError):
            catalog.parse_lyrics('<!DOCTYPE x [<!ENTITY a "b">]>' + self.TTML)
        with self.assertRaises(ValueError):
            catalog.parse_lyrics("<tt>" + "x" * (catalog.LYRICS_MAX_BYTES + 1) + "</tt>")


class Helpers(unittest.TestCase):
    def test_ids(self):
        self.assertEqual(_ident("ra.978194965"), "ra.978194965")
        self.assertEqual(_ident("pl.u-abc_1"), "pl.u-abc_1")
        for bad in ("../x", "a/b", "", None, "x" * 200):
            with self.assertRaises(ApiError):
                _ident(bad)
        self.assertEqual(_ident(None, ""), "")

    def test_airing(self):
        schedule = [{"title": "A", "start": "2026-11-16T18:00:00Z", "end": "2026-11-16T20:00:00Z"},
                    {"title": "B", "start": "2026-11-16T20:00:00Z", "end": "2026-11-16T22:00:00Z"}]
        from datetime import datetime
        at = datetime.fromisoformat("2026-11-16T20:30:00+00:00").timestamp()
        self.assertEqual(_airing(schedule, at), "B")
        self.assertEqual(_airing(schedule, at + 86400), "")

    def test_a_stream_loadfile_buffers_and_quotes_the_title(self):
        command = Player._loadfile("http://127.0.0.1:1/t/s/index.m3u8", 0, False,
                                   stream_title="Hits, Hits=Hits")
        self.assertEqual(command[:4], ["loadfile", "http://127.0.0.1:1/t/s/index.m3u8", "replace", -1])
        self.assertIn("cache=yes", command[4])
        self.assertTrue(command[4].endswith("force-media-title=%15%Hits, Hits=Hits"))
        self.assertEqual(Player._loadfile("/a.m4a", 0, False), ["loadfile", "/a.m4a", "replace"])


def song(song_id) -> dict:
    return {"id": str(song_id), "type": "song", "title": f"t{song_id}", "albumId": "a", "artistId": "r"}


class Stations(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="am-radio-")
        root = Path(self.tmp.name)
        self.env = mock.patch.dict(os.environ, {
            "XDG_RUNTIME_DIR": str(root / "run"), "XDG_DATA_HOME": str(root / "data"),
            "XDG_CACHE_HOME": str(root / "cache"), "HOME": str(root / "home")})
        self.env.start()
        (root / "run").mkdir()
        (config.data_dir() / "settings.json").write_text(json.dumps({"prefetch": 0}), "utf-8")

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def daemon(self) -> Daemon:
        d = Daemon()
        d.ready = True
        api = d.api = mock.Mock(media_user_token="token")
        api.station_tracks = mock.AsyncMock(side_effect=[[song(1), song(2), song(3)],
                                                         [song(3), song(4), song(5)]])
        api.continuous = mock.AsyncMock(return_value=({"id": "ra.cp-1", "artwork": ""},
                                                      [song(7), song(8)]))
        api.station = mock.AsyncMock(return_value={"info": {}, "schedule": []})
        d.builder = mock.Mock()
        d.builder.build = mock.AsyncMock(side_effect=lambda track, codec="": Path("/dev/null"))
        d.radio.tune = mock.AsyncMock(return_value="http://127.0.0.1:9/t/s/index.m3u8")

        async def broadcast(event, data):
            pass
        d.broadcast = broadcast
        self.loads = []

        async def load(path, song_id, start=0.0, stream_title=None):
            self.loads.append((str(path), str(song_id), stream_title))
            d.player.state.update({"songId": str(song_id), "idle": False, "paused": False,
                                   "loading": False})
        d.player.load = mock.AsyncMock(side_effect=load)
        d.player.stop = mock.AsyncMock()
        d.player.seek = mock.AsyncMock(return_value=True)
        return d

    async def settle(self, d, rounds=60):
        for _ in range(rounds):
            await asyncio.sleep(0)
        tasks = [t for t in (d._build_task, d._extend_task, d._prefetch_task) if t and not t.done()]
        if tasks:
            await asyncio.wait(tasks, timeout=0.2)
        for _ in range(rounds):
            await asyncio.sleep(0)

    LIVE = {"id": "ra.978194965", "type": "station", "title": "Apple Music 1", "live": True,
            "stream": True, "episode": False, "artist": "LIVE", "artwork": "",
            "playParams": {"id": "ra.978194965", "format": "stream", "hasDrm": True}}

    async def test_a_live_station_is_one_queue_item_played_through_the_proxy(self):
        d = self.daemon()
        result = await d.cmd_playStation({"station": dict(self.LIVE)})
        await self.settle(d)
        self.assertEqual(result["count"], 1)
        self.assertEqual(d.queue[0]["type"], "station")
        self.assertEqual(self.loads, [("http://127.0.0.1:9/t/s/index.m3u8", "ra.978194965", "Apple Music 1")])
        d.builder.build.assert_not_awaited()
        self.assertEqual(d.context["type"], "station")

    async def test_seek_does_nothing_on_live_radio(self):
        d = self.daemon()
        await d.cmd_playStation({"station": dict(self.LIVE)})
        await self.settle(d)
        await d.cmd_seek({"pos": 30})
        d.player.seek.assert_not_awaited()

    async def test_a_dropped_live_stream_is_tuned_again(self):
        d = self.daemon()
        await d.cmd_playStation({"station": dict(self.LIVE)})
        await self.settle(d)
        d.player.state.update({"idle": True})
        await d._track_finished()
        await self.settle(d)
        self.assertEqual(len(self.loads), 2)
        self.assertEqual(d.index, 0)

    async def test_a_long_pause_on_live_radio_resumes_at_the_live_edge(self):
        d = self.daemon()
        await d.cmd_playStation({"station": dict(self.LIVE)})
        await self.settle(d)
        state = dict(d.player.state, paused=True)
        d._note_pause(state)
        d._paused_since = (d._paused_since[0], d._paused_since[1] - 120)
        d._note_pause(dict(state, paused=False))
        await self.settle(d)
        self.assertEqual(len(self.loads), 2)
        # A short pause just carries on.
        d._note_pause(dict(state, paused=True))
        d._note_pause(dict(state, paused=False))
        await self.settle(d)
        self.assertEqual(len(self.loads), 2)

    async def test_a_track_station_refills_before_it_runs_out(self):
        d = self.daemon()
        await d.cmd_playStation({"station": {"id": "ra.985488923", "type": "station",
                                             "stream": False, "title": "Hip-Hop",
                                             "playParams": {"format": "tracks"}}})
        await self.settle(d)
        self.assertTrue(d.context.get("refill"))
        # 3 tracks, playing the first: two left, under STATION_REFILL_AT.
        self.assertEqual([t["id"] for t in d.queue], ["1", "2", "3", "4", "5"])
        self.assertEqual(d.api.station_tracks.await_count, 2)

    async def test_autoplay_continues_after_the_last_track(self):
        d = self.daemon()
        await d.cmd_play({"tracks": [song(1)], "index": 0})
        await self.settle(d)
        self.assertEqual([t["id"] for t in d.queue], ["1", "7", "8"])
        self.assertTrue(d.context.get("autoplay"))
        d.api.continuous.assert_awaited_once()

    async def test_autoplay_off_or_repeat_leaves_the_queue_alone(self):
        d = self.daemon()
        d.settings["autoplay"] = False
        await d.cmd_play({"tracks": [song(1)], "index": 0})
        await self.settle(d)
        self.assertEqual([t["id"] for t in d.queue], ["1"])
        d2 = self.daemon()
        d2.repeat = "all"
        await d2.cmd_play({"tracks": [song(1)], "index": 0})
        await self.settle(d2)
        d2.api.continuous.assert_not_awaited()

    async def test_a_batch_for_an_old_queue_is_dropped(self):
        d = self.daemon()
        gate = asyncio.Event()

        async def slow(seeds, limit=10):
            await gate.wait()
            return {"id": "ra.cp-1"}, [song(9)]
        d.api.continuous = mock.AsyncMock(side_effect=slow)
        await d.cmd_play({"tracks": [song(1)], "index": 0})
        await self.settle(d)
        d.settings["autoplay"] = False
        await d.cmd_play({"tracks": [song(2)], "index": 0})
        gate.set()
        await self.settle(d)
        self.assertEqual([t["id"] for t in d.queue], ["2"])

    async def test_play_next_and_later(self):
        d = self.daemon()
        d.settings["autoplay"] = False
        await d.cmd_play({"tracks": [song(1), song(2)], "index": 0})
        await self.settle(d)
        await d.cmd_enqueue({"tracks": [song(9)], "mode": "next"})
        await d.cmd_enqueue({"tracks": [song(8)], "mode": "later"})
        self.assertEqual([t["id"] for t in d.queue], ["1", "9", "2", "8"])

    async def test_radio_page_leads_with_recent_stations(self):
        d = self.daemon()
        d.api.browse_page = mock.AsyncMock(return_value={"title": "Radio", "sections": [
            {"id": "x", "title": "On Air Now", "kind": "hero", "items": [], "seeAll": None}]})
        d.api.recent_stations = mock.AsyncMock(return_value=[brief(Shelves.station("ra.1"))])
        result = await d.cmd_browse({"page": "radio"})
        self.assertEqual([s["title"] for s in result["sections"]], ["Recently Played", "On Air Now"])
        d.api.browse_page.assert_awaited_with("radio")
        with self.assertRaises(ApiError):
            await d.cmd_browse({"page": "nope"})


class ApiShapes(unittest.TestCase):
    def test_play_assets_sends_the_play_params_as_strings(self):
        api = apple.AppleApi(media_user_token="t", storefront="us")
        api._request = mock.Mock(return_value={"results": {"assets": [{"url": "https://x"}]}})
        asyncio.run(api.play_assets({"id": "ra.1", "hasDrm": True, "mediaType": 0}))
        _method, uri, params = api._request.call_args.args[:3]
        self.assertEqual(uri, "/v1/play/assets")
        self.assertEqual(params, {"id": "ra.1", "hasDrm": "true", "mediaType": "0", "keyFormat": "web"})

    def test_the_radio_licence_goes_only_to_apple(self):
        api = apple.AppleApi(media_user_token="t", storefront="us")
        with self.assertRaises(ApiError):
            api.radio_license("https://evil.example/key", "data:x", "c")
        with self.assertRaises(ApiError):
            api.radio_license("http://linear.tv.apple.com/key", "data:x", "c")
        with self.assertRaises(ApiError):
            api.radio_license("https://linear.tv.apple.com.evil.example/key", "data:x", "c")


if __name__ == "__main__":
    unittest.main()
