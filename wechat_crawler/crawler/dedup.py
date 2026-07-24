from __future__ import annotations

import hashlib
import re


def normalized_text_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text or "").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def identity_key(account_id: str | None, title: str, publish_time: str | None) -> str:
    return "|".join([account_id or "", title or "", publish_time or ""])
