from __future__ import annotations

from typing import Protocol

from wechat_crawler.crawler.models import DiscoveredUrl


class Discoverer(Protocol):
    name: str

    def discover(self, clue: str) -> list[DiscoveredUrl]:
        ...
