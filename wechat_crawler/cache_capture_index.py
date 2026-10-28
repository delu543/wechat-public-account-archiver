"""Validate normalized, independent all-publication pagination evidence.

This checks evidence; it neither discovers a source nor turns cached bodies into
an index. A source adapter must retain actual page order/termination metadata.
Page hashes detect accidental changes, not source authenticity or authorization.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .cache_capture_validation import canonical_url, digest


def verified_pagination(index, scope, expected, *, now=None):
    """Return False for incomplete, stale, filtered or self-labeled catalogs."""
    try:
        if index.get("source") not in ("native_history_index", "official_export"):
            return False
        evidence = index["pagination"]
        if (not isinstance(evidence, dict) or evidence.get("schema") != 1
            or evidence.get("content_types") != "all"
            or evidence.get("order") != "newest_first"
            or evidence.get("filters") != []):
            return False
        observed = datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00"))
        now = now or datetime.now(timezone.utc)
        if (observed.tzinfo is None or observed > now + timedelta(minutes=5)
            or observed.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat() < scope["until"]):
            return False
        pages = evidence["pages"]
        if not isinstance(pages, list) or not 1 <= len(pages) <= 1000:
            return False
        next_offset, previous_day, found, seen = 0, None, {}, set()
        crossed, ended = False, False
        for position, page in enumerate(pages):
            if not isinstance(page, dict) or set(page) != {"payload", "sha256"}:
                return False
            payload = page["payload"]
            if (not isinstance(payload, dict) or set(payload) != {
                    "biz", "offset", "next_offset", "has_more", "articles"}
                or page["sha256"] != digest(payload) or payload["biz"] != scope["biz"]
                or type(payload["offset"]) is not int or payload["offset"] != next_offset
                or type(payload["has_more"]) is not bool):
                return False
            rows = payload["articles"]
            if not isinstance(rows, list) or len(rows) > 20000:
                return False
            if payload["has_more"]:
                if (not rows or type(payload["next_offset"]) is not int
                    or payload["next_offset"] != next_offset + len(rows)):
                    return False
            elif payload["next_offset"] is not None or position != len(pages) - 1:
                return False
            for row in rows:
                if not isinstance(row, dict) or set(row) != {"url", "publication_day"}:
                    return False
                url = canonical_url(row["url"], scope["biz"])
                if row["url"] != url:
                    return False  # Evidence must contain sanitized permanent URLs only.
                day = date.fromisoformat(row["publication_day"]).isoformat()
                if url in seen or len(seen) >= 20000 or (previous_day is not None and day > previous_day):
                    return False
                seen.add(url)
                previous_day = day
                crossed |= day < scope["since"]
                if scope["since"] <= day <= scope["until"]:
                    found[url] = day
            ended = not payload["has_more"]
            next_offset = payload["next_offset"]
        reason = evidence.get("stop_reason")
        terminated = ((reason == "end_of_history" and ended)
                      or (reason == "crossed_before_since" and crossed and not ended))
        return bool(terminated and found == expected)
    except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
        return False
