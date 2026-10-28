"""Public-only staging contract. Cache/provider fields never grant authority."""
from __future__ import annotations

from datetime import date
import hashlib
import json
import re

from .biz_push_reader import permanent_url, public_identity
from .cache_capture_window import public_text
from .crawler.dedup import normalized_text_hash
from .crawler.discoverers.content_flow_cache_discoverer import WechatContentFlowCacheDiscoverer
from .manual_cache_source import publication


def canonical_url(value, biz):
    key = public_identity(value, biz)
    if not key:
        raise ValueError("invalid_complete_public_identity")
    return permanent_url(key)


def scope_value(scope):
    biz, gh = scope["biz"], scope["wechat_id"]
    if not isinstance(biz, str) or not re.fullmatch(r"[A-Za-z0-9_+=/-]{4,128}", biz):
        raise ValueError("invalid_biz")
    if not isinstance(gh, str) or not re.fullmatch(r"gh_[A-Za-z0-9_]{1,80}", gh):
        raise ValueError("invalid_wechat_id")
    since, until = date.fromisoformat(scope["since"]), date.fromisoformat(scope["until"])
    if since > until or (until - since).days > 4000:
        raise ValueError("invalid_capture_interval")
    newest = canonical_url(scope["newest_url"], biz) if scope.get("newest_url") else None
    old = sorted({canonical_url(u, biz) for u in scope.get("old_urls", [])})
    return {"schema": 1, "biz": biz, "wechat_id": gh, "account_name": str(scope["account_name"])[:200],
            "since": since.isoformat(), "until": until.isoformat(), "newest_url": newest, "old_urls": old}


def record_value(record, scope):
    url = canonical_url(record["url"], scope["biz"])
    if record.get("biz") != scope["biz"] or record.get("wechat_id") != scope["wechat_id"]:
        raise ValueError("cross_account_record")
    body, title = record.get("content_text"), record.get("title")
    if not isinstance(body, str) or not isinstance(title, str) or len(body.encode()) > 4 * 1024 * 1024:
        raise ValueError("invalid_public_body")
    stamp = publication(record.get("publish_time"))
    if not stamp:
        raise ValueError("unknown_publication_time")
    body, title = public_text(body), public_text(title.strip())[:200]
    flow = WechatContentFlowCacheDiscoverer("unused", scope["biz"])
    return {"url": url, "biz": scope["biz"], "wechat_id": scope["wechat_id"],
            "account_name": scope["account_name"], "title": title, "publish_time": stamp,
            "content_text": body, "content_html": flow._text_to_html(body),
            "content_hash": normalized_text_hash(body),
            "body_ready": bool(body and record.get("body_ready") is True),
            "body_evidence": "structured_cache_content_field" if record.get("body_ready") is True else (
                "restricted_or_truncated" if record.get("body_evidence") == "restricted_or_truncated" else "unverified_or_missing"),
            "title_kind": str(record.get("title_kind") or "unknown")[:80], "source": "passive_public_cache"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
