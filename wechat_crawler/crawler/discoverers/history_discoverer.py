from __future__ import annotations

import html
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from wechat_crawler.crawler.discoverers.wechat_cache_discoverer import WechatCacheDiscoverer
from wechat_crawler.crawler.date_range import publish_timestamp_to_date
from wechat_crawler.crawler.normalizer import normalize_url


DEFAULT_WECHAT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36 NetType/WIFI "
    "MicroMessenger/7.0.20.1781(0x6700143B) MacWechat/3.8.7(0x13080712) "
    "UnifiedPCMacWechat(0xf2641702) XWEB/18788"
)
MAX_SESSION_CANDIDATES = 3


@dataclass(frozen=True)
class HistoryArticleItem:
    title: str
    public_url: str
    fetch_url: str
    digest: str | None
    author: str | None
    cover: str | None
    publish_timestamp: int | None


@dataclass(frozen=True)
class HistoryPage:
    items: list[HistoryArticleItem]
    next_offset: int
    can_continue: bool


@dataclass(frozen=True)
class HistorySession:
    biz: str
    uin: str
    key: str
    pass_ticket: str
    appmsg_token: str


class HistoryDiscoveryError(RuntimeError):
    pass


class HistoryAccessRestrictedError(HistoryDiscoveryError):
    """A native verification/access limit, never an automatic refresh signal."""


SESSION_RECOVERY_MARKERS = (
    "no signed article url found",
    "missing appmsg_token",
    "ret=-3",
    "ret=-6",
    "invalid appmsg_token",
    "signed article url is missing required session fields",
    "signed article url fetch failed",
)


SESSION_RECOVERY_STEPS = (
    "系统已先尝试保存的公开文章链接和账号 biz；出现此状态表示当前签名会话仍不可用。",
    "保持 VPN 正常运行；不要为了刷新会话关闭或重启代理。",
    "保持 Mac 解锁且 Mac 微信正常登录；系统只会粘贴该公众号已保存的 seed 链接并打开一篇正文。",
    "如果微信显示验证码或登录确认，只在微信里正常完成，程序不会绕过验证。",
    "如果外部唤起后显示“提示/当前页面无法访问”，关闭该提示页并改用微信主界面的正常入口。",
    "自动恢复仍失败时，任务会暂停并保留进度；不会改用第三方搜索或重复导出。",
)

SESSION_ERROR_PRIORITY = {
    "no usable signed article URL found in local WeChat cache": 0,
    "signed article URL is missing required session fields": 1,
    "signed article URL fetch failed": 2,
    "missing appmsg_token in signed article page": 3,
}

TRANSIENT_HISTORY_MARKERS = (
    "profile history request failed",
    "connecttimeout",
    "readtimeout",
    "timeout",
)


def is_history_access_restriction(error: object) -> bool:
    message = str(error).lower()
    return isinstance(error, HistoryAccessRestrictedError) or any(
        marker in message for marker in ("requires verification", "request restricted")
    )


def is_session_recovery_error(error: object) -> bool:
    if is_history_access_restriction(error):
        return False
    message = str(error).lower()
    return any(marker in message for marker in SESSION_RECOVERY_MARKERS)


def is_transient_history_error(error: object) -> bool:
    if is_history_access_restriction(error):
        return False
    message = str(error).lower()
    return any(marker in message for marker in TRANSIENT_HISTORY_MARKERS)


def session_recovery_guidance(account_name: str | None = None) -> str:
    target = f"「{account_name}」" if account_name else "该公众号"
    steps = "\n".join(f"{index}. {step}" for index, step in enumerate(SESSION_RECOVERY_STEPS, start=1))
    return f"{target}的历史文章发现需要先刷新本机微信可见会话：\n{steps}"


class WechatHistoryDiscoverer:
    name = "wechat_history"

    def __init__(
        self,
        app_data_dir: str | Path,
        biz: str,
        wechat_id: str,
        delay_seconds: float = 2.0,
        count: int = 10,
        timeout_seconds: float = 20.0,
    ) -> None:
        self.app_data_dir = Path(app_data_dir).expanduser()
        self.biz = biz
        self.wechat_id = wechat_id
        self.delay_seconds = delay_seconds
        self.count = count
        self.timeout_seconds = timeout_seconds

    def discover_pages(
        self,
        max_pages: int | None = None,
        max_items: int | None = None,
        start_offset: int = 0,
        since_date: str | None = None,
        until_date: str | None = None,
    ):
        session = self.load_session()
        offset = max(0, start_offset)
        page_count = 0
        seen_urls: set[str] = set()
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, headers=self._headers()) as client:
            while True:
                try:
                    page = self.fetch_page(client, session, offset)
                except HistoryDiscoveryError as exc:
                    session_error = is_session_recovery_error(exc)
                    if not session_error and not is_transient_history_error(exc):
                        raise
                    if self.delay_seconds:
                        time.sleep(self.delay_seconds)
                    if session_error:
                        session = self.load_session()
                    page = self.fetch_page(client, session, offset)
                fresh_items: list[HistoryArticleItem] = []
                crossed_since_boundary = False
                for item in page.items:
                    publish_date = publish_timestamp_to_date(item.publish_timestamp)
                    if since_date and publish_date and publish_date < since_date:
                        crossed_since_boundary = True
                        continue
                    if until_date and publish_date and publish_date > until_date:
                        continue
                    key = normalize_url(item.public_url)
                    if key in seen_urls:
                        continue
                    seen_urls.add(key)
                    fresh_items.append(item)
                    if max_items and len(seen_urls) >= max_items:
                        break
                yield offset, fresh_items, page
                page_count += 1
                if max_items and len(seen_urls) >= max_items:
                    break
                if crossed_since_boundary:
                    break
                if not page.can_continue or page.next_offset <= offset:
                    break
                if max_pages and page_count >= max_pages:
                    break
                offset = page.next_offset
                if self.delay_seconds:
                    time.sleep(self.delay_seconds)

    def load_session(self) -> HistorySession:
        candidates = [
            item
            for item in WechatCacheDiscoverer(self.app_data_dir, self.biz, self.wechat_id).discover(
                stop_when_session_fields=True
            )
            if item.fetch_url
        ]
        if not candidates:
            raise HistoryDiscoveryError("no signed article URL found in local WeChat cache")
        last_error = "no usable signed article URL found in local WeChat cache"

        def record_error(message: str) -> None:
            nonlocal last_error
            if SESSION_ERROR_PRIORITY[message] > SESSION_ERROR_PRIORITY[last_error]:
                last_error = message

        seen_sessions: set[tuple[str, str, str]] = set()
        with httpx.Client(timeout=self.timeout_seconds, follow_redirects=True, headers=self._headers()) as client:
            for candidate in candidates:
                signed_url = candidate.fetch_url
                if not signed_url:
                    continue
                parsed = urlparse(signed_url)
                qs = parse_qs(parsed.query)
                if not qs.get("uin") or not qs.get("key"):
                    record_error("signed article URL is missing required session fields")
                    continue
                session_key = (
                    qs.get("uin", [""])[0],
                    qs.get("key", [""])[0],
                    qs.get("pass_ticket", [""])[0],
                )
                if session_key in seen_sessions:
                    continue
                seen_sessions.add(session_key)
                if len(seen_sessions) > MAX_SESSION_CANDIDATES:
                    break
                try:
                    response = client.get(signed_url)
                except (httpx.HTTPError, httpx.InvalidURL):
                    record_error("signed article URL fetch failed")
                    continue
                if response.status_code in {401, 403, 429}:
                    raise HistoryAccessRestrictedError(
                        f"signed article URL request restricted: {response.status_code}"
                    )
                if "wappoc_appmsgcaptcha" in str(response.url) or "验证码" in response.text:
                    raise HistoryAccessRestrictedError("signed article URL requires verification")
                token = self._extract_appmsg_token(response.text)
                if not token:
                    record_error("missing appmsg_token in signed article page")
                    continue
                return HistorySession(
                    biz=self.biz,
                    uin=qs.get("uin", [""])[0],
                    key=qs.get("key", [""])[0],
                    pass_ticket=qs.get("pass_ticket", [""])[0],
                    appmsg_token=token,
                )
        raise HistoryDiscoveryError(last_error)

    def fetch_page(self, client: httpx.Client, session: HistorySession, offset: int) -> HistoryPage:
        params = {
            "action": "getmsg",
            "__biz": session.biz,
            "f": "json",
            "offset": str(offset),
            "count": str(self.count),
            "is_ok": "1",
            "scene": "124",
            "uin": session.uin,
            "key": session.key,
            "pass_ticket": session.pass_ticket,
            "wxtoken": "",
            "x5": "0",
            "appmsg_token": session.appmsg_token,
        }
        try:
            response = client.get("https://mp.weixin.qq.com/mp/profile_ext", params=params)
        except httpx.HTTPError as exc:
            raise HistoryDiscoveryError(f"profile history request failed: {exc.__class__.__name__}") from exc
        if response.status_code in {401, 403, 429}:
            raise HistoryAccessRestrictedError(f"profile history request restricted: {response.status_code}")
        if "验证码" in response.text or "wappoc" in str(response.url):
            raise HistoryAccessRestrictedError("profile history request requires verification")
        try:
            data = response.json()
        except ValueError as exc:
            raise HistoryDiscoveryError("profile history response is not JSON") from exc
        if data.get("ret") not in (0, "0"):
            raise HistoryDiscoveryError(f"profile history returned ret={data.get('ret')} errmsg={data.get('errmsg')}")
        return parse_history_response(data)

    def _headers(self) -> dict[str, str]:
        return {
            "User-Agent": DEFAULT_WECHAT_UA,
            "Accept": "application/json,text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://mp.weixin.qq.com/",
        }

    def _extract_appmsg_token(self, text: str) -> str | None:
        match = re.search(r"window\.appmsg_token\s*=\s*['\"]([^'\"]+)['\"]", text)
        return match.group(1) if match else None


def parse_history_response(data: dict[str, Any]) -> HistoryPage:
    raw = data.get("general_msg_list") or "{}"
    parsed = json.loads(raw)
    items: list[HistoryArticleItem] = []
    for entry in parsed.get("list", []):
        comm = entry.get("comm_msg_info") or {}
        ext = entry.get("app_msg_ext_info") or {}
        publish_ts = comm.get("datetime")
        article_exts = [ext] + list(ext.get("multi_app_msg_item_list") or [])
        for article in article_exts:
            content_url = html.unescape(article.get("content_url") or "")
            if not content_url:
                continue
            public_url = _sanitize_content_url(content_url)
            if "mp.weixin.qq.com/s" not in public_url:
                continue
            items.append(
                HistoryArticleItem(
                    title=article.get("title") or "",
                    public_url=public_url,
                    fetch_url=content_url,
                    digest=article.get("digest"),
                    author=article.get("author"),
                    cover=article.get("cover"),
                    publish_timestamp=int(publish_ts) if publish_ts else None,
                )
            )
    return HistoryPage(
        items=items,
        next_offset=int(data.get("next_offset") or 0),
        can_continue=bool(int(data.get("can_msg_continue") or 0)),
    )


def _sanitize_content_url(url: str) -> str:
    parsed = urlparse(html.unescape(url))
    values = dict(parse_qs(parsed.query, keep_blank_values=True))
    stable = []
    for key in ("__biz", "mid", "idx", "sn", "chksm"):
        if key in values and values[key]:
            stable.append((key, values[key][0]))
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}?{urlencode(stable)}"
