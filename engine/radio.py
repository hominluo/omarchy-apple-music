"""Radio: Apple Music 1 and the other live stations, broadcasters, episodes.

A song is a file: it is downloaded, decrypted and handed to mpv whole (see
stream.py). A live station never ends, and an episode runs for two hours, so
neither can be built first. Instead the daemon serves mpv a decrypted copy of
the station's HLS stream, from a small HTTP server on the loopback interface:

  mpv ── GET /<token>/<session>/index.m3u8 ──▶ RadioProxy
            the station's media playlist, with every segment pointing back
            here and the keys left out
  mpv ── GET /<token>/<session>/<seq>.ts ──▶ RadioProxy
            fetches that segment and the init segment from Apple, gets the
            content key (once per key) from the radio key server with the
            local Widevine device, and has ffmpeg turn init+segment into a
            plain MPEG-TS segment

mpv's own HLS reader does the rest: it follows the live edge, re-reads the
playlist, buffers, and seeks within an episode. Broadcasters (NPR, iHeart,
TuneIn stations) carry no DRM and go to mpv as Apple's URL directly.

Apple's radio is CMAF with the `cbcs` scheme. Episodes declare no encryption
pattern in their `tenc` box, which ffmpeg refuses; every sample is in fact
encrypted whole, which is the 1:0 pattern, so the init segment is patched to
say so (live streams decrypt byte-identically either way).

The server listens on 127.0.0.1 only, and every path starts with a random
token that only this daemon and the mpv it starts know.
"""

import asyncio
import base64
import collections
import logging
import re
import secrets
import shutil
import time
from urllib.parse import urljoin, urlsplit

import requests

from . import stream
from .apple import ApiError

log = logging.getLogger("apple-music.radio")

WIDEVINE = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
PLAYLIST_MAX_BYTES = 4 * 1024 * 1024
SEGMENT_MAX_BYTES = 16 * 1024 * 1024
OUTPUT_MAX_BYTES = 32 * 1024 * 1024
FFMPEG_TIMEOUT = 30
# Decrypted segments kept in memory per session (~600 kB each at 256 kbps).
SEGMENT_CACHE = 8
# A live playlist is re-read at most this often, however often mpv asks.
LIVE_REFRESH_SECONDS = 2.0
# Sessions kept: the one playing and the one before it, so a quick switch
# back does not start over.
SESSIONS_KEPT = 2
REQUEST_LINE_MAX = 4096
CLIENTS_MAX = 8


class RadioError(ApiError):
    pass


def _fetch_capped(url: str, limit: int, timeout: int = 30) -> bytes:
    """GET `url`, refusing anything larger than `limit` bytes."""
    with requests.get(url, timeout=timeout, stream=True,
                      headers={"User-Agent": "Mozilla/5.0"}) as response:
        response.raise_for_status()
        declared = response.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise RadioError("A radio segment was larger than expected")
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > limit:
                raise RadioError("A radio segment was larger than expected")
            chunks.append(chunk)
        return b"".join(chunks)


def _attributes(text: str) -> dict:
    return stream._parse_attributes(text)


def patch_pattern(init: bytes) -> bytes:
    """Give a `cbcs` track encryption box the 1:0 pattern when it declares
    none (see the module notes). Anything else is returned untouched."""
    data = bytearray(init)
    start = 0
    while True:
        at = data.find(b"tenc", start)
        if at < 4:
            return bytes(data)
        box = at - 4
        version = data[at + 4] if at + 4 < len(data) else 0
        pattern = box + 13
        if version >= 1 and pattern < len(data) and data[pattern] == 0:
            data[pattern] = 0x10
        start = at + 4


def choose_variant(master: str, base_url: str) -> tuple[str, list[str]]:
    """The 256 kbps AAC variant of a master playlist (the highest bandwidth
    AAC-LC one), and the same-bandwidth backups on other hosts. A media
    playlist is its own variant."""
    if "#EXT-X-STREAM-INF" not in master:
        return base_url, []
    variants = []
    lines = [line.strip() for line in master.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:"):
            continue
        attributes = _attributes(line.partition(":")[2])
        uri = next((following for following in lines[index + 1:]
                    if not following.startswith("#")), "")
        if not uri:
            continue
        codecs = str(attributes.get("CODECS") or "")
        lc = "mp4a.40.2" in codecs or not codecs
        try:
            bandwidth = int(attributes.get("BANDWIDTH") or 0)
        except ValueError:
            bandwidth = 0
        variants.append((lc, bandwidth, urljoin(base_url, uri)))
    if not variants:
        raise RadioError("The station's playlist lists no audio")
    variants.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    best = variants[0]
    backups = [url for lc, bandwidth, url in variants[1:] if (lc, bandwidth) == best[:2]]
    return best[2], backups


class Segment:
    __slots__ = ("seq", "url", "duration", "init", "key", "discontinuity", "date")

    def __init__(self, seq, url, duration, init, key, discontinuity, date):
        self.seq = seq
        self.url = url
        self.duration = duration
        self.init = init
        self.key = key
        self.discontinuity = discontinuity
        self.date = date


def parse_media(text: str, base_url: str) -> dict:
    """The parts of a media playlist the proxy re-serves: target duration,
    first sequence number, VOD or live, and every segment with the init
    segment and Widevine key URI in force for it."""
    target, sequence, ended, vod = 10, 0, False, False
    discontinuity_seq = 0
    key, init = "", ""
    pending_duration, pending_disc, pending_date = None, False, ""
    segments: list[Segment] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            try:
                target = max(1, int(float(line.partition(":")[2])))
            except ValueError:
                pass
        elif line.startswith("#EXT-X-MEDIA-SEQUENCE:"):
            try:
                sequence = int(line.partition(":")[2])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-DISCONTINUITY-SEQUENCE:"):
            try:
                discontinuity_seq = int(line.partition(":")[2])
            except ValueError:
                pass
        elif line.startswith("#EXT-X-PLAYLIST-TYPE:"):
            vod = line.partition(":")[2].strip().upper() == "VOD"
        elif line.startswith("#EXT-X-ENDLIST"):
            ended = True
        elif line.startswith("#EXT-X-KEY:"):
            attributes = _attributes(line.partition(":")[2])
            method = str(attributes.get("METHOD") or "").upper()
            if method == "NONE":
                key = ""
            elif str(attributes.get("KEYFORMAT") or "") == WIDEVINE:
                key = str(attributes.get("URI") or "")
        elif line.startswith("#EXT-X-MAP:"):
            attributes = _attributes(line.partition(":")[2])
            if attributes.get("URI"):
                init = urljoin(base_url, attributes["URI"])
        elif line.startswith("#EXTINF:"):
            try:
                pending_duration = float(line.partition(":")[2].split(",")[0])
            except ValueError:
                pending_duration = float(target)
        elif line.startswith("#EXT-X-DISCONTINUITY") and not line.startswith(
                "#EXT-X-DISCONTINUITY-SEQUENCE"):
            pending_disc = True
        elif line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
            pending_date = line.partition(":")[2]
        elif not line.startswith("#"):
            seq = sequence + len(segments)
            segments.append(Segment(seq, urljoin(base_url, line),
                                    pending_duration if pending_duration is not None else float(target),
                                    init, key, pending_disc, pending_date))
            pending_duration, pending_disc, pending_date = None, False, ""
    return {"target": target, "sequence": sequence, "ended": ended or vod,
            "discontinuity": discontinuity_seq, "segments": segments}


class RadioSession:
    """One station being played: its playlist, its keys, its decrypted
    segments."""

    def __init__(self, proxy: "RadioProxy", session_id: str, station: dict,
                 media_url: str, backups: list[str], key_server: str):
        self.proxy = proxy
        self.id = session_id
        self.station = station
        self.media_url = media_url
        self.backups = list(backups)
        self.key_server = key_server
        self.segments: dict[int, Segment] = {}
        self.parsed: dict | None = None
        self.fetched_at = 0.0
        self.keys: dict[str, str] = {}
        self.inits: dict[str, bytes] = {}
        self.cache: "collections.OrderedDict[int, bytes]" = collections.OrderedDict()
        self.inflight: dict[int, asyncio.Future] = {}
        self._playlist_lock = asyncio.Lock()
        self._key_lock = asyncio.Lock()
        self.last_used = time.monotonic()

    # -- playlist ---------------------------------------------------------
    async def refresh(self, force: bool = False) -> dict:
        async with self._playlist_lock:
            fresh = self.parsed is not None and (
                self.parsed["ended"] or time.monotonic() - self.fetched_at < LIVE_REFRESH_SECONDS)
            if fresh and not force:
                return self.parsed
            text, url = await self._read_playlist()
            parsed = parse_media(text, url)
            if not parsed["segments"]:
                raise RadioError("The station's playlist is empty")
            self.parsed, self.fetched_at = parsed, time.monotonic()
            for segment in parsed["segments"]:
                self.segments[segment.seq] = segment
            # A live window slides; keep a little history for a paused mpv.
            oldest = parsed["segments"][0].seq - 64
            for seq in [seq for seq in self.segments if seq < oldest]:
                del self.segments[seq]
            return parsed

    async def _read_playlist(self) -> tuple[str, str]:
        errors = []
        for url in [self.media_url, *self.backups]:
            try:
                data = await asyncio.to_thread(_fetch_capped, url, PLAYLIST_MAX_BYTES, 15)
            except (requests.RequestException, RadioError) as error:
                errors.append(error)
                continue
            if url != self.media_url:
                # The backup answered: stay on it.
                self.backups = [self.media_url] + [b for b in self.backups if b != url]
                self.media_url = url
            return data.decode("utf-8", "ignore"), url
        raise RadioError(stream._readable_failure(errors[0]) if errors else "No playlist",
                         code="network")

    def local_playlist(self, parsed: dict) -> str:
        lines = ["#EXTM3U", "#EXT-X-VERSION:3",
                 f"#EXT-X-TARGETDURATION:{parsed['target']}",
                 f"#EXT-X-MEDIA-SEQUENCE:{parsed['sequence']}"]
        if parsed["discontinuity"]:
            lines.append(f"#EXT-X-DISCONTINUITY-SEQUENCE:{parsed['discontinuity']}")
        if parsed["ended"]:
            lines.append("#EXT-X-PLAYLIST-TYPE:VOD")
        for segment in parsed["segments"]:
            if segment.discontinuity:
                lines.append("#EXT-X-DISCONTINUITY")
            if segment.date:
                lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{segment.date}")
            lines.append(f"#EXTINF:{segment.duration:.5f},")
            lines.append(f"{segment.seq}.ts")
        if parsed["ended"]:
            lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    # -- segments ---------------------------------------------------------
    async def segment(self, seq: int) -> bytes:
        self.last_used = time.monotonic()
        if seq in self.cache:
            self.cache.move_to_end(seq)
            return self.cache[seq]
        future = self.inflight.get(seq)
        if future is None:
            future = asyncio.ensure_future(self._build(seq))
            self.inflight[seq] = future
            future.add_done_callback(lambda _f, seq=seq: self.inflight.pop(seq, None))
        data = await asyncio.shield(future)
        # mpv reads ahead one segment at a time; having the next one ready
        # keeps a slow licence or CDN answer from becoming a gap.
        following = seq + 1
        if following in self.segments and following not in self.cache and following not in self.inflight:
            prefetch = asyncio.ensure_future(self._build(following))
            self.inflight[following] = prefetch
            prefetch.add_done_callback(lambda f, s=following: (
                self.inflight.pop(s, None), f.cancelled() or f.exception()))
        return data

    async def _build(self, seq: int) -> bytes:
        record = self.segments.get(seq)
        if record is None and not (self.parsed and self.parsed["ended"]):
            await self.refresh(force=True)
            record = self.segments.get(seq)
        if record is None:
            raise KeyError(seq)
        media = await asyncio.to_thread(_fetch_capped, record.url, SEGMENT_MAX_BYTES, 30)
        init = await self._init(record.init) if record.init else b""
        key = await self._key(record.key) if record.key else ""
        data = await self.proxy.transmux(init + media, key)
        self.cache[seq] = data
        self.cache.move_to_end(seq)
        while len(self.cache) > SEGMENT_CACHE:
            self.cache.popitem(last=False)
        return data

    async def _init(self, url: str) -> bytes:
        if url not in self.inits:
            data = await asyncio.to_thread(_fetch_capped, url, SEGMENT_MAX_BYTES, 30)
            self.inits[url] = patch_pattern(data)
        return self.inits[url]

    async def _key(self, uri: str) -> str:
        if uri in self.keys:
            return self.keys[uri]
        async with self._key_lock:
            if uri not in self.keys:
                self.keys[uri] = await self.proxy.license(self.key_server, uri)
            return self.keys[uri]


class RadioProxy:
    def __init__(self, api_getter, builder_getter):
        self._api = api_getter
        self._builder = builder_getter
        self.token = secrets.token_urlsafe(18)
        self.server: asyncio.AbstractServer | None = None
        self.port = 0
        self.sessions: "collections.OrderedDict[str, RadioSession]" = collections.OrderedDict()
        self._clients = 0

    # -- lifecycle --------------------------------------------------------
    async def start(self) -> None:
        if self.server is not None:
            return
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0,
                                                 limit=REQUEST_LINE_MAX)
        self.port = self.server.sockets[0].getsockname()[1]
        log.info("radio proxy on 127.0.0.1:%d", self.port)

    async def close(self) -> None:
        server, self.server = self.server, None
        self.sessions.clear()
        if server is not None:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass

    # -- tuning -----------------------------------------------------------
    async def tune(self, station: dict) -> str:
        """The URL mpv should open for a stream station."""
        api = self._api()
        if api is None or not api.media_user_token:
            raise RadioError("Sign in to Apple Music to listen to radio", code="unauthorized")
        play = station.get("playParams") or {}
        if not play:
            raise RadioError("This station cannot be streamed", code="unavailable")
        asset = await api.play_assets(play)
        url = str(asset.get("url") or "")
        if not url.startswith("https://"):
            # Every stream Apple lists is https; anything else is not
            # handed to mpv.
            raise RadioError("Apple Music gave no stream for this station", code="unavailable")
        key_server = str(asset.get("keyServerUrl") or "")
        if not key_server or play.get("hasDrm") is False:
            # A broadcaster: a plain stream mpv can open itself.
            return url
        master = (await asyncio.to_thread(_fetch_capped, url, PLAYLIST_MAX_BYTES, 15)).decode(
            "utf-8", "ignore")
        media_url, backups = choose_variant(master, url)
        await self.start()
        session_id = secrets.token_hex(6)
        session = RadioSession(self, session_id, station, media_url, backups, key_server)
        await session.refresh()
        self.sessions[session_id] = session
        while len(self.sessions) > SESSIONS_KEPT:
            self.sessions.popitem(last=False)
        return f"http://127.0.0.1:{self.port}/{self.token}/{session_id}/index.m3u8"

    def owns(self, url: str) -> bool:
        return bool(self.port) and url.startswith(f"http://127.0.0.1:{self.port}/{self.token}/")

    async def license(self, key_server: str, key_uri: str) -> str:
        """The content key for one radio key URI, as hex."""
        from pywidevine import PSSH

        api = self._api()
        builder = self._builder()
        if api is None or builder is None:
            raise RadioError("The player is not ready", code="starting")
        cdm = builder.cdm()
        session = cdm.open()
        try:
            pssh = PSSH(stream.reconstruct_pssh(key_uri))
            challenge = base64.b64encode(cdm.get_license_challenge(session, pssh)).decode()
            answer = await asyncio.to_thread(api.radio_license, key_server, key_uri, challenge)
            cdm.parse_license(session, answer["license"])
            keys = [key for key in cdm.get_keys(session) if key.type == "CONTENT"]
            if not keys:
                raise RadioError("Apple returned a radio licence without a key")
            return keys[0].key.hex()
        finally:
            cdm.close(session)

    async def transmux(self, data: bytes, key: str) -> bytes:
        """init+segment (encrypted fMP4) → one decrypted MPEG-TS segment, its
        timestamps kept so consecutive segments line up."""
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RadioError("ffmpeg is required to play radio")
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin"]
        if key:
            command += ["-decryption_key", key]
        command += ["-i", "pipe:0", "-map", "0:a", "-c", "copy", "-copyts",
                    "-muxdelay", "0", "-muxpreload", "0", "-f", "mpegts", "pipe:1"]
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(process.communicate(data), FFMPEG_TIMEOUT)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()
            raise
        if process.returncode != 0 or not out:
            line = (err.decode("utf-8", "ignore").strip().splitlines() or ["ffmpeg error"])[-1]
            raise RadioError(f"Could not decrypt the radio stream: {line[:200]}")
        if len(out) > OUTPUT_MAX_BYTES:
            raise RadioError("A radio segment was larger than expected")
        return out

    # -- http -------------------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        if self._clients >= CLIENTS_MAX:
            writer.close()
            return
        self._clients += 1
        try:
            while True:
                try:
                    request = await asyncio.wait_for(reader.readline(), 30)
                except (asyncio.TimeoutError, asyncio.LimitOverrunError, ValueError):
                    return
                if not request:
                    return
                headers_ok = await self._skip_headers(reader)
                if not headers_ok:
                    return
                keep = await self._answer(request.decode("latin-1", "replace").strip(), writer)
                if not keep:
                    return
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._clients -= 1
            try:
                writer.close()
            except OSError:
                pass

    @staticmethod
    async def _skip_headers(reader) -> bool:
        for _ in range(100):
            try:
                line = await asyncio.wait_for(reader.readline(), 30)
            except (asyncio.TimeoutError, asyncio.LimitOverrunError, ValueError):
                return False
            if not line:
                return False
            if line in (b"\r\n", b"\n"):
                return True
        return False

    async def _answer(self, request: str, writer) -> bool:
        parts = request.split(" ")
        if len(parts) != 3 or parts[0] not in ("GET", "HEAD"):
            await self._send(writer, 405, b"", "text/plain")
            return False
        head = parts[0] == "HEAD"
        path = urlsplit(parts[1]).path
        match = re.fullmatch(r"/([A-Za-z0-9_-]+)/([0-9a-f]+)/(index\.m3u8|(\d+)\.ts)", path)
        if not match or not secrets.compare_digest(match.group(1), self.token):
            await self._send(writer, 404, b"", "text/plain")
            return False
        session = self.sessions.get(match.group(2))
        if session is None:
            await self._send(writer, 404, b"", "text/plain")
            return False
        try:
            if match.group(4) is None:
                parsed = await session.refresh()
                body = session.local_playlist(parsed).encode()
                kind = "application/vnd.apple.mpegurl"
            else:
                body = await session.segment(int(match.group(4)))
                kind = "video/mp2t"
        except KeyError:
            await self._send(writer, 404, b"", "text/plain")
            return True
        except Exception as error:  # noqa: BLE001 - mpv gets a 502 and retries or skips
            log.warning("radio %s: %s", path.rsplit("/", 1)[-1],
                        getattr(error, "message", "") or error)
            await self._send(writer, 502, b"", "text/plain")
            return True
        await self._send(writer, 200, b"" if head else body, kind, len(body))
        return True

    @staticmethod
    async def _send(writer, status: int, body: bytes, kind: str, length: int | None = None):
        reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed",
                  502: "Bad Gateway"}.get(status, "Error")
        head = (f"HTTP/1.1 {status} {reason}\r\nContent-Type: {kind}\r\n"
                f"Content-Length: {length if length is not None else len(body)}\r\n"
                "Cache-Control: no-store\r\nConnection: keep-alive\r\n\r\n").encode()
        writer.write(head + body)
        await writer.drain()
