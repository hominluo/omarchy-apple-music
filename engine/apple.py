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
import re
import time
from datetime import datetime, timezone

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

def scrape_developer_token() -> dict:
    """Read Apple's current web-player token out of the page bundle."""
    headers = {"User-Agent": USER_AGENT, "Accept": "text/html"}
    page = requests.get(f"{HOMEPAGE}/us/new", headers=headers, timeout=20)
    page.raise_for_status()
    match = BUNDLE_RE.search(page.text)
    if not match:
        raise ApiError("Could not find the web player bundle", code="unavailable")
    bundle = requests.get(f"{HOMEPAGE}/{match.group(1)}", headers=headers, timeout=30)
    bundle.raise_for_status()
    candidates = [_analyse(token) for token in set(JWT_RE.findall(bundle.text))]
    now = time.time()
    usable = [c for c in candidates if c["expiresAt"] > now + 60]
    web = [c for c in usable if "AMPWebPlay" in c["issuer"] or "WebPlayKid" in json.dumps(_jwt_payload(c["token"]))]
    chosen = (web or usable or candidates)
    if not chosen:
        raise ApiError("No developer token in the web player bundle", code="unavailable")
    chosen.sort(key=lambda c: c["expiresAt"], reverse=True)
    return chosen[0]


def developer_token(force: bool = False) -> str:
    path = config.cache_dir() / "devtoken.json"
    if not force:
        try:
            cached = json.loads(path.read_text("utf-8"))
            if cached.get("expiresAt", 0) > time.time() + 600 and cached.get("token"):
                return str(cached["token"])
        except (OSError, ValueError):
            pass
    token = scrape_developer_token()
    try:
        path.write_text(json.dumps(token, indent=2) + "\n", "utf-8")
    except OSError:
        pass
    return str(token["token"])


# ---- catalog shapes ------------------------------------------------------

def _art(attributes: dict, size: int = 360) -> str:
    artwork = attributes.get("artwork") or {}
    return config.artwork_url(artwork.get("url"), size)


def brief(item: dict, kind: str | None = None) -> dict:
    attributes = item.get("attributes") or {}
    item_type = kind or item.get("type") or ""
    item_id = str(item.get("id") or "")
    base = {
        "id": item_id,
        "type": item_type,
        "title": attributes.get("name") or attributes.get("title") or "",
        "artwork": _art(attributes),
        "library": bool(item.get("_library")) or item_id.startswith("i."),
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


def _with_library(items: list[dict], library: bool) -> list[dict]:
    return [brief(dict(item, _library=library)) for item in items if item.get("id")]


# Songs are shown with a cover and an artist line that both lead somewhere;
# asking for the relationships costs nothing extra and saves the daemon a
# second round trip per track.
_RELATED = "albums,artists"


class AppleApi:
    """One signed-in (or not) view of the catalog."""

    def __init__(self, media_user_token: str = "", storefront: str = "",
                 language: str = "en-US"):
        self.media_user_token = media_user_token or ""
        self.storefront = storefront or ""
        self.language = language
        self._developer = ""
        self.account = None
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
            self._developer = developer_token(force=force)
        return self._developer

    def _headers(self) -> dict:
        headers = {"authorization": f"Bearer {self._ensure_token()}"}
        if self.media_user_token:
            headers["cookie"] = f"media-user-token={self.media_user_token}"
        return headers

    def _request(self, method: str, url: str, params=None, payload=None,
                 retry_auth: bool = True) -> dict:
        if not url.startswith("http"):
            url = AMP + url
        try:
            response = self.session.request(
                method, url, params=params, json=payload,
                headers=self._headers(), timeout=30,
            )
        except requests.RequestException as error:
            raise ApiError(f"Network error: {error}", code="network") from error
        if response.status_code == 401 and retry_auth:
            # The developer token rotates; a 401 is the one case worth
            # re-scraping instead of surfacing.
            self._ensure_token(force=True)
            return self._request(method, url, params, payload, retry_auth=False)
        if response.status_code == 403:
            raise ApiError(
                "Apple Music refused the request. Sign in again (the music user "
                "token is missing, expired, or has no subscription).",
                status=403, code="unauthorized",
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
            "songs": _with_library((results.get("songs") or {}).get("data") or [], False),
            "albums": _with_library((results.get("albums") or {}).get("data") or [], False),
            "artists": _with_library((results.get("artists") or {}).get("data") or [], False),
            "playlists": _with_library((results.get("playlists") or {}).get("data") or [], False),
        }

    async def _collection(self, uri: str, params: dict, library: bool) -> dict:
        data = await self._get(uri, params)
        records = data.get("data") or []
        if not records:
            raise ApiError("Not found", code="not-found")
        record = records[0]
        info = brief(dict(record, _library=library))
        related = ((record.get("relationships") or {}).get("tracks") or {})
        tracks = [brief(dict(t, _library=library)) for t in (related.get("data") or [])]
        # Keep following the track pagination; a playlist can be huge.
        next_uri = related.get("next")
        guard = 0
        while next_uri and guard < 20:
            guard += 1
            page = await self._get(next_uri)
            tracks.extend(brief(dict(t, _library=library)) for t in (page.get("data") or []))
            next_uri = page.get("next")
        for index, track in enumerate(tracks):
            track["index"] = index
        return {"info": info, "tracks": tracks}

    async def album(self, album_id: str, library: bool = False) -> dict:
        if library:
            return await self._collection(
                f"/v1/me/library/albums/{album_id}", {"include": "tracks"}, True)
        return await self._collection(
            f"/v1/catalog/{self.storefront}/albums/{album_id}",
            {"include": "tracks", "limit[tracks]": 100, "l": self.language}, False)

    async def playlist(self, playlist_id: str, library: bool = False) -> dict:
        if library:
            return await self._collection(
                f"/v1/me/library/playlists/{playlist_id}", {"include": "tracks"}, True)
        return await self._collection(
            f"/v1/catalog/{self.storefront}/playlists/{playlist_id}",
            {"include": "tracks", "limit[tracks]": 100, "l": self.language}, False)

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
        info = brief(dict(record, _library=library))
        related = ((record.get("relationships") or {}).get("albums") or {})
        albums = list(related.get("data") or [])
        next_uri = related.get("next")
        guard = 0
        while next_uri and guard < 8:
            guard += 1
            page = await self._get(next_uri)
            albums.extend(page.get("data") or [])
            next_uri = page.get("next")
        return {"info": info, "albums": _with_library(albums, library)}

    async def library(self, kind: str = "songs", limit: int = 100, offset: int = 0) -> dict:
        kind = kind if kind in ("songs", "albums", "playlists", "artists") else "songs"
        params = {"limit": limit, "offset": offset, "l": self.language}
        if kind == "songs":
            params["include"] = _RELATED
        data = await self._get(f"/v1/me/library/{kind}", params)
        items = _with_library(data.get("data") or [], True)
        return {"items": items, "next": data.get("next")}

    async def home(self) -> dict:
        sections = []
        tasks = [
            ("recent", "/v1/me/recent/played/tracks",
             {"limit": 12, "include": _RELATED, "l": self.language}),
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
                items = _with_library(result.get("data") or [], True)
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
                    items = _with_library(flat, False)
                    if items:
                        sections.append({
                            "title": "Top " + ("Songs" if key == "songs" else "Albums"),
                            "kind": key, "items": items,
                        })
        return {"sections": sections}

    async def song(self, song_id: str, library: bool = False) -> dict:
        if library:
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
        return brief(dict(records[0], _library=library))

    # -- playback ---------------------------------------------------------
    async def webplayback(self, song_id: str, library: bool = False) -> dict:
        payload = {"universalLibraryId": song_id} if library else {"salableAdamId": song_id}
        payload["language"] = self.language
        data = await self._post(WEBPLAYBACK, payload)
        if "dialog" in data:
            raise ApiError(
                (data.get("dialog") or {}).get("explanation")
                or "Apple Music will not stream this track",
                code="unavailable",
            )
        return data

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
