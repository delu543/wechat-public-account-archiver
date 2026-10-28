"""One ordinary PC query/account; candidates are NOT verified article records.

No usip/tsn/ft/et, login, pagination, mobile fallback or completeness claim.
Only the later public-body stage may validate biz/mid/idx/sn and commit data.
"""
from __future__ import annotations

from urllib.parse import quote_plus, urljoin, urlparse

from bs4 import BeautifulSoup

from .biz_push_reader import public_identity, permanent_url
from .crawler.fetcher import FetchConfig
from .passive_public_http import PassivePublicFetcher, public_navigation_url


def pc_search_candidates(account, fetcher=None):
    fetcher = fetcher or PassivePublicFetcher(FetchConfig(min_delay_seconds=5, max_delay_seconds=8, max_retries=0))
    query = "https://weixin.sogou.com/weixin?type=2&query=" + quote_plus(account["account_name"])
    response = fetcher.fetch(query)
    base = {"records": [], "candidates": [], "complete": False, "query_count": 1,
            "all_publications_verified": False, "source": "sogou_pc"}
    if response.status != "ok":
        provider = "wechat" if urlparse(response.final_url).netloc == "mp.weixin.qq.com" else "sogou"
        return dict(base, status=response.status, reason="pc_search_restricted" if response.status == "restricted" else "pc_search_unavailable",
                    restriction_provider=provider if response.status == "restricted" else None)
    if urlparse(response.final_url).path != "/weixin" or urlparse(response.final_url).netloc != "weixin.sogou.com":
        return dict(base, status="failed", reason="pc_search_redirected_unusable")
    soup, found = BeautifulSoup(response.text, "html.parser"), {}
    for anchor in soup.select("h3 a[href]"):
        value = urljoin(query, str(anchor.get("href")))
        safe = public_navigation_url(value)
        if not safe or (urlparse(safe).netloc == "weixin.sogou.com" and urlparse(safe).path != "/link"):
            continue
        if key := public_identity(safe):
            safe = permanent_url(key)
        found[safe] = {"url": safe, "source": "sogou_pc", "unverified_candidate": True}
        if len(found) >= 12:
            break
    return dict(base, status="read", candidates=list(found.values()), candidate_count=len(found))
