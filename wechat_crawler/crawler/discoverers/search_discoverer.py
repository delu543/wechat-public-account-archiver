from __future__ import annotations

import html
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import quote_plus, unquote

import httpx

from wechat_crawler.crawler.models import DiscoveredUrl
from wechat_crawler.crawler.normalizer import is_wechat_article_url, normalize_url

MP_URL_RE = re.compile(r"https?://mp\.weixin\.qq\.com/s[^\s\"'<>]+")
BLOCK_SIGNALS = ("验证码", "antispider", "请输入验证码", "访问频繁", "操作频繁")


@dataclass(frozen=True)
class SearchConfig:
    max_results_per_clue: int = 10
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 5.0
    timeout_seconds: float = 20.0
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )


class SearchDiscoverer:
    name = "search"

    def __init__(self, config: SearchConfig | None = None) -> None:
        self.config = config or SearchConfig()

    def discover(self, clue: str) -> list[DiscoveredUrl]:
        query = clue.strip()
        if not query:
            return []
        self._sleep()
        url = f"https://weixin.sogou.com/weixin?type=2&query={quote_plus(query)}"
        headers = {"User-Agent": self.config.user_agent, "Accept-Language": "zh-CN,zh;q=0.9"}
        try:
            response = httpx.get(url, headers=headers, timeout=self.config.timeout_seconds, follow_redirects=True)
        except httpx.HTTPError as exc:
            return [
                DiscoveredUrl(
                    url=url,
                    normalized_url=normalize_url(url),
                    discoverer=self.name,
                    source_clue=clue,
                    status="failed",
                    error_message=str(exc),
                )
            ]
        if response.status_code in {401, 403, 429} or any(signal in response.text for signal in BLOCK_SIGNALS):
            return [
                DiscoveredUrl(
                    url=url,
                    normalized_url=normalize_url(url),
                    discoverer=self.name,
                    source_clue=clue,
                    status="restricted",
                    error_message="public search restricted or captcha required",
                )
            ]
        seen: set[str] = set()
        results: list[DiscoveredUrl] = []
        body = unquote(html.unescape(response.text))
        for match in MP_URL_RE.finditer(body):
            article_url = match.group(0).split("&amp;")[0]
            if not is_wechat_article_url(article_url):
                continue
            normalized = normalize_url(article_url)
            if normalized in seen:
                continue
            seen.add(normalized)
            results.append(
                DiscoveredUrl(
                    url=article_url,
                    normalized_url=normalized,
                    discoverer=self.name,
                    source_clue=clue,
                    account_hint=clue,
                )
            )
            if len(results) >= self.config.max_results_per_clue:
                break
        return results

    def _sleep(self) -> None:
        low = max(0.0, self.config.min_delay_seconds)
        high = max(low, self.config.max_delay_seconds)
        delay = random.uniform(low, high)
        if delay:
            time.sleep(delay)
