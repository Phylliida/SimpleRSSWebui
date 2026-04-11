import asyncio
import json
import re
import time
import base64
import shutil
from pathlib import Path
from typing import List
from urllib.parse import urlparse, unquote

from bs4 import BeautifulSoup
import zendriver as uc
import os

os.chdir(os.path.dirname(__file__))

LIST_URL = "https://x.com/i/lists/2009779378327302653"
PROFILE_DIR = Path(__file__).parent / ".twitter_profile"
LOG_PATH = Path(__file__).parent / "twitter_scrolls.log"
JSONL_PATH = Path(__file__).parent / "twitter_scrolls.jsonl"
AVATAR_DIR = Path(__file__).parent / "twitter_profile_pics"
MEDIA_DIR = Path(__file__).parent / "twitter_media"
URL_MAP_PATH = MEDIA_DIR / "url_mapping.json"


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


def _strip_profile_size(path: str) -> str:
    return re.sub(
        r"_(mini|normal|bigger|reasonably_small|200x200|400x400)(\.\w+)$",
        r"\2",
        path,
    )


def _find_captured(url: str | None, captured: dict[str, Path]) -> Path | None:
    if not url:
        return None

    # 1. Direct match
    if url in captured:
        return captured[url]

    # 2. Normalize &amp;
    normalized = url.replace("&amp;", "&")
    if normalized in captured:
        return captured[normalized]

    # 3. Match by path only (ignore query params)
    base_path = urlparse(normalized).path
    for cap_url, path in captured.items():
        if urlparse(cap_url).path == base_path:
            return path

    # 4. Strip profile-image size variants and re-match
    stripped = _strip_profile_size(base_path)
    if stripped != base_path:
        for cap_url, path in captured.items():
            cap_stripped = _strip_profile_size(urlparse(cap_url).path)
            if cap_stripped == stripped:
                return path

    # 5. Match just the last path segment (filename)
    filename = base_path.rstrip("/").split("/")[-1] if base_path else ""
    if filename:
        stripped_fn = _strip_profile_size(filename)
        for cap_url, path in captured.items():
            cap_fn = urlparse(cap_url).path.rstrip("/").split("/")[-1]
            if cap_fn == filename:
                return path
            if _strip_profile_size(cap_fn) == stripped_fn:
                return path

    return None


def _best_pbs_url(img, *, allow_profile: bool = False) -> str | None:
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
    return url


async def scrape_list(
    scrolls: int = 1000, pause: float = 5, wait_for_login: bool = True
) -> List[str]:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Starting browser (profile: {PROFILE_DIR})")

    html_chunks: List[str] = []
    captured_images: dict[str, Path] = {}
    lookup_map: dict[str, str] = {}
    counter = 0

    browser = await uc.start(user_data_dir=str(PROFILE_DIR), headless=False)
    try:
        # ──────────────────────────────────────────────────────────────
        # KEY FIX #1: open about:blank first, set up CDP hooks BEFORE
        # navigating to the real page so we capture every image from
        # the very first network request onward.
        # ──────────────────────────────────────────────────────────────
        tab = await browser.get("about:blank")
        print("Opened about:blank — setting up CDP hooks before navigation…")

        async def on_response(event: uc.cdp.network.ResponseReceived):
            nonlocal counter
            mime = event.response.mime_type or ""
            if "image" not in mime:
                return

            url = event.response.url
            if url in captured_images:
                return

            try:
                result = await tab.send(
                    uc.cdp.network.get_response_body(event.request_id)
                )
                body_str, is_b64 = result

                if is_b64:
                    data = base64.b64decode(body_str)
                else:
                    data = body_str.encode("utf-8")

                if len(data) < 50:
                    return

                ext = mime.split("/")[-1]
                ext = ext.replace("svg+xml", "svg").replace("jpeg", "jpg")

                parsed = urlparse(url)
                path_name = unquote(parsed.path.split("/")[-1])
                stem = Path(path_name).stem if path_name else ""
                if not stem or not stem.strip():
                    stem = "image"

                fname = f"{counter:04d}_{stem}.{ext}"
                counter += 1

                dest = MEDIA_DIR / fname
                dest.write_bytes(data)

                captured_images[url] = dest
                lookup_map[fname] = url

                is_profile = "profile_images" in url
                tag = "[avatar]" if is_profile else "[media] "
                print(f"  {tag} {fname} ← {url[:120]}")

            except Exception as e:
                if "profile_images" in (event.response.url or ""):
                    print(f"  [avatar FAIL] {event.response.url[:100]}: {e}")

        tab.add_handler(uc.cdp.network.ResponseReceived, on_response)

        # Enable network domain
        try:
            await tab.send(
                uc.cdp.network.enable(
                    max_total_buffer_size=100_000_000,
                    max_resource_buffer_size=10_000_000,
                )
            )
        except TypeError:
            await tab.send(uc.cdp.network.enable())

        # ──────────────────────────────────────────────────────────────
        # KEY FIX #2: disable the disk cache so profile images that the
        # browser has seen before still generate real network responses
        # that our CDP handler can intercept.
        # ──────────────────────────────────────────────────────────────
        try:
            await tab.send(
                uc.cdp.network.set_cache_disabled(cache_disabled=True)
            )
            print("Browser disk-cache DISABLED (forces fresh fetches)")
        except Exception as e:
            print(f"⚠ Could not disable cache: {e}")

        print("CDP network capture active — now navigating to list…")

        # NOW navigate to the real page
        await tab.get(LIST_URL)
        print(f"Navigated to {LIST_URL}")

        if wait_for_login:
            print(
                "Log in / dismiss dialogs in the browser, then press Enter…"
            )
            #await asyncio.get_running_loop().run_in_executor(
            #    None, lambda: input("")
            #)

        for i in range(scrolls):
            try:
                await tab.evaluate(
                    "window.scrollBy(0, window.innerHeight * 0.9);"
                )
                await tab.sleep(pause)

                html = await tab.get_content()
                html_chunks.append(html)

                n_avatars = sum(
                    1 for u in captured_images if "profile_images" in u
                )
                n_media = len(captured_images) - n_avatars
                print(
                    f"Scroll {i + 1}/{scrolls} — "
                    f"html {len(html):,} chars, "
                    f"{n_avatars} avatars + {n_media} media = "
                    f"{len(captured_images)} total"
                )

                with LOG_PATH.open("a", encoding="utf-8") as f:
                    f.write(f"\n\n<!-- scroll {i + 1} -->\n")
                    f.write(html)
                    f.write("\n")

            except KeyboardInterrupt:
                print(
                    f"\nInterrupted after {i + 1} scroll(s); "
                    f"will parse what we have"
                )
                break
            except asyncio.CancelledError:
                print(
                    f"\nCancelled after {i + 1} scroll(s); "
                    f"will parse what we have"
                )
                break

        # Re-enable cache before closing
        try:
            await tab.send(
                uc.cdp.network.set_cache_disabled(cache_disabled=False)
            )
        except Exception:
            pass

        URL_MAP_PATH.write_text(
            json.dumps(lookup_map, indent=2, ensure_ascii=False)
        )
        print(
            f"URL mapping saved to {URL_MAP_PATH} ({len(lookup_map)} entries)"
        )

    finally:
        await browser.stop()
        print("Browser closed")

    if not html_chunks:
        print("No HTML captured; nothing to parse")
        return []

    # ═══════════════ Parse tweets from collected HTML ═══════════════
    combined_html = "\n".join(html_chunks)
    soup = BeautifulSoup(combined_html, "lxml")
    articles = soup.select('article[data-testid="tweet"]')
    print(f"Parsing {len(articles)} tweet articles…")

    tweet_objects: List[dict] = []
    seen: set = set()

    for idx, article in enumerate(articles, 1):
        # --- Tweet URL / user ---
        status_el = article.select_one('a[href*="/status/"]')
        url = status_el.get("href") if status_el else None
        if url and url.startswith("/"):
            url = "https://x.com" + url

        tweet_id = None
        user = None
        if url:
            parts = [p for p in urlparse(url).path.split("/") if p]
            if len(parts) >= 2 and parts[-1].isdigit():
                tweet_id = parts[-1]
                user = (
                    parts[-3]
                    if len(parts) >= 3 and parts[-2] == "status"
                    else parts[-2]
                )

        if not user:
            name_link = article.select_one(
                'div[data-testid="User-Name"] a[href^="/"]'
            )
            href = name_link.get("href") if name_link else None
            if href:
                segs = [p for p in urlparse(href).path.split("/") if p]
                if segs:
                    user = segs[0]

        # --- Retweet / repost ---
        retweeted_by = None
        social_el = article.select_one('[data-testid="socialContext"]')
        social_link = social_el.find_parent("a") if social_el else None
        if not social_link:
            for cand in article.select('a[href^="/"]'):
                if cand.select_one('[data-testid="socialContext"]'):
                    social_link = cand
                    break
        if not social_link:
            social_link = article.select_one(
                '[data-testid="socialContext"] a[href^="/"]'
            )
        if social_link:
            href = social_link.get("href")
            if href:
                segs = [p for p in urlparse(href).path.split("/") if p]
                if segs:
                    retweeted_by = segs[0]
        if not retweeted_by and social_el:
            st = social_el.get_text(" ", strip=True)
            hm = re.search(r"@([A-Za-z0-9_]+)", st)
            rm = re.search(
                r"^(.*?)\s*reposted", st, flags=re.IGNORECASE
            )
            if hm:
                retweeted_by = hm.group(1)
            elif rm:
                ng = rm.group(1).strip()
                if ng and ng.lower() != "you":
                    retweeted_by = ng

        # --- Quote block ---
        quote_block = None
        for cand in article.select('div[role="link"][tabindex="0"]'):
            if cand.select_one('[data-testid="tweetText"]'):
                quote_block = cand
                break

        # --- Tweet text ---
        text_parts: List[str] = []
        for tn in article.select('[data-testid="tweetText"]'):
            if quote_block and quote_block in tn.parents:
                continue
            piece = tn.get_text(" ", strip=True)
            if piece:
                text_parts.append(piece)
        text = "\n".join(text_parts).strip()
        if not text:
            continue

        time_el = article.select_one("time")
        created_at = time_el.get("datetime") if time_el else None

        # ── Avatar (3 extraction methods) ──────────────────────────
        avatar_src = None

        # Method 1: <img src="…/profile_images/…">
        avatar_el = article.select_one(
            'img[src*="pbs.twimg.com/profile_images"]'
        )
        if avatar_el:
            avatar_src = avatar_el.get("src")

        # Method 2: srcset contains profile_images
        if not avatar_src:
            for img in article.select("img[srcset]"):
                srcset = img.get("srcset", "")
                if "profile_images" in srcset:
                    first_url = srcset.split(",")[0].strip().split()[0]
                    if "profile_images" in first_url:
                        avatar_src = first_url
                        break

        # Method 3: CSS background-image on the avatar wrapper
        if not avatar_src:
            for el in article.select(
                '[data-testid="Tweet-User-Avatar"] [style]'
            ):
                style = el.get("style", "")
                m = re.search(
                    r'url\(["\']?(https://pbs\.twimg\.com/profile_images/'
                    r'[^"\')]+)',
                    style,
                )
                if m:
                    avatar_src = m.group(1)
                    break

        # Match to captured file
        avatar_local = _find_captured(avatar_src, captured_images)

        avatar_rel = None
        if user and avatar_local and avatar_local.exists():
            ext = avatar_local.suffix or ".jpg"
            avatar_dest = AVATAR_DIR / f"{user}{ext}"
            if not avatar_dest.exists():
                shutil.copy2(avatar_local, avatar_dest)
            avatar_rel = f"twitter_profile_pics/{avatar_dest.name}"

        # ── Media images ───────────────────────────────────────────
        media_local: List[str] = []
        media_orig_urls: List[str] = []

        for img in article.select("img[src]"):
            if quote_block and quote_block in img.parents:
                continue
            src = img.get("src")
            if not src:
                continue
            if "profile_images" in src:
                continue
            if "emoji" in src.lower():
                continue

            local = _find_captured(src, captured_images)
            if local:
                rel = f"twitter_media/{local.name}"
                if rel not in media_local:
                    media_local.append(rel)

            if "pbs.twimg.com" in src:
                best = _best_pbs_url(img) or src
                orig = _orig_media_url(best)
            else:
                orig = src
            if orig not in media_orig_urls:
                media_orig_urls.append(orig)

        # Background images
        for el in article.select("[style]"):
            if quote_block and quote_block in el.parents:
                continue
            style = el.get("style", "")
            bg_urls = re.findall(
                r'url\(["\']?(https?://[^"\')]+)', style
            )
            for bg_url in bg_urls:
                if "profile_images" in bg_url:
                    continue
                local = _find_captured(bg_url, captured_images)
                if local:
                    rel = f"twitter_media/{local.name}"
                    if rel not in media_local:
                        media_local.append(rel)
                if bg_url not in media_orig_urls:
                    media_orig_urls.append(bg_url)

        # Video posters
        for vid in article.select("video[poster]"):
            if quote_block and quote_block in vid.parents:
                continue
            poster = vid.get("poster")
            if poster:
                local = _find_captured(poster, captured_images)
                if local:
                    rel = f"twitter_media/{local.name}"
                    if rel not in media_local:
                        media_local.append(rel)
                if poster not in media_orig_urls:
                    media_orig_urls.append(poster)

        # --- Engagement ---
        rep_el = article.select_one('button[data-testid="reply"]')
        ret_el = article.select_one('button[data-testid="retweet"]')
        lik_el = article.select_one('button[data-testid="like"]')
        replies = (
            _parse_count(rep_el.get("aria-label")) if rep_el else None
        )
        reposts = (
            _parse_count(ret_el.get("aria-label")) if ret_el else None
        )
        likes = (
            _parse_count(lik_el.get("aria-label")) if lik_el else None
        )

        # --- Quote tweet ---
        quote = None
        if quote_block:
            qs_el = quote_block.select_one('a[href*="/status/"]')
            q_url = qs_el.get("href") if qs_el else None
            if q_url and q_url.startswith("/"):
                q_url = "https://x.com" + q_url

            q_id = q_user = None
            if q_url:
                qp = [p for p in urlparse(q_url).path.split("/") if p]
                if len(qp) >= 2:
                    ii = next(
                        (j for j, p in enumerate(qp) if p.isdigit()),
                        None,
                    )
                    if ii is not None:
                        q_id = qp[ii]
                        q_url = (
                            urlparse(q_url)
                            ._replace(
                                path="/" + "/".join(qp[: ii + 1]),
                                params="",
                                query="",
                                fragment="",
                            )
                            .geturl()
                        )
                        if ii >= 2 and qp[ii - 1] == "status":
                            q_user = qp[ii - 2]
                        elif ii >= 1:
                            q_user = qp[ii - 1]

            if not q_user:
                nl = quote_block.select_one(
                    'div[data-testid="User-Name"] a[href^="/"]'
                )
                href = nl.get("href") if nl else None
                if href:
                    segs = [
                        p for p in urlparse(href).path.split("/") if p
                    ]
                    if segs:
                        q_user = segs[0]
            if not q_user:
                ne = quote_block.select_one('[data-testid="User-Name"]')
                if ne:
                    hm = re.search(
                        r"@([A-Za-z0-9_]+)",
                        ne.get_text(" ", strip=True),
                    )
                    if hm:
                        q_user = hm.group(1)

            q_text_parts: List[str] = []
            for qtn in quote_block.select('[data-testid="tweetText"]'):
                piece = qtn.get_text(" ", strip=True)
                if piece:
                    q_text_parts.append(piece)
            q_text = "\n".join(q_text_parts).strip()

            q_media_local: List[str] = []
            q_media_orig: List[str] = []
            for img in quote_block.select("img[src]"):
                src = img.get("src")
                if (
                    not src
                    or "profile_images" in src
                    or "emoji" in src.lower()
                ):
                    continue
                local = _find_captured(src, captured_images)
                if local:
                    rel = f"twitter_media/{local.name}"
                    if rel not in q_media_local:
                        q_media_local.append(rel)
                if "pbs.twimg.com" in src:
                    best = _best_pbs_url(img) or src
                    orig = _orig_media_url(best)
                else:
                    orig = src
                if orig not in q_media_orig:
                    q_media_orig.append(orig)

            if q_text or q_url or q_user:
                quote = {
                    "id": q_id,
                    "url": q_url,
                    "user": q_user,
                    "text": q_text,
                    "media": (
                        q_media_local if q_media_local else q_media_orig
                    ),
                    "media_fallback_urls": q_media_orig,
                }

        # --- Deduplicate ---
        key = tweet_id or text
        if key in seen:
            continue
        seen.add(key)

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
                "avatar": avatar_rel or avatar_src,
                "quote": quote,
                "retweeted_by": retweeted_by,
                "is_retweet": retweeted_by is not None,
                "media": (
                    media_local if media_local else media_orig_urls
                ),
                "media_fallback_urls": media_orig_urls,
            }
        )

        if idx % 50 == 0:
            print(f"  …parsed {idx}/{len(articles)}")

    # ── Summary ──
    avatars_local = sum(
        1
        for t in tweet_objects
        if (t.get("avatar") or "").startswith("twitter_profile_pics/")
    )
    avatars_url_only = sum(
        1
        for t in tweet_objects
        if t.get("avatar")
        and not t["avatar"].startswith("twitter_profile_pics/")
    )
    avatars_none = sum(1 for t in tweet_objects if not t.get("avatar"))
    print(
        f"Parsed {len(tweet_objects)} unique tweets "
        f"from {len(articles)} articles"
    )
    print(
        f"Avatars: {avatars_local} local, "
        f"{avatars_url_only} URL-only fallback, "
        f"{avatars_none} missing"
    )

    with JSONL_PATH.open("w", encoding="utf-8") as f:
        for t in tweet_objects:
            f.write(json.dumps(t, ensure_ascii=False) + "\n")
    print(f"Wrote {len(tweet_objects)} tweets → {JSONL_PATH}")

    return [t["text"] for t in tweet_objects]


if __name__ == "__main__":
    scraped = asyncio.run(scrape_list())
    for i, text in enumerate(scraped, 1):
        print(f"--- {i} ---")
        print(text)
