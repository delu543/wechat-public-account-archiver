"""Public-only, bounded navigation for PC search and article bodies.

Reuse the project's HTTP/status/pacing contract. No client credentials, login,
unrestricted redirects, alternate route, retries, or JavaScript execution.
"""
from __future__ import annotations

import html
import re
from urllib.parse import parse_qsl, urlparse

import httpx

from .biz_push_reader import public_identity, permanent_url
from .crawler.fetcher import ArticleFetcher, FetchConfig
from .crawler.models import FetchResult


def public_navigation_url(value):
    parsed = urlparse(str(value))
    if len(str(value)) > 8192 or parsed.scheme not in ("https", "http") or parsed.username or parsed.password:
        return None
    if parsed.netloc == "mp.weixin.qq.com":
        if identity := public_identity(value):
            return permanent_url(identity)  # never replay cached client key/uin
        if parsed.path.startswith("/s/") and re.fullmatch(r"/s/[A-Za-z0-9_-]{22}", parsed.path) and not parsed.query:
            return parsed._replace(scheme="https", fragment="").geturl()
    elif parsed.netloc == "weixin.sogou.com" and parsed.scheme == "https":
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        if len(pairs) != len({k for k, _ in pairs}):
            return None
        params = dict(pairs)
        if parsed.path == "/weixin" and set(params) == {"type", "query"} and params["type"] == "2" and params["query"]:
            return parsed._replace(fragment="").geturl()
        if parsed.path == "/link" and params.get("url"):
            return parsed._replace(fragment="").geturl()
    return None


def challenge_url(value):
    parsed = urlparse(str(value))
    return (parsed.hostname in ("mp.weixin.qq.com", "weixin.sogou.com", "sec.sogou.com")
            and any(word in parsed.path.lower() for word in ("captcha", "antispider", "/verify")))


def literal_public_redirect(text):
    """A normal literal public link only; no concatenation/deobfuscation/eval."""
    found = []
    for match in re.finditer(r"(?:var\s+url|(?:window\.)?location(?:\.href)?)\s*=\s*(['\"])(.*?)\1", text):
        tail = text[match.end():].lstrip()
        if tail and not tail.startswith(";"):
            continue
        value = html.unescape(match[2].replace("\\/", "/"))
        if urlparse(value).netloc == "mp.weixin.qq.com" and public_navigation_url(value):
            found.append(public_navigation_url(value))
    return found[0] if len(set(found)) == 1 else None


class PassivePublicFetcher(ArticleFetcher):
    def __init__(self, config=None, *, transport=None):
        super().__init__(config or FetchConfig(min_delay_seconds=3, max_delay_seconds=6, max_retries=0))
        self.transport = transport

    def fetch(self, url):
        original, current, visited = url, public_navigation_url(url), set()
        if not current:
            return FetchResult(original, original, None, "", "failed", "non_public_navigation_rejected")
        headers = {"User-Agent": self.config.user_agent, "Accept": "text/html,application/xhtml+xml",
                   "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
        try:
            with httpx.Client(follow_redirects=False, timeout=self.config.timeout_seconds, transport=self.transport) as client:
                for _ in range(6):
                    if current in visited:
                        break
                    visited.add(current)
                    self._sleep()
                    with client.stream("GET", current, headers=headers) as response:
                        if 300 <= response.status_code < 400:
                            target = str(response.url.join(response.headers.get("location", "")))
                            if challenge_url(target):
                                return FetchResult(original, target, response.status_code, "", "restricted", "captcha")
                            current = public_navigation_url(target)
                            if not current:
                                return FetchResult(original, str(response.url), response.status_code, "", "failed", "public_redirect_rejected")
                            continue
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 8 * 1024 * 1024:
                                return FetchResult(original, current, response.status_code, "", "failed", "public_page_size_limit")
                            chunks.append(chunk)
                        text = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                        status = self._status_from_response(response.status_code, text, current)
                        if status.startswith("restricted:"):
                            return FetchResult(original, current, response.status_code, "", "restricted", status.removeprefix("restricted:"))
                        if status != "ok":
                            return FetchResult(original, current, response.status_code, "", "failed", "public_http_failed")
                        if urlparse(current).netloc == "weixin.sogou.com" and urlparse(current).path == "/link":
                            target = literal_public_redirect(text)
                            if target:
                                current = target
                                continue
                            return FetchResult(original, current, response.status_code, "", "failed", "public_redirect_unsupported")
                        return FetchResult(original, current, response.status_code, text, "ok")
        except (httpx.HTTPError, UnicodeError, LookupError):
            return FetchResult(original, current or original, None, "", "failed", "public_http_unavailable")
        return FetchResult(original, current or original, None, "", "failed", "public_redirect_limit")
