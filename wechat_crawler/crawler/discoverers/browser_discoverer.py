from __future__ import annotations

import re

from wechat_crawler.crawler.models import DiscoveredUrl
from wechat_crawler.crawler.normalizer import is_wechat_article_url, normalize_url

URL_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s[^\s\"'<>]+")


class BrowserDiscoverer:
    name = "browser"

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    def discover(self, clue: str) -> list[DiscoveredUrl]:
        if not self.enabled:
            return []
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return [
                DiscoveredUrl(
                    url=clue,
                    normalized_url=normalize_url(clue),
                    discoverer=self.name,
                    source_clue=clue,
                    status="failed",
                    error_message="playwright is not installed",
                )
            ]
        if not clue.startswith("http"):
            return []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(clue, wait_until="domcontentloaded", timeout=20000)
            text = page.content()
            browser.close()
        results: list[DiscoveredUrl] = []
        seen: set[str] = set()
        for match in URL_RE.finditer(text):
            url = match.group(0)
            if not is_wechat_article_url(url):
                continue
            normalized = normalize_url(url)
            if normalized in seen:
                continue
            seen.add(normalized)
            results.append(
                DiscoveredUrl(
                    url=url,
                    normalized_url=normalized,
                    discoverer=self.name,
                    source_clue=clue,
                )
            )
        return results
