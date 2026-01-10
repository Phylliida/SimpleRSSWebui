import asyncio
import json
import re
import urllib.request
from pathlib import Path
from typing import List
from urllib.parse import urlparse

from zendriver import start

LIST_URL = "https://x.com/i/lists/2009779378327302653"
PROFILE_DIR = Path(__file__).parent / ".twitter_profile"
LOG_PATH = Path(__file__).parent / "twitter_scrolls.log"
JSONL_PATH = Path(__file__).parent / "twitter_scrolls.jsonl"
AVATAR_DIR = Path(__file__).parent / "twitter_profile_pics"


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


async def scrape_list(
    scrolls: int = 5, pause: float = 3, wait_for_login: bool = True
) -> List[str]:
    """
    Open the list in a real browser window, optionally pause for manual login,
    scroll, and return visible post text.
    """
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    browser = await start(user_data_dir=str(PROFILE_DIR), headless=False)
    try:
        tab = await browser.get(LIST_URL)

        if wait_for_login:
            print("Log in or dismiss dialogs in the opened window, then press Enter here…")
            await _wait_for_enter("")

        html_chunks: List[str] = []
        for i in range(scrolls):
            await tab.evaluate("window.scrollBy(0, document.body.scrollHeight);")
            await tab.sleep(pause)
            html = await tab.get_content()
            html_chunks.append(html)
            with LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(f"\n\n<!-- scroll {i + 1} -->\n")
                f.write(html)
                f.write("\n")

        tweet_objects: List[dict] = []
        seen = set()
        for article in await tab.query_selector_all('article[data-testid="tweet"]'):
            status_el = await article.query_selector('a[href*="/status/"]')
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
            else:
                user = None

            if not user:
                # Fallback: take handle from the profile link in the tweet header
                name_link = await article.query_selector('div[data-testid="User-Name"] a[href^="/"]')
                href = name_link.get("href") if name_link else None
                if href:
                    parsed = urlparse(href)
                    segments = [p for p in parsed.path.split("/") if p]
                    if segments:
                        user = segments[0]

            text_parts: List[str] = []
            for text_node in await article.query_selector_all('[data-testid="tweetText"]'):
                text_parts.append(text_node.text_all.strip())
            text = "\n".join(p for p in text_parts if p).strip()
            if not text:
                continue

            time_el = await article.query_selector("time")
            created_at = time_el.get("datetime") if time_el else None

            avatar_el = await article.query_selector('img[src*="pbs.twimg.com/profile_images"]')
            avatar_url = avatar_el.get("src") if avatar_el else None

            replies_el = await article.query_selector('button[data-testid="reply"]')
            reposts_el = await article.query_selector('button[data-testid="retweet"]')
            likes_el = await article.query_selector('button[data-testid="like"]')
            replies = _parse_count(replies_el.get("aria-label")) if replies_el else None
            reposts = _parse_count(reposts_el.get("aria-label")) if reposts_el else None
            likes = _parse_count(likes_el.get("aria-label")) if likes_el else None

            dedupe_key = tweet_id or text
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

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
                }
            )

        avatars: dict[str, str] = {}
        for tweet in tweet_objects:
            if tweet["user"] and tweet.get("avatar_url"):
                avatars[tweet["user"]] = tweet["avatar_url"]

        AVATAR_DIR.mkdir(parents=True, exist_ok=True)
        for handle, avatar_url in avatars.items():
            parsed = urlparse(avatar_url)
            ext = Path(parsed.path).suffix or ".jpg"
            avatar_path = AVATAR_DIR / f"{handle}{ext}"
            if avatar_path.exists():
                continue
            try:
                with urllib.request.urlopen(avatar_url) as resp:
                    avatar_path.write_bytes(resp.read())
            except Exception as e:
                print(f"Avatar download failed for {handle}: {e}")

        with JSONL_PATH.open("w", encoding="utf-8") as f:
            for tweet in tweet_objects:
                f.write(json.dumps(tweet, ensure_ascii=False))
                f.write("\n")

        return [tweet["text"] for tweet in tweet_objects]
    finally:
        await browser.stop()


if __name__ == "__main__":
    scraped = asyncio.run(scrape_list())
    for i, text in enumerate(scraped, 1):
        print(f"--- {i} ---")
        print(text)
