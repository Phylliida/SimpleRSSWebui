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
from datetime import datetime, timezone
from html import escape
from io import BytesIO
from pathlib import Path
from collections import deque
from typing import Iterable, List, Set
from urllib.parse import parse_qs, urlparse
import re

import feedparser
import listparser
import requests
from flask import Flask, jsonify, request, send_file

app = Flask(__name__)

LOG_PATH = Path(
    os.environ.get("FEED_LOG_PATH", Path(__file__).with_name("feeds.jsonl"))
)
BOOKMARKS_PATH = Path(
    os.environ.get("BOOKMARKS_LOG_PATH", Path(__file__).with_name("bookmarks.jsonl"))
)
BOOKMARKS_FILTER = "__bookmarks__"
INDEX_HTML_PATH = Path(__file__).with_name("index.html")
CACHE_DIR = Path(
    os.environ.get("FEED_CACHE_DIR", Path(__file__).with_name("cache"))
)
CACHE_ITEMS_PATH = CACHE_DIR / "items.json"
DEFAULT_FOLDER = "Default"
BSKY_API_BASE = "https://public.api.bsky.app/xrpc"
_BSKY_RATE_WINDOW = 1.0
_BSKY_RATE_MAX = 35
_bsky_rate_calls: deque[float] = deque(maxlen=_BSKY_RATE_MAX)
_FEED_RATE_WINDOW = 1.0
_FEED_RATE_MAX = 3
_feed_rate_calls: deque[float] = deque(maxlen=_FEED_RATE_MAX)


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


def _cache_last_refreshed() -> str | None:
    try:
        stamp = CACHE_ITEMS_PATH.stat().st_mtime
    except (FileNotFoundError, OSError):
        return None
    dt = datetime.fromtimestamp(stamp, tz=timezone.utc).replace(microsecond=0)
    return dt.isoformat()


def _is_youtube_feed(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    host = (parsed.hostname or "").lower()
    path = parsed.path or ""
    if "youtube.com" not in host or "feeds/videos.xml" not in path:
        return False
    return "channel_id" in parse_qs(parsed.query or "")


_YT_RSS_RE = re.compile(
    r'"rssUrl":"(https://www\.youtube\.com/feeds/videos\.xml\?channel_id=[^"]+)"'
)


def _resolve_youtube_feed_url(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").rstrip("/")
    if "youtube.com" not in host or not path or _is_youtube_feed(url):
        return None
    if not (path.startswith("/@") or path.startswith("/channel/") or path.startswith("/user/") or path.startswith("/c/")):
        return None
    about_path = path if path.endswith("/about") else f"{path}/about"
    about_url = f"{parsed.scheme or 'https'}://{parsed.netloc}{about_path}"
    try:
        resp = requests.get(
            about_url,
            timeout=10,
            headers={"User-Agent": "SimpleRSSWebui/1.0"},
        )
        resp.raise_for_status()
    except Exception:
        return None
    match = _YT_RSS_RE.search(resp.text)
    if not match:
        return None
    rss_url = match.group(1).replace("\\u0026", "&").replace("\\/", "/")
    return rss_url


def _display_feed_title(feed_url: str, title: str | None) -> str:
    clean = (title or "").strip()
    if _is_youtube_feed(feed_url):
        return clean if clean.lower().startswith("youtube:") else f"YouTube: {clean or 'Channel'}"
    return clean


def _extract_feed_title(feed_url: str, parsed: object) -> str:
    feed_data = getattr(parsed, "feed", {}) or {}
    raw_title = ""
    if isinstance(feed_data, dict):
        raw_title = str(feed_data.get("title") or "").strip()
    else:
        raw_title = str(getattr(feed_data, "title", "") or "").strip()
    return _display_feed_title(feed_url, raw_title)


def _extract_feed_image(feed_url: str, parsed: object) -> str:
    if _is_youtube_feed(feed_url):
        return ""
    feed_data = getattr(parsed, "feed", {}) or {}

    def safe_url(val: object) -> str:
        url = str(val or "").strip()
        return url if url.startswith(("http://", "https://")) else ""

    def image_from(obj: object) -> str:
        if not obj:
            return ""
        if isinstance(obj, dict):
            for key in ("href", "url", "link"):
                url = safe_url(obj.get(key))
                if url:
                    return url
        else:
            for key in ("href", "url", "link"):
                url = safe_url(getattr(obj, key, "") or "")
                if url:
                    return url
        return ""

    image = None
    if isinstance(feed_data, dict):
        image = feed_data.get("image")
    else:
        image = getattr(feed_data, "image", None)
    url = image_from(image)
    if url:
        return url
    for key in ("webfeeds_icon", "icon", "logo"):
        if isinstance(feed_data, dict):
            candidate = safe_url(feed_data.get(key))
        else:
            candidate = safe_url(getattr(feed_data, key, ""))
        if candidate:
            return candidate
    return ""


def _feed_titles_from_items(items: Iterable[dict]) -> dict[str, str]:
    titles: dict[str, str] = {}
    for item in items:
        url = str(item.get("feed") or "").strip()
        title = _display_feed_title(url, item.get("feed_title"))
        if url and title and url not in titles:
            titles[url] = title
    return titles


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


def _folder_name(folder: object) -> str:
    return str(folder or "").strip()


def _folder_value(folder: object) -> str:
    val = _folder_name(folder)
    return val or DEFAULT_FOLDER


def _folder_leaf(folder: object) -> str:
    name = _folder_name(folder)
    return name.rsplit("/", 1)[-1] if name else ""


def _folder_path(name: object, parent: object | None = None) -> str:
    clean_name = _folder_name(name)
    parent_name = _folder_name(parent)
    if not clean_name:
        return ""
    return f"{parent_name}/{clean_name}" if parent_name else clean_name


def _resolve_folder(
    folder: object, moves: Iterable[tuple[str, str]], default_on_empty: bool = True
) -> str:
    base = _folder_name(folder)
    current = _folder_value(base) if default_on_empty else base
    if not current and not default_on_empty:
        return ""
    for old, new in moves:
        if not old:
            continue
        if current == old:
            current = new
        elif current.startswith(f"{old}/"):
            current = f"{new}{current[len(old):]}"
    return _folder_value(current) if default_on_empty else _folder_name(current)


def _folders_from_events(
    events: Iterable[dict],
) -> tuple[Set[str], list[tuple[str, str]], Set[str]]:
    folders: Set[str] = set()
    moves: list[tuple[str, str]] = []
    removed: Set[str] = set()
    for evt in events:
        action = evt.get("action")
        if action == "add_folder":
            name = _resolve_folder(evt.get("folder"), moves)
            if name:
                removed = {r for r in removed if not (r == name or r.startswith(f"{name}/") or name.startswith(f"{r}/"))}
                folders.add(name)
        elif action == "move_folder":
            old = _resolve_folder(evt.get("folder"), moves, default_on_empty=False)
            if not old:
                continue
            parent = _folder_name(evt.get("parent"))
            if parent:
                parent = _resolve_folder(parent, moves, default_on_empty=False)
            new_path = _folder_name(_folder_path(_folder_leaf(old), parent))
            if not new_path:
                continue
            if old in folders:
                folders.discard(old)
            folders.add(new_path)
            removed = {r for r in removed if not (r == new_path or r.startswith(f"{new_path}/") or new_path.startswith(f"{r}/"))}
            moves.append((old, new_path))
        elif action == "remove_folder":
            target = _resolve_folder(evt.get("folder"), moves, default_on_empty=False)
            if not target or target == DEFAULT_FOLDER:
                continue
            removed.add(target)
            folders = {f for f in folders if not (f == target or f.startswith(f"{target}/"))}
    return folders, moves, removed


def _feed_folders_from_events(
    events: Iterable[dict],
    moves: Iterable[tuple[str, str]] | None = None,
    removed: Iterable[str] | None = None,
) -> dict[str, List[str]]:
    moves_list = list(moves or [])
    removed_set = {name for name in (removed or []) if name}
    folders: dict[str, Set[str]] = {}
    for evt in events:
        action = evt.get("action")
        if action == "remove_folder":
            target = _resolve_folder(evt.get("folder"), moves_list, default_on_empty=False)
            if not target:
                continue
            for feed_url, names in folders.items():
                folders[feed_url] = {name for name in names if not (name == target or name.startswith(f"{target}/"))}
            continue
        url = str(evt.get("url", "")).strip()
        if not url:
            continue
        if action == "remove_feed":
            folders.pop(url, None)
            continue
        if action not in {"add_feed", "move_feed"}:
            continue
        folder = _folder_value(evt.get("folder"))
        if action == "move_feed":
            folders[url] = {folder}
        else:
            folders.setdefault(url, set()).add(folder)
    resolved: dict[str, List[str]] = {}
    is_removed = lambda name: any(name == r or name.startswith(f"{r}/") for r in removed_set)
    for url, names in folders.items():
        cleaned = {
            _resolve_folder(name, moves_list, default_on_empty=False)
            for name in names
        }
        cleaned = {name for name in cleaned if name and not is_removed(name)}
        resolved[url] = sorted(cleaned or {DEFAULT_FOLDER})
    return resolved


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
    feeds = _feeds_from_events(events_list)
    folder_names_set, moves, removed = _folders_from_events(events_list)
    folders = _feed_folders_from_events(events_list, moves, removed)
    if feeds and not folders:
        folders = {url: [DEFAULT_FOLDER] for url in feeds}
    normalized_folders: dict[str, List[str]] = {}
    for url in feeds:
        raw = folders.get(url, [])
        names = sorted({name for name in raw if name}) or [DEFAULT_FOLDER]
        normalized_folders[url] = names
    folder_names: set[str] = {DEFAULT_FOLDER, *folder_names_set}
    for names in normalized_folders.values():
        folder_names.update(names)
    for old, new in moves:
        if old in folder_names:
            folder_names.discard(old)
            if new:
                folder_names.add(new)
    folder_names_sorted = sorted(folder_names)
    tags = _feed_tags_from_events(events_list)
    cached_titles = _feed_titles_from_items(_load_cached_items())
    feed_titles = {url: title for url, title in cached_titles.items() if url in feeds}
    return {
        "feeds": feeds,
        "feed_folders": normalized_folders,
        "folders": folder_names_sorted,
        "favorites": sorted(url for url, tagset in tags.items() if "favorite" in tagset),
        "tags": {url: sorted(tagset) for url, tagset in tags.items() if tagset},
        "feed_titles": feed_titles,
        "last_refreshed": _cache_last_refreshed(),
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


def _entry_author(entry: dict) -> str:
    author = str(entry.get("author") or "").strip()
    if author:
        return author
    details = entry.get("author_detail") or {}
    if isinstance(details, dict):
        return str(details.get("name") or details.get("email") or "").strip()
    return str(getattr(details, "name", "") or getattr(details, "email", "") or "").strip()


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


def _youtube_view_count(entry: dict) -> int | None:
    def _parse(val: object) -> int | None:
        try:
            parsed = int(str(val).replace(",", "").strip())
            return parsed if parsed >= 0 else None
        except Exception:
            return None

    stats = entry.get("media_statistics")
    if isinstance(stats, dict):
        views = _parse(stats.get("views") or stats.get("viewCount") or stats.get("viewcount"))
        if views is not None:
            return views

    community = entry.get("media_community")
    if isinstance(community, dict):
        inner_stats = community.get("media_statistics") or community.get("statistics")
        if isinstance(inner_stats, dict):
            views = _parse(inner_stats.get("views") or inner_stats.get("viewCount") or inner_stats.get("viewcount"))
            if views is not None:
                return views
    yt_stats = entry.get("yt_statistics")
    if isinstance(yt_stats, dict):
        views = _parse(yt_stats.get("viewCount") or yt_stats.get("views"))
        if views is not None:
            return views
    return None


def _bluesky_handle_rkey_from_link(link: str) -> tuple[str, str] | None:
    try:
        parsed = urlparse(link)
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if "bsky.app" not in host:
        return None
    parts = (parsed.path or "").strip("/").split("/")
    if len(parts) >= 4 and parts[0] == "profile" and parts[2] == "post":
        return parts[1], parts[3]
    return None


def _bluesky_summary_html(bluesky_json: dict, link: str | None = None) -> str:
    try:
        thread = bluesky_json.get("thread", {}) or {}
        post = thread.get("post", {}) or {}
        author = post.get("author", {}) or {}
        record = post.get("record", {}) or {}
        text = str(record.get("text", "") or "")
        avatar = str(author.get("avatar", "") or "")
        reply = post.get("replyCount", 0)
        repost = post.get("repostCount", 0)
        like = post.get("likeCount", 0)
        quote = post.get("quoteCount", 0)
        embed_view = post.get("embed") or record.get("embed") or {}
        images = []
        if isinstance(embed_view, dict) and embed_view.get("$type", "").startswith("app.bsky.embed.images"):
            for img in embed_view.get("images", []):
                if not isinstance(img, dict):
                    continue
                src = img.get("fullsize") or img.get("thumb") or ""
                if src:
                    images.append((src, img.get("alt") or ""))
        text_html = escape(text).replace("\n", "<br/>")
        parts: list[str] = []
        if text:
            parts.append(f"<div>{text_html}</div>")
        if images:
            img_tags = "".join(
                f'<div><img src="{escape(src)}" alt="{escape(alt)}" style="max-width:100%;height:auto;"/></div>'
                for src, alt in images
            )
            parts.append(img_tags)
        parts.append(
            f"<div><small>Replies: {reply} · Reposts: {repost} · Likes: {like} · Quotes: {quote}</small></div>"
        )
        return "\n".join(parts)
    except Exception:
        return ""


def _bsky_rate_limit():
    now = time.time()
    if len(_bsky_rate_calls) == _BSKY_RATE_MAX:
        earliest = _bsky_rate_calls[0]
        elapsed = now - earliest
        if elapsed < _BSKY_RATE_WINDOW:
            time.sleep(_BSKY_RATE_WINDOW - elapsed)
    _bsky_rate_calls.append(time.time())


def _feed_rate_limit():
    now = time.time()
    if len(_feed_rate_calls) == _FEED_RATE_MAX:
        earliest = _feed_rate_calls[0]
        elapsed = now - earliest
        if elapsed < _FEED_RATE_WINDOW:
            time.sleep(_FEED_RATE_WINDOW - elapsed)
    _feed_rate_calls.append(time.time())


def _fetch_bluesky_post_json(link: str) -> dict | None:
    parsed = _bluesky_handle_rkey_from_link(link)
    if not parsed:
        return None
    handle, rkey = parsed
    try:
        _bsky_rate_limit()
        did_resp = requests.get(
            f"{BSKY_API_BASE}/com.atproto.identity.resolveHandle",
            params={"handle": handle},
            timeout=10,
        )
        did_resp.raise_for_status()
        did = did_resp.json().get("did")
        if not did:
            return None
        _bsky_rate_limit()
        at_uri = f"at://{did}/app.bsky.feed.post/{rkey}"
        post_resp = requests.get(
            f"{BSKY_API_BASE}/app.bsky.feed.getPostThread",
            params={"uri": at_uri},
            timeout=10,
        )
        post_resp.raise_for_status()
        return post_resp.json()
    except Exception:
        return None


def _item_from_entry(feed_url: str, entry: dict, feed_title: str = "", feed_image: str = "") -> dict:
    title = entry.get("title")
    display_feed_title = _display_feed_title(feed_url, feed_title)
    ids = _entry_id(feed_url, entry)
    if not title:
        title = _entry_author(entry) or (display_feed_title or (_entry_id(feed_url, entry) or "(no title)"))
    link = entry.get("link")
    is_youtube = _is_youtube_feed(feed_url)
    youtube_views = _youtube_view_count(entry) if is_youtube else None
    bluesky_json = _fetch_bluesky_post_json(link) if link else None
    bluesky_author_avatar = ""
    bluesky_author_handle = ""
    bluesky_author_display = ""
    like_count: int | None = None
    summary_value = entry.get("summary") or entry.get("description") or ""
    if is_youtube:
        summary_value = ""
    if bluesky_json:
        summary_value = _bluesky_summary_html(bluesky_json, link) or json.dumps(bluesky_json)
        post = (bluesky_json.get("thread") or {}).get("post") or {}
        author = post.get("author") or {}
        bluesky_author_avatar = str(author.get("avatar") or "").strip()
        bluesky_author_handle = str(author.get("handle") or "").strip()
        bluesky_author_display = str(author.get("displayName") or bluesky_author_handle or "").strip()
        author_title = str(author.get("displayName") or author.get("handle") or "").strip()
        try:
            like_raw = post.get("likeCount")
            like_parsed = int(like_raw)
            like_count = like_parsed if like_parsed >= 0 else None
        except Exception:
            like_count = None
        if author_title:
            title = author_title
    item = {
        "feed": feed_url,
        "feed_title": display_feed_title,
        "id": ids,
        "title": title,
        "link": link,
        "published": entry.get("published") or entry.get("updated") or "",
        "summary": summary_value,
        "thumbnail": _thumbnail_from_entry(entry),
        "_ts": _entry_timestamp(entry),
        "_viewed": False,
    }
    if feed_image and not is_youtube:
        item["feed_image"] = feed_image
    if bluesky_json is not None:
        item["bluesky_json"] = bluesky_json
    if bluesky_author_avatar:
        item["bluesky_author_avatar"] = bluesky_author_avatar
    if bluesky_author_handle:
        item["bluesky_author_handle"] = bluesky_author_handle
    if bluesky_author_display:
        item["bluesky_author_display"] = bluesky_author_display
    if youtube_views is not None:
        item["youtube_views"] = youtube_views
    if like_count is not None:
        item["like_count"] = like_count
    return item


def _gather_feed_items(feeds: Iterable[str]) -> List[dict]:
    items: List[dict] = []
    for url in feeds:
        try:
            _feed_rate_limit()
            parsed = feedparser.parse(url)
        except Exception:
            continue
        feed_title = _extract_feed_title(url, parsed)
        feed_image = _extract_feed_image(url, parsed)
        entries = parsed.entries if hasattr(parsed, "entries") else []
        items.extend(_item_from_entry(url, entry, feed_title, feed_image) for entry in entries)
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


def _fold_bookmarks(events: Iterable[dict]) -> dict[str, dict]:
    saved: dict[str, dict] = {}
    for evt in events:
        action = evt.get("action")
        if action == "add_entry":
            entry = evt.get("entry")
            if not isinstance(entry, dict):
                continue
            item_id = str(entry.get("id") or entry.get("guid") or entry.get("link") or "").strip()
            if not item_id:
                continue
            data = dict(entry)
            data["id"] = item_id
            data["_ts"] = data.get("_ts") or _entry_timestamp(data) or time.time()
            saved[item_id] = data
        elif action == "remove_entry":
            item_id = str(evt.get("item_id") or "").strip()
            if item_id:
                saved.pop(item_id, None)
    return saved


def _bookmarked_items(events: Iterable[dict] | None = None) -> List[dict]:
    events_list = list(events) if events is not None else _load_events(BOOKMARKS_PATH)
    return list(_fold_bookmarks(events_list).values())


def _bookmarked_ids(events: Iterable[dict] | None = None) -> Set[str]:
    events_list = list(events) if events is not None else _load_events(BOOKMARKS_PATH)
    return set(_fold_bookmarks(events_list).keys())


def _collect_items(
    feeds: Iterable[str],
    limit: int | None = 30,
    include_viewed: bool = False,
    viewed_ids: Set[str] | None = None,
    bookmarked_ids: Set[str] | None = None,
    offset: int = 0,
    allowed_feeds: Set[str] | None = None,
    sort_by: str = "recent",
    time_range: str = "all",
    view_filter: str = "unviewed",
) -> tuple[List[dict], int, dict[str, str]]:
    view_mode = (view_filter or "").lower()
    if view_mode not in {"all", "viewed", "unviewed"}:
        view_mode = "all" if include_viewed else "unviewed"
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
    feed_titles = _feed_titles_from_items(items)
    if allowed:
        items = [i for i in items if i.get("feed") in allowed]
    else:
        items = []
    for item in items:
        item["_viewed"] = item.get("id") in viewed_ids
    if view_mode == "unviewed":
        items = [i for i in items if not i["_viewed"]]
    elif view_mode == "viewed":
        items = [i for i in items if i["_viewed"]]
    range_key = (time_range or "all").lower()
    seconds = {"today": 86400, "week": 604800, "month": 2592000}.get(range_key)
    if seconds:
        cutoff = time.time() - seconds
        items = [i for i in items if i.get("_ts", 0) >= cutoff]
    key = (sort_by or "recent").lower()
    def _pos_int(val: object) -> int:
        try:
            parsed = int(val)
            return parsed if parsed >= 0 else -1
        except Exception:
            return -1

    if key == "views":
        items.sort(key=lambda i: (_pos_int(i.get("youtube_views")), i.get("_ts", 0)), reverse=True)
    elif key == "likes":
        items.sort(key=lambda i: (_pos_int(i.get("like_count")), i.get("_ts", 0)), reverse=True)
    else:
        items.sort(key=lambda i: i.get("_ts", 0), reverse=True)
    total = len(items)
    trimmed = items[offset:] if offset else items
    if limit and limit > 0:
        trimmed = trimmed[:limit]
    cleaned = []
    for item in trimmed:
        base = {k: v for k, v in item.items() if k not in {"_ts", "_viewed"}}
        base["viewed"] = item["_viewed"]
        if bookmarked_ids is not None:
            base["bookmarked"] = base.get("id") in bookmarked_ids
        cleaned.append(base)
    return cleaned, total, feed_titles


@app.route("/api/feeds", methods=["GET"])
def api_list_feeds():
    return jsonify(_state_payload())


@app.route("/api/feeds", methods=["POST"])
def api_add_feed():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    folder = _folder_value(payload.get("folder"))
    if not url:
        return jsonify({"error": "url is required"}), 400
    resolved_url = _resolve_youtube_feed_url(url)
    if resolved_url:
        url = resolved_url

    events = _load_events(LOG_PATH)
    feeds = _feeds_from_events(events)
    _, moves, removed = _folders_from_events(events)
    feed_folders = _feed_folders_from_events(events, moves, removed)
    current_folders = set(feed_folders.get(url, []))
    if url in feeds and not current_folders:
        current_folders = {DEFAULT_FOLDER}
    added_new_feed = False
    added_folder = False
    if url not in feeds:
        _append_event(LOG_PATH, {"action": "add_feed", "url": url, "folder": folder})
        _clear_cache()
        added_new_feed = True
    elif folder not in current_folders:
        _append_event(LOG_PATH, {"action": "add_feed", "url": url, "folder": folder})
        added_folder = True
    state = _state_payload()
    if added_new_feed:
        state["message"] = "added"
        if resolved_url:
            state["resolved_url"] = resolved_url
        return jsonify(state), 201
    if added_folder:
        state["message"] = "added to folder"
        if resolved_url:
            state["resolved_url"] = resolved_url
        return jsonify(state)
    state["message"] = "already in folder"
    if resolved_url:
        state["resolved_url"] = resolved_url
    return jsonify(state)


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
        _append_event(LOG_PATH, {"action": "add_feed", "url": url, "folder": DEFAULT_FOLDER})
    # never refresh the cache unless we manually ask, because that's expensive
    #if new_urls:
    #    _clear_cache()
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


@app.route("/api/folders", methods=["POST"])
def api_add_folder():
    payload = request.get_json(silent=True) or {}
    name = _folder_name(payload.get("name"))
    parent = _folder_name(payload.get("parent"))
    folder = _folder_path(name, parent)
    if not name:
        return jsonify({"error": "name is required"}), 400
    events = _load_events(LOG_PATH)
    existing = set(_state_payload(events).get("folders", []))
    if folder in existing:
        state = _state_payload(events)
        state["message"] = "already present"
        return jsonify(state)
    _append_event(LOG_PATH, {"action": "add_folder", "folder": folder})
    state = _state_payload()
    state["message"] = "folder created"
    return jsonify(state), 201


@app.route("/api/folders/move", methods=["POST"])
def api_move_folder():
    payload = request.get_json(silent=True) or {}
    folder = _folder_name(payload.get("folder"))
    parent = _folder_name(payload.get("parent"))
    if not folder or folder == DEFAULT_FOLDER:
        return jsonify({"error": "folder is required"}), 400
    events = _load_events(LOG_PATH)
    folder_names, moves, _ = _folders_from_events(events)
    state = _state_payload(events)
    existing = set(state.get("folders", []))
    folder = _resolve_folder(folder, moves, default_on_empty=False)
    parent = _resolve_folder(parent, moves, default_on_empty=False) if parent else ""
    if folder not in existing:
        return jsonify({"error": "folder not present"}), 400
    if parent and (parent == folder or parent.startswith(f"{folder}/")):
        return jsonify({"error": "invalid parent"}), 400
    new_path = _folder_path(_folder_leaf(folder), parent)
    if new_path == folder:
        state = _state_payload(events)
        state["message"] = "no change"
        return jsonify(state)
    _append_event(LOG_PATH, {"action": "move_folder", "folder": folder, "parent": parent})
    state = _state_payload()
    state["message"] = "folder moved"
    return jsonify(state)


@app.route("/api/folders", methods=["DELETE"])
def api_delete_folder():
    payload = request.get_json(silent=True) or {}
    folder = _folder_name(payload.get("folder"))
    if not folder or folder == DEFAULT_FOLDER:
        return jsonify({"error": "folder is required"}), 400
    events = _load_events(LOG_PATH)
    _, moves, _ = _folders_from_events(events)
    target = _resolve_folder(folder, moves, default_on_empty=False)
    if not target or target == DEFAULT_FOLDER:
        return jsonify({"error": "folder is required"}), 400
    existing = set(_state_payload(events).get("folders", []))
    if target not in existing:
        return jsonify({"error": "folder not present"}), 400
    _append_event(LOG_PATH, {"action": "remove_folder", "folder": target})
    state = _state_payload()
    state["message"] = "folder removed"
    return jsonify(state)


@app.route("/api/feeds/folder", methods=["POST"])
def api_feed_folder():
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    folder = _folder_value(payload.get("folder"))
    if not url:
        return jsonify({"error": "url is required"}), 400
    feeds = current_feeds()
    if url not in feeds:
        return jsonify({"error": "feed not present", "feeds": feeds}), 400
    events = _load_events(LOG_PATH)
    _, moves, removed = _folders_from_events(events)
    feed_folders = _feed_folders_from_events(events, moves, removed)
    current_folders = set(feed_folders.get(url, [])) or {DEFAULT_FOLDER}
    if current_folders == {folder}:
        state = _state_payload()
        state["message"] = "no change"
        return jsonify(state)
    _append_event(LOG_PATH, {"action": "move_feed", "url": url, "folder": folder})
    state = _state_payload()
    state["message"] = "moved"
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
    folder_names_set, moves, removed = _folders_from_events(events)
    viewed_ids = _viewed_ids(events)
    include_viewed = (
        str(request.args.get("include_viewed", "")).lower() in {"1", "true", "yes", "on"}
    )
    view_filter = str(request.args.get("view", "")).lower()
    if view_filter not in {"all", "viewed", "unviewed"}:
        view_filter = "all" if include_viewed else "unviewed"
    include_viewed = include_viewed or view_filter in {"all", "viewed"}
    favorites_only = (
        str(request.args.get("favorites_only", "")).lower() in {"1", "true", "yes", "on"}
    )
    sort_by = str(request.args.get("sort", "recent") or "").lower()
    if sort_by not in {"recent", "views", "likes"}:
        sort_by = "recent"
    time_range = str(request.args.get("range", "all") or "").lower()
    if time_range not in {"all", "today", "week", "month"}:
        time_range = "all"
    raw_folder = request.args.get("folder", "")
    is_bookmarks = str(raw_folder or "").strip() == BOOKMARKS_FILTER
    folder_filter = "" if is_bookmarks else _resolve_folder(raw_folder, moves, default_on_empty=False)
    bookmark_events = _load_events(BOOKMARKS_PATH)
    feed_folders = _feed_folders_from_events(events, moves, removed)
    for url in feeds:
        feed_folders.setdefault(url, [DEFAULT_FOLDER])
    favorite_feeds = _favorite_feeds(events)
    try:
        page = max(1, int(request.args.get("page", "1")))
    except (ValueError, TypeError):
        page = 1
    offset = 0
    if limit and limit > 0:
        offset = (page - 1) * limit
    if is_bookmarks:
        items = _bookmarked_items(bookmark_events)
        items.sort(key=lambda i: i.get("_ts", 0), reverse=True)
        total = len(items)
        trimmed = items[offset:] if offset else items
        if limit and limit > 0:
            trimmed = trimmed[:limit]
        cleaned = []
        for item in trimmed:
            base = {k: v for k, v in item.items() if k != "_ts"}
            base["viewed"] = bool(item.get("viewed") or item.get("_viewed"))
            base["bookmarked"] = True
            cleaned.append(base)
        page_size = limit if limit and limit > 0 else total
        return jsonify(
            {
                "items": cleaned,
                "total": total,
                "page": page,
                "page_size": page_size,
                "feed_titles": {},
                "last_refreshed": _cache_last_refreshed(),
                "sort": sort_by,
                "range": time_range,
                "view": view_filter,
            }
        )
    allowed_feeds = set(feeds)
    if favorites_only:
        allowed_feeds &= favorite_feeds
    if folder_filter:
        allowed_feeds &= {
            url
            for url, folders in feed_folders.items()
            if any(
                folder == folder_filter or folder.startswith(f"{folder_filter}/")
                for folder in folders
            )
        }
    if (favorites_only or folder_filter) and not allowed_feeds:
        page_size = limit if limit and limit > 0 else 0
        return jsonify(
            {
                "items": [],
                "total": 0,
                "page": page,
                "page_size": page_size,
                "sort": sort_by,
                "range": time_range,
                "last_refreshed": _cache_last_refreshed(),
                "view": view_filter,
            }
        )
    bookmark_ids = _bookmarked_ids(bookmark_events)
    items, total, feed_titles = _collect_items(
        feeds,
        limit=limit,
        include_viewed=include_viewed,
        viewed_ids=viewed_ids,
        bookmarked_ids=bookmark_ids,
        offset=offset,
        allowed_feeds=allowed_feeds,
        sort_by=sort_by,
        time_range=time_range,
        view_filter=view_filter,
    )
    page_size = limit if limit and limit > 0 else total
    return jsonify(
        {
            "items": items,
            "total": total,
            "page": page,
            "page_size": page_size,
            "feed_titles": {url: title for url, title in feed_titles.items() if url in feeds},
            "last_refreshed": _cache_last_refreshed(),
            "sort": sort_by,
            "range": time_range,
            "view": view_filter,
        }
    )


@app.route("/api/bookmarks", methods=["POST"])
def api_add_bookmark():
    payload = request.get_json(silent=True) or {}
    entry = payload.get("entry")
    if not isinstance(entry, dict):
        return jsonify({"error": "entry is required"}), 400
    _append_event(BOOKMARKS_PATH, {"action": "add_entry", "entry": entry})
    return jsonify({"message": "bookmarked"})


@app.route("/api/bookmarks", methods=["DELETE"])
def api_remove_bookmark():
    payload = request.get_json(silent=True) or {}
    item_id = str(payload.get("id", "")).strip()
    if not item_id:
        return jsonify({"error": "id is required"}), 400
    _append_event(BOOKMARKS_PATH, {"action": "remove_entry", "item_id": item_id})
    return jsonify({"id": item_id, "message": "unbookmarked"})


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
