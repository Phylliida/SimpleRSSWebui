"""
Minimal Flask app to collect RSS feed URLs.

Feeds are stored as an append-only JSONL log with entries shaped as:
{"action": "add_feed", "url": "<feed url>"}
The current feed list is derived by folding over the log, which keeps this
compatible with feedparser/reader: the API returns a simple list of feed URLs.
"""
from __future__ import annotations

import json
import os
import time
from html import escape
from io import BytesIO
from pathlib import Path
from typing import Iterable, List, Set

import feedparser
import listparser
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

LOG_PATH = Path(
    os.environ.get("FEED_LOG_PATH", Path(__file__).with_name("feeds.jsonl"))
)
INDEX_HTML_PATH = Path(__file__).with_name("index.html")
CACHE_DIR = Path(
    os.environ.get("FEED_CACHE_DIR", Path(__file__).with_name("cache"))
)
CACHE_ITEMS_PATH = CACHE_DIR / "items.json"


def _load_events(path: Path) -> List[dict]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _append_event(path: Path, event: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event))
        handle.write("\n")


def _clear_cache() -> None:
    try:
        CACHE_ITEMS_PATH.unlink()
    except FileNotFoundError:
        return


def _load_cached_items() -> List[dict]:
    if not CACHE_ITEMS_PATH.exists():
        return []
    try:
        with CACHE_ITEMS_PATH.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_cache(items: List[dict]) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with CACHE_ITEMS_PATH.open("w", encoding="utf-8") as handle:
        json.dump(items, handle)


def _feeds_from_events(events: Iterable[dict]) -> List[str]:
    feeds: List[str] = []
    present = set()
    for evt in events:
        url = str(evt.get("url", "")).strip()
        if not url:
            continue
        action = evt.get("action")
        if action == "add_feed" and url not in present:
            feeds.append(url)
            present.add(url)
        elif action == "remove_feed" and url in present:
            present.remove(url)
            feeds = [f for f in feeds if f != url]
    return feeds


def _feed_tags_from_events(events: Iterable[dict]) -> dict[str, Set[str]]:
    tags: dict[str, Set[str]] = {}
    for evt in events:
        action = evt.get("action")
        url = str(evt.get("url", "")).strip()
        tag = str(evt.get("tag", "")).strip().lower()
        if action == "remove_feed" and url:
            tags.pop(url, None)
        if not url or not tag:
            continue
        if action == "tag_feed":
            tags.setdefault(url, set()).add(tag)
        elif action == "untag_feed":
            current = tags.get(url)
            if current and tag in current:
                current.remove(tag)
                if not current:
                    tags.pop(url, None)
    return tags


def _favorite_feeds(events: Iterable[dict]) -> Set[str]:
    tags = _feed_tags_from_events(events)
    return {url for url, tagset in tags.items() if "favorite" in tagset}


def _state_payload(events: Iterable[dict] | None = None) -> dict:
    events_list = list(events) if events is not None else _load_events(LOG_PATH)
    tags = _feed_tags_from_events(events_list)
    return {
        "feeds": _feeds_from_events(events_list),
        "favorites": sorted(url for url, tagset in tags.items() if "favorite" in tagset),
        "tags": {url: sorted(tagset) for url, tagset in tags.items() if tagset},
    }


def current_feeds() -> List[str]:
    return _feeds_from_events(_load_events(LOG_PATH))


def _feeds_to_opml(feeds: Iterable[str]) -> str:
    outlines = "\n".join(
        f'    <outline type="rss" text="{escape(url)}" xmlUrl="{escape(url)}" />'
        for url in feeds
    )
    return "\n".join(
        [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<opml version=\"1.0\">",
            "  <head><title>SimpleRSSWebui</title></head>",
            "  <body>",
            outlines,
            "  </body>",
            "</opml>",
        ]
    )


def _entry_timestamp(entry: dict) -> float:
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    return time.mktime(stamp) if stamp else 0.0


def _entry_id(feed_url: str, entry: dict) -> str:
    preferred = entry.get("id") or entry.get("guid") or entry.get("link")
    if preferred:
        return f"{feed_url}|{preferred}"
    return f"{feed_url}|{entry.get('title', '')}|{entry.get('published') or entry.get('updated')}"


def _thumbnail_from_entry(entry: dict) -> str:
    def safe_url(val: object) -> str:
        url = str(val or "")
        return url if url.startswith(("http://", "https://")) else ""

    def first_url(items: object) -> str:
        if not items:
            return ""
        if isinstance(items, dict):
            return safe_url(items.get("url") or items.get("href"))
        if isinstance(items, (list, tuple)):
            for item in items:
                if isinstance(item, dict):
                    url = safe_url(item.get("url") or item.get("href"))
                else:
                    url = safe_url(getattr(item, "url", "") or getattr(item, "href", ""))
                if url:
                    return url
        return ""

    thumb = first_url(entry.get("media_thumbnail"))
    if not thumb:
        thumb = first_url(entry.get("media_content"))
    if thumb:
        return thumb

    video_id = str(entry.get("yt_videoid") or entry.get("videoid") or "")
    if "yt:video:" in video_id:
        video_id = video_id.split("yt:video:", 1)[1]
    if not video_id:
        link = str(entry.get("link") or "")
        if "youtube.com" in link and "v=" in link:
            video_id = link.split("v=", 1)[1].split("&", 1)[0]
        elif "youtu.be/" in link:
            video_id = link.split("youtu.be/", 1)[1].split("?", 1)[0]
    return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg" if video_id else ""


def _item_from_entry(feed_url: str, entry: dict) -> dict:
    return {
        "feed": feed_url,
        "id": _entry_id(feed_url, entry),
        "title": entry.get("title") or "(no title)",
        "link": entry.get("link"),
        "published": entry.get("published") or entry.get("updated") or "",
        "summary": entry.get("summary") or entry.get("description") or "",
        "thumbnail": _thumbnail_from_entry(entry),
        "_ts": _entry_timestamp(entry),
        "_viewed": False,
    }


def _gather_feed_items(feeds: Iterable[str]) -> List[dict]:
    items: List[dict] = []
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception:
            continue
        entries = parsed.entries if hasattr(parsed, "entries") else []
        items.extend(_item_from_entry(url, entry) for entry in entries)
    return items


def _refresh_cache(feeds: Iterable[str]) -> List[dict]:
    feed_list = list(feeds)
    items = _gather_feed_items(feed_list)
    _save_cache(items)
    return items


def _viewed_ids(events: Iterable[dict]) -> Set[str]:
    seen: Set[str] = set()
    for evt in events:
        item_id = str(evt.get("item_id") or "").strip()
        if not item_id:
            continue
        action = evt.get("action")
        if action == "mark_viewed":
            seen.add(item_id)
        elif action == "unmark_viewed" and item_id in seen:
            seen.remove(item_id)
    return seen


def _collect_items(
    feeds: Iterable[str],
    limit: int | None = 30,
    include_viewed: bool = False,
    viewed_ids: Set[str] | None = None,
    offset: int = 0,
    allowed_feeds: Set[str] | None = None,
) -> tuple[List[dict], int]:
    viewed_ids = viewed_ids or set()
    if limit is not None and limit < 0:
        limit = 0
    offset = max(0, offset)
    feed_list = list(feeds)
    allowed = set(feed_list if allowed_feeds is None else allowed_feeds)
    items = _load_cached_items()
    if not items:
        items = _refresh_cache(feed_list)
    else:
        items = list(items)
    if allowed:
        items = [i for i in items if i.get("feed") in allowed]
    else:
        items = []
    for item in items:
        item["_viewed"] = item.get("id") in viewed_ids
    if not include_viewed:
        items = [i for i in items if not i["_viewed"]]
    items.sort(key=lambda i: i["_ts"], reverse=True)
    total = len(items)
    trimmed = items[offset:] if offset else items
    if limit and limit > 0:
        trimmed = trimmed[:limit]
    cleaned = []
    for item in trimmed:
        base = {k: v for k, v in item.items() if k not in {"_ts", "_viewed"}}
        base["viewed"] = item["_viewed"]
        cleaned.append(base)
    return cleaned, total


@app.route("/api/feeds", methods=["GET"])
def api_list_feeds():
    return jsonify(_state_payload())


@app.route("/api/feeds", methods=["POST"])
def api_add_feed():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    feeds = current_feeds()
    if url in feeds:
        state = _state_payload()
        state["message"] = "already present"
        return jsonify(state)

    _append_event(LOG_PATH, {"action": "add_feed", "url": url})
    _clear_cache()
    state = _state_payload()
    state["message"] = "added"
    return jsonify(state), 201


@app.route("/api/feeds", methods=["DELETE"])
def api_delete_feed():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    feeds = current_feeds()
    if url not in feeds:
        state = _state_payload()
        state["message"] = "not present"
        return jsonify(state)

    _append_event(LOG_PATH, {"action": "remove_feed", "url": url})
    _clear_cache()
    state = _state_payload()
    state["message"] = "removed"
    return jsonify(state)


@app.route("/api/feeds/import", methods=["POST"])
def api_import_opml():
    uploaded = request.files.get("file")
    if not uploaded:
        return jsonify({"error": "file is required"}), 400
    try:
        raw = uploaded.read()
        payload = raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else raw
        parsed = listparser.parse(payload)
    except Exception:
        return jsonify({"error": "failed to parse opml"}), 400
    def feed_url(feed: object) -> str:
        if isinstance(feed, dict):
            return str(feed.get("url") or feed.get("xmlUrl") or "").strip()
        return str(getattr(feed, "url", "") or getattr(feed, "xmlUrl", "")).strip()
    urls: list[str] = []
    for feed in getattr(parsed, "feeds", []):
        url = feed_url(feed)
        if url:
            urls.append(url)
    if not urls:
        state = _state_payload()
        state.update({"imported": 0, "message": "no feeds found"})
        return jsonify(state)
    feeds = current_feeds()
    new_urls = [u for u in urls if u and u not in feeds]
    for url in new_urls:
        _append_event(LOG_PATH, {"action": "add_feed", "url": url})
    if new_urls:
        _clear_cache()
    state = _state_payload()
    state.update(
        {
            "imported": len(new_urls),
            "message": f"imported {len(new_urls)} new feeds",
        }
    )
    return jsonify(state)

@app.route("/api/feeds/export", methods=["GET"])
def api_export_opml():
    opml = _feeds_to_opml(current_feeds())
    buf = BytesIO(opml.encode("utf-8"))
    buf.seek(0)
    return send_file(
        buf,
        mimetype="text/x-opml",
        as_attachment=True,
        download_name="feeds.opml",
    )


@app.route("/api/feeds/refresh", methods=["POST"])
def api_refresh_feeds():
    events = _load_events(LOG_PATH)
    feeds = _feeds_from_events(events)
    items = _refresh_cache(feeds)
    state = _state_payload(events)
    state.update({"items_cached": len(items), "message": "refreshed"})
    return jsonify(state)


@app.route("/api/feeds/tags", methods=["POST", "DELETE"])
def api_feed_tags():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    tag = str(payload.get("tag", "")).strip().lower()
    if not url or not tag:
        return jsonify({"error": "url and tag are required"}), 400
    feeds = current_feeds()
    if url not in feeds:
        return jsonify({"error": "feed not present", "feeds": feeds}), 400
    action = "tag_feed" if request.method == "POST" else "untag_feed"
    _append_event(LOG_PATH, {"action": action, "url": url, "tag": tag})
    state = _state_payload()
    state["message"] = "tagged" if request.method == "POST" else "untagged"
    return jsonify(state)


@app.route("/", methods=["GET"])
def index():
    return send_file(INDEX_HTML_PATH, mimetype="text/html")


@app.route("/api/items", methods=["GET"])
def api_list_items():
    try:
        raw_limit = request.args.get("limit", "30")
        if isinstance(raw_limit, str) and raw_limit.lower() == "all":
            limit = 0
        else:
            limit = int(raw_limit)
    except (ValueError, TypeError):
        limit = 30
    events = _load_events(LOG_PATH)
    feeds = _feeds_from_events(events)
    viewed_ids = _viewed_ids(events)
    include_viewed = (
        str(request.args.get("include_viewed", "")).lower() in {"1", "true", "yes", "on"}
    )
    favorites_only = (
        str(request.args.get("favorites_only", "")).lower() in {"1", "true", "yes", "on"}
    )
    favorite_feeds = _favorite_feeds(events)
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    offset = 0
    if limit and limit > 0:
        offset = (page - 1) * limit
    allowed_feeds = favorite_feeds if favorites_only else set(feeds)
    if favorites_only and not allowed_feeds:
        page_size = limit if limit and limit > 0 else 0
        return jsonify({"items": [], "total": 0, "page": page, "page_size": page_size})
    items, total = _collect_items(
        feeds,
        limit=limit,
        include_viewed=include_viewed,
        viewed_ids=viewed_ids,
        offset=offset,
        allowed_feeds=allowed_feeds,
    )
    page_size = limit if limit and limit > 0 else total
    return jsonify(
        {"items": items, "total": total, "page": page, "page_size": page_size}
    )


@app.route("/api/items/viewed", methods=["POST"])
def api_mark_viewed():
    payload = request.get_json(silent=True) or {}
    item_id = str(payload.get("id", "")).strip()
    if not item_id:
        return jsonify({"error": "id is required"}), 400
    _append_event(LOG_PATH, {"action": "mark_viewed", "item_id": item_id})
    return jsonify({"id": item_id, "message": "marked"})


@app.route("/api/items/viewed", methods=["DELETE"])
def api_unmark_viewed():
    payload = request.get_json(silent=True) or {}
    item_id = str(payload.get("id", "")).strip()
    if not item_id:
        return jsonify({"error": "id is required"}), 400
    _append_event(LOG_PATH, {"action": "unmark_viewed", "item_id": item_id})
    return jsonify({"id": item_id, "message": "unmarked"})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
