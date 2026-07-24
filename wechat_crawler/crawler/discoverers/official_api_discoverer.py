from __future__ import annotations

from wechat_crawler.crawler.models import DiscoveredUrl


class OfficialApiDiscoverer:
    name = "official_api"

    def __init__(self, access_token: str | None = None) -> None:
        self.access_token = access_token

    def discover(self, clue: str) -> list[DiscoveredUrl]:
        if not self.access_token:
            return []
        raise NotImplementedError(
            "official API discovery needs a configured WeChat Official Account access token"
        )
