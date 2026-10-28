from __future__ import annotations

import random
import time
from dataclasses import dataclass

import httpx

from .models import FetchResult


RESTRICTION_SIGNALS = {
    "captcha": ["验证码", "请输入验证码", "antispider", "wappoc_appmsgcaptcha", "<title>验证</title>"],
    "login_required": ["请在微信客户端打开链接", "登录后", "请先登录"],
    "rate_limited": ["访问频繁", "操作频繁", "请求过于频繁"],
    "forbidden": ["环境异常", "访问异常", "暂时无法访问"],
    "unavailable": [
        "此内容因违规无法查看",
        "此内容被投诉",
        "此内容已被发布者删除",
        "此内容不存在",
        "文章内容无法查看",
    ],
    "paywall": ["付费阅读", "购买后继续阅读"],
}


@dataclass(frozen=True)
class FetchConfig:
    timeout_seconds: float = 20.0
    min_delay_seconds: float = 2.0
    max_delay_seconds: float = 5.0
    max_retries: int = 2
    user_agent: str = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )


class ArticleFetcher:
    def __init__(self, config: FetchConfig | None = None) -> None:
        self.config = config or FetchConfig()

    def fetch(self, url: str) -> FetchResult:
        headers = {
            "User-Agent": self.config.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
        last_error: str | None = None
        for attempt in range(self.config.max_retries + 1):
            if attempt > 0 or self.config.min_delay_seconds > 0:
                self._sleep()
            try:
                with httpx.Client(follow_redirects=True, timeout=self.config.timeout_seconds) as client:
                    response = client.get(url, headers=headers)
            except httpx.HTTPError as exc:
                last_error = str(exc)
                continue

            status = self._status_from_response(response.status_code, response.text, str(response.url))
            if status == "ok":
                return FetchResult(
                    url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    text=response.text,
                    status="ok",
                )
            if status.startswith("restricted:"):
                return FetchResult(
                    url=url,
                    final_url=str(response.url),
                    status_code=response.status_code,
                    text=response.text,
                    status="restricted",
                    error_message=status.removeprefix("restricted:"),
                )
            last_error = status

        return FetchResult(
            url=url,
            final_url=url,
            status_code=None,
            text="",
            status="failed",
            error_message=last_error or "fetch failed",
        )

    def _sleep(self) -> None:
        low = max(0.0, self.config.min_delay_seconds)
        high = max(low, self.config.max_delay_seconds)
        delay = random.uniform(low, high)
        if delay:
            time.sleep(delay)

    def _status_from_response(self, status_code: int, text: str, final_url: str | None = None) -> str:
        if final_url and "wappoc_appmsgcaptcha" in final_url:
            return "restricted:captcha"
        if status_code in {401, 403}:
            return "restricted:forbidden status"
        if status_code == 429:
            return "restricted:rate limited status"
        if status_code >= 500:
            return f"http {status_code}"
        if status_code >= 400:
            return f"http {status_code}"

        lowered = text.lower()
        has_article_body = (
            "id=\"js_content\"" in text
            or "id='js_content'" in text
            or "rich_media_content" in text
            or ("window.msg_title" in text and ("bizuin:" in text or "user_name:" in text or "nick_name:" in text))
        )
        if "微信扫一扫可打开此内容" in text and "使用完整服务" in text and not has_article_body:
            return "restricted:unsupported"
        for reason, signals in RESTRICTION_SIGNALS.items():
            if any(signal.lower() in lowered for signal in signals):
                return f"restricted:{reason}"
        return "ok"
