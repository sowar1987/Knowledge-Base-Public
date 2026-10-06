from __future__ import annotations

import html
import json
import re
import socket
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

import httpx
import trafilatura
import yt_dlp
from scrapling.fetchers import DynamicFetcher, Fetcher, StealthyFetcher


BLOCK_MARKERS = (
    "access denied",
    "forbidden",
    "just a moment",
    "enable javascript",
    "verify you are human",
    "captcha",
    "robot check",
    "安全验证",
    "访问受限",
    "请完成验证",
)

CONTENT_KEYS = {
    "content",
    "text",
    "article_content",
    "rich_content",
    "articlebody",
    "body",
    "description",
    "abstract",
    "summary",
    "title",
    "name",
}

TOUTIAO_MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)
TOUTIAO_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass
class FetchResult:
    url: str
    final_url: str
    title: str
    author: str
    published_at: str
    method: str
    status_code: int | None
    markdown: str
    media_url: str
    cover_url: str
    images: list[str]
    content_type: str
    verified_complete: bool
    errors: list[str]

    @property
    def ok(self) -> bool:
        return (
            self.method != "failed"
            and (self.verified_complete or _quality(self.markdown))
        )

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        return data


def _empty_result(url: str, errors: list[str], final_url: str | None = None) -> FetchResult:
    return FetchResult(
        url=url,
        final_url=final_url or url,
        title="",
        author="",
        published_at="",
        method="failed",
        status_code=None,
        markdown="",
        media_url="",
        cover_url="",
        images=[],
        content_type="unknown",
        verified_complete=False,
        errors=errors.copy(),
    )


def validate_public_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Only http/https URLs are allowed")
    if not parsed.hostname:
        raise ValueError("URL must contain a hostname")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are not allowed")

    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"DNS resolution failed: {exc}") from exc

    if not infos:
        raise ValueError("DNS resolution returned no addresses")

    for info in infos:
        addr = ip_address(info[4][0])
        if not addr.is_global:
            raise ValueError(f"Non-public target address is not allowed: {addr}")
    return url


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _looks_truncated(text: str) -> bool:
    text = _clean(text)
    if not text:
        return True
    tail = text[-80:]
    return (
        text.endswith("...")
        or text.endswith("…")
        or "..." in tail[-12:]
        or "阅读全文" in tail
        or "展开全文" in tail
    )


def _quality(markdown: str) -> bool:
    text = _clean(markdown)
    if len(text) < 160:
        return False
    lower = text.lower()
    if any(marker in lower for marker in BLOCK_MARKERS) and len(text) < 1200:
        return False
    if _looks_truncated(text):
        return False
    return True


def _small_complete_text(text: str) -> bool:
    cleaned = _clean(text)
    if len(cleaned) < 12:
        return False
    if any(marker in cleaned.lower() for marker in BLOCK_MARKERS):
        return False
    return not _looks_truncated(cleaned)


def _html_to_text(fragment: str) -> str:
    if not fragment:
        return ""
    text = html.unescape(fragment)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
    text = re.sub(
        r"</(?:p|div|h[1-6]|li|blockquote|section|article|pre)>",
        "\n",
        text,
        flags=re.I,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    lines = [
        re.sub(r"[ \t\u3000]+", " ", line).strip()
        for line in text.splitlines()
    ]
    return "\n\n".join(line for line in lines if line)


def _normalize_urls(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = _clean(raw)
        if value.startswith("//"):
            value = "https:" + value
        if not value.startswith(("http://", "https://")):
            continue
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


def _parse_render_data(raw_html: str) -> dict[str, Any]:
    match = re.search(
        r'<script[^>]+id=["\']RENDER_DATA["\'][^>]*>(.*?)</script>',
        raw_html,
        re.S | re.I,
    )
    if not match:
        return {}
    encoded = html.unescape(match.group(1))
    try:
        return json.loads(urllib.parse.unquote(encoded))
    except Exception:
        return {}


def _walk_dicts(value: Any):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_dicts(child)


def _is_toutiao_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "toutiao.com" or host.endswith(".toutiao.com")


def _toutiao_identity_from_url(url: str) -> tuple[str, str] | None:
    match = re.search(r"/(article|w|video)/(\d+)", url)
    if not match:
        return None
    return match.group(1), match.group(2)


def _toutiao_identity_from_html(raw_html: str) -> tuple[str, str] | None:
    normalized = html.unescape(raw_html).replace("\\u002F", "/")
    match = re.search(
        r'https?://(?:www|m)\.toutiao\.com/(article|w|video)/(\d+)',
        normalized,
        re.I,
    )
    if match:
        return match.group(1).lower(), match.group(2)

    payload = _parse_render_data(raw_html)
    if payload:
        thread_base = (
            payload.get("articleInfo", {})
            .get("thread", {})
            .get("threadBase")
        )
        if isinstance(thread_base, dict):
            for obj in _walk_dicts(payload):
                for key in ("itemId", "groupId", "threadId"):
                    value = obj.get(key)
                    if value and str(value).isdigit():
                        return "w", str(value)

        initial_video = (payload.get("data") or {}).get("initialVideo")
        if isinstance(initial_video, dict):
            detail_url = _clean(initial_video.get("detailUrl"))
            identity = _toutiao_identity_from_url(detail_url)
            if identity:
                return identity
            for obj in _walk_dicts(payload):
                value = obj.get("itemId")
                if value and str(value).isdigit():
                    return "video", str(value)

    return None


def _resolve_toutiao_url(url: str) -> tuple[str, str, int | None, list[str]]:
    errors: list[str] = []
    headers = {
        "User-Agent": TOUTIAO_MOBILE_UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://m.toutiao.com/",
    }
    raw_html = ""
    status: int | None = None
    final_url = url
    try:
        with httpx.Client(
            follow_redirects=True,
            timeout=15,
            headers=headers,
        ) as client:
            response = client.get(url)
            status = response.status_code
            final_url = str(response.url)
            raw_html = response.text
    except Exception as exc:
        errors.append(f"Toutiao short-link resolve failed: {type(exc).__name__}: {exc}")
        return final_url, raw_html, status, errors

    if not _is_toutiao_host(final_url):
        errors.append(f"Toutiao short-link redirected outside toutiao.com: {final_url}")
        return url, raw_html, status, errors

    identity = _toutiao_identity_from_url(final_url) or _toutiao_identity_from_html(raw_html)
    if identity:
        kind, item_id = identity
        canonical_host = "www.toutiao.com" if kind in {"article", "video"} else "m.toutiao.com"
        final_url = f"https://{canonical_host}/{kind}/{item_id}/"
    else:
        errors.append("Resolved Toutiao page but could not identify article/w/video item")

    return final_url, raw_html, status, errors


def _fetch_toutiao_thread(original_url: str, item_id: str, errors: list[str]) -> FetchResult:
    detail_url = f"https://m.toutiao.com/w/{item_id}/"
    headers = {
        "User-Agent": TOUTIAO_MOBILE_UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Referer": "https://m.toutiao.com/",
    }

    try:
        with httpx.Client(follow_redirects=True, timeout=20, headers=headers) as client:
            response = client.get(detail_url)
        raw_html = response.text
        payload = _parse_render_data(raw_html)
        base = (
            payload.get("articleInfo", {})
            .get("thread", {})
            .get("threadBase")
        )
        if not isinstance(base, dict):
            raise ValueError("threadBase not found in RENDER_DATA")

        user = (base.get("user") or {}).get("info") or {}
        content_html = _clean(base.get("richContent") or base.get("content") or "")
        content = _html_to_text(content_html)
        if not content:
            content = _clean(base.get("content"))

        images_nodes = base.get("largeImageList") or base.get("originImageList") or []
        images = []
        for node in images_nodes:
            if isinstance(node, dict) and node.get("url"):
                images.append(str(node["url"]))
        images = _normalize_urls(images)

        created = base.get("createTime")
        published = ""
        try:
            if created:
                published = datetime.fromtimestamp(int(created)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

        title = _clean(base.get("title"))
        if not title:
            title = _clean(content)[:120]

        return FetchResult(
            url=original_url,
            final_url=detail_url,
            title=title,
            author=_clean(user.get("name")),
            published_at=published,
            method="toutiao-thread-render-data",
            status_code=response.status_code,
            markdown=content,
            media_url="",
            cover_url=images[0] if images else "",
            images=images,
            content_type="toutiao-thread",
            verified_complete=_small_complete_text(content),
            errors=errors.copy(),
        )
    except Exception as exc:
        errors.append(f"Toutiao thread fetch failed: {type(exc).__name__}: {exc}")
        return _empty_result(original_url, errors, detail_url)


def _page_html(page: Any) -> str:
    body = getattr(page, "body", b"")
    if isinstance(body, bytes):
        encoding = getattr(page, "encoding", None) or "utf-8"
        return body.decode(encoding, errors="ignore")
    return str(body or "")


def _first(page: Any, selectors: list[str]) -> str:
    for selector in selectors:
        try:
            value = page.css(selector).get()
            value = _clean(value)
            if value:
                return value
        except Exception:
            continue
    return ""


def _fetch_toutiao_article(original_url: str, item_id: str, errors: list[str]) -> FetchResult:
    canonical = f"https://www.toutiao.com/article/{item_id}/"
    attempts = [
        ("toutiao-article-browser", DynamicFetcher),
        ("toutiao-article-stealth", StealthyFetcher),
    ]

    for method, fetcher in attempts:
        try:
            kwargs: dict[str, Any] = {
                "headless": True,
                "timeout": 50000,
                "wait": 1200,
                "network_idle": True,
                "block_ads": True,
            }
            if fetcher is StealthyFetcher:
                kwargs.update({"block_webrtc": True, "hide_canvas": True})

            page = fetcher.fetch(canonical, **kwargs)
            title = _first(page, ["h1::text", 'meta[property="og:title"]::attr(content)', "title::text"])
            author = _first(page, [
                ".article-meta .name a::text",
                'meta[name="author"]::attr(content)',
                'meta[property="article:author"]::attr(content)',
            ])
            published = ""
            try:
                spans = page.css(".article-meta span::text").getall()
                if spans:
                    published = _clean(spans[0])
            except Exception:
                pass

            images: list[str] = []
            try:
                images = _normalize_urls(page.css("article img::attr(src)").getall())
            except Exception:
                pass

            article_md = ""
            try:
                article_md = page.markdown(css_selector="article") or ""
            except Exception:
                pass

            raw_html = _page_html(page)
            if len(_clean(article_md)) < 100 and raw_html:
                extracted = trafilatura.extract(
                    raw_html,
                    output_format="markdown",
                    include_comments=False,
                    include_tables=True,
                    include_images=True,
                    favor_precision=True,
                )
                if extracted:
                    article_md = extracted

            prose_only = re.sub(r"!\\[[^\\]]*\\]\\([^)]*\\)", " ", article_md)
            prose_only = re.sub(r"https?://\\S+", " ", prose_only)
            prose_chars = len(_clean(prose_only))
            complete = prose_chars >= 100 and not _looks_truncated(article_md)
            if complete:
                return FetchResult(
                    url=original_url,
                    final_url=canonical,
                    title=title,
                    author=author,
                    published_at=published,
                    method=method,
                    status_code=getattr(page, "status", None),
                    markdown=article_md,
                    media_url="",
                    cover_url=images[0] if images else "",
                    images=images,
                    content_type="toutiao-article",
                    verified_complete=True,
                    errors=errors.copy(),
                )
            errors.append(f"{method} returned no complete article body")
        except Exception as exc:
            errors.append(f"{method} failed: {type(exc).__name__}: {exc}")

    return _empty_result(original_url, errors, canonical)


def _fetch_toutiao_video(original_url: str, item_id: str, errors: list[str]) -> FetchResult:
    canonical = f"https://www.toutiao.com/video/{item_id}/"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(canonical, download=False)
        if not info:
            raise ValueError("yt-dlp returned no info")

        formats = [
            f for f in (info.get("formats") or [])
            if f.get("url") and (f.get("vcodec") or "none") != "none"
        ]
        formats.sort(
            key=lambda f: (
                int(f.get("height") or 0),
                int(f.get("width") or 0),
                int(f.get("filesize") or f.get("filesize_approx") or 0),
                float(f.get("tbr") or 0),
            ),
            reverse=True,
        )
        media_url = _clean(formats[0].get("url")) if formats else ""
        title = _clean(info.get("title"))
        description = _clean(info.get("description"))
        markdown = description or title

        published = ""
        timestamp = info.get("release_timestamp") or info.get("timestamp")
        try:
            if timestamp:
                published = datetime.fromtimestamp(int(timestamp)).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            pass

        return FetchResult(
            url=original_url,
            final_url=canonical,
            title=title,
            author=_clean(info.get("uploader")),
            published_at=published,
            method="toutiao-video-yt-dlp",
            status_code=200,
            markdown=markdown,
            media_url=media_url,
            cover_url=_clean(info.get("thumbnail")),
            images=[],
            content_type="toutiao-video",
            verified_complete=bool(title and media_url),
            errors=errors.copy(),
        )
    except Exception as exc:
        errors.append(f"Toutiao video yt-dlp failed: {type(exc).__name__}: {exc}")
        return _empty_result(original_url, errors, canonical)


def _fetch_toutiao(url: str) -> FetchResult:
    errors: list[str] = []
    final_url, resolver_html, resolver_status, resolve_errors = _resolve_toutiao_url(url)
    errors.extend(resolve_errors)

    identity = _toutiao_identity_from_url(final_url)
    if not identity and resolver_html:
        identity = _toutiao_identity_from_html(resolver_html)

    if not identity:
        result = _empty_result(url, errors or ["Unable to resolve Toutiao item"], final_url)
        result.status_code = resolver_status
        return result

    kind, item_id = identity
    if kind == "w":
        return _fetch_toutiao_thread(url, item_id, errors)
    if kind == "article":
        return _fetch_toutiao_article(url, item_id, errors)
    if kind == "video":
        return _fetch_toutiao_video(url, item_id, errors)

    return _empty_result(url, errors + [f"Unsupported Toutiao item kind: {kind}"], final_url)


def _normalize_candidate(value: str) -> str:
    value = html.unescape(value)
    if "<" in value and ">" in value:
        value = re.sub(r"<[^>]+>", " ", value)
    return _clean(value)


def _collect_text_candidates(value: Any, candidates: list[str], key: str = "") -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            _collect_text_candidates(v, candidates, str(k).lower())
    elif isinstance(value, list):
        for item in value:
            _collect_text_candidates(item, candidates, key)
    elif isinstance(value, str):
        s = _normalize_candidate(value)
        if len(s) >= 40 and (key in CONTENT_KEYS or len(s) >= 500):
            candidates.append(s)


def _dedupe_candidates(candidates: list[str]) -> str:
    seen: set[str] = set()
    unique: list[str] = []
    for text in sorted(candidates, key=len, reverse=True):
        fingerprint = text[:240]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(text)
        if len(unique) >= 12:
            break
    return "\n\n".join(unique)


def _extract_embedded_json_text(page: Any) -> str:
    candidates: list[str] = []
    scripts: list[tuple[str, bool]] = []

    for selector, encoded in (
        ('script#RENDER_DATA::text', True),
        ('script[type="application/ld+json"]::text', False),
    ):
        try:
            values = page.css(selector).getall()
        except Exception:
            values = []
        for raw in values or []:
            scripts.append((str(raw), encoded))

    for raw, encoded in scripts:
        try:
            text = urllib.parse.unquote(raw) if encoded else raw
            data = json.loads(text)
            _collect_text_candidates(data, candidates)
        except Exception:
            continue

    return _dedupe_candidates(candidates)


def _extract_xhr_text(page: Any) -> str:
    candidates: list[str] = []
    for xhr in getattr(page, "captured_xhr", []) or []:
        try:
            content_type = _clean(getattr(xhr, "headers", {}).get("content-type", "")).lower()
            if "json" not in content_type:
                continue
            body = getattr(xhr, "body", b"")
            if isinstance(body, bytes):
                body = body.decode(getattr(xhr, "encoding", None) or "utf-8", errors="ignore")
            _collect_text_candidates(json.loads(body), candidates)
        except Exception:
            continue
    return _dedupe_candidates(candidates)


def _generic_result_from_page(url: str, page: Any, method: str, errors: list[str]) -> FetchResult:
    title = _first(page, [
        'meta[property="og:title"]::attr(content)',
        'meta[name="twitter:title"]::attr(content)',
        "title::text",
        "h1::text",
    ])
    author = _first(page, [
        'meta[name="author"]::attr(content)',
        'meta[property="article:author"]::attr(content)',
    ])
    published = _first(page, [
        'meta[property="article:published_time"]::attr(content)',
        'meta[name="publishdate"]::attr(content)',
        'time::attr(datetime)',
    ])
    try:
        markdown = page.markdown(main_content_only=True) or ""
    except Exception as exc:
        errors.append(f"Markdown conversion failed: {type(exc).__name__}: {exc}")
        markdown = ""

    for suffix, candidate in (
        ("embedded", _extract_embedded_json_text(page)),
        ("xhr", _extract_xhr_text(page)),
    ):
        if candidate and (
            (_looks_truncated(markdown) and not _looks_truncated(candidate))
            or len(_clean(candidate)) > max(len(_clean(markdown)) + 120, int(len(_clean(markdown)) * 1.35))
        ):
            if _quality(candidate):
                markdown = candidate
                method = f"{method}+{suffix}"

    return FetchResult(
        url=url,
        final_url=_clean(getattr(page, "url", "")) or url,
        title=title,
        author=author,
        published_at=published,
        method=method,
        status_code=getattr(page, "status", None),
        markdown=markdown,
        media_url="",
        cover_url="",
        images=[],
        content_type="web",
        verified_complete=False,
        errors=errors.copy(),
    )


def _fetch_generic(url: str) -> FetchResult:
    errors: list[str] = []

    try:
        page = Fetcher.get(url)
        result = _generic_result_from_page(url, page, "scrapling-http", errors)
        if result.ok:
            return result
        errors.append("HTTP fetch returned insufficient or blocked content")
    except Exception as exc:
        errors.append(f"HTTP fetch failed: {type(exc).__name__}: {exc}")

    try:
        page = DynamicFetcher.fetch(
            url,
            headless=True,
            timeout=45000,
            wait=1500,
            block_ads=True,
            capture_xhr=r".*",
        )
        result = _generic_result_from_page(url, page, "scrapling-dynamic", errors)
        if result.ok:
            return result
        errors.append("Dynamic browser returned insufficient or blocked content")
    except Exception as exc:
        errors.append(f"Dynamic browser failed: {type(exc).__name__}: {exc}")

    try:
        page = StealthyFetcher.fetch(
            url,
            headless=True,
            timeout=60000,
            wait=2000,
            block_ads=True,
            block_webrtc=True,
            hide_canvas=True,
            capture_xhr=r".*",
        )
        result = _generic_result_from_page(url, page, "scrapling-stealthy", errors)
        if result.ok:
            return result
        errors.append("Stealth browser returned insufficient or blocked content")
    except Exception as exc:
        errors.append(f"Stealth browser failed: {type(exc).__name__}: {exc}")

    return _empty_result(url, errors)


def fetch_url(url: str) -> FetchResult:
    url = validate_public_url(url)
    if _is_toutiao_host(url):
        return _fetch_toutiao(url)
    return _fetch_generic(url)
