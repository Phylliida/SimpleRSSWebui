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


def _item_from_entry(feed_url: str, entry: dict) -> dict:
    return {
        "feed": feed_url,
        "id": _entry_id(feed_url, entry),
        "title": entry.get("title") or "(no title)",
        "link": entry.get("link"),
        "published": entry.get("published") or entry.get("updated") or "",
        "summary": entry.get("summary") or entry.get("description") or "",
        "_ts": _entry_timestamp(entry),
        "_viewed": False,
    }


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
) -> tuple[List[dict], int]:
    viewed_ids = viewed_ids or set()
    if limit is not None and limit < 0:
        limit = 0
    offset = max(0, offset)
    items = []
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception:
            continue
        entries = parsed.entries if hasattr(parsed, "entries") else []
        items.extend(
            _item_from_entry(url, entry) for entry in entries
        )
    for item in items:
        item["_viewed"] = item["id"] in viewed_ids
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
    return jsonify({"feeds": current_feeds()})


@app.route("/api/feeds", methods=["POST"])
def api_add_feed():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    feeds = current_feeds()
    if url in feeds:
        return jsonify({"feeds": feeds, "message": "already present"})

    _append_event(LOG_PATH, {"action": "add_feed", "url": url})
    return jsonify({"feeds": current_feeds(), "message": "added"}), 201


@app.route("/api/feeds", methods=["DELETE"])
def api_delete_feed():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    if not url:
        return jsonify({"error": "url is required"}), 400

    feeds = current_feeds()
    if url not in feeds:
        return jsonify({"feeds": feeds, "message": "not present"})

    _append_event(LOG_PATH, {"action": "remove_feed", "url": url})
    return jsonify({"feeds": current_feeds(), "message": "removed"})


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
        return jsonify({"feeds": current_feeds(), "imported": 0, "message": "no feeds found"})
    feeds = current_feeds()
    new_urls = [u for u in urls if u and u not in feeds]
    for url in new_urls:
        _append_event(LOG_PATH, {"action": "add_feed", "url": url})
    return jsonify(
        {
            "feeds": current_feeds(),
            "imported": len(new_urls),
            "message": f"imported {len(new_urls)} new feeds",
        }
    )

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
    viewed_ids = _viewed_ids(events)
    include_viewed = (
        str(request.args.get("include_viewed", "")).lower() in {"1", "true", "yes", "on"}
    )
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    offset = 0
    if limit and limit > 0:
        offset = (page - 1) * limit
    items, total = _collect_items(
        current_feeds(),
        limit=limit,
        include_viewed=include_viewed,
        viewed_ids=viewed_ids,
        offset=offset,
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
