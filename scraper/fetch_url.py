from __future__ import annotations

import json
import re
import socket
from dataclasses import asdict, dataclass
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlparse

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
    "body",
    "description",
    "abstract",
    "summary",
    "title",
}


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
    errors: list[str]

    @property
    def ok(self) -> bool:
        return self.method != "failed" and _quality(self.markdown)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["ok"] = self.ok
        return data


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


def _metadata(page: Any) -> tuple[str, str, str]:
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
    return title, author, published


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


def _extract_xhr_text(page: Any) -> str:
    candidates: list[str] = []

    def walk(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, str(k).lower())
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif isinstance(value, str):
            s = _clean(value)
            if len(s) >= 40 and (key in CONTENT_KEYS or len(s) >= 300):
                candidates.append(s)

    for xhr in getattr(page, "captured_xhr", []) or []:
        try:
            content_type = _clean(getattr(xhr, "headers", {}).get("content-type", "")).lower()
            if "json" not in content_type:
                continue
            body = getattr(xhr, "body", b"")
            if isinstance(body, bytes):
                body = body.decode(getattr(xhr, "encoding", None) or "utf-8", errors="ignore")
            walk(json.loads(body))
        except Exception:
            continue

    seen: set[str] = set()
    unique: list[str] = []
    for text in sorted(candidates, key=len, reverse=True):
        key = text[:240]
        if key in seen:
            continue
        seen.add(key)
        unique.append(text)
        if len(unique) >= 12:
            break
    return "\n\n".join(unique)


def _result_from_page(url: str, page: Any, method: str, errors: list[str]) -> FetchResult:
    title, author, published = _metadata(page)
    try:
        markdown = page.markdown(main_content_only=True) or ""
    except Exception as exc:
        errors.append(f"Markdown conversion failed: {type(exc).__name__}: {exc}")
        markdown = ""

    # Always inspect captured XHR/fetch payloads when available. Dynamic sites often
    # render only a teaser in the DOM while the full article lives in JSON.
    xhr_text = _extract_xhr_text(page)
    md_clean = _clean(markdown)
    xhr_clean = _clean(xhr_text)

    if xhr_clean:
        prefer_xhr = (
            (_looks_truncated(md_clean) and not _looks_truncated(xhr_clean))
            or len(xhr_clean) > max(len(md_clean) + 120, int(len(md_clean) * 1.35))
        )
        if prefer_xhr and _quality(xhr_text):
            markdown = xhr_text
            method = f"{method}+xhr"

    final_url = _clean(getattr(page, "url", "")) or url

    return FetchResult(
        url=url,
        final_url=final_url,
        title=title,
        author=author,
        published_at=published,
        method=method,
        status_code=getattr(page, "status", None),
        markdown=markdown,
        errors=errors.copy(),
    )


def fetch_url(url: str) -> FetchResult:
    url = validate_public_url(url)
    errors: list[str] = []

    try:
        page = Fetcher.get(url)
        result = _result_from_page(url, page, "scrapling-http", errors)
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
        result = _result_from_page(url, page, "scrapling-dynamic", errors)
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
        result = _result_from_page(url, page, "scrapling-stealthy", errors)
        if result.ok:
            return result
        errors.append("Stealth browser returned insufficient or blocked content")
    except Exception as exc:
        errors.append(f"Stealth browser failed: {type(exc).__name__}: {exc}")

    return FetchResult(
        url=url,
        final_url=url,
        title="",
        author="",
        published_at="",
        method="failed",
        status_code=None,
        markdown="",
        errors=errors,
    )
