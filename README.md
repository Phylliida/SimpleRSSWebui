# SimpleRSSWebui

Tiny Flask app that keeps an append-only JSONL log of RSS feed URLs and serves a minimal HTML UI.

## Run
- Install Flask: `pip install flask`
- Start the server: `python app.py`
- Optionally set `FEED_LOG_PATH` to choose where the log (`feeds.jsonl` by default) is written.
- UI is served from `index.html` at `/`.

## API
- `GET /api/feeds` → `{"feeds": ["https://example.com/rss", ...]}`
- `POST /api/feeds` with JSON `{"url": "https://example.com/rss"}` appends an `{"action":"add_feed"}` entry to the log and returns the current feed list.
- `DELETE /api/feeds` with JSON `{"url": "https://example.com/rss"}` appends a `{"action":"remove_feed"}` entry to the log and returns the current feed list.
