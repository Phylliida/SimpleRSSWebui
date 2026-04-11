import asyncio
import json
import re
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
JSONL_PATH = Path(__file__).parent / "twitter_scrolls.jsonl"
AVATAR_DIR = Path(__file__).parent / "twitter_profile_pics"
MEDIA_DIR = Path(__file__).parent / "twitter_media"
URL_MAP_PATH = MEDIA_DIR / "url_mapping.json"

BUFFER_RESET_INTERVAL = 5
CACHE_WARM_SCROLLS = 5
DOM_PRUNE_KEEP = 8  # keep this many cells after pruning

_COUNT_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)([KkMm])?")

# ──────────────────────────────────────────────────────────────────
# JS that guts old tweet cells to free renderer memory.
# We keep the outer div (with a fixed height) so Twitter's virtual
# scroller doesn't recalculate and jump.  Everything heavy inside
# (images, videos, nested DOM) gets destroyed.
# ──────────────────────────────────────────────────────────────────
PRUNE_DOM_JS = """
(() => {
    // 1. Kill ALL videos on the page (autoplay eats huge memory)
    document.querySelectorAll('video').forEach(v => {
        v.pause();
        v.removeAttribute('src');
        v.load();
    });

    // 2. Revoke any blob URLs
    document.querySelectorAll('[src^="blob:"]').forEach(el => {
        try { URL.revokeObjectURL(el.src); } catch(e) {}
    });

    // 3. Gut old cells, preserve height shell
    const cells = [...document.querySelectorAll('[data-testid="cellInnerDiv"]')];
    const keep = """ + str(DOM_PRUNE_KEEP) + """;
    if (cells.length > keep) {
        for (let i = 0; i < cells.length - keep; i++) {
            const cell = cells[i];
            if (cell.dataset.pruned) continue;
            const h = cell.offsetHeight;
            // Null out heavy resources before removing subtree
            cell.querySelectorAll('img').forEach(img => {
                img.removeAttribute('src');
                img.removeAttribute('srcset');
            });
            cell.querySelectorAll('video, source, iframe').forEach(el => el.remove());
            // Replace contents with empty shell
            cell.innerHTML = '';
            cell.style.height = h + 'px';
            cell.style.minHeight = h + 'px';
            cell.style.overflow = 'hidden';
            cell.dataset.pruned = '1';
        }
    }

    // 4. Hint to GC (works if DevTools protocol exposed it)
    if (typeof gc === 'function') { try { gc(); } catch(e) {} }
})();
"""


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

    if url in captured:
        return captured[url]

    normalized = url.replace("&amp;", "&")
    if normalized in captured:
        return captured[normalized]

    base_path = urlparse(normalized).path
    for cap_url, path in captured.items():
        if urlparse(cap_url).path == base_path:
            return path

    stripped = _strip_profile_size(base_path)
    if stripped != base_path:
        for cap_url, path in captured.items():
            cap_stripped = _strip_profile_size(urlparse(cap_url).path)
            if cap_stripped == stripped:
                return path

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


def _parse_articles(
    html: str,
    captured_images: dict[str, Path],
    seen: set,
    tweet_objects: list[dict],
) -> int:
    soup = BeautifulSoup(html, "lxml")
    articles = soup.select('article[data-testid="tweet"]')
    added = 0

    for idx, article in enumerate(articles, 1):
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

        quote_block = None
        for cand in article.select('div[role="link"][tabindex="0"]'):
            if cand.select_one('[data-testid="tweetText"]'):
                quote_block = cand
                break

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

        avatar_src = None

        avatar_el = article.select_one(
            'img[src*="pbs.twimg.com/profile_images"]'
        )
        if avatar_el:
            avatar_src = avatar_el.get("src")

        if not avatar_src:
            for img in article.select("img[srcset]"):
                srcset = img.get("srcset", "")
                if "profile_images" in srcset:
                    first_url = srcset.split(",")[0].strip().split()[0]
                    if "profile_images" in first_url:
                        avatar_src = first_url
                        break

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

        avatar_local = _find_captured(avatar_src, captured_images)

        avatar_rel = None
        if user and avatar_local and avatar_local.exists():
            ext = avatar_local.suffix or ".jpg"
            avatar_dest = AVATAR_DIR / f"{user}{ext}"
            if not avatar_dest.exists():
                shutil.copy2(avatar_local, avatar_dest)
            avatar_rel = f"twitter_profile_pics/{avatar_dest.name}"

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
        added += 1

    return added


# ══════════════════════════════════════════════════════════════════════
#  Main scraper
# ══════════════════════════════════════════════════════════════════════

async def scrape_list(
    scrolls: int = 1000, pause: float = 5, wait_for_login: bool = True
) -> List[str]:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    AVATAR_DIR.mkdir(parents=True, exist_ok=True)

    captured_images: dict[str, Path] = {}
    captured_paths: set[str] = set()
    lookup_map: dict[str, str] = {}
    counter = 0

    tweet_objects: list[dict] = []
    seen: set = set()

    pending_requests: list[tuple[uc.cdp.network.RequestId, str, str]] = []

    browser = await uc.start(user_data_dir=str(PROFILE_DIR), headless=False)
    tab = None

    try:
        tab = await browser.get("about:blank")

        # ── CDP handler: zero awaits, just queue ──
        async def on_response(event: uc.cdp.network.ResponseReceived):
            mime = event.response.mime_type or ""
            if "image" not in mime:
                return
            url = event.response.url
            if url in captured_images:
                return
            normalized = url.replace("&amp;", "&")
            if normalized in captured_images:
                return
            if urlparse(normalized).path in captured_paths:
                return
            pending_requests.append((event.request_id, url, mime))

        # ── Drain queue sequentially ──
        async def drain_pending():
            nonlocal counter
            batch = pending_requests[:]
            pending_requests.clear()
            for request_id, url, mime in batch:
                if url in captured_images:
                    continue
                normalized = url.replace("&amp;", "&")
                if normalized in captured_images:
                    continue
                base_path = urlparse(normalized).path
                if base_path in captured_paths:
                    continue
                try:
                    result = await tab.send(
                        uc.cdp.network.get_response_body(request_id)
                    )
                    body_str, is_b64 = result
                    if is_b64:
                        data = base64.b64decode(body_str)
                    else:
                        data = body_str.encode("utf-8")
                    if len(data) < 50:
                        continue
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
                    captured_paths.add(base_path)
                    lookup_map[fname] = url
                except Exception:
                    continue

        # ── Reset CDP network buffers ──
        async def reset_network():
            try:
                await tab.send(uc.cdp.network.disable())
            except Exception:
                pass
            await asyncio.sleep(0.1)
            try:
                await tab.send(
                    uc.cdp.network.enable(
                        max_total_buffer_size=100_000_000,
                        max_resource_buffer_size=10_000_000,
                    )
                )
            except TypeError:
                try:
                    await tab.send(uc.cdp.network.enable())
                except Exception:
                    pass
            except Exception:
                pass

        # ── Wire up handler + enable network ──
        tab.add_handler(uc.cdp.network.ResponseReceived, on_response)

        try:
            await tab.send(
                uc.cdp.network.enable(
                    max_total_buffer_size=100_000_000,
                    max_resource_buffer_size=10_000_000,
                )
            )
        except TypeError:
            await tab.send(uc.cdp.network.enable())

        try:
            await tab.send(
                uc.cdp.network.set_cache_disabled(cache_disabled=True)
            )
        except Exception:
            pass

        await tab.get(LIST_URL)

        JSONL_PATH.write_text("", encoding="utf-8")

        # ══════════════════════════════════════════════════════════
        #  Scroll loop
        # ══════════════════════════════════════════════════════════
        for i in range(scrolls):
            try:
                await tab.evaluate(
                    "window.scrollBy(0, window.innerHeight * 0.9);"
                )
                await tab.sleep(pause)

                if i == CACHE_WARM_SCROLLS:
                    try:
                        await tab.send(
                            uc.cdp.network.set_cache_disabled(
                                cache_disabled=False
                            )
                        )
                    except Exception:
                        pass

                # 1. Drain queued image bodies (sequential)
                await drain_pending()

                # 2. Parse current page HTML
                html = await tab.get_content()

                new_count = _parse_articles(
                    html, captured_images, seen, tweet_objects
                )

                if new_count > 0:
                    with JSONL_PATH.open("a", encoding="utf-8") as f:
                        for t in tweet_objects[-new_count:]:
                            f.write(
                                json.dumps(t, ensure_ascii=False) + "\n"
                            )

                # 3. ★ PRUNE DOM — free renderer memory ★
                try:
                    await tab.evaluate(PRUNE_DOM_JS)
                except Exception:
                    pass

                # 4. Periodically reset CDP buffers
                if i > 0 and i % BUFFER_RESET_INTERVAL == 0:
                    await reset_network()

                if (i + 1) % 10 == 0:
                    print(
                        f"  scroll {i+1}: {len(tweet_objects)} tweets, "
                        f"{len(captured_images)} images captured"
                    )

            except (KeyboardInterrupt, asyncio.CancelledError):
                break
            except Exception as exc:
                print(f"  scroll {i+1} error: {exc}")
                try:
                    pending_requests.clear()
                    await reset_network()
                except Exception:
                    break

        # ── Final drain ──
        try:
            await drain_pending()
        except Exception:
            pass

        try:
            await tab.send(
                uc.cdp.network.set_cache_disabled(cache_disabled=False)
            )
        except Exception:
            pass

        URL_MAP_PATH.write_text(
            json.dumps(lookup_map, indent=2, ensure_ascii=False)
        )

    finally:
        try:
            await browser.stop()
        except Exception:
            pass

    if tweet_objects:
        with JSONL_PATH.open("w", encoding="utf-8") as f:
            for t in tweet_objects:
                f.write(json.dumps(t, ensure_ascii=False) + "\n")

    return [t["text"] for t in tweet_objects]


if __name__ == "__main__":
    asyncio.run(scrape_list())
