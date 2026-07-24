from __future__ import annotations

import html
import re
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

WECHAT_ARTICLE_HOSTS = {"mp.weixin.qq.com"}
WECHAT_STABLE_PARAMS = ("__biz", "mid", "idx", "sn", "chksm")
TRACKING_PREFIXES = ("utm_",)
TRACKING_PARAMS = {
    "from",
    "scene",
    "subscene",
    "clicktime",
    "enterid",
    "ascene",
    "devicetype",
    "version",
    "lang",
    "isappinstalled",
    "exportkey",
    "pass_ticket",
    "key",
    "uin",
    "token",
    "platform",
    "vid",
}


def looks_like_url(value: str) -> bool:
    value = value.strip()
    return bool(re.match(r"^https?://", value, re.I))


def normalize_url(url: str) -> str:
    url = html.unescape(url.strip())
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url

    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    query_items = parse_qsl(parsed.query, keep_blank_values=True)
    if netloc in WECHAT_ARTICLE_HOSTS:
        values = dict(query_items)
        filtered = [(k, values[k]) for k in WECHAT_STABLE_PARAMS if k in values]
        query = urlencode(filtered, doseq=True)
    else:
        filtered = [
            (k, v)
            for k, v in query_items
            if not k.startswith(TRACKING_PREFIXES) and k not in TRACKING_PARAMS
        ]
        query = urlencode(sorted(filtered), doseq=True)
    path = quote(parsed.path or "/", safe="/:%")
    if path == "/" and netloc in WECHAT_ARTICLE_HOSTS and parsed.path == "":
        path = ""
    return urlunparse((scheme, netloc, path, "", query, ""))


def extract_mp_params(url: str) -> dict[str, str]:
    parsed = urlparse(html.unescape(url.strip()))
    return dict(parse_qsl(parsed.query, keep_blank_values=True))


def stable_url_key(url: str) -> str:
    normalized = normalize_url(url)
    parsed = urlparse(normalized)
    if parsed.netloc in WECHAT_ARTICLE_HOSTS:
        params = dict(parse_qsl(parsed.query, keep_blank_values=True))
        biz = params.get("__biz")
        mid = params.get("mid")
        idx = params.get("idx")
        sn = params.get("sn")
        if biz and mid and idx and sn:
            return f"wechat:{biz}:{mid}:{idx}:{sn}"
    return normalized


def is_wechat_article_url(url: str) -> bool:
    parsed = urlparse(html.unescape(url.strip()))
    return parsed.netloc.lower() in WECHAT_ARTICLE_HOSTS and (
        parsed.path.startswith("/s") or "__biz=" in parsed.query
    )


def source_domain(url: str) -> str:
    parsed = urlparse(html.unescape(url.strip()))
    return parsed.netloc.lower() or "unknown"


def safe_path_part(value: str, fallback: str = "unknown") -> str:
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", (value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if not cleaned:
        cleaned = fallback
    return cleaned[:120]


def ensure_parent(path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
