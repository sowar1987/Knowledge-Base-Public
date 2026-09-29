from __future__ import annotations

import html
import json
import re
import socket
import urllib.parse
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
    "articlebody",
    "body",
    "description",
    "abstract",
    "summary",
    "title",
    "name",
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


def _result_from_page(url: str, page: Any, method: str, errors: list[str]) -> FetchResult:
    title, author, published = _metadata(page)
    try:
        markdown = page.markdown(main_content_only=True) or ""
    except Exception as exc:
        errors.append(f"Markdown conversion failed: {type(exc).__name__}: {exc}")
        markdown = ""

    # Always inspect structured data exposed by the public page. Dynamic sites often
    # render only a teaser in the visible DOM while full text exists in JSON-LD,
    # RENDER_DATA, or XHR/fetch payloads.
    structured = [
        ("embedded", _extract_embedded_json_text(page)),
        ("xhr", _extract_xhr_text(page)),
    ]
    current = markdown
    current_method = method

    for suffix, candidate in structured:
        cur_clean = _clean(current)
        cand_clean = _clean(candidate)
        if not cand_clean:
            continue
        prefer = (
            (_looks_truncated(cur_clean) and not _looks_truncated(cand_clean))
            or len(cand_clean) > max(len(cur_clean) + 120, int(len(cur_clean) * 1.35))
        )
        if prefer and _quality(candidate):
            current = candidate
            current_method = f"{method}+{suffix}"

    markdown = current
    method = current_method

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
