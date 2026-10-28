"""Offline HTTP transport + synthetic pages. Never searches or fetches WeChat."""
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from wechat_crawler.crawler.fetcher import FetchConfig
from wechat_crawler.passive_public_http import PassivePublicFetcher, literal_public_redirect
from wechat_crawler.public_article_identity import primary_page_identity
from wechat_crawler.sogou_pc_source import pc_search_candidates

SN = "0123456789abcdef0123456789abcdef"
URL = f"https://mp.weixin.qq.com/s?__biz=biz-account&mid=2&idx=1&sn={SN}"
ACCOUNT = {"account_name": "公众号示例", "biz": "biz-account", "wechat_id": "gh_target"}


def fetcher(handler):
    return PassivePublicFetcher(FetchConfig(min_delay_seconds=0, max_delay_seconds=0, max_retries=0),
                                transport=httpx.MockTransport(handler))


def test_one_plain_pc_query_no_filters_or_pagination_and_no_verified_records():
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.host == "weixin.sogou.com" and request.url.path == "/weixin"
        assert parse_qs(urlparse(str(request.url)).query) == {"type": ["2"], "query": [ACCOUNT["account_name"]]}
        return httpx.Response(200, text=f'<h3><a href="/link?url=opaque">一</a></h3><h3><a href="{URL}">二</a></h3>')
    report = pc_search_candidates(ACCOUNT, fetcher(handler))
    assert len(requests) == report["query_count"] == 1
    assert len(report["candidates"]) == 2 and report["records"] == []
    assert all(r["unverified_candidate"] for r in report["candidates"])
    assert report["all_publications_verified"] is False


@pytest.mark.parametrize("status,text", [(200, "请输入验证码"), (403, "forbidden"), (429, "too many")])
def test_query_challenge_has_one_request_no_retry_or_alternate_endpoint(status, text):
    requests = []
    def handler(request):
        requests.append(str(request.url))
        return httpx.Response(status, text=text)
    report = pc_search_candidates(ACCOUNT, fetcher(handler))
    assert report["status"] == "restricted" and report["restriction_provider"] == "sogou"
    assert len(requests) == 1 and report["candidates"] == []


def test_home_redirect_is_not_retried_as_mobile_or_filtered_query():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(302, headers={"location": "/"})
    report = pc_search_candidates(ACCOUNT, fetcher(handler))
    assert report["status"] == "failed" and calls == ["/weixin"]


@pytest.mark.parametrize("target", ["https://127.0.0.1/private", "file:///private/data", "https://example.com/", "https://mp.weixin.qq.com@evil.example/s"])
def test_redirects_cannot_open_arbitrary_hosts_or_local_resources(target):
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": target})
    result = fetcher(handler).fetch("https://weixin.sogou.com/link?url=opaque")
    assert result.status == "failed" and len(calls) == 1


def test_public_link_redirect_strips_client_credentials_before_article_fetch():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        if request.url.host == "weixin.sogou.com":
            return httpx.Response(302, headers={"location": URL + "&key=SECRET&uin=PRIVATE"})
        assert "key=" not in str(request.url) and "uin=" not in str(request.url)
        return httpx.Response(200, text="public body")
    result = fetcher(handler).fetch("https://weixin.sogou.com/link?url=opaque")
    assert result.status == "ok" and len(calls) == 2
    assert "SECRET" not in result.final_url and "PRIVATE" not in result.final_url


def test_redirect_to_challenge_stops_before_fetching_challenge():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha"})
    result = fetcher(handler).fetch("https://weixin.sogou.com/link?url=opaque")
    assert result.status == "restricted" and result.error_message == "captcha" and len(calls) == 1


def test_timeout_has_no_retry():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        raise httpx.ReadTimeout("private error must be redacted", request=request)
    result = fetcher(handler).fetch(URL)
    assert len(calls) == 1 and result.status == "failed" and result.error_message == "public_http_unavailable"


def test_primary_identity_is_independent_of_links_in_article_body():
    assert primary_page_identity(f'<article><a href="{URL}">相关链接</a></article>') is None
    assert primary_page_identity(f'<link rel="canonical" href="{URL}">') == ("biz-account", "2", "1", SN)
    assert primary_page_identity(f'<meta property="og:url" content="{URL}"><script>var mid="3";</script>') is None


def test_all_four_script_fields_and_no_ambiguous_canonical():
    page = f'<script>var biz="biz-account"; var appmsgid=2; var itemidx="1"; var sn="{SN}";</script>'
    assert primary_page_identity(page) == ("biz-account", "2", "1", SN)
    assert primary_page_identity(page + f'<link rel="canonical" href="{URL.replace("mid=2", "mid=4")}">') is None


def test_redirect_literal_is_not_a_javascript_program():
    assert literal_public_redirect(f'var url = "{URL}";')
    assert literal_public_redirect(f'var url = "{URL}" + secret;') is None
    assert literal_public_redirect('eval(atob("anything"))') is None
