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
from pathlib import Path
from typing import Iterable, List

import feedparser
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


def _entry_timestamp(entry: dict) -> float:
    stamp = entry.get("published_parsed") or entry.get("updated_parsed")
    return time.mktime(stamp) if stamp else 0.0


def _item_from_entry(feed_url: str, entry: dict) -> dict:
    return {
        "feed": feed_url,
        "title": entry.get("title") or "(no title)",
        "link": entry.get("link"),
        "published": entry.get("published") or entry.get("updated") or "",
        "summary": entry.get("summary") or entry.get("description") or "",
        "_ts": _entry_timestamp(entry),
    }


def _collect_items(feeds: Iterable[str], limit: int | None = 30) -> List[dict]:
    items = []
    for url in feeds:
        try:
            parsed = feedparser.parse(url)
        except Exception:
            continue
        entries = parsed.entries if hasattr(parsed, "entries") else []
        items.extend(_item_from_entry(url, entry) for entry in entries)
    items.sort(key=lambda i: i["_ts"], reverse=True)
    trimmed = items if not limit or limit < 1 else items[:limit]
    return [{k: v for k, v in item.items() if k != "_ts"} for item in trimmed]


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
    return jsonify({"items": _collect_items(current_feeds(), limit=limit)})


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
