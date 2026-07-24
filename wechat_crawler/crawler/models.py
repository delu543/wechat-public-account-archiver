from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class DiscoveredUrl:
    url: str
    normalized_url: str
    discoverer: str
    source_clue: str
    account_hint: Optional[str] = None
    status: str = "discovered"
    error_message: Optional[str] = None


@dataclass(frozen=True)
class FetchResult:
    url: str
    final_url: str
    status_code: Optional[int]
    text: str
    status: str
    error_message: Optional[str] = None


@dataclass
class ParsedArticle:
    source_url: str
    normalized_url: str
    title: str
    account_name: str
    account_id: Optional[str]
    wechat_id: Optional[str]
    author: Optional[str]
    publish_time: Optional[str]
    summary: Optional[str]
    content_html: str
    content_text: str
    image_urls: list[str] = field(default_factory=list)
    cover_image_url: Optional[str] = None
    raw_html: str = ""
    content_hash: str = ""
    status: str = "parsed"
    error_message: Optional[str] = None
    crawl_time: str = field(default_factory=now_iso)
