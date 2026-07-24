import json
from datetime import datetime

import pytest

from wechat_crawler.crawler.discoverers.wechat_cache_discoverer import CacheArticleCandidate
from wechat_crawler.crawler.discoverers.history_discoverer import (
    HistoryArticleItem,
    HistoryDiscoveryError,
    HistoryPage,
    HistorySession,
    WechatHistoryDiscoverer,
    is_session_recovery_error,
    is_transient_history_error,
    parse_history_response,
    session_recovery_guidance,
)


def test_parse_history_response_extracts_all_article_items_and_next_offset():
    response = {
        "ret": 0,
        "can_msg_continue": 1,
        "next_offset": 10,
        "general_msg_list": json.dumps(
            {
                "list": [
                    {
                        "comm_msg_info": {"datetime": 1782816998},
                        "app_msg_ext_info": {
                            "title": "主文章",
                            "author": "作者",
                            "digest": "摘要",
                            "content_url": (
                                "https://mp.weixin.qq.com/s?__biz=MzExampleAccount=="
                                "&mid=2647929883&idx=1&sn=abc&chksm=def&scene=124"
                            ),
                            "cover": "https://mmbiz.qpic.cn/cover.jpg",
                            "multi_app_msg_item_list": [
                                {
                                    "title": "次条",
                                    "content_url": (
                                        "https://mp.weixin.qq.com/s?__biz=MzExampleAccount=="
                                        "&mid=2647929883&idx=2&sn=ghi&chksm=jkl&key=secret"
                                    ),
                                }
                            ],
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
    }

    page = parse_history_response(response)

    assert page.can_continue is True
    assert page.next_offset == 10
    assert [item.title for item in page.items] == ["主文章", "次条"]
    assert page.items[0].public_url == (
        "https://mp.weixin.qq.com/s?__biz=MzExampleAccount%3D%3D"
        "&mid=2647929883&idx=1&sn=abc&chksm=def"
    )
    assert "key=secret" not in page.items[1].public_url
    assert page.items[0].publish_timestamp == 1782816998


def test_session_recovery_errors_are_classified_without_sensitive_values():
    assert is_session_recovery_error("missing appmsg_token in signed article page")
    assert is_session_recovery_error("profile history returned ret=-3 errmsg=invalid session")
    assert is_session_recovery_error("profile history returned ret=-6 errmsg=unknown error")
    assert is_session_recovery_error("signed article URL requires verification")
    assert is_session_recovery_error("signed article URL is missing required session fields")
    assert not is_session_recovery_error("profile history response is not JSON")

    guidance = session_recovery_guidance("测试号")

    assert "测试号" in guidance
    assert "Mac 微信" in guidance
    assert "保持 VPN 正常运行" in guidance
    assert "当前页面无法访问" in guidance
    assert "appmsg_token" not in guidance
    assert "pass_ticket" not in guidance


def test_transient_history_errors_are_classified_separately():
    assert is_transient_history_error("profile history request failed: ConnectTimeout")
    assert is_transient_history_error("profile history request failed: ReadTimeout")
    assert not is_transient_history_error("missing appmsg_token in signed article page")


def test_load_session_keeps_stronger_missing_token_error(monkeypatch, tmp_path):
    candidates = [
        CacheArticleCandidate(
            public_url="https://mp.weixin.qq.com/s?__biz=MzTest%3D%3D&mid=2&idx=1",
            fetch_url=(
                "https://mp.weixin.qq.com/s?__biz=MzTest%3D%3D&mid=2&idx=1"
                "&uin=visible&key=current"
            ),
            source_file="Share Data",
            biz="MzTest==",
            mid="2",
            idx="1",
            sn=None,
            chksm=None,
        ),
        CacheArticleCandidate(
            public_url="https://mp.weixin.qq.com/s?__biz=MzTest%3D%3D&mid=1&idx=1",
            fetch_url="https://mp.weixin.qq.com/s?__biz=MzTest%3D%3D&mid=1&idx=1",
            source_file="Share Data",
            biz="MzTest==",
            mid="1",
            idx="1",
            sn=None,
            chksm=None,
        ),
    ]

    monkeypatch.setattr(
        "wechat_crawler.crawler.discoverers.history_discoverer.WechatCacheDiscoverer.discover",
        lambda self, **kwargs: candidates,
    )

    class Response:
        url = "https://mp.weixin.qq.com/s"
        text = "<html><body>public article without a history token</body></html>"

    monkeypatch.setattr("httpx.Client.get", lambda self, url, **kwargs: Response())
    discoverer = WechatHistoryDiscoverer(tmp_path, biz="MzTest==", wechat_id="gh_test", delay_seconds=0)

    with pytest.raises(HistoryDiscoveryError, match="missing appmsg_token"):
        discoverer.load_session()


def test_history_discovery_stops_after_crossing_since_date(monkeypatch, tmp_path):
    def item(title: str, day: int) -> HistoryArticleItem:
        timestamp = int(datetime(2026, 7, day, 12, 0).astimezone().timestamp())
        return HistoryArticleItem(
            title=title,
            public_url=f"https://mp.weixin.qq.com/s?__biz=MzDateTest==&mid={day}&idx=1&sn={title}",
            fetch_url="",
            digest=None,
            author=None,
            cover=None,
            publish_timestamp=timestamp,
        )

    pages = {
        0: HistoryPage(items=[item("new-1", 5), item("new-2", 4)], next_offset=10, can_continue=True),
        10: HistoryPage(items=[item("boundary", 3), item("old", 2)], next_offset=20, can_continue=True),
        20: HistoryPage(items=[item("too-old", 1)], next_offset=0, can_continue=False),
    }
    fetched_offsets: list[int] = []
    discoverer = WechatHistoryDiscoverer(tmp_path, biz="MzDateTest==", wechat_id="gh_date", delay_seconds=0)
    monkeypatch.setattr(
        discoverer,
        "load_session",
        lambda: HistorySession("MzDateTest==", "uin", "key", "ticket", "token"),
    )

    def fetch_page(client, session, offset):
        fetched_offsets.append(offset)
        return pages[offset]

    monkeypatch.setattr(discoverer, "fetch_page", fetch_page)

    discovered = [
        article.title
        for _, items, _ in discoverer.discover_pages(since_date="2026-07-03")
        for article in items
    ]

    assert discovered == ["new-1", "new-2", "boundary"]
    assert fetched_offsets == [0, 10]

    fetched_offsets.clear()
    full_history = [
        article.title
        for _, items, _ in discoverer.discover_pages()
        for article in items
    ]

    assert full_history == ["new-1", "new-2", "boundary", "old", "too-old"]
    assert fetched_offsets == [0, 10, 20]
