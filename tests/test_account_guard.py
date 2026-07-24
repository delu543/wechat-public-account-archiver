from __future__ import annotations

from argparse import Namespace

from wechat_crawler.crawler.models import ParsedArticle
from wechat_crawler.main import article_matches_account_target


def make_article(account_id: str | None) -> ParsedArticle:
    return ParsedArticle(
        source_url="https://mp.weixin.qq.com/s/example",
        normalized_url="https://mp.weixin.qq.com/s/example",
        title="账号校验文章",
        account_name="账号A",
        account_id=account_id,
        wechat_id="gh_guard",
        author=None,
        publish_time="2026-07-01 09:00",
        summary=None,
        content_html="<p>正文</p>",
        content_text="正文",
        image_urls=[],
        cover_image_url=None,
        raw_html="<html></html>",
        content_hash="c" * 64,
    )


def test_article_matches_exact_account_biz_target():
    args = Namespace(account=None, account_biz="biz-target")

    assert article_matches_account_target(make_article("biz-target"), args) is True
    assert article_matches_account_target(make_article("other-biz"), args) is False
    assert article_matches_account_target(make_article(None), args) is False


def test_article_matches_name_target_when_no_account_biz():
    args = Namespace(account="账号A", account_biz=None)

    assert article_matches_account_target(make_article("any-biz"), args) is True
