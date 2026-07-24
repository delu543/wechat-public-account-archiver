from __future__ import annotations

import re

from wechat_crawler.crawler.models import DiscoveredUrl
from wechat_crawler.crawler.normalizer import is_wechat_article_url, normalize_url

URL_RE = re.compile(r"https?://[^\s<>'\"]+")


class UrlDiscoverer:
    name = "url"

    def discover(self, clue: str) -> list[DiscoveredUrl]:
        results: list[DiscoveredUrl] = []
        for match in URL_RE.finditer(clue.strip()):
            url = match.group(0).rstrip("),.;")
            if not is_wechat_article_url(url):
                continue
            results.append(
                DiscoveredUrl(
                    url=url,
                    normalized_url=normalize_url(url),
                    discoverer=self.name,
                    source_clue=clue,
                )
            )
        return results
