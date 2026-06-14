import json
import os
import re
import subprocess
import threading
import urllib.request
import time
import uuid
from pathlib import Path

import httpx

DOWNLOADS_DIR = Path(__file__).parent / "downloads"

# Auto-detect system proxy for httpx (used for Twitter/TikTok)
_system_proxies = urllib.request.getproxies()
PROXY_URL = _system_proxies.get("http") or _system_proxies.get("https")

# Set proxy env vars so httpx picks them up automatically (works with all httpx versions)
if PROXY_URL:
    os.environ.setdefault("HTTP_PROXY", PROXY_URL)
    os.environ.setdefault("HTTPS_PROXY", PROXY_URL)

# Monkey-patch httpx to always use proxy from env vars (for older httpx versions)
_orig_get = httpx.get
_orig_post = httpx.post
def _get(*a, **kw): kw.setdefault("trust_env", True); return _orig_get(*a, **kw)
def _post(*a, **kw): kw.setdefault("trust_env", True); return _orig_post(*a, **kw)
httpx.get = _get
httpx.post = _post

# Simple in-memory cache to avoid hitting Douyin repeatedly for the same URL
_cache: dict = {}
_CACHE_TTL = 600  # 10 minutes


def _cache_get(key: str):
    entry = _cache.get(key)
    if entry and time.time() - entry["_ts"] < _CACHE_TTL:
        return entry
    return None


def _cache_set(key: str, info: dict):
    info["_ts"] = time.time()
    _cache[key] = info

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
)


def _ensure_downloads_dir():
    DOWNLOADS_DIR.mkdir(exist_ok=True)


def _extract_url(text: str) -> str:
    """Extract and normalize the first URL from user input (may contain share text)."""
    text = text.strip()
    url_match = re.search(
        r"(https?://\S+|v\.douyin\.com/\S+|vm\.tiktok\.com/\S+|vt\.tiktok\.com/\S+)",
        text,
    )
    if url_match:
        url = url_match.group(1)
        url = url.rstrip(".,;:!?，。；：！？)")
    else:
        url = text

    if not url.startswith("http://") and not url.startswith("https://"):
        url = "https://" + url
    return url


def _is_douyin(url: str) -> bool:
    return any(d in url for d in ["douyin.com", "iesdouyin.com"])


def _is_twitter(url: str) -> bool:
    return any(d in url for d in ["twitter.com", "x.com"])


def _is_tiktok(url: str) -> bool:
    return "tiktok.com" in url


def _is_bilibili(url: str) -> bool:
    return any(d in url for d in ["bilibili.com", "b23.tv"])


def _is_kuaishou(url: str) -> bool:
    return any(d in url for d in ["kuaishou.com", "v.kuaishou.com", "gifshow.com"])


def _resolve_douyin(url: str) -> tuple[str, str]:
    """Resolve a Douyin short link. Returns (id, type) where type is 'video' or 'note'."""
    # Direct URL patterns — slides also use note endpoint for data
    for pattern, content_type in [(r"/video/(\d+)", "video"), (r"/note/(\d+)", "note"), (r"/slides/(\d+)", "note")]:
        match = re.search(pattern, url)
        if match:
            return match.group(1), content_type

    # Resolve short link (302 redirect)
    headers = {"User-Agent": MOBILE_UA}
    r = httpx.get(url, headers=headers, follow_redirects=False, timeout=30)

    if r.status_code in (301, 302):
        location = r.headers.get("location", "")
        for pattern, content_type in [(r"/video/(\d+)", "video"), (r"/note/(\d+)", "note"), (r"/slides/(\d+)", "note")]:
            match = re.search(pattern, location)
            if match:
                return match.group(1), content_type

    # Follow full redirect chain
    r = httpx.get(url, headers=headers, follow_redirects=True, timeout=30)
    final_url = str(r.url)
    for pattern, content_type in [(r"/video/(\d+)", "video"), (r"/note/(\d+)", "note"), (r"/slides/(\d+)", "note")]:
        match = re.search(pattern, final_url)
        if match:
            return match.group(1), content_type

    raise ValueError(f"无法从链接中解析: {url}")


def _extract_images_with_playwright(share_url: str) -> tuple[list[str], list[str]]:
    """Use Playwright to extract video URLs and images from a Douyin note page.
    Supports pagination by clicking through slides.
    Returns (video_urls, images) - for live photos/animated images, video_urls contains all videos.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return [], []  # Playwright not installed

    video_urls = []
    images = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(share_url, wait_until='domcontentloaded', timeout=15000)

        # Wait for images to stabilize (lazy-loaded), poll every 400ms, max 5s
        prev_count = 0
        stable_rounds = 0
        for _ in range(13):
            try:
                count = page.evaluate('''() => {
                    let n = 0;
                    document.querySelectorAll('img').forEach(el => {
                        const src = el.src || '';
                        if (src.includes('douyinpic') && src.includes('tos-cn-i-') &&
                            !src.includes('100x100') && !src.includes('avatar')) n++;
                    });
                    return n;
                }''')
            except Exception:
                page.wait_for_timeout(400)
                continue
            if count != prev_count:
                stable_rounds = 0
                prev_count = count
                page.wait_for_timeout(400)
                continue
            stable_rounds += 1
            if count > 0 and stable_rounds >= 4:
                break
            if count == 0 and stable_rounds >= 6:
                break
            page.wait_for_timeout(400)

        # Wait for video elements to appear (they load after images)
        for _ in range(8):
            has_video = page.evaluate('''() => {
                let found = false;
                document.querySelectorAll('video source').forEach(el => {
                    const src = el.src || el.getAttribute('src') || '';
                    if (src.includes('douyinvod')) found = true;
                });
                return found;
            }''')
            if has_video:
                break
            page.wait_for_timeout(400)

        # Batch extract via JS - container scoped + filters + dedup by ID
        all_data = page.evaluate('''() => {
            const videos = [];
            const seenPaths = new Set();
            document.querySelectorAll('video source').forEach(el => {
                const src = el.src || el.getAttribute('src') || '';
                if (!src || !src.includes('douyinvod')) return;
                const pm = src.match(/douyinvod\\.com\\/[^\\/]+\\/[^\\/]+(\\/video\\/[^?]+)/);
                const path = pm ? pm[1] : src;
                if (seenPaths.has(path)) return;
                seenPaths.add(path);
                videos.push(src);
            });
            const images = [];
            const seen = new Set();
            const container = document.querySelector('.note-detail-container');
            const scope = container || document;
            scope.querySelectorAll('img').forEach(el => {
                const src = el.src || '';
                if (!src || !src.includes('douyinpic')) return;
                if (src.includes('100x100') || src.includes('avatar')) return;
                if (src.includes('image-cut-tos-priv')) return;
                if (src.includes('image-cut-tos')) return;
                if (src.includes('ies.fe.effect')) return;
                if (src.includes('sticker')) return;
                if (src.includes('p14lwwcsbr')) return;
                if (!src.includes('tos-cn-i-')) return;
                const w = el.naturalWidth || el.width || 0;
                const h = el.naturalHeight || el.height || 0;
                if (w < 200 || h < 200) return;
                const idMatch = src.match(/tos-cn-i-[^~?]+/);
                const imgId = idMatch ? idMatch[0] : src;
                if (seen.has(imgId)) return;
                seen.add(imgId);
                images.push(src);
            });
            return {videos, images};
        }''')
        video_urls = all_data.get('videos', [])
        images = all_data.get('images', [])

        browser.close()

    return video_urls, images




def _extract_with_playwright_async(share_url: str) -> tuple[list[str], list[str]]:
    """Async version of Playwright extraction for use in FastAPI."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return [], []

    import asyncio

    async def _extract():
        video_urls = []
        images = []

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(share_url, wait_until='domcontentloaded', timeout=15000)

            # Wait for images to stabilize (lazy-loaded), poll every 400ms, max 5s
            prev_count = 0
            stable_rounds = 0
            for _ in range(13):  # 13 * 400ms = 5.2s max
                try:
                    count = await page.evaluate('''() => {
                        let n = 0;
                        document.querySelectorAll('img').forEach(el => {
                            const src = el.src || '';
                            if (src.includes('douyinpic') && src.includes('tos-cn-i-') &&
                                !src.includes('100x100') && !src.includes('avatar')) n++;
                        });
                        return n;
                    }''')
                except Exception:
                    await page.wait_for_timeout(400)
                    continue
                if count != prev_count:
                    stable_rounds = 0
                    prev_count = count
                    await page.wait_for_timeout(400)
                    continue
                stable_rounds += 1
                if count > 0 and stable_rounds >= 4:
                    break
                if count == 0 and stable_rounds >= 6:
                    break
                await page.wait_for_timeout(400)

            # Wait for video elements to appear (they load after images)
            for _ in range(8):  # 8 * 400ms = 3.2s max
                has_video = await page.evaluate('''() => {
                    let found = false;
                    document.querySelectorAll('video source').forEach(el => {
                        const src = el.src || el.getAttribute('src') || '';
                        if (src.includes('douyinvod')) found = true;
                    });
                    return found;
                }''')
                if has_video:
                    break
                await page.wait_for_timeout(400)

            # Batch extract via JS - container scoped + filters + dedup by ID
            all_data = await page.evaluate('''() => {
                const videos = [];
                const seenPaths = new Set();
                document.querySelectorAll('video source').forEach(el => {
                    const src = el.src || el.getAttribute('src') || '';
                    if (!src || !src.includes('douyinvod')) return;
                    const pm = src.match(/douyinvod\\.com\\/[^\\/]+\\/[^\\/]+(\\/video\\/[^?]+)/);
                    const path = pm ? pm[1] : src;
                    if (seenPaths.has(path)) return;
                    seenPaths.add(path);
                    videos.push(src);
                });
                const images = [];
                const seen = new Set();
                const container = document.querySelector('.note-detail-container');
                const scope = container || document;
                scope.querySelectorAll('img').forEach(el => {
                    const src = el.src || '';
                    if (!src || !src.includes('douyinpic')) return;
                    if (src.includes('100x100') || src.includes('avatar')) return;
                    if (src.includes('image-cut-tos-priv')) return;
                    if (src.includes('image-cut-tos')) return;
                    if (src.includes('ies.fe.effect')) return;
                    if (src.includes('sticker')) return;
                    if (src.includes('p14lwwcsbr')) return;
                    if (!src.includes('tos-cn-i-')) return;
                    const w = el.naturalWidth || el.width || 0;
                    const h = el.naturalHeight || el.height || 0;
                    if (w < 200 || h < 200) return;
                    const idMatch = src.match(/tos-cn-i-[^~?]+/);
                    const imgId = idMatch ? idMatch[0] : src;
                    if (seen.has(imgId)) return;
                    seen.add(imgId);
                    images.push(src);
                });
                return {videos, images};
            }''')
            video_urls = all_data.get('videos', [])
            images = all_data.get('images', [])

            await browser.close()

        return video_urls, images

    # Run in a new thread to avoid asyncio conflicts
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor() as executor:
        future = executor.submit(asyncio.run, _extract())
        return future.result(timeout=120)


def _extract_douyin(url: str) -> dict:
    """Extract Douyin video or photo note info by scraping the mobile share page."""
    item_id, content_type = _resolve_douyin(url)
    path_segment = "note" if content_type == "note" else "video"
    share_url = f"https://www.iesdouyin.com/share/{path_segment}/{item_id}/"
    headers = {"User-Agent": MOBILE_UA, "Referer": share_url}

    r = httpx.get(share_url, headers=headers, follow_redirects=True, timeout=30)
    html = r.text

    # Extract title
    title = "未知标题"
    desc_match = re.search(r'"desc":"([^"]{1,300})"', html)
    if desc_match:
        title = json.loads('"' + desc_match.group(1) + '"')

    # Extract thumbnail (cover image, prefer high-res)
    thumbnail = ""
    cover_urls = re.findall(r"https:[^\"\s]*douyinpic\.com[^\"\s]*", html)
    for raw in cover_urls:
        try:
            decoded = json.loads('"' + raw + '"')
        except json.JSONDecodeError:
            decoded = raw
        if "avatar" in decoded or "100x100" in decoded:
            continue
        if not thumbnail or "1080x1080" in decoded:
            thumbnail = decoded
            if "1080x1080" in decoded:
                break

    # Extract background music MP3 (for slides)
    music_url = ""
    music_match = re.search(r'"play_addr":\{"uri":"([^"]+\.mp3)"', html)
    if music_match:
        music_url = json.loads('"' + music_match.group(1) + '"')

    if content_type == "note":
        # Extract all images from photo note (use bracket counting for nested JSON)
        images = []
        img_start = html.find('"images":[')
        if img_start >= 0:
            arr_start = img_start + 9  # position of opening '[' after "images":
            depth = 0
            end = arr_start
            for i in range(arr_start, min(arr_start + 500000, len(html))):
                if html[i] == "[":
                    depth += 1
                elif html[i] == "]":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            img_data = html[arr_start:end]
            # Each image: "url_list":["URL1","URL2",...] — grab the first URL (best quality)
            # Support both plain and unicode-escaped URLs
            for block in re.finditer(r'"url_list":\[\"(https?:[^\"]+)"', img_data):
                raw = block.group(1)
                raw = raw.replace("\\u002F", "/")
                try:
                    img_url = json.loads('"' + raw + '"')
                except json.JSONDecodeError:
                    img_url = raw
                # Only keep content images, skip music covers etc.
                if 'tos-cn-i-' not in img_url:
                    continue
                if img_url not in images:
                    images.append(img_url)

        # Fallback: use Playwright for live photos / animated images
        # Skip for pure photo posts to avoid 8s+ delay
        # Live photos have img_bitrate=null, pure photos have img_bitrate=[...]
        has_video_hint = '"img_bitrate":null' in html
        # Do NOT use len(images) <= 1 — single-image posts are valid and don't need Playwright
        if (has_video_hint or len(images) == 0 or len(html) < 10000):
            try:
                pw_videos, pw_images = _extract_with_playwright_async(share_url)
                if pw_images:
                    images = pw_images
                if pw_videos:
                    # Multiple animated images (live photos)
                    return {
                        "title": title,
                        "thumbnail": images[0] if images else thumbnail,
                        "duration": 0,
                        "type": "live_photo",
                        "video_url": pw_videos[0],  # Primary video
                        "video_urls": pw_videos,     # All videos for multi-animated
                        "images": images,             # Also include images
                        "music_url": music_url,
                        "platform": "douyin",
                    }
            except Exception as e:
                print(f"[playwright] Error: {e}")  # Debug

        return {
            "title": title,
            "thumbnail": images[0] if images else thumbnail,
            "duration": 0,
            "type": "photo",
            "images": images,
            "music_url": music_url,
            "platform": "douyin",
        }

    # Video post
    duration = 0
    dur_match = re.search(r'"duration":(\d+)', html)
    if dur_match:
        duration = int(dur_match.group(1))

    video_url = ""
    # Method 1: Try playwm URL pattern (supports both plain and unicode-escaped)
    play_match = re.search(r'"url_list":\["(https?:[^"]+playwm[^"]+)"\]', html)
    if not play_match:
        play_match = re.search(r'"url_list":\["(https?:\\u002F[^"]+playwm[^"]+)"\]', html)
    if play_match:
        wm_url = play_match.group(1)
        wm_url = wm_url.replace("\\u002F", "/")
        try:
            wm_url = json.loads('"' + wm_url + '"')
        except json.JSONDecodeError:
            pass
        video_url = wm_url.replace("/playwm/", "/play/")

    # Method 2: Fallback to direct CDN URI if play URL returns empty
    if not video_url:
        uri_match = re.search(r'"uri":"(https?:[^"]+douyinstatic[^"]+)"', html)
        if not uri_match:
            uri_match = re.search(r'"uri":"(https?:\\u002F[^"]+douyinstatic[^"]+)"', html)
        if uri_match:
            video_url = uri_match.group(1)
            video_url = video_url.replace("\\u002F", "/")
            try:
                video_url = json.loads('"' + video_url + '"')
            except json.JSONDecodeError:
                pass

    return {
        "title": title,
        "thumbnail": thumbnail,
        "duration": duration,
        "type": "video",
        "video_url": video_url,
        "platform": "douyin",
    }


def _extract_tiktok(url: str) -> dict:
    """Extract TikTok video info via ssstiktok.cc (no proxy needed)."""
    import subprocess, tempfile

    # Step 1: POST to ssstiktok.cc to get preview token
    r = httpx.post("https://ssstiktok.cc/",
                   data={"url": url, "mode": "video"},
                   headers={"User-Agent": MOBILE_UA, "Referer": "https://ssstiktok.cc/"},
                   follow_redirects=True, timeout=30)
    if r.status_code != 200:
        raise ValueError(f"ssstiktok 返回 {r.status_code}")

    # Extract preview path (redirects to /preview/<token>)
    preview_match = re.search(r'href="(/preview/[^"]+)"', r.text)
    if not preview_match:
        raise ValueError("ssstiktok 未返回预览链接")

    preview_path = preview_match.group(1)

    # Step 2: Get download link from preview page
    r2 = httpx.get(f"https://ssstiktok.cc{preview_path}",
                   headers={"User-Agent": MOBILE_UA}, follow_redirects=True, timeout=30)
    if r2.status_code != 200:
        raise ValueError(f"ssstiktok 预览页返回 {r2.status_code}")

    # Extract download path
    dl_match = re.search(r'href="(/download/video/[^"]+)"', r2.text)
    if not dl_match:
        raise ValueError("ssstiktok 未找到下载链接")

    download_url = f"https://ssstiktok.cc{dl_match.group(1)}"

    # Extract title
    title_match = re.search(r'<h2[^>]*>([^<]+)</h2>', r2.text)
    title = title_match.group(1).strip() if title_match else "tiktok_video"

    return {
        "title": title,
        "thumbnail": "",
        "duration": 0,
        "type": "video",
        "video_url": download_url,
        "hd_url": download_url,
        "platform": "tiktok",
    }


def _resolve_twitter_url(url: str) -> str:
    """Resolve t.co short links to full Twitter/X URL."""
    if "t.co/" in url:
        r = httpx.get(url, headers={"User-Agent": MOBILE_UA}, follow_redirects=True, timeout=15)
        return str(r.url)
    return url


def _parse_twitter_url(url: str) -> tuple[str, str]:
    """Extract (username, tweet_id) from a Twitter/X URL."""
    url = _resolve_twitter_url(url)
    m = re.search(r"(?:twitter\.com|x\.com)/(\w+)/status/(\d+)", url)
    if not m:
        raise ValueError(f"无法解析推特链接: {url}")
    return m.group(1), m.group(2)


def _curl_get(url: str, timeout: int = 30) -> str:
    """Use subprocess curl to bypass httpx proxy issues on cloud servers."""
    import subprocess
    cmd = ["curl", "-s", "-L", "--max-time", str(timeout), "-H", f"User-Agent: {MOBILE_UA}"]
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy:
        cmd += ["--proxy", proxy]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    if result.returncode != 0:
        raise ValueError(f"curl failed: {result.stderr[:200]}")
    return result.stdout.decode("utf-8", errors="replace")


def _curl_download(url: str, filepath: str, timeout: int = 120, headers: dict = None) -> int:
    """Use subprocess curl to download a file. Returns file size."""
    import subprocess
    cmd = ["curl", "-s", "-L", "--max-time", str(timeout), "-o", filepath]
    proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy:
        cmd += ["--proxy", proxy]
    if headers:
        for k, v in headers.items():
            cmd += ["-H", f"{k}: {v}"]
    cmd.append(url)
    result = subprocess.run(cmd, capture_output=True, timeout=timeout + 10)
    if result.returncode != 0:
        raise ValueError(f"curl download failed: {result.stderr[:200]}")
    return os.path.getsize(filepath)


def _extract_twitter(url: str) -> dict:
    """Extract Twitter/X video info via fxtwitter API (uses curl for proxy compatibility)."""
    username, tweet_id = _parse_twitter_url(url)
    api_url = f"https://api.fxtwitter.com/{username}/status/{tweet_id}"
    text = _curl_get(api_url, timeout=30)
    data = json.loads(text)
    if data.get("code") != 200:
        raise ValueError(data.get("message", "fxtwitter API 错误"))

    tweet = data.get("tweet", {})
    media = tweet.get("media", {})
    all_media = media.get("all", [])

    # Find first video
    video_url = ""
    thumbnail = ""
    duration = 0
    variants = []
    for item in all_media:
        if item.get("type") == "video":
            video_url = item.get("url", "")
            thumbnail = item.get("thumbnail_url", "")
            duration = item.get("duration", 0)
            variants = item.get("variants", [])
            break

    if not video_url:
        raise ValueError("该推特不包含视频")

    # Pick best MP4 quality from variants
    best_url = video_url
    best_bitrate = 0
    for v in variants:
        if v.get("content_type") == "video/mp4":
            br = v.get("bitrate", 0)
            if br > best_bitrate:
                best_bitrate = br
                best_url = v["url"]
    video_url = best_url

    title = tweet.get("text", "未知标题")[:100]

    # Find m3u8 URL for ffmpeg download (video.twimg.com direct URLs may be blocked)
    m3u8_url = ""
    for v in variants:
        if v.get("content_type") == "application/x-mpegURL":
            m3u8_url = v.get("url", "")
            break

    return {
        "title": title,
        "thumbnail": thumbnail,
        "duration": duration,
        "type": "video",
        "video_url": video_url,
        "m3u8_url": m3u8_url,
        "platform": "twitter",
    }


def _extract_bilibili(url: str) -> dict:
    """Extract Bilibili video info via API (no cookies needed for 480p)."""
    # Resolve b23.tv short links
    if "b23.tv" in url:
        r = httpx.get(url, headers={"User-Agent": MOBILE_UA}, follow_redirects=True, timeout=15)
        url = str(r.url)

    # Extract BV ID (with or without BV prefix)
    bv_match = re.search(r'(BV[\w]+)', url)
    if bv_match:
        bvid = bv_match.group(1)
    else:
        # Try /video/ID format (may be missing BV prefix)
        id_match = re.search(r'/video/([\w]+)', url)
        if id_match:
            raw_id = id_match.group(1)
            bvid = raw_id if raw_id.startswith("BV") else "BV" + raw_id
        else:
            raise ValueError(f"无法从链接中提取 BV ID: {url}")

    headers = {"User-Agent": MOBILE_UA, "Referer": "https://www.bilibili.com/"}

    # Step 1: Get video info
    info_url = f"https://api.bilibili.com/x/web-interface/view?bvid={bvid}"
    r = httpx.get(info_url, headers=headers, timeout=15)
    data = r.json()
    if data.get("code") != 0:
        raise ValueError(data.get("message", "B站 API 错误"))

    video_data = data["data"]
    title = video_data.get("title", "未知标题")
    cid = video_data.get("cid")
    thumbnail = video_data.get("pic", "")
    if thumbnail.startswith("//"):
        thumbnail = "https:" + thumbnail

    # Step 2: Get video stream URL (durl = single file, no need to merge)
    play_url = f"https://api.bilibili.com/x/player/playurl?bvid={bvid}&cid={cid}&qn=80&fnval=0"
    r = httpx.get(play_url, headers=headers, timeout=15)
    play_data = r.json()
    if play_data.get("code") != 0:
        raise ValueError(play_data.get("message", "B站播放地址获取失败"))

    durls = play_data["data"].get("durl", [])
    if not durls:
        raise ValueError("B站未返回视频地址")

    video_url = durls[0].get("url", "")

    return {
        "title": title,
        "thumbnail": thumbnail,
        "duration": video_data.get("duration", 0),
        "type": "video",
        "video_url": video_url,
        "platform": "bilibili",
    }


def _extract_kuaishou(url: str) -> dict:
    """Extract Kuaishou video info by scraping mobile share page."""
    headers = {"User-Agent": MOBILE_UA}
    r = httpx.get(url, headers=headers, follow_redirects=True, timeout=15)
    html = r.text
    final_url = str(r.url)

    # Extract video ID from URL
    vid_match = re.search(r'shareObjectId=([^&]+)', final_url)
    if not vid_match:
        vid_match = re.search(r'/short-video/([^?]+)', final_url)
    if not vid_match:
        raise ValueError("无法从快手链接中提取视频 ID")

    # Find video URLs (prefer Ultra > High > others)
    mp4_urls = re.findall(r'https://[^"\s]+\.mp4[^"\s]*', html)
    mp4_urls = list(dict.fromkeys(mp4_urls))  # dedupe preserving order

    if not mp4_urls:
        raise ValueError("该快手作品不包含视频")

    # Separate by quality
    high_url = ""
    ultra_url = ""
    for u in mp4_urls:
        if 'UltraV5' in u and not ultra_url:
            ultra_url = u
        elif 'HighV5' in u and not high_url:
            high_url = u

    fallback = mp4_urls[0]
    video_url = high_url or fallback        # H.264 for browser preview
    hd_url = ultra_url or high_url or fallback  # highest for download

    # Extract title
    title = "未知标题"
    title_match = re.search(r'"caption":"([^"]{1,300})"', html)
    if title_match:
        title = title_match.group(1)

    # Extract cover
    thumbnail = ""
    cover_match = re.search(r'"coverUrl":"([^"]+)"', html)
    if cover_match:
        thumbnail = cover_match.group(1)

    return {
        "title": title,
        "thumbnail": thumbnail,
        "duration": 0,
        "type": "video",
        "video_url": video_url,   # High (H.264) for preview
        "hd_url": hd_url,         # Ultra for download
        "platform": "kuaishou",
    }


def apply_quality(video_url: str, quality: str) -> str:
    """Apply quality setting to a Douyin video URL."""
    # Only apply quality to play/playwm URLs, not direct CDN URLs
    if "aweme.snssdk.com" not in video_url:
        return video_url
    ratio_map = {"720p": "720p", "1080p": "1080p", "hd": "1080p"}
    ratio = ratio_map.get(quality, "1080p")
    if "ratio=" in video_url:
        return re.sub(r"ratio=\w+", f"ratio={ratio}", video_url)
    return video_url + f"&ratio={ratio}"


def extract_video_info(url: str) -> dict:
    """Extract media metadata. Routes to platform-specific extractors."""
    url = _extract_url(url)
    cached = _cache_get(url)
    if cached:
        return {k: v for k, v in cached.items() if not k.startswith("_")}

    if _is_douyin(url):
        info = _extract_douyin(url)
    elif _is_twitter(url):
        info = _extract_twitter(url)
    elif _is_bilibili(url):
        info = _extract_bilibili(url)
    elif _is_kuaishou(url):
        info = _extract_kuaishou(url)
    elif _is_tiktok(url):
        info = _extract_tiktok(url)
    else:
        raise ValueError("不支持的平台链接")
    _cache_set(url, info)
    return {k: v for k, v in info.items() if not k.startswith("_")}


def _convert_to_mp3(video_path: str) -> str:
    """Convert video to MP3 audio using ffmpeg, returns the mp3 file path."""
    mp3_path = str(Path(video_path).with_suffix(".mp3"))
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-vn", "-acodec", "libmp3lame", "-q:a", "2", mp3_path],
        capture_output=True,
        check=True,
    )
    os.remove(video_path)
    return mp3_path


def _make_slides_video(info: dict) -> str:
    """Combine slides images + music into an MP4 video using ffmpeg."""
    images = info.get("images", [])
    music_url = info.get("music_url", "")
    if not images:
        raise ValueError("No images to convert")

    headers = {"User-Agent": MOBILE_UA, "Referer": "https://www.iesdouyin.com/"}
    img_count = len(images)

    # Download music and get duration
    music_path = None
    image_duration = 3.0
    if music_url:
        r = httpx.get(music_url, headers=headers, follow_redirects=True, timeout=60)
        music_path = str(DOWNLOADS_DIR / f"_slide_a_{uuid.uuid4().hex[:8]}.mp3")
        with open(music_path, "wb") as f:
            f.write(r.content)
        image_duration = 3.0

    # Step 1: Convert each image to a short video clip
    vid_paths = []
    for i, img_url in enumerate(images):
        r = httpx.get(img_url, headers=headers, follow_redirects=True, timeout=60)
        img_tmp = str(DOWNLOADS_DIR / f"_slide_img_{uuid.uuid4().hex[:4]}.webp")
        with open(img_tmp, "wb") as f:
            f.write(r.content)
        vid_path = str(DOWNLOADS_DIR / f"_slide_v_{i}_{uuid.uuid4().hex[:4]}.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-loop", "1", "-i", img_tmp,
             "-c:v", "libx264", "-t", f"{image_duration:.2f}",
             "-pix_fmt", "yuv420p", "-preset", "ultrafast", "-crf", "23",
             "-vf", "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2",
             vid_path],
            capture_output=True, check=True,
        )
        os.remove(img_tmp)
        vid_paths.append(vid_path)

    # Step 2: Concat all clips + add audio
    concat_file = str(DOWNLOADS_DIR / f"_concat_{uuid.uuid4().hex[:4]}.txt")
    with open(concat_file, "w") as f:
        for v in vid_paths:
            f.write(f"file '{v}'\n")

    safe_name = re.sub(r'[\n\r\t\\/*?:"<>|#]', '', info["title"])[:50] or uuid.uuid4().hex[:12]
    out_path = str(DOWNLOADS_DIR / f"{safe_name}.mp4")
    cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file]
    if music_path:
        cmd += ["-i", music_path, "-c:v", "copy", "-c:a", "aac", "-shortest", "-map", "0:v", "-map", "1:a"]
    else:
        cmd += ["-c:v", "copy"]
    cmd.append(out_path)

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr[:500]}")

    # Cleanup temp files
    for p in vid_paths:
        try: os.remove(p)
        except OSError: pass
    if music_path:
        try: os.remove(music_path)
        except OSError: pass
    try: os.remove(concat_file)
    except OSError: pass

    _schedule_cleanup(out_path)
    return out_path


def download_video(
    url: str, quality: str = "1080p", media_type: str = "video", image_index: int = 0
) -> tuple[str, str]:
    """Download video/image or extract audio. Returns (file_path, filename)."""
    url = _extract_url(url)
    _ensure_downloads_dir()

    if _is_douyin(url):
        info = extract_video_info(url)  # uses cache
        if info["type"] == "photo":
            if media_type == "video" and info.get("music_url"):
                # Slides with music → make slideshow video
                out_path = _make_slides_video(info)
                filename = os.path.basename(out_path)
                return out_path, filename
            elif media_type == "mp3" and info.get("music_url"):
                # Download music directly
                r = httpx.get(info["music_url"], headers={"User-Agent": MOBILE_UA}, follow_redirects=True, timeout=60)
                safe_title = re.sub(r'[\n\r\t\\/*?:"<>|#]', '', info["title"])[:50]
                filename = f"{safe_title}.mp3" if safe_title else f"{uuid.uuid4().hex[:12]}.mp3"
                filepath = str(DOWNLOADS_DIR / filename)
                with open(filepath, "wb") as f:
                    f.write(r.content)
                _schedule_cleanup(filepath)
                return filepath, filename
            else:
                # image type → download single photo
                filepath, filename = _download_single_photo(info, image_index)
        elif info["type"] == "live_photo":
            # Live photos / animated images
            video_urls = info.get("video_urls", [])
            if len(video_urls) == 1:
                # Single animated image - download directly
                filepath, filename = _download_douyin_video(info, quality)
            elif len(video_urls) > 1:
                # Multiple animated images - merge into one video
                filepath, filename = _download_live_photos(info)
            else:
                raise ValueError("未找到动图视频")
        else:
            filepath, filename = _download_douyin_video(info, quality)
    elif _is_twitter(url):
        filepath, filename = _download_twitter_video(url)
    elif _is_kuaishou(url):
        filepath, filename = _download_kuaishou_video(url)
    elif _is_bilibili(url):
        filepath, filename = _download_bilibili_video(url)
    elif _is_tiktok(url):
        filepath, filename = _download_tiktok(url)
    else:
        raise ValueError("不支持的平台链接")

    if media_type == "mp3" and not filename.endswith(".mp3") and not filename.endswith(".zip"):
        mp3_path = _convert_to_mp3(filepath)
        mp3_name = str(Path(filename).with_suffix(".mp3"))
        _schedule_cleanup(mp3_path)
        return mp3_path, mp3_name

    return filepath, filename


def _download_douyin_video(info: dict, quality: str = "1080p") -> tuple[str, str]:
    """Download Douyin video via streaming HTTP (safe for large files)."""
    video_url = info["video_url"]
    if not video_url:
        raise ValueError("未能提取视频下载地址")

    video_url = apply_quality(video_url, quality)

    safe_title = re.sub(r'[\n\r\t\\/*?:"<>|#]', '', info["title"])[:50]
    filename = f"{safe_title}.mp4" if safe_title else f"{uuid.uuid4().hex[:12]}.mp4"
    filepath = str(DOWNLOADS_DIR / filename)

    headers = {"User-Agent": MOBILE_UA, "Referer": "https://www.iesdouyin.com/"}

    # Streaming download - write to disk in chunks (safe for large files)
    with httpx.stream("GET", video_url, headers=headers, follow_redirects=True,
                       timeout=httpx.Timeout(connect=10, read=300, write=10, pool=10)) as r:
        # If play URL returns empty, try direct CDN URI
        if int(r.headers.get("content-length", 0)) == 0 and "aweme.snssdk.com" in video_url:
            vid_match = re.search(r'video_id=([^&]+)', video_url)
            if vid_match:
                direct_url = vid_match.group(1)
                with httpx.stream("GET", direct_url, headers={"User-Agent": MOBILE_UA},
                                   follow_redirects=True, timeout=httpx.Timeout(connect=10, read=300, write=10, pool=10)) as r2:
                    with open(filepath, "wb") as f:
                        for chunk in r2.iter_bytes(65536):
                            f.write(chunk)
                _schedule_cleanup(filepath)
                return filepath, filename

        with open(filepath, "wb") as f:
            for chunk in r.iter_bytes(65536):
                f.write(chunk)

    _schedule_cleanup(filepath)
    return filepath, filename


def _download_live_photos(info: dict) -> tuple[str, str]:
    """Download and merge multiple live photos (animated images) into one video."""
    video_urls = info.get("video_urls", [])
    if not video_urls:
        raise ValueError("未找到动图视频")

    safe_title = re.sub(r'[\n\r\t\\/*?:"<>|#]', '', info["title"])[:50] or uuid.uuid4().hex[:12]
    headers = {"User-Agent": MOBILE_UA, "Referer": "https://www.douyin.com/"}

    # Download each video clip, filter out invalid ones
    vid_paths = []
    for i, url in enumerate(video_urls):
        r = httpx.get(url, headers=headers, follow_redirects=True, timeout=120)
        # Skip invalid responses (HTML error pages, empty content)
        if len(r.content) < 1000 or r.content[:1] == b'<':
            continue
        vid_path = str(DOWNLOADS_DIR / f"_live_{i}_{uuid.uuid4().hex[:4]}.mp4")
        with open(vid_path, "wb") as f:
            f.write(r.content)
        vid_paths.append(vid_path)

    if len(vid_paths) == 1:
        # Only one video, rename and return
        out_path = str(DOWNLOADS_DIR / f"{safe_title}.mp4")
        os.rename(vid_paths[0], out_path)
        _schedule_cleanup(out_path)
        return out_path, f"{safe_title}.mp4"

    # Merge multiple videos using ffmpeg concat (use absolute paths)
    concat_file = str(DOWNLOADS_DIR / f"_concat_live_{uuid.uuid4().hex[:4]}.txt")
    with open(concat_file, "w") as f:
        for v in vid_paths:
            f.write(f"file '{os.path.abspath(v)}'\n")

    out_path = str(DOWNLOADS_DIR / f"{safe_title}.mp4")
    # Re-encode to ensure compatible format (clips may differ in codec/resolution/fps)
    cmd = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_file,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart",
        out_path,
    ]
    result = subprocess.run(cmd, capture_output=True)

    # Cleanup temp files
    for v in vid_paths:
        try:
            os.remove(v)
        except OSError:
            pass
    try:
        os.remove(concat_file)
    except OSError:
        pass

    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed: {result.stderr[:500]}")

    _schedule_cleanup(out_path)
    return out_path, f"{safe_title}.mp4"


def _download_twitter_video(url: str) -> tuple[str, str]:
    """Download Twitter/X video via fxtwitter API. Tries direct URL first, falls back to m3u8/ffmpeg."""
    info = extract_video_info(url)
    m3u8_url = info.get("m3u8_url", "")
    video_url = info.get("video_url", "")
    if not video_url and not m3u8_url:
        raise ValueError("未能提取视频下载地址")

    safe_title = re.sub(r'[\n\r\t\\/*?:"<>|#]', '', info["title"])[:50]
    filename = f"{safe_title}.mp4" if safe_title else f"{uuid.uuid4().hex[:12]}.mp4"
    filepath = str(DOWNLOADS_DIR / filename)

    # Try direct download first (works locally, may fail on cloud servers)
    downloaded = False
    if video_url:
        try:
            headers = {"User-Agent": MOBILE_UA, "Referer": "https://x.com/"}
            _curl_download(video_url, filepath, timeout=30, headers=headers)
            if os.path.getsize(filepath) > 1000:
                downloaded = True
        except Exception:
            pass

    # Fall back to m3u8/ffmpeg (for cloud servers where video.twimg.com is blocked)
    if not downloaded and m3u8_url and ".m3u8" in m3u8_url:
        import subprocess
        # Rewrite master m3u8 to specific stream URL (pick highest bitrate)
        if "/pl/" in m3u8_url and "/pl/avc1/" not in m3u8_url:
            m3u8_text = _curl_get(m3u8_url, timeout=15)
            # Find all video streams, pick the last one (highest quality)
            stream_match = None
            for line in m3u8_text.splitlines():
                if "/pl/avc1/" in line:
                    stream_match = line
            if stream_match:
                if stream_match.startswith("/"):
                    m3u8_url = "https://video.twimg.com" + stream_match
                else:
                    base_url = m3u8_url.rsplit("/", 1)[0]
                    m3u8_url = base_url + "/" + stream_match

        proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
        cmd = ["ffmpeg", "-y"]
        if proxy:
            cmd += ["-http_proxy", proxy]
        cmd += ["-i", m3u8_url, "-c", "copy", "-bsf:a", "aac_adtstoasc", filepath]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise ValueError("ffmpeg failed: " + result.stderr.decode("utf-8", errors="replace")[-200:])
        downloaded = True

    if not downloaded:
        raise ValueError("无法下载视频")

    _schedule_cleanup(filepath)
    return filepath, filename


def _download_single_photo(info: dict, index: int = 0) -> tuple[str, str]:
    """Download a single image from a Douyin photo note."""
    images = info.get("images", [])
    if not images:
        raise ValueError("未能提取图片地址")

    idx = max(0, min(index, len(images) - 1))
    img_url = images[idx]

    headers = {"User-Agent": MOBILE_UA, "Referer": "https://www.iesdouyin.com/"}
    r = httpx.get(img_url, headers=headers, follow_redirects=True, timeout=60)

    # Determine extension
    ct = r.headers.get("content-type", "")
    if "jpeg" in ct or "jpg" in ct:
        ext = ".jpg"
    elif "png" in ct:
        ext = ".png"
    elif "gif" in ct:
        ext = ".gif"
    elif "webp" in ct:
        ext = ".webp"
    else:
        # Fallback: guess from URL
        if ".gif" in img_url:
            ext = ".gif"
        elif ".png" in img_url:
            ext = ".png"
        elif ".jpg" in img_url or ".jpeg" in img_url:
            ext = ".jpg"
        else:
            ext = ".webp"

    safe_title = re.sub(r'[\n\r\t\\/*?:"<>|#]', '', info["title"])[:50]
    filename = f"{safe_title}_{idx+1}{ext}" if safe_title else f"{uuid.uuid4().hex[:12]}{ext}"
    filepath = str(DOWNLOADS_DIR / filename)

    with open(filepath, "wb") as f:
        f.write(r.content)

    _schedule_cleanup(filepath)
    return filepath, filename


def _download_tiktok(url: str) -> tuple[str, str]:
    """Download TikTok video via tikwm API + direct HTTP."""
    info = extract_video_info(url)
    video_url = info.get("hd_url") or info.get("video_url", "")
    if not video_url:
        raise ValueError("未能提取视频下载地址")

    r = httpx.get(video_url, headers={"User-Agent": MOBILE_UA}, follow_redirects=True, timeout=120)

    safe_title = re.sub(r'[\n\r\t\\/*?:"<>|#]', '', info["title"])[:50]
    filename = f"{safe_title}.mp4" if safe_title else f"{uuid.uuid4().hex[:12]}.mp4"
    filepath = str(DOWNLOADS_DIR / filename)

    with open(filepath, "wb") as f:
        f.write(r.content)

    _schedule_cleanup(filepath)
    return filepath, filename


def _download_kuaishou_video(url: str) -> tuple[str, str]:
    """Download Kuaishou video via direct HTTP."""
    info = extract_video_info(url)
    video_url = info.get("hd_url") or info.get("video_url", "")
    if not video_url:
        raise ValueError("未能提取视频下载地址")

    r = httpx.get(video_url, headers={"User-Agent": MOBILE_UA}, follow_redirects=True, timeout=120, verify=False)

    safe_title = re.sub(r'[\n\r\t\\/*?:"<>|#]', '', info["title"])[:50]
    filename = f"{safe_title}.mp4" if safe_title else f"{uuid.uuid4().hex[:12]}.mp4"
    filepath = str(DOWNLOADS_DIR / filename)

    with open(filepath, "wb") as f:
        f.write(r.content)

    _schedule_cleanup(filepath)
    return filepath, filename


def _download_bilibili_video(url: str) -> tuple[str, str]:
    """Download Bilibili video via API + direct HTTP."""
    info = extract_video_info(url)
    video_url = info.get("video_url", "")
    if not video_url:
        raise ValueError("未能提取视频下载地址")

    headers = {"User-Agent": MOBILE_UA, "Referer": "https://www.bilibili.com/"}
    r = httpx.get(video_url, headers=headers, follow_redirects=True, timeout=120)

    safe_title = re.sub(r'[\n\r\t\\/*?:"<>|#]', '', info["title"])[:50]
    filename = f"{safe_title}.mp4" if safe_title else f"{uuid.uuid4().hex[:12]}.mp4"
    filepath = str(DOWNLOADS_DIR / filename)

    with open(filepath, "wb") as f:
        f.write(r.content)

    _schedule_cleanup(filepath)
    return filepath, filename


def download_video_for_stream(video_url: str, m3u8_url: str = "") -> tuple[str, str]:
    """Download a video from its CDN URL for streaming. Returns (filepath, filename)."""
    _ensure_downloads_dir()

    safe_name = f"{uuid.uuid4().hex[:12]}.mp4"
    filepath = str(DOWNLOADS_DIR / safe_name)

    # Try direct download first (works locally for Twitter, always for other platforms)
    downloaded = False
    if video_url and "video.twimg.com" in video_url:
        try:
            headers = {"User-Agent": MOBILE_UA, "Referer": "https://x.com/"}
            _curl_download(video_url, filepath, timeout=30, headers=headers)
            if os.path.getsize(filepath) > 1000:
                downloaded = True
        except Exception:
            pass

    # Fall back to m3u8/ffmpeg for Twitter (cloud servers where video.twimg.com is blocked)
    if not downloaded and m3u8_url and ".m3u8" in m3u8_url:
        import subprocess
        # Rewrite master m3u8 to specific stream URL (pick highest bitrate)
        if "/pl/" in m3u8_url and "/pl/avc1/" not in m3u8_url:
            m3u8_text = _curl_get(m3u8_url, timeout=15)
            stream_match = None
            for line in m3u8_text.splitlines():
                if "/pl/avc1/" in line:
                    stream_match = line  # last match = highest quality
            if stream_match:
                if stream_match.startswith("/"):
                    m3u8_url = "https://video.twimg.com" + stream_match
                else:
                    base_url = m3u8_url.rsplit("/", 1)[0]
                    m3u8_url = base_url + "/" + stream_match

        proxy = os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
        cmd = ["ffmpeg", "-y"]
        if proxy:
            cmd += ["-http_proxy", proxy]
        cmd += ["-i", m3u8_url, "-c", "copy", "-bsf:a", "aac_adtstoasc", filepath]
        result = subprocess.run(cmd, capture_output=True, timeout=120)
        if result.returncode != 0:
            raise ValueError("ffmpeg failed: " + result.stderr.decode("utf-8", errors="replace")[-200:])
        downloaded = True

    # Non-Twitter platforms or fallback
    if not downloaded:
        headers = {"User-Agent": MOBILE_UA}
        if "douyin" in video_url or "snssdk" in video_url:
            headers["Referer"] = "https://www.iesdouyin.com/"
        elif "video.twimg.com" in video_url:
            headers["Referer"] = "https://x.com/"
        elif "tiktokcdn" in video_url:
            headers["Referer"] = "https://www.tiktok.com/"
        elif "bilibili" in video_url or "bilivideo" in video_url or "hdslb" in video_url:
            headers["Referer"] = "https://www.bilibili.com/"

        needs_curl = "tiktokcdn" in video_url or "tiktok" in video_url or "ssstiktok" in video_url
        if needs_curl:
            _curl_download(video_url, filepath, timeout=120, headers=headers)
        else:
            verify = "kwaicdn" not in video_url and "kuaishou" not in video_url
            r = httpx.get(video_url, headers=headers, follow_redirects=True, timeout=120, verify=verify)
            with open(filepath, "wb") as f:
                f.write(r.content)

    _schedule_cleanup(filepath)
    return filepath, safe_name


def _schedule_cleanup(filepath: str):
    """Delete file after 10 minutes."""
    def _cleanup():
        import time
        time.sleep(600)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except OSError:
            pass

    t = threading.Thread(target=_cleanup, daemon=True)
    t.start()


def cleanup_old_files(max_age_seconds: int = 1800):
    """Delete download files older than max_age_seconds (default 30 min). Called at startup."""
    _ensure_downloads_dir()
    now = time.time()
    deleted = 0
    for f in DOWNLOADS_DIR.iterdir():
        if f.is_file():
            try:
                if now - f.stat().st_mtime > max_age_seconds:
                    f.unlink()
                    deleted += 1
            except OSError:
                pass
    if deleted:
        print(f"[cleanup] Removed {deleted} old file(s) from downloads/")
