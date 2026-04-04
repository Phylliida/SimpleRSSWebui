import asyncio
import json
import re
import time
import random
import urllib.request
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from zendriver import start

LIST_URL = "https://x.com/i/lists/2009779378327302653"
PROFILE_DIR = Path(__file__).parent / ".twitter_profile"
LOG_PATH = Path(__file__).parent / "twitter_scrolls.log"
JSONL_PATH = Path(__file__).parent / "twitter_scrolls.jsonl"
AVATAR_DIR = Path(__file__).parent / "twitter_profile_pics"
MEDIA_DIR = Path(__file__).parent / "twitter_media"


async def _wait_for_enter(prompt: str) -> None:
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: input(prompt))


_COUNT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)([KkMm])?")


def _parse_count(label: str) -> int | None:
    match = _COUNT_RE.search(label.replace(",", ""))
    if not match:
        return None
    num = float(match.group(1))
    suffix = match.group(2)
    if suffix:
        if suffix.lower() == "k":
            num *= 1_000
        elif suffix.lower() == "m":
            num *= 1_000_000
    return int(num)


def _orig_media_url(src: str) -> str:
    parsed = urlparse(src)
    qs = parsed.query
    if "name=" in qs:
        qs = re.sub(r"name=[^&]+", "name=orig", qs)
    else:
        qs = (qs + "&name=orig").lstrip("&")
    return parsed._replace(query=qs).geturl()


def _best_pbs_url(img, *, allow_profile: bool = False) -> str | None:
    """
    Pick the highest-resolution image URL from an <img> tag by preferring the
    largest entry in srcset (if present) and normalizing to the original size.
    """
    candidates: list[tuple[int, int, str]] = []
    order = 0
    srcset = img.get("srcset")
    if srcset:
        for entry in srcset.split(","):
            parts = entry.strip().split()
            if not parts:
                continue
            url = parts[0]
            width = 0
            if len(parts) > 1 and parts[1].endswith("w"):
                try:
                    width = int(parts[1][:-1])
                except ValueError:
                    width = 0
            if "pbs.twimg.com" not in url:
                continue
            if not allow_profile and "profile_images" in url:
                continue
            candidates.append((width, order, url))
            order += 1

    src = img.get("src")
    if src and "pbs.twimg.com" in src:
        if allow_profile or "profile_images" not in src:
            candidates.append((0, order, src))

    if not candidates:
        return None

    _, _, url = max(candidates, key=lambda c: (c[0], c[1]))
    if allow_profile:
        return url
    return _orig_media_url(url)


class _RateLimiter:
    def __init__(self, min_interval: float) -> None:
        self._min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last = time.monotonic()


def _download_with_retry(
    url: str,
    dest_path: Path,
    rate_limiter: _RateLimiter | None,
    *,
    label: str = "Download",
    retries: int = 2,
    retry_delay_range: tuple[float, float] = (3.0, 4.0),
) -> bool:
    for attempt in range(retries + 1):
        if rate_limiter:
            rate_limiter.wait()
        try:
            with urllib.request.urlopen(url) as resp:
                dest_path.write_bytes(resp.read())
            return True
        except Exception as e:
            if attempt >= retries:
                print(f"{label} failed for {url}: {e}")
                return False
            wait_for = random.uniform(*retry_delay_range)
            print(
                f"{label} failed (attempt {attempt + 1}/{retries + 1}) for {url}: {e}; "
                f"retrying in {wait_for:.1f}s"
            )
            time.sleep(wait_for)


async def scrape_list(
    scrolls: int = 2000, pause: float = 5, wait_for_login: bool = True
) -> List[str]:
    """
    Open the list in a real browser window, optionally pause for manual login,
    scroll, and return visible post text.
    """
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Starting browser with profile at {PROFILE_DIR}")

    html_chunks: List[str] = []
    browser = await start(user_data_dir=str(PROFILE_DIR), headless=False)
    try:
        tab = await browser.get(LIST_URL)
        print(f"Opened list URL: {LIST_URL}")

        if wait_for_login:
            print("Log in or dismiss dialogs in the opened window, then press Enter here…")
            await _wait_for_enter("")

        interrupted = False
        for i in range(scrolls):
            try:
                # Scroll by roughly one viewport each iteration to avoid racing far down the feed
                await tab.evaluate("window.scrollBy(0, window.innerHeight * 0.9);")
                await tab.sleep(pause)
                html = await tab.get_content()
                html_chunks.append(html)
                print(f"Captured scroll {i + 1}/{scrolls}, html length {len(html)}")
                with LOG_PATH.open("a", encoding="utf-8") as f:
                    f.write(f"\n\n<!-- scroll {i + 1} -->\n")
                    f.write(html)
                    f.write("\n")
            except KeyboardInterrupt:
                interrupted = True
                print(f"Interrupted after {i + 1} scroll(s); parsing what was captured so far")
                break
            except asyncio.CancelledError:
                interrupted = True
                print(f"Cancelled after {i + 1} scroll(s); parsing what was captured so far")
                break
    finally:
        await browser.stop()
        print("Browser closed")

    if not html_chunks:
        print("No HTML captured; nothing to parse")
        return []
    with open("twitter_scrolls.log", "r") as f:
        html_chunks = re.split(r"\n\n<!-- scroll \d+ -->\n", f.read())
        html_chunks = html_chunks[:len(html_chunks)//2]
    combined_html = "\n".join(html_chunks)
    soup = BeautifulSoup(combined_html, "lxml")

    tweet_objects: List[dict] = []
    seen = set()
    print("Starting tweet parsing from collected HTML…")
    articles = soup.select('article[data-testid="tweet"]')
    print(f"Found {len(articles)} tweet articles to parse")
    for idx, article in enumerate(articles, 1):
        print(f"Parsing article {idx}/{len(articles)}")
        status_el = article.select_one('a[href*="/status/"]')
        url = status_el.get("href") if status_el else None
        if url and url.startswith("/"):
            url = "https://x.com" + url

        tweet_id = None
        user = None
        if url:
            parsed = urlparse(url)
            parts = [p for p in parsed.path.split("/") if p]
            if len(parts) >= 2 and parts[-1].isdigit():
                tweet_id = parts[-1]
                if len(parts) >= 3 and parts[-2] == "status":
                    user = parts[-3]
                else:
                    user = parts[-2]

        if not user:
            name_link = article.select_one('div[data-testid="User-Name"] a[href^="/"]')
            href = name_link.get("href") if name_link else None
            if href:
                parsed = urlparse(href)
                segments = [p for p in parsed.path.split("/") if p]
                if segments:
                    user = segments[0]
        print(f"Article {idx}: status url={url}, user={user}")

        retweeted_by = None
        social_el = article.select_one('[data-testid="socialContext"]')
        social_link = social_el.find_parent("a") if social_el else None
        if not social_link:
            for candidate in article.select('a[href^="/"]'):
                if candidate.select_one('[data-testid="socialContext"]'):
                    social_link = candidate
                    break
        if not social_link:
            social_link = article.select_one('[data-testid="socialContext"] a[href^="/"]')
        if social_link:
            href = social_link.get("href")
            if href:
                parsed = urlparse(href)
                segs = [p for p in parsed.path.split("/") if p]
                if segs:
                    retweeted_by = segs[0]
        if not retweeted_by and social_el:
            social_text = social_el.get_text(" ", strip=True)
            handle_match = re.search(r"@([A-Za-z0-9_]+)", social_text)
            repost_match = re.search(r"^(.*?)\s*reposted", social_text, flags=re.IGNORECASE)
            if handle_match:
                retweeted_by = handle_match.group(1)
            elif repost_match:
                name_guess = repost_match.group(1).strip()
                if name_guess and name_guess.lower() != "you":
                    retweeted_by = name_guess

        quote_block = None
        for candidate in article.select('div[role="link"][tabindex="0"]'):
            if candidate.select_one('[data-testid="tweetText"]'):
                quote_block = candidate
                break

        text_parts: List[str] = []
        for text_node in article.select('[data-testid="tweetText"]'):
            if quote_block and quote_block in text_node.parents:
                continue
            piece = text_node.get_text(" ", strip=True)
            if piece:
                text_parts.append(piece)
        text = "\n".join(text_parts).strip()
        if not text:
            continue
        print(f"Article {idx}: text length={len(text)}")

        time_el = article.select_one("time")
        created_at = time_el.get("datetime") if time_el else None

        avatar_el = article.select_one('img[src*="pbs.twimg.com/profile_images"]')
        avatar_url = _best_pbs_url(avatar_el, allow_profile=True) if avatar_el else None

        media_urls: List[str] = []
        for img in article.select('img[src*="pbs.twimg.com"]'):
            if quote_block and quote_block in img.parents:
                continue
            best_src = _best_pbs_url(img)
            if not best_src:
                continue
            if best_src not in media_urls:
                media_urls.append(best_src)
        if media_urls:
            print(f"Article {idx}: found {len(media_urls)} media images")

        replies_el = article.select_one('button[data-testid="reply"]')
        reposts_el = article.select_one('button[data-testid="retweet"]')
        likes_el = article.select_one('button[data-testid="like"]')
        replies = _parse_count(replies_el.get("aria-label")) if replies_el else None
        reposts = _parse_count(reposts_el.get("aria-label")) if reposts_el else None
        likes = _parse_count(likes_el.get("aria-label")) if likes_el else None
        print(f"Article {idx}: counts replies={replies} reposts={reposts} likes={likes} retweeted_by={retweeted_by}")

        quote = None
        if quote_block:
            q_status_el = quote_block.select_one('a[href*="/status/"]')
            q_url = q_status_el.get("href") if q_status_el else None
            if q_url and q_url.startswith("/"):
                q_url = "https://x.com" + q_url

            q_id = None
            q_user = None
            if q_url:
                parsed_q = urlparse(q_url)
                q_parts = [p for p in parsed_q.path.split("/") if p]
                if len(q_parts) >= 2:
                    id_index = next((i for i, part in enumerate(q_parts) if part.isdigit()), None)
                    if id_index is not None:
                        q_id = q_parts[id_index]
                        base_parts = q_parts[: id_index + 1]
                        q_url = parsed_q._replace(
                            path="/" + "/".join(base_parts),
                            params="",
                            query="",
                            fragment="",
                        ).geturl()
                        if id_index >= 2 and q_parts[id_index - 1] == "status":
                            q_user = q_parts[id_index - 2]
                        elif id_index >= 1:
                            q_user = q_parts[id_index - 1]

            if not q_user:
                name_link = quote_block.select_one('div[data-testid="User-Name"] a[href^="/"]')
                href = name_link.get("href") if name_link else None
                if href:
                    parsed = urlparse(href)
                    segments = [p for p in parsed.path.split("/") if p]
                    if segments:
                        q_user = segments[0]
            if not q_user:
                name_el = quote_block.select_one('[data-testid="User-Name"]')
                if name_el:
                    handle_match = re.search(r"@([A-Za-z0-9_]+)", name_el.get_text(" ", strip=True))
                    if handle_match:
                        q_user = handle_match.group(1)

            q_text_parts: List[str] = []
            for q_text_node in quote_block.select('[data-testid="tweetText"]'):
                piece = q_text_node.get_text(" ", strip=True)
                if piece:
                    q_text_parts.append(piece)
            q_text = "\n".join(q_text_parts).strip()

            if q_text or q_url or q_user:
                quote = {
                    "id": q_id,
                    "url": q_url,
                    "user": q_user,
                    "text": q_text,
                }
            if quote:
                print(f"Article {idx}: quote id={q_id} user={q_user} text_len={len(q_text) if q_text else 0}")

        dedupe_key = tweet_id or text
        if dedupe_key in seen:
            print(f"Skipping duplicate tweet key: {dedupe_key}")
            continue
        seen.add(dedupe_key)

        if idx % 20 == 0 or idx == len(articles):
            print(f"Parsed {idx}/{len(articles)} articles so far")

        tweet_objects.append(
            {
                "id": tweet_id,
                "url": url,
                "user": user,
                "text": text,
                "created_at": created_at,
                "replies": replies,
                "reposts": reposts,
                "likes": likes,
                "avatar_url": avatar_url,
                "quote": quote,
                "retweeted_by": retweeted_by,
                "is_retweet": retweeted_by is not None,
                "media_urls": media_urls,
            }
        )

    avatars: dict[str, str] = {}
    for tweet in tweet_objects:
        if tweet["user"] and tweet.get("avatar_url"):
            avatars[tweet["user"]] = tweet["avatar_url"]

    print(f"Found {len(tweet_objects)} tweets, downloading {len(avatars)} avatars")
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    download_rate_limiter = _RateLimiter(min_interval=3)  # ~1-2 downloads/sec
    avatar_files: dict[str, str] = {}
    for handle, avatar_url in avatars.items():
        parsed = urlparse(avatar_url)
        ext = Path(parsed.path).suffix or ".jpg"
        avatar_path = AVATAR_DIR / f"{handle}{ext}"
        avatar_rel = f"/twitter_profile_pics/{avatar_path.name}"
        if avatar_path.exists():
            print(f"Avatar already exists for {handle}, skipping")
            avatar_files[handle] = avatar_rel
            continue
        if _download_with_retry(
            avatar_url,
            avatar_path,
            download_rate_limiter,
            label=f"Avatar download for {handle}",
        ):
            print(f"Downloaded avatar for {handle}")
            avatar_files[handle] = avatar_rel

    media_to_download: dict[str, str] = {}
    for tweet in tweet_objects:
        tid = tweet.get("id") or "unknown"
        for idx, src in enumerate(tweet.get("media_urls") or []):
            key = f"{tid}_{idx}"
            if key not in media_to_download:
                media_to_download[key] = src

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {len(media_to_download)} media images")
    media_map: dict[str, str] = {}
    for key, src in media_to_download.items():
        parsed = urlparse(src)
        ext = Path(parsed.path).suffix or ".jpg"
        media_path = MEDIA_DIR / f"{key}{ext}"
        media_rel = f"/twitter_media/{media_path.name}"
        if media_path.exists():
            print(f"Media already exists for {key}, skipping")
            media_map[src] = media_rel
            continue
        if _download_with_retry(
            src,
            media_path,
            download_rate_limiter,
            label=f"Media download for {key}",
        ):
            print(f"Downloaded media for {key}")
            media_map[src] = media_rel

    # Rewrite URLs to local paths where available
    for tweet in tweet_objects:
        if tweet.get("user") and tweet["user"] in avatar_files:
            tweet["avatar_url"] = avatar_files[tweet["user"]]
        if tweet.get("media_urls"):
            tweet["media_urls"] = [media_map.get(src, src) for src in tweet["media_urls"]]

    with JSONL_PATH.open("w", encoding="utf-8") as f:
        for tweet in tweet_objects:
            f.write(json.dumps(tweet, ensure_ascii=False))
            f.write("\n")

    print(f"Wrote {len(tweet_objects)} tweets to {JSONL_PATH}")
    return [tweet["text"] for tweet in tweet_objects]


if __name__ == "__main__":
    scraped = asyncio.run(scrape_list())
    for i, text in enumerate(scraped, 1):
        print(f"--- {i} ---")
        print(text)
