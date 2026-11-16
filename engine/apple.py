"""The catalog side: Apple's web API, called the way the web player calls it.

Two tokens gate everything. The *developer* token is Apple's own, baked into
the music.apple.com bundle and refreshed on every deploy; we scrape it from
the page's index bundle and cache it until it expires. The *user* token is
the `media-user-token` cookie from a signed-in session (see auth.py).

Apple checks the Origin header, so requests carry ``Origin: music.apple.com``
even though they come from a Python process. http is done with ``requests``
in worker threads; the daemon is asyncio but nothing here is latency-bound
enough to want a second HTTP stack.
"""

import asyncio
import base64
import json
import logging
import random
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit

import requests

from . import config

log = logging.getLogger("apple-music.api")

HOMEPAGE = "https://music.apple.com"
AMP = "https://amp-api.music.apple.com"
WEBPLAYBACK = "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/webPlayback"
LICENSE = "https://play.itunes.apple.com/WebObjects/MZPlay.woa/wa/acquireWebPlaybackLicense"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
BUNDLE_RE = re.compile(r"/(assets/index[~-][^/\"]+\.js)")
JWT_RE = re.compile(r"\"(eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,})\"")

# Transient answers worth another try, and how hard to try. Requests run in
# worker threads and a caller is waiting on each one, so the whole budget is
# a few seconds: a Retry-After longer than RETRY_AFTER_MAX fails at once with
# a message that says so, instead of hanging a page load for a minute.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 0.5
RETRY_AFTER_MAX = 5.0
# A run of 401s (every request in flight when the token rotates) re-scrapes
# the multi-MB bundle once, not once each.
RESCRAPE_INTERVAL = 60.0
# Followed pages of one album/playlist and one artist's albums. Past this the
# result says "truncated" rather than silently stopping.
COLLECTION_PAGES = 20
ARTIST_PAGES = 8


class ApiError(Exception):
    """An error the daemon can explain to the UI."""

    def __init__(self, message, status=None, code="unavailable"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


def _jwt_payload(token: str) -> dict:
    try:
        body = token.split(".")[1]
        body += "=" * (-len(body) % 4)
        return json.loads(base64.urlsafe_b64decode(body))
    except (IndexError, ValueError, TypeError):
        return {}


def _analyse(token: str) -> dict:
    header_payload = _jwt_payload(token)
    return {
        "token": token,
        "issuedAt": header_payload.get("iat", 0),
        "expiresAt": header_payload.get("exp", 0),
        "issuer": header_payload.get("iss", ""),
    }


# ---- developer token -----------------------------------------------------

def _fetch_text(url: str, headers: dict, timeout: float) -> str:
    """One page of music.apple.com, with every way it can fail as ApiError:
    a raw ConnectionError used to escape the 401 path and leave a build
    spinning."""
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
    except requests.RequestException as error:
        raise ApiError(f"Network error: {_describe(error)}", code="network") from error
    if response.status_code >= 400:
        raise ApiError(f"music.apple.com answered {response.status_code}",
                       status=response.status_code, code="unavailable")
    return response.text


def _describe(error: Exception) -> str:
    """A requests error without the URL, which can carry signed parameters."""
    name = type(error).__name__
    if isinstance(error, requests.Timeout):
        return "timed out"
    if isinstance(error, requests.ConnectionError):
        return "could not connect"
    return name


def scrape_developer_token() -> dict:
    """Read Apple's current web-player token out of the page bundle."""
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    page = _fetch_text(f"{HOMEPAGE}/us/new", headers, 20)
    match = BUNDLE_RE.search(page)
    if not match:
        raise ApiError("Could not find the web player bundle", code="unavailable")
    bundle = _fetch_text(f"{HOMEPAGE}/{match.group(1)}", headers, 30)
    candidates = [_analyse(token) for token in set(JWT_RE.findall(bundle))]
    now = time.time()
    usable = [c for c in candidates if c["expiresAt"] > now + 60]
    web = [c for c in usable if "AMPWebPlay" in c["issuer"] or "WebPlayKid" in json.dumps(_jwt_payload(c["token"]))]
    chosen = (web or usable or candidates)
    if not chosen:
        raise ApiError("No developer token in the web player bundle", code="unavailable")
    chosen.sort(key=lambda c: c["expiresAt"], reverse=True)
    return chosen[0]


_scrape_lock = threading.Lock()
_last_scrape: float | None = None


def _cached_developer_token() -> dict:
    try:
        cached = json.loads(config.devtoken_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return cached if isinstance(cached, dict) else {}


def developer_token(force: bool = False, stale: str = "") -> str:
    """The cached developer token, or a freshly scraped one.

    `force` is a 401's re-scrape. `stale` names the token that 401'd: when
    another request has replaced it meanwhile, that one is used, and a
    forced scrape within RESCRAPE_INTERVAL of the last reuses its result, so
    a burst of 401s pulls the bundle once.
    """
    global _last_scrape
    with _scrape_lock:
        cached = _cached_developer_token()
        token = str(cached.get("token") or "")
        fresh = token and cached.get("expiresAt", 0) > time.time() + 600
        if fresh and not force:
            return token
        recent = (_last_scrape is not None
                  and time.monotonic() - _last_scrape < RESCRAPE_INTERVAL)
        if force and fresh and (token != stale or recent):
            return token
        scraped = scrape_developer_token()
        _last_scrape = time.monotonic()
        try:
            config.devtoken_path().write_text(json.dumps(scraped, indent=2) + "\n", "utf-8")
        except OSError:
            pass
        return str(scraped["token"])


# ---- catalog shapes ------------------------------------------------------

def _art(attributes: dict, size: int = 360) -> str:
    artwork = attributes.get("artwork") or {}
    return config.artwork_url(artwork.get("url"), size)


def in_library(item: dict) -> bool:
    """Whether a record is the user's own copy rather than a catalogue entry.

    The record says so itself, with a ``library-…`` type and an id in a
    library namespace (``i.`` songs, ``l.`` albums, ``p.`` playlists, ``r.``
    artists, ``a.`` playlist-only songs). The endpoint it came from does not:
    /v1/me/recent/played/tracks is a /me endpoint that answers with catalogue
    songs, and tagging those as library made every one of them unplayable.
    """
    if str(item.get("type") or "").startswith("library-"):
        return True
    return str(item.get("id") or "")[:2] in ("i.", "l.", "p.", "r.", "a.")


def playback_requests(song_id: str, catalog_id: str = "") -> list[dict]:
    """What to ask webPlayback for, best first. The id decides, not the page
    the track was listed on:

      - a catalogue id (all digits) is a ``salableAdamId``. Sent as a
        ``universalLibraryId`` Apple answers with a dialog, which the UI
        showed as "Apple Music will not stream this track";
      - ``i.…`` is a saved track and a ``universalLibraryId``, as it always
        was here; its catalogue twin is the fallback when Apple refuses that;
      - any other library id (``a.…``, a playlist track that was never added
        to the library) is refused as a ``universalLibraryId`` and only plays
        through its catalogue twin, so that goes first.
    """
    song_id, catalog_id = str(song_id or ""), str(catalog_id or "")
    if song_id.isdigit():
        return [{"salableAdamId": song_id}]
    own = {"universalLibraryId": song_id}
    twin = [{"salableAdamId": catalog_id}] if catalog_id and catalog_id != song_id else []
    return [own, *twin] if song_id.startswith("i.") else [*twin, own]


def brief(item: dict, kind: str | None = None) -> dict:
    attributes = item.get("attributes") or {}
    item_type = kind or item.get("type") or ""
    item_id = str(item.get("id") or "")
    base = {
        "id": item_id,
        "type": item_type,
        "title": attributes.get("name") or attributes.get("title") or "",
        "artwork": _art(attributes),
        "library": in_library(item),
    }
    # Where the cover and the artist name should lead. Only present when the
    # request asked for the relationships (see `_RELATED`), so absence is
    # normal and the UI has to cope with it.
    relationships = item.get("relationships") or {}
    for key, field in (("albums", "album"), ("artists", "artist")):
        related = ((relationships.get(key) or {}).get("data") or [])
        if not related:
            continue
        first = related[0] or {}
        related_id = str(first.get("id") or "")
        if related_id:
            base[field + "Id"] = related_id
            base[field + "Library"] = (related_id[:2] in ("l.", "r.")
                                       or str(first.get("type") or "").startswith("library-"))
    if item_type in ("songs", "song", "library-songs"):
        base.update({
            "type": "song",
            "artist": attributes.get("artistName") or "",
            "album": attributes.get("albumName") or "",
            "durationMs": int(attributes.get("durationInMillis") or 0),
            "discNumber": int(attributes.get("discNumber") or 1),
            "trackNumber": int(attributes.get("trackNumber") or 0),
            "explicit": (attributes.get("contentRating") == "explicit"),
        })
        # A library song names its catalogue twin; playback needs it for the
        # tracks Apple will not stream by their library id.
        catalog_id = str((attributes.get("playParams") or {}).get("catalogId") or "")
        if catalog_id and catalog_id != item_id:
            base["catalogId"] = catalog_id
    elif item_type in ("albums", "album", "library-albums"):
        base.update({
            "type": "album",
            "artist": attributes.get("artistName") or "",
            "trackCount": int(attributes.get("trackCount") or 0),
            "year": (attributes.get("releaseDate") or "")[:4],
        })
    elif item_type in ("playlists", "playlist", "library-playlists"):
        base.update({
            "type": "playlist",
            "artist": attributes.get("curatorName") or attributes.get("artistName") or "",
            "description": (attributes.get("description") or {}).get("short") or "",
        })
    elif item_type in ("artists", "artist", "library-artists"):
        base.update({"type": "artist", "artist": attributes.get("name") or ""})
    return base


def _briefs(items: list[dict]) -> list[dict]:
    return [brief(item) for item in items if item.get("id")]


# Songs are shown with a cover and an artist line that both lead somewhere;
# asking for the relationships costs nothing extra and saves the daemon a
# second round trip per track.
_RELATED = "albums,artists"


class AppleApi:
    """One signed-in (or not) view of the catalog."""

    def __init__(self, media_user_token: str = "", storefront: str = "",
                 language: str = "en-US", on_unauthorized=None):
        self.media_user_token = media_user_token or ""
        self.storefront = storefront or ""
        self.language = language
        self._developer = ""
        self.account = None
        # Called as on_unauthorized(api, token, error) from the worker thread
        # whenever Apple turns the user token away outside the licence call,
        # so a token that died mid-session shows up as signed out instead of
        # as a string of failed tracks.
        self.on_unauthorized = on_unauthorized
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Origin": HOMEPAGE,
            "Referer": HOMEPAGE + "/",
            "Accept": "application/json",
        })

    # -- plumbing ---------------------------------------------------------
    def _ensure_token(self, force: bool = False) -> str:
        if force or not self._developer:
            self._developer = developer_token(force=force, stale=self._developer)
        return self._developer

    def _headers(self, token: str | None = None) -> dict:
        headers = {"authorization": f"Bearer {self._ensure_token()}"}
        token = self.media_user_token if token is None else token
        if token:
            headers["cookie"] = f"media-user-token={token}"
        return headers

    def _send(self, method: str, url: str, params, payload, token: str,
              retries: int) -> requests.Response:
        """One request, tried again on a dropped connection or a transient
        status, with exponential backoff that honours Retry-After."""
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.session.request(
                    method, url, params=params, json=payload,
                    headers=self._headers(token), timeout=30,
                )
            except requests.RequestException as error:
                # A refused or reset connection fails fast and is worth a
                # retry; a timeout has already cost 30 s and is not. That
                # includes ConnectTimeout, which is also a ConnectionError:
                # retried, a network that drops SYNs held every request for
                # three timeouts, longer than the daemon's STARTUP_WAIT.
                if (attempt < retries and isinstance(error, requests.ConnectionError)
                        and not isinstance(error, requests.Timeout)):
                    time.sleep(_backoff(attempt))
                    continue
                raise ApiError(f"Network error: {_describe(error)}", code="network") from error
            if response.status_code not in RETRY_STATUSES or attempt >= retries:
                return response
            wait = _retry_after(response)
            if wait is not None and wait > RETRY_AFTER_MAX:
                return response  # too long to hold a caller; reported below
            time.sleep(_backoff(attempt) if wait is None else wait)

    def _request(self, method: str, url: str, params=None, payload=None,
                 retry_auth: bool = True) -> dict:
        if not url.startswith("http"):
            url = AMP + url
        token = self.media_user_token
        # Catalogue reads are idempotent, and so is the webPlayback lookup;
        # the licence exchange is tried exactly once, as it always was.
        retries = 1 if url == LICENSE else (
            RETRY_ATTEMPTS if method == "GET" or url == WEBPLAYBACK else 1)
        response = self._send(method, url, params, payload, token, retries)
        if response.status_code == 401 and retry_auth:
            # The developer token rotates; a 401 is the one case worth
            # re-scraping instead of surfacing. The re-scrape fails as an
            # ApiError like everything else (see _fetch_text).
            self._ensure_token(force=True)
            response = self._send(method, url, params, payload, token, retries)
            if response.status_code == 401 and token:
                # Still refused with a fresh developer token: the user token
                # is what Apple does not accept.
                raise self._unauthorized(url, token, 401)
        if response.status_code == 403:
            raise self._unauthorized(url, token, 403)
        if response.status_code == 429:
            wait = _retry_after(response)
            raise ApiError(
                "Apple Music is limiting requests; try again "
                + (f"in {int(wait + 0.999)} s" if wait else "in a moment"),
                status=429, code="unavailable",
            )
        if response.status_code >= 400:
            detail = ""
            try:
                errors = response.json().get("errors")
                if errors:
                    detail = "; ".join(str(e.get("title", e)) for e in errors)
            except ValueError:
                pass
            raise ApiError(
                f"Apple Music error {response.status_code}{': ' + detail if detail else ''}",
                status=response.status_code, code="unavailable",
            )
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError:
            return {}
        if isinstance(body, dict) and body.get("errors"):
            errors = body["errors"]
            raise ApiError(
                "; ".join(str(e.get("title", e)) for e in errors),
                code="unavailable",
            )
        return body

    def _unauthorized(self, url: str, token: str, status: int) -> ApiError:
        error = ApiError(
            "Apple Music refused the request. Sign in again (the music user "
            "token is missing, expired, or has no subscription).",
            status=status, code="unauthorized",
        )
        # The licence server refuses for its own reasons (the device, the
        # track); it says nothing about the session.
        if token and url != LICENSE and self.on_unauthorized is not None:
            try:
                self.on_unauthorized(self, token, error)
            except Exception:  # noqa: BLE001 - the caller still gets the error
                log.debug("on_unauthorized failed", exc_info=True)
        return error

    async def _get(self, uri: str, params=None) -> dict:
        return await asyncio.to_thread(self._request, "GET", uri, params)

    async def _post(self, url: str, payload: dict) -> dict:
        return await asyncio.to_thread(self._request, "POST", url, None, payload)

    # -- account ----------------------------------------------------------
    async def fetch_account(self) -> dict:
        data = await self._get("/v1/me/account", {"meta": "subscription"})
        self.account = data
        storefront = (((data.get("meta") or {}).get("subscription") or {}).get("storefront") or "")
        if storefront:
            self.storefront = storefront.lower()
        return data

    def subscription(self) -> dict:
        meta = (self.account or {}).get("meta") or {}
        return meta.get("subscription") or {}

    # -- catalog ----------------------------------------------------------
    async def search(self, term: str, kinds: str = "songs,albums,artists,playlists",
                     limit: int = 20) -> dict:
        if not term.strip():
            return {"songs": [], "albums": [], "artists": [], "playlists": []}
        data = await self._get(
            f"/v1/catalog/{self.storefront}/search",
            {"term": term, "types": kinds, "limit": limit, "l": self.language,
             "include[songs]": _RELATED},
        )
        results = data.get("results") or {}
        return {
            "songs": _briefs((results.get("songs") or {}).get("data") or []),
            "albums": _briefs((results.get("albums") or {}).get("data") or []),
            "artists": _briefs((results.get("artists") or {}).get("data") or []),
            "playlists": _briefs((results.get("playlists") or {}).get("data") or []),
        }

    async def _collection(self, uri: str, params: dict) -> dict:
        data = await self._get(uri, params)
        records = data.get("data") or []
        if not records:
            raise ApiError("Not found", code="not-found")
        record = records[0]
        info = brief(record)
        related = ((record.get("relationships") or {}).get("tracks") or {})
        tracks = [brief(t) for t in (related.get("data") or [])]
        # Keep following the track pagination; a playlist can be huge.
        pages, truncated = await self._follow(related.get("next"), COLLECTION_PAGES)
        for page in pages:
            tracks.extend(brief(t) for t in page)
        for index, track in enumerate(tracks):
            track["index"] = index
        return {"info": info, "tracks": tracks, "truncated": truncated}

    async def _follow(self, next_uri, limit: int) -> tuple[list[list], bool]:
        """Follow `next` links for up to `limit` pages: (pages of records,
        truncated). A page that still fails after its retries ends the walk
        with what came before it, flagged truncated, instead of throwing away
        a 1,500-track playlist for its ninth page. A dead session is not
        papered over: that error goes up."""
        pages: list[list] = []
        while next_uri:
            if len(pages) >= limit:
                return pages, True
            try:
                page = await self._get(next_uri)
            except ApiError as error:
                if error.code == "unauthorized":
                    raise
                log.warning("stopped paging after %d pages: %s", len(pages), error.message)
                return pages, True
            pages.append(list(page.get("data") or []))
            next_uri = page.get("next")
        return pages, False

    async def album(self, album_id: str, library: bool = False) -> dict:
        if library:
            return await self._collection(
                f"/v1/me/library/albums/{album_id}", {"include": "tracks"})
        return await self._collection(
            f"/v1/catalog/{self.storefront}/albums/{album_id}",
            {"include": "tracks", "limit[tracks]": 100, "l": self.language})

    async def playlist(self, playlist_id: str, library: bool = False) -> dict:
        if library:
            return await self._collection(
                f"/v1/me/library/playlists/{playlist_id}", {"include": "tracks"})
        return await self._collection(
            f"/v1/catalog/{self.storefront}/playlists/{playlist_id}",
            {"include": "tracks", "limit[tracks]": 100, "l": self.language})

    async def artist(self, artist_id: str, library: bool = False) -> dict:
        if library:
            data = await self._get(
                f"/v1/me/library/artists/{artist_id}", {"include": "albums"})
        else:
            data = await self._get(
                f"/v1/catalog/{self.storefront}/artists/{artist_id}",
                {"include": "albums", "l": self.language},
            )
        records = data.get("data") or []
        if not records:
            raise ApiError("Artist not found", code="not-found")
        record = records[0]
        info = brief(record)
        related = ((record.get("relationships") or {}).get("albums") or {})
        albums = list(related.get("data") or [])
        pages, truncated = await self._follow(related.get("next"), ARTIST_PAGES)
        for page in pages:
            albums.extend(page)
        return {"info": info, "albums": _briefs(albums), "truncated": truncated}

    async def library(self, kind: str = "songs", limit: int = 100, offset: int = 0) -> dict:
        """One page of the library: {items, next}, where `next` is the offset
        to ask for after this page, or None after the last one."""
        kind = kind if kind in ("songs", "albums", "playlists", "artists") else "songs"
        try:
            offset = max(0, int(offset or 0))
            limit = max(1, min(100, int(limit or 100)))  # Apple's own page cap
        except (TypeError, ValueError):
            offset, limit = 0, 100
        params = {"limit": limit, "offset": offset, "l": self.language}
        if kind == "songs":
            params["include"] = _RELATED
        data = await self._get(f"/v1/me/library/{kind}", params)
        records = data.get("data") or []
        items = _briefs(records)
        return {"items": items, "next": _next_offset(data.get("next"), offset, len(records))}

    async def home(self) -> dict:
        sections = []
        tasks = [
            # Both kinds, by name: without `types` Apple leaves out everything
            # played from the library. And `include` per type: this endpoint
            # ignores the plain `include`, which left the rows without the
            # album and artist their cover and artist line lead to.
            ("recent", "/v1/me/recent/played/tracks",
             {"limit": 12, "types": "songs,library-songs",
              "include[songs]": _RELATED, "include[library-songs]": _RELATED,
              "l": self.language}),
            ("charts", f"/v1/catalog/{self.storefront}/charts",
             {"types": "songs,albums", "limit": 12, "include[songs]": _RELATED,
              "l": self.language}),
        ]
        results = await asyncio.gather(
            *(self._get(uri, params) for _name, uri, params in tasks),
            return_exceptions=True,
        )
        for (name, _uri, _params), result in zip(tasks, results):
            if isinstance(result, Exception):
                log.debug("home section %s failed: %s", name, result)
                continue
            if name == "recent":
                items = _briefs(result.get("data") or [])
                if items:
                    sections.append({"title": "Recently Played", "kind": "songs", "items": items})
            else:
                charts = (result.get("results") or {})
                for key in ("songs", "albums"):
                    # The charts endpoint nests each entry as {"data": [item]}.
                    flat = []
                    for entry in (charts.get(key) or []):
                        if "id" in entry:
                            flat.append(entry)
                        else:
                            flat.extend(entry.get("data") or [])
                    items = _briefs(flat)
                    if items:
                        sections.append({
                            "title": "Top " + ("Songs" if key == "songs" else "Albums"),
                            "kind": key, "items": items,
                        })
        return {"sections": sections}

    async def song(self, song_id: str, library: bool = False) -> dict:
        # A numeric id is a catalogue id, whatever the caller was told.
        if library and not str(song_id).isdigit():
            data = await self._get(f"/v1/me/library/songs/{song_id}",
                                   {"include": _RELATED, "l": self.language})
        else:
            data = await self._get(
                f"/v1/catalog/{self.storefront}/songs/{song_id}",
                {"include": _RELATED, "l": self.language},
            )
        records = data.get("data") or []
        if not records:
            raise ApiError("Song not found", code="not-found")
        return brief(records[0])

    # -- playback ---------------------------------------------------------
    async def webplayback(self, song_id: str, catalog_id: str = "") -> dict:
        """The signed playlist for a track, asked for by the id it answers to
        (see playback_requests). A refusal moves on to the next way of asking;
        a transport error does not, it is raised by _post as it always was.
        When every way is refused, the first refusal is the one reported."""
        refused: dict | None = None
        for payload in playback_requests(song_id, catalog_id):
            data = await self._post(WEBPLAYBACK, {**payload, "language": self.language})
            if data.get("songList"):
                return data
            refused = data if refused is None else refused
        refused = refused or {}
        if "dialog" in refused:
            raise ApiError(
                (refused.get("dialog") or {}).get("explanation")
                or "Apple Music will not stream this track",
                code="unavailable",
            )
        return refused

    async def license(self, track_id: str, uri: str, challenge: str) -> dict:
        data = await self._post(LICENSE, {
            "challenge": challenge,
            "key-system": "com.widevine.alpha",
            "uri": uri,
            "adamId": track_id,
            "isLibrary": False,
            "user-initiated": True,
        })
        if data.get("status") not in (0, None):
            raise ApiError(
                f"License exchange failed ({data.get('status')}): "
                f"{data.get('statusString') or ''}".strip(),
                code="unavailable",
            )
        return data


def _backoff(attempt: int) -> float:
    """0.5 s, 1 s, 2 s ..., with jitter so concurrent retries spread out."""
    return RETRY_BACKOFF * (2 ** (attempt - 1)) * random.uniform(0.8, 1.2)


def _retry_after(response) -> float | None:
    """Retry-After in seconds (delta or HTTP date), or None without one."""
    value = (response.headers.get("Retry-After") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


def _next_offset(next_uri, offset: int, count: int) -> int | None:
    """The offset of the page after this one, from Apple's `next` link, or
    None after the last page."""
    if not next_uri:
        return None
    try:
        values = parse_qs(urlsplit(str(next_uri)).query).get("offset")
        if values:
            return max(0, int(values[0]))
    except (TypeError, ValueError):
        pass
    return offset + count if count else None


def storefront_from_account(account: dict | None, fallback: str = "us") -> str:
    try:
        value = account["meta"]["subscription"]["storefront"]
    except (KeyError, TypeError):
        return fallback
    return (value or fallback).lower()


def subscription_end(account: dict | None) -> str:
    try:
        subscription = account["meta"]["subscription"]
    except (KeyError, TypeError):
        return ""
    return str(subscription.get("subscriptionEndDate") or "")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
