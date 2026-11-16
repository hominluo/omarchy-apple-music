"""Apple's pages, turned into shelves the window can draw.

music.apple.com builds Home, New, Radio, every genre page and every radio
show's page out of the same few records, so one reader serves them all:

  - an editorial *grouping* (New, Radio, a genre, a show) is a tree of
    `editorial-elements`. An element with `contents` is a shelf; one with
    `children` is either a carousel of one-item children (the hero rows at
    the top, On Air Now) or a list of shelves; one with `links` is a row of
    links (genres, Charts, Moods and Activities). A shelf's `room` is its
    See All page.
  - Home is a list of `personal-recommendation` records, each a shelf.
  - a *room* is one See All page: a title and paged `contents`.

Every item goes through apple.brief, so a shelf holds the same item shapes
as search results and the library. What cannot be played here (videos,
concerts) is kept with `playable: false` or left out, never shown as
something that would play.
"""

import re
from urllib.parse import parse_qs, urlsplit
from xml.etree import ElementTree

from .apple import brief

# Shelves Apple fills with things an audio player has no use for. A shelf is
# dropped when nothing in it can be played or opened.
_UNOPENABLE = frozenset({"video"})
# Links that lead outside what the window can show.
_SKIPPED_LINK_LABELS = frozenset({"concerts", "music videos"})
SECTION_LIMIT = 40


def _kids(node: dict, key: str) -> list[dict]:
    return list((((node.get("relationships") or {}).get(key) or {}).get("data")) or [])


def _title(attributes: dict) -> str:
    title = attributes.get("title")
    if isinstance(title, dict):
        title = title.get("stringForDisplay") or title.get("stringForDisplayShort")
    return str(title or attributes.get("name") or "")


def _items(records: list[dict]) -> list[dict]:
    """Briefs for a shelf's records, editorial items resolved, duplicates
    (a hero carousel repeats one station five times) dropped."""
    seen, items = set(), []
    for record in records:
        if not isinstance(record, dict) or not record.get("id"):
            continue
        item = editorial_item(record) if record.get("type") == "editorial-items" else brief(record)
        if not item:
            continue
        key = (item.get("type"), item.get("id"))
        if key in seen:
            continue
        seen.add(key)
        items.append(item)
    return items


def editorial_item(record: dict) -> dict | None:
    """An editorial item is a card that points at something else: the record
    in its contents if there is one, else the page its link names."""
    contents = _kids(record, "contents") or _kids(record, "primary-content")
    if contents:
        item = brief(contents[0])
        attributes = record.get("attributes") or {}
        designed = str(attributes.get("designBadge") or "")
        if designed and not item.get("subtitle"):
            item["subtitle"] = designed
        return item
    attributes = record.get("attributes") or {}
    link = attributes.get("link") or {}
    target = link_target(str(link.get("url") or ""), _title(attributes) or str(link.get("label") or ""))
    if not target:
        return None
    item = brief(record)
    item.update({"type": "link", "title": _title(attributes) or str(link.get("label") or ""),
                 "target": target})
    return item


def link_target(url: str, label: str = "") -> dict | None:
    """Where one of Apple's links leads, as a view the window has, or None.

    Editorial links come in the old iTunes Store shapes (viewGrouping?id=,
    collection/…?fcId=, viewTop) as well as music.apple.com paths."""
    if not url:
        return None
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    path = parts.path
    title = label or ""
    if "viewGrouping" in path and query.get("id"):
        return {"view": "grouping", "args": {"id": query["id"][0], "title": title}}
    if "/collection/" in path and query.get("fcId"):
        return {"view": "room", "args": {"id": query["fcId"][0], "title": title}}
    if "viewTop" in path or path.rstrip("/").endswith("/charts"):
        return {"view": "charts", "args": {"title": title or "Charts"}}
    match = re.search(r"/(room|curator|playlist|album|artist)/(?:[^/]+/)?([A-Za-z0-9.\-]+)$",
                      path.rstrip("/"))
    if match:
        kind, ident = match.groups()
        if kind == "room":
            return {"view": "room", "args": {"id": ident, "title": title}}
        if kind == "curator":
            return {"view": "curator", "args": {"id": ident, "title": title, "kind": "apple-curators"}}
        return {"view": kind, "args": {"id": ident, "title": title, "library": False}}
    return None


def _links_section(element: dict, title: str) -> dict | None:
    items = []
    for link in (element.get("attributes") or {}).get("links") or []:
        label = str(link.get("label") or "")
        if not label or label.lower() in _SKIPPED_LINK_LABELS or link.get("target") == "external":
            continue
        target = link_target(str(link.get("url") or ""), label)
        if target:
            items.append({"id": f"{element.get('id')}:{len(items)}", "type": "link",
                          "title": label, "target": target, "artwork": "",
                          "library": False, "playable": True})
    if not items:
        return None
    return {"id": str(element.get("id") or ""), "title": title, "kind": "links",
            "items": items, "seeAll": None}


def _section(element: dict, items: list[dict], title: str, hero: bool = False) -> dict | None:
    items = [item for item in items if item.get("type") not in _UNOPENABLE]
    if not items:
        return None
    rooms = _kids(element, "room")
    see_all = None
    if rooms:
        see_all = {"view": "room", "args": {"id": str(rooms[0].get("id")), "title": title}}
    if hero:
        kind = "hero"
    elif all(item.get("type") == "song" for item in items):
        kind = "songs"
    else:
        kind = "cards"
    return {"id": str(element.get("id") or ""), "title": title, "kind": kind,
            "items": items[:SECTION_LIMIT], "seeAll": see_all}


def _is_carousel(children: list[dict]) -> bool:
    """Children that each hold a single item and no title of their own are
    one carousel, not a list of one-card shelves."""
    if not children:
        return False
    for child in children:
        attributes = child.get("attributes") or {}
        if _kids(child, "children") or len(_kids(child, "contents")) > 1:
            return False
        if _title(attributes) and _kids(child, "room"):
            return False
    return True


def _walk(element: dict, sections: list[dict], depth: int = 0) -> None:
    if depth > 6:
        return
    attributes = element.get("attributes") or {}
    title = _title(attributes)
    if attributes.get("links"):
        section = _links_section(element, title or "Browse by Genre")
        if section:
            sections.append(section)
        return
    contents = _kids(element, "contents")
    children = _kids(element, "children")
    if contents:
        section = _section(element, _items(contents), title)
        if section:
            sections.append(section)
    if not children:
        return
    if _is_carousel(children):
        records = [record for child in children for record in _kids(child, "contents")]
        section = _section(element, _items(records), title or "Featured", hero=True)
        if section:
            sections.append(section)
        return
    for child in children:
        _walk(child, sections, depth + 1)


def grouping_sections(data: dict) -> tuple[str, list[dict]]:
    """(page title, sections) of an editorial grouping answer."""
    records = data.get("data") or []
    if not records:
        return "", []
    grouping = records[0]
    title = _title(grouping.get("attributes") or {})
    sections: list[dict] = []
    tabs = _kids(grouping, "tabs") or [grouping]
    for tab in tabs[:1]:
        for child in _kids(tab, "children"):
            _walk(child, sections)
    return title, _dedupe_titles(sections)


def _dedupe_titles(sections: list[dict]) -> list[dict]:
    """Two untitled hero rows in a row read as one; merge them."""
    merged: list[dict] = []
    for section in sections:
        if merged and section["kind"] == "hero" and merged[-1]["kind"] == "hero":
            previous = merged[-1]
            known = {(i.get("type"), i.get("id")) for i in previous["items"]}
            fresh = [i for i in section["items"] if (i.get("type"), i.get("id")) not in known]
            if not fresh or section["title"] in ("Featured", previous["title"]):
                # The radio page leads with a carousel of the live stations
                # and then lists the same stations as On Air Now: one row,
                # under the name that says something.
                previous["items"].extend(fresh)
                if previous["title"] == "Featured" and section["title"] != "Featured":
                    previous["title"] = section["title"]
                continue
        merged.append(section)
    return merged


def recommendation_sections(data: dict) -> list[dict]:
    """Home: each personal recommendation is one shelf."""
    sections = []
    for record in data.get("data") or []:
        attributes = record.get("attributes") or {}
        title = _title(attributes)
        display = str(((attributes.get("display") or {}).get("kind")) or "")
        contents = _kids(record, "contents")
        if not contents:
            continue
        section = _section(record, _items(contents), title, hero="Hero" in display)
        if section:
            sections.append(section)
    return sections


def room_page(data: dict) -> tuple[str, list[dict], str | None]:
    """(title, items, next href) of a room or of one page of its contents."""
    records = data.get("data") or []
    if records and records[0].get("type") == "rooms":
        room = records[0]
        related = (room.get("relationships") or {}).get("contents") or {}
        return (_title(room.get("attributes") or {}), _items(related.get("data") or []),
                related.get("next"))
    return "", _items(records), data.get("next")


ARTIST_VIEWS = (
    ("latest-release", "cards"),
    ("top-songs", "songs"),
    ("full-albums", "cards"),
    ("singles", "cards"),
    ("live-albums", "cards"),
    ("compilation-albums", "cards"),
    ("featured-albums", "cards"),
    ("appears-on-albums", "cards"),
    ("featured-playlists", "cards"),
    ("similar-artists", "cards"),
)


def artist_sections(record: dict) -> list[dict]:
    views = record.get("views") or {}
    sections = []
    for name, kind in ARTIST_VIEWS:
        view = views.get(name) or {}
        items = _items(view.get("data") or [])
        if not items:
            continue
        title = str((view.get("attributes") or {}).get("title") or name.replace("-", " ").title())
        sections.append({"id": name, "title": title, "kind": kind,
                         "items": items[:SECTION_LIMIT], "seeAll": None})
    return sections


# ---- lyrics ------------------------------------------------------------------

LYRICS_MAX_BYTES = 1024 * 1024
_TTML = "{http://www.w3.org/ns/ttml}"


def _seconds(value: str | None) -> float | None:
    """TTML clock values: 12.345, 1:02.5, 1:02:03.25, or 12.3s."""
    if not value:
        return None
    text = value.strip().rstrip("s")
    try:
        parts = [float(part) for part in text.split(":")]
    except ValueError:
        return None
    total = 0.0
    for part in parts:
        total = total * 60 + part
    return total


def parse_lyrics(ttml: str) -> dict:
    """{lines: [{begin, end, text}], synced, writers} from Apple's TTML.

    The document comes from Apple's API, but it is still remote markup: a
    DOCTYPE (the only way to declare entities) is refused outright, and so is
    anything larger than any real lyric sheet."""
    if not ttml:
        return {"lines": [], "synced": False, "writers": []}
    if len(ttml.encode("utf-8", "ignore")) > LYRICS_MAX_BYTES or "<!DOCTYPE" in ttml.upper():
        raise ValueError("Unexpected lyrics document")
    root = ElementTree.fromstring(ttml)
    timing = ""
    for key, value in root.attrib.items():
        if key.endswith("timing"):
            timing = value
    writers = [str(node.text or "").strip() for node in root.iter()
               if node.tag.endswith("songwriter") and (node.text or "").strip()]
    lines = []
    for paragraph in root.iter(f"{_TTML}p"):
        text = "".join(paragraph.itertext()).strip()
        text = re.sub(r"\s+", " ", text)
        begin = _seconds(paragraph.get("begin"))
        end = _seconds(paragraph.get("end"))
        lines.append({"begin": begin if begin is not None else 0.0,
                      "end": end if end is not None else 0.0,
                      "text": text})
    synced = timing.lower() != "none" and any(line["begin"] > 0 or line["end"] > 0 for line in lines)
    return {"lines": lines, "synced": synced, "writers": writers}
