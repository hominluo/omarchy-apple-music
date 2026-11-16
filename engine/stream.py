"""From a song id to a file mpv can play.

Apple's HLS audio is fMP4 encrypted with Widevine (``28:ctrp256`` for the
256 kbps AAC asset the web player uses). mpv speaks neither HLS-Widevine nor
CENC, so the daemon:

  1. asks the web playback endpoint for the signed master playlist,
  2. pulls the media playlist, picks the best variant and fetches every
     segment,
  3. exchanges a Widevine challenge for a content key with an Android L3
     device,
  4. decrypts the concatenated fragments with ffmpeg and keeps the result in
     the audio cache.

The cache is the reason a replayed track starts instantly and a queued one
starts gaplessly: the next track is built while the current one plays.
"""

import asyncio
import base64
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

from . import config
from .apple import ApiError

log = logging.getLogger("apple-music.stream")

CODEC_FLAVORS = {
    "aac-web": "28:ctrp256",
    "aac-he-web": "32:ctrp64",
}
WIDEVINE_KEYFORMAT = "urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"
SEGMENT_WORKERS = 6
DEFAULT_CACHE_LIMIT_MB = 1024
# Files build() returned in the last RECENT_SECONDS, {path: when}. The tracks
# really in use come from the daemon (StreamBuilder.in_use); a count of recent
# builds alone let the playing track drop out behind a second prefetch pass
# (shuffle turned on with prefetch 10) and be pruned. This covers only the
# moment between a build returning and the daemon's state naming the file
# (mpv's load, the prefetch window moving on), so it is kept by age, not by
# count: a dozen files kept by count made cacheLimitMb 0 mean "the tracks in
# use and the last twelve built". Module-wide because build(), prune() and
# clear_cache() all read it and a process has one cache-managing builder;
# probe runs in its own process with manage_cache=False and never prunes.
RECENT_SECONDS = 60.0
_RECENT: dict[Path, float] = {}
# Scratch folders of builds still running, or of cancelled builds whose ffmpeg
# thread has not finished yet. Sweeping build/ leaves these alone.
_ACTIVE_WORK: set[Path] = set()
# _ACTIVE_WORK only knows this process's builds. A routine sweep leaves
# anything younger than this alone, so a second process (`probe`, an old
# launcher) never removes a folder another one is still writing; a build
# lives at most about ffmpeg's 180 s plus ffprobe's 30 s.
SWEEP_MIN_AGE = 15 * 60


class StreamError(ApiError):
    pass


def _remember(path: Path) -> None:
    _RECENT[path] = time.monotonic()


def _recent() -> set[Path]:
    """The files built in the last RECENT_SECONDS; older entries go."""
    cutoff = time.monotonic() - RECENT_SECONDS
    for path, when in list(_RECENT.items()):
        if when < cutoff:
            del _RECENT[path]
    return set(_RECENT)


def cache_limit_mb() -> int:
    """cacheLimitMb, read as written: 0 is a real answer (keep only the
    tracks in use, see prune), not a missing one, and garbage means the
    default."""
    value = config.load_settings().get("cacheLimitMb", DEFAULT_CACHE_LIMIT_MB)
    if isinstance(value, bool):
        return DEFAULT_CACHE_LIMIT_MB
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_CACHE_LIMIT_MB


def work_dir() -> Path:
    path = config.cache_dir() / "build"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _discard_work(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)
    _ACTIVE_WORK.discard(path)


def sweep_work_dir(min_age: float = SWEEP_MIN_AGE) -> int:
    """Remove what failed or cancelled builds left in build/.

    Each build works in its own folder and removes it, but a daemon that was
    killed mid-build cannot, and older versions left loose files here. None of
    it counts towards cacheLimitMb or shows in the cache stats, so it would
    grow unseen. Folders of builds still running here are kept, and so is
    anything younger than `min_age`, which may be another process's build.
    Daemon startup and an explicit clear pass 0."""
    removed = 0
    try:
        entries = list(work_dir().iterdir())
    except OSError:
        return 0
    now = time.time()
    for entry in entries:
        if entry in _ACTIVE_WORK:
            continue
        if min_age > 0:
            try:
                if now - entry.lstat().st_mtime < min_age:
                    continue
            except OSError:
                continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _readable_failure(error: BaseException) -> str:
    """One line for the UI about a failed download or file step. CDN URLs
    are signed, so they stay out of it."""
    if isinstance(error, requests.HTTPError) and error.response is not None:
        return f"Apple's server refused the download (HTTP {error.response.status_code})"
    if isinstance(error, requests.Timeout):
        return "The download timed out"
    if isinstance(error, requests.ConnectionError):
        return "Could not reach Apple's servers"
    if isinstance(error, requests.RequestException):
        return f"The download failed ({type(error).__name__})"
    if isinstance(error, subprocess.TimeoutExpired):
        return "ffmpeg took too long with the track"
    if isinstance(error, OSError):
        detail = error.strerror or str(error)
        name = os.path.basename(str(error.filename)) if error.filename else ""
        return f"Could not prepare the track: {name + ': ' if name else ''}{detail}"
    return str(error) or type(error).__name__


class HlsInfo:
    """What one asset's playlist tells us about how to get the bytes."""

    def __init__(self):
        self.playlist_url = ""
        self.variant_url = ""
        # Each segment: {"url": str, "offset": int|None, "length": int|None}
        self.segments: list[dict] = []
        # The fMP4 init segment (#EXT-X-MAP), same shape, or None.
        self.init_segment: dict | None = None
        self.key_uri = ""
        self.key_format = ""
        self.track_id = ""
        self.flavor = ""


def _parse_attributes(text: str) -> dict:
    attributes = {}
    for match in re.finditer(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)', text):
        value = match.group(2)
        if value.startswith('"') and value.endswith('"'):
            value = value[1:-1]
        attributes[match.group(1)] = value
    return attributes


def _byterange(value: str) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    length, _, offset = value.partition("@")
    try:
        return (int(offset) if offset else None), int(length)
    except ValueError:
        return None, None


def _parse_m3u8(text: str, base_url: str) -> dict:
    """A deliberately small HLS reader: variants, keys, map and segments.

    Apple serves a single-file playlist: one .mp4 addressed by
    ``#EXT-X-BYTERANGE``, preceded by an ``#EXT-X-MAP`` init segment. Both
    shapes are kept, because other tracks come back as a plain list of
    segment URLs.
    """
    variants, keys, segments = [], [], []
    init_segment = None
    pending_range: tuple[int | None, int | None] = (None, None)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("#EXT-X-STREAM-INF:"):
            attributes = _parse_attributes(line.partition(":")[2])
            for following in lines[index + 1:]:
                if not following.startswith("#"):
                    variants.append((int(attributes.get("BANDWIDTH") or 0),
                                     urljoin(base_url, following)))
                    break
        elif line.startswith("#EXT-X-SESSION-KEY:") or line.startswith("#EXT-X-KEY:"):
            keys.append(_parse_attributes(line.partition(":")[2]))
        elif line.startswith("#EXT-X-MAP:"):
            attributes = _parse_attributes(line.partition(":")[2])
            offset, length = _byterange(attributes.get("BYTERANGE", ""))
            if attributes.get("URI"):
                init_segment = {
                    "url": urljoin(base_url, attributes["URI"]),
                    "offset": offset,
                    "length": length,
                }
        elif line.startswith("#EXT-X-BYTERANGE:"):
            pending_range = _byterange(line.partition(":")[2])
        elif not line.startswith("#"):
            offset, length = pending_range
            segments.append({
                "url": urljoin(base_url, line),
                "offset": offset,
                "length": length,
            })
            pending_range = (None, None)
        index += 1
    return {"variants": variants, "keys": keys, "segments": segments,
            "init": init_segment}


def _drm_uri(keys: list[dict], keyformat: str | None) -> str:
    """The licence URI for a key system, or the untyped one for CENC.

    Apple's CENC playlists carry ``#EXT-X-KEY:METHOD=ISO-23001-7`` with a
    ``data:;base64,<key id>`` URI and no KEYFORMAT at all, so "no keyformat"
    is a legitimate answer rather than a fallback.
    """
    if keyformat:
        for key in keys:
            if (key.get("KEYFORMAT") or "") == keyformat and key.get("URI"):
                return key["URI"]
        return ""
    for key in keys:
        if not key.get("KEYFORMAT") and key.get("URI"):
            return key["URI"]
    return ""


def reconstruct_pssh(uri: str) -> bytes:
    """Turn a Widevine key URI into a PSSH box pywidevine accepts.

    A short payload is the key id itself, which has to be wrapped in a
    WidevinePsshData; anything longer is already a serialised box.
    """
    raw = uri.split(",")[-1]
    try:
        decoded = base64.b64decode(raw)
    except (ValueError, TypeError) as error:
        raise StreamError(f"Bad Widevine PSSH: {error}") from error
    if len(decoded) > 30:
        return decoded
    from pywidevine.license_protocol_pb2 import WidevinePsshData

    data = WidevinePsshData(algorithm=1, key_ids=[decoded])
    return data.SerializeToString()


def _fetch(url: str, timeout: int = 30, offset: int | None = None,
           length: int | None = None) -> bytes:
    headers = {"User-Agent": "Mozilla/5.0"}
    if offset is not None and length is not None:
        headers["Range"] = f"bytes={offset}-{offset + length - 1}"
    response = requests.get(url, timeout=timeout, headers=headers)
    response.raise_for_status()
    return response.content


class StreamBuilder:
    """Builds playable files, one at a time per song."""

    def __init__(self, api, notify=None, in_use=None, manage_cache: bool = True):
        self.api = api
        self.notify = notify or (lambda *a, **k: None)
        # A callable naming the song ids in use (the file mpv has, the queue
        # position, the prefetch window); prune never deletes their files.
        self.in_use = in_use
        # False for a builder outside the daemon (probe): it builds its one
        # file and leaves pruning and sweeping to the daemon that owns them.
        self.manage_cache = manage_cache
        self._locks: dict[str, asyncio.Lock] = {}
        self._cdm = None

    # -- helpers ----------------------------------------------------------
    def _lock(self, key: str) -> asyncio.Lock:
        if key not in self._locks:
            self._locks[key] = asyncio.Lock()
        return self._locks[key]

    def cached_path(self, song_id: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", song_id)
        return config.audio_dir() / f"{safe}.m4a"

    def cdm(self):
        if self._cdm is None:
            from pywidevine import Cdm, Device

            if not config.DEVICE_PATH.exists():
                raise StreamError(
                    "No Widevine device file at "
                    f"{config.DEVICE_PATH} — put an L3 .wvd there or set "
                    "APPLE_MUSIC_WVD",
                    code="no-device",
                )
            device = Device.load(str(config.DEVICE_PATH))
            self._cdm = Cdm.from_device(device)
            self._cdm.MAX_NUM_OF_SESSIONS = float("inf")
        return self._cdm

    async def _progress(self, song_id: str, stage: str, **extra):
        payload = {"songId": song_id, "stage": stage, **extra}
        result = self.notify("build", payload)
        if asyncio.iscoroutine(result):
            await result

    # -- resolve ----------------------------------------------------------
    async def _playback(self, song_id: str, library: bool, codec: str) -> dict:
        playback = await self.api.webplayback(song_id, library)
        songs = playback.get("songList") or []
        if not songs:
            message = (playback.get("customerMessage")
                       or playback.get("failureType")
                       or "Apple returned no playable asset")
            raise StreamError(str(message), code="unavailable")
        flavor = CODEC_FLAVORS.get(codec, CODEC_FLAVORS["aac-web"])
        assets = songs[0].get("assets") or []
        asset = next((a for a in assets if a.get("flavor") == flavor), None)
        asset = asset or (assets[0] if assets else None)
        if not asset or not asset.get("URL"):
            raise StreamError("No stream URL for this track", code="unavailable")
        return {"url": asset["URL"], "trackId": songs[0].get("songId") or song_id,
                "flavor": asset.get("flavor")}

    async def resolve(self, song_id: str, library: bool = False, codec: str = "aac-web") -> HlsInfo:
        """Read the playlist(s) down to the bytes and the licence URI.

        The asset URL is usually the media playlist itself; when it is a
        master (it has EXT-X-STREAM-INF lines) the highest-bandwidth variant
        is followed. Both shapes end up in the same HlsInfo.
        """
        playback = await self._playback(song_id, library, codec)
        url = playback["url"]
        text = await asyncio.to_thread(lambda: _fetch(url).decode("utf-8", "ignore"))
        parsed = _parse_m3u8(text, url)
        playlist_url = url
        if parsed["variants"]:
            parsed["variants"].sort(key=lambda pair: pair[0], reverse=True)
            playlist_url = parsed["variants"][0][1]
            child = await asyncio.to_thread(
                lambda: _fetch(playlist_url).decode("utf-8", "ignore"))
            media = _parse_m3u8(child, playlist_url)
            media["keys"] = media["keys"] or parsed["keys"]
            parsed = media
        if not parsed["segments"]:
            raise StreamError("HLS playlist has no segments", code="unavailable")

        keys = parsed["keys"]
        info = HlsInfo()
        info.playlist_url = playlist_url
        info.segments = parsed["segments"]
        info.init_segment = parsed["init"]
        info.key_uri = _drm_uri(keys, None) or _drm_uri(keys, WIDEVINE_KEYFORMAT)
        info.key_format = "untyped" if info.key_uri and not any(
            (k.get("KEYFORMAT") or "") == WIDEVINE_KEYFORMAT for k in keys) else WIDEVINE_KEYFORMAT
        info.track_id = playback["trackId"]
        info.flavor = playback["flavor"]
        log.debug("resolved %s: %d segments, key=%s...", song_id, len(info.segments),
                  info.key_uri[:40])
        return info

    # -- license ----------------------------------------------------------
    async def _key(self, info: HlsInfo) -> str:
        if not info.key_uri:
            return ""
        from pywidevine import PSSH

        cdm = self.cdm()
        session_id = cdm.open()
        try:
            challenge = cdm.get_license_challenge(session_id, PSSH(reconstruct_pssh(info.key_uri)))
            encoded = base64.b64encode(challenge).decode()
            try:
                license_data = await self.api.license(info.track_id, info.key_uri, encoded)
            except ApiError:
                # Some assets only accept the canonical skd:// URI.
                license_data = await self.api.license(
                    info.track_id, f"skd://itunes.apple.com/{info.track_id}", encoded)
            cdm.parse_license(session_id, license_data["license"])
            content_keys = [k for k in cdm.get_keys(session_id) if k.type == "CONTENT"]
            if not content_keys:
                raise StreamError("Apple returned a license without a content key")
            return content_keys[0].key.hex()
        finally:
            cdm.close(session_id)

    # -- build ------------------------------------------------------------
    async def build(self, song: dict, codec: str = "aac-web", force: bool = False) -> Path:
        song_id = str(song.get("id") or "")
        if not song_id:
            raise StreamError("Missing song id")
        target = self.cached_path(song_id)
        if target.exists() and not force:
            try:
                os.utime(target)
            except OSError:
                pass
            _remember(target)
            return target
        lock = self._lock(song_id)
        async with lock:
            if target.exists() and not force:
                _remember(target)
                return target
            try:
                await self._build_locked(song, song_id, codec, target)
            except ApiError:
                raise
            except (requests.RequestException, subprocess.TimeoutExpired, OSError) as error:
                # The playlist and media fetches, the work files and the
                # ffmpeg calls fail with library exceptions; as StreamError
                # they take the daemon's failure path (mark the track failed,
                # count it, move on) instead of stalling the queue.
                raise StreamError(_readable_failure(error), code=(
                    "network" if isinstance(error, requests.RequestException) else "io")) from error
            _remember(target)
            await self._progress(song_id, "ready")
            if self.manage_cache:
                self.prune(keep={target})
            return target

    async def _build_locked(self, song: dict, song_id: str, codec: str, target: Path) -> None:
        await self._progress(song_id, "resolving")
        info = await self.resolve(song_id, bool(song.get("library")), codec)

        await self._progress(song_id, "downloading", total=len(info.segments))
        chunks = await self._download(info, song_id)

        await self._progress(song_id, "decrypting")
        key = await self._key(info)

        # One folder per attempt, removed however the attempt ends. A cancel
        # (skip, new queue) cannot stop the ffmpeg thread, so a fixed name
        # would let that orphan write over a retry of the same song.
        scratch = Path(tempfile.mkdtemp(dir=work_dir(), prefix=f"{target.stem}-"))
        _ACTIVE_WORK.add(scratch)
        worker = None
        try:
            encrypted = scratch / f"{target.stem}.enc"
            with open(encrypted, "wb") as handle:
                for chunk in chunks:
                    handle.write(chunk)
                del chunks

            partial = scratch / f"{target.stem}.m4a"
            cover = await self._cover(song, scratch, target.stem)
            # Shielded so a cancel leaves the worker running to its end and
            # the folder is removed after it, not under it.
            worker = asyncio.get_running_loop().run_in_executor(
                None, functools.partial(self._decrypt, encrypted, partial, key, song, cover))
            await asyncio.shield(worker)
            partial.replace(target)
        finally:
            if worker is not None and not worker.done():
                worker.add_done_callback(
                    lambda done: (done.cancelled() or done.exception(), _discard_work(scratch)))
            else:
                _discard_work(scratch)

    async def _cover(self, song: dict, workdir, song_id: str):
        url = str(song.get("artwork") or "")
        if not url:
            return None
        url = config.artwork_url(url, 640) if "{w}" in url else url
        path = workdir / f"{song_id}.jpg"
        try:
            data = await asyncio.to_thread(_fetch, url, 20)
            if len(data) < 1024:
                return None
            path.write_bytes(data)
            return path
        except Exception as error:  # noqa: BLE001 - artwork is optional
            log.debug("cover fetch failed: %s", error)
            return None

    async def _download(self, info: HlsInfo, song_id: str) -> list[bytes]:
        """Fetch the media, one file or many segments, in order.

        Apple's web assets are a single .mp4 addressed by byte ranges, with
        the init segment in front; a plain segment list also appears for
        older assets. Both are handled, and a byte range that the server
        ignores (200 instead of 206) is sliced locally rather than trusted.
        """
        urls = {segment["url"] for segment in info.segments}
        if len(urls) == 1:
            only = info.segments[0]["url"]
            total = (info.segments[-1]["offset"] or 0) + (info.segments[-1]["length"] or 0)
            whole = await asyncio.to_thread(_fetch, only, 120)
            if (info.init_segment and info.init_segment["url"] == only) or not total:
                data = whole
            elif len(whole) >= total:
                data = whole[:total]
            else:
                data = whole
            await self._progress(song_id, "downloading", done=len(info.segments),
                                 total=len(info.segments))
            return [data]

        semaphore = asyncio.Semaphore(SEGMENT_WORKERS)
        done = 0
        total_count = len(info.segments)

        async def fetch(segment: dict) -> bytes:
            nonlocal done
            async with semaphore:
                chunk = await asyncio.to_thread(
                    _fetch, segment["url"], 60, segment["offset"], segment["length"])
            done += 1
            if done == total_count or done % 8 == 0:
                await self._progress(song_id, "downloading", done=done, total=total_count)
            return chunk

        chunks = list(await asyncio.gather(*(fetch(segment) for segment in info.segments)))
        if info.init_segment:
            init = await asyncio.to_thread(
                _fetch, info.init_segment["url"], 60,
                info.init_segment["offset"], info.init_segment["length"])
            chunks.insert(0, init)
        return chunks

    @staticmethod
    def _decrypt(source: Path, target: Path, key: str, song: dict, cover=None) -> None:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise StreamError("ffmpeg is required to decrypt Apple Music streams")
        command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y"]
        if key:
            command += ["-decryption_key", key]
        command += ["-i", str(source)]
        if cover:
            command += ["-i", str(cover)]
        command += ["-map", "0:a", "-c:a", "copy"]
        if cover:
            command += ["-map", "1:v", "-c:v", "mjpeg", "-disposition:v", "attached_pic"]
        for tag in ("title", "artist", "album", "album_artist", "date", "track"):
            value = song.get(tag) or (song.get("albumArtist") if tag == "album_artist" else "")
            if value:
                command += ["-metadata", f"{tag}={value}"]
        command += ["-movflags", "+faststart", str(target)]
        result = subprocess.run(command, capture_output=True, text=True, timeout=180)
        if result.returncode != 0 or not target.exists() or target.stat().st_size == 0:
            if cover:
                # A cover that ffmpeg dislikes must not cost the track.
                StreamBuilder._decrypt(source, target, key, song, cover=None)
                return
            raise StreamError(
                "Decryption failed: " + (result.stderr.strip().splitlines() or ["ffmpeg error"])[-1])
        # A file left with ciphertext still in it plays as noise; probe once
        # instead of trusting that "ffmpeg exited 0" means the audio is clean.
        probe = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,channels",
             "-of", "json", str(target)],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0:
            raise StreamError("Decrypted file is not readable audio")

    # -- housekeeping -----------------------------------------------------
    @staticmethod
    def _audio_files() -> list[tuple[float, int, Path]]:
        """(mtime, size, path) of every cached track, oldest first. A file can
        vanish between the listing and the stat (clear cache, another prune),
        which must not fail the build that happened to call prune."""
        files = []
        for path in config.audio_dir().glob("*.m4a"):
            try:
                info = path.stat()
            except OSError:
                continue
            files.append((info.st_mtime, info.st_size, path))
        files.sort(key=lambda item: item[0])
        return files

    def prune(self, limit_mb: int | None = None, keep=()) -> None:
        """Delete the least recently used tracks until the cache fits.

        The tracks in use are never chosen: the playing one, the queue
        position and the prefetch window (the daemon's in_use), and any file
        built in the last RECENT_SECONDS (otherwise a limit smaller than one
        track hands mpv a path that is already gone). With a small limit the
        cache may stay over it by those few files; 0 therefore means "keep
        only the tracks in use", and a file kept only for being fresh goes at
        the first prune after its minute is up."""
        limit = cache_limit_mb() if limit_mb is None else max(0, int(limit_mb))
        kept = {Path(path) for path in keep} | _recent() | self._paths_in_use()
        files = self._audio_files()
        total = sum(size for _, size, _ in files)
        budget = limit * 1024 * 1024
        for _, size, path in files:
            if total <= budget:
                break
            if path in kept:
                continue
            try:
                path.unlink()
                total -= size
            except OSError:
                pass
        sweep_work_dir()

    def _paths_in_use(self) -> set[Path]:
        if self.in_use is None:
            return set()
        try:
            return {self.cached_path(str(song_id)) for song_id in self.in_use() if song_id}
        except Exception:  # noqa: BLE001 - a prune must never fail the build that ran it
            log.warning("could not tell which tracks are in use", exc_info=True)
            return set()

    def cache_stats(self) -> dict:
        files = self._audio_files()
        return {
            "tracks": len(files),
            "bytes": sum(size for _, size, _ in files),
            "limitMb": cache_limit_mb(),
        }

    def clear_cache(self, keep=()) -> dict:
        """Empty the audio cache, except `keep` (the daemon passes the track
        mpv is playing, so repeat and previous still find it), and sweep the
        leftovers of failed builds."""
        kept = {Path(path) for path in keep}
        removed = 0
        for path in config.audio_dir().glob("*"):
            if path in kept:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        removed += sweep_work_dir(min_age=0)
        for path in list(_RECENT):
            if path not in kept:
                del _RECENT[path]
        return {"removed": removed}
