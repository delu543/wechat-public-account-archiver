"""Passive source adapters. No GUI, profile_ext, client key replay or chat scans."""
from __future__ import annotations

import html
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import quote_plus, unquote

from .biz_push_reader import permanent_url, public_identity
from .crawler.discoverers.content_flow_cache_discoverer import WechatContentFlowCacheDiscoverer
from .crawler.discoverers.wechat_cache_discoverer import WechatCacheDiscoverer
from .crawler.fetcher import ArticleFetcher, FetchConfig

APP_DATA = Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data"
HELPER_PYTHON = Path.home() / "Library/Application Support/WeChatLocalExport/tools/python/bin/python3"


def child_json(module, request, *, argument, timeout, python=None):
    try:
        result = subprocess.run([python or sys.executable, "-m", module, argument],
            input=json.dumps(request), capture_output=True, text=True, timeout=timeout, check=False,
            cwd=Path(__file__).resolve().parents[1])
        if len(result.stdout.encode()) > 16 * 1024 * 1024:
            raise ValueError()
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict) or payload.get("status") not in ("read", "blocked"):
            raise ValueError()
        return payload
    except Exception:
        return {"status": "blocked", "reason": "bounded_helper_unavailable", "coverage_measured": False}


class PassiveSources:
    def __init__(self, settings):
        self.settings = settings

    def read(self, accounts):
        targets = [{"biz": a["biz"], "wechat_id": a["wechat_id"]} for a in accounts]
        source = child_json("wechat_crawler.biz_push_reader", {"accounts": targets},
            argument="--authorized-biz-only", timeout=60, python=self.settings.get("helper_python")
            or (str(HELPER_PYTHON) if HELPER_PYTHON.is_file() else sys.executable))
        if source.get("status") == "read":
            cache = child_json("wechat_crawler.passive_weekly_sources", {"accounts": targets},
                               argument="--cache-only", timeout=30)
            source["cache_status"] = cache.get("status")
            source["records"] = source.get("records", []) + cache.get("records", [])
        return source


def read_cache(accounts):
    workers = [(a, WechatCacheDiscoverer(APP_DATA, a["biz"], a["wechat_id"]), {}) for a in accounts]
    records, files, decoded = {}, 0, 0
    started = time.monotonic()
    reader = workers[0][1]
    complete = True
    for path in reader._iter_files():
        if files >= 32 or time.monotonic() - started > 20:
            complete = False
            break
        data = reader._read_bytes(path)
        if not data:
            continue
        files += 1
        for account, parser, candidates in workers:
            parser._extract_full_urls(data, path, candidates)
            parser._extract_encoded_full_urls(data, path, candidates)
            if b"__batch_get_appmsg_data" in data:
                flow = WechatContentFlowCacheDiscoverer(APP_DATA, account["biz"], account["wechat_id"])
                for batch in flow._decode_batches(data):
                    decoded += 1
                    for url, payload in batch.items():
                        article = flow._article_from_payload(url, payload)
                        if article and (key := public_identity(article.source_url, account["biz"])):
                            records[key] = {"url": permanent_url(key), "biz": key[0], "title": article.title,
                                "publish_time": article.publish_time, "source": "content_flow_cache"}
    for account, _, candidates in workers:
        for candidate in candidates.values():
            if key := public_identity(candidate.public_url, account["biz"]):
                records.setdefault(key, {"url": permanent_url(key), "biz": key[0], "title": "",
                                        "publish_time": None, "source": "public_url_cache"})
    return {"status": "read", "records": list(records.values()), "files_read": files,
            "decoded_batches": decoded, "scan_within_bounds": complete,
            "all_publications_verified": False, "network_requests": 0, "wechat_input_events": 0}


def mobile_search_candidates(account, fetcher=None):
    """One low-frequency mobile query; only exact-biz permanent results survive.

    Short links/redirect resolvers are not followed. Search is not an exhaustive
    history source and never grants a complete-publication checkpoint.
    """
    fetcher = fetcher or ArticleFetcher(FetchConfig(min_delay_seconds=5, max_delay_seconds=8, max_retries=0))
    query = "https://weixin.sogou.com/weixinwap?type=2&query=" + quote_plus(account["account_name"])
    result = fetcher.fetch(query)
    if result.status != "ok":
        return {"status": result.status, "records": [], "complete": False}
    records = {}
    for url in re.findall(r'https?://mp\.weixin\.qq\.com/s\?[^\s"\'<>]+', unquote(html.unescape(result.text))):
        if key := public_identity(url, account["biz"]):
            records[key] = {"url": permanent_url(key), "biz": key[0], "title": "",
                            "publish_time": None, "source": "sogou_mobile"}
    return {"status": "read", "records": list(records.values()), "complete": False}


if __name__ == "__main__":
    try:
        if sys.argv[1:] != ["--cache-only"]:
            raise ValueError()
        request = json.loads(sys.stdin.buffer.read(65537))
        accounts = request["accounts"]
        if not 1 <= len(accounts) <= 32:
            raise ValueError()
        result = read_cache(accounts)
    except Exception:
        result = {"status": "blocked", "reason": "bounded_cache_unavailable", "records": []}
    print(json.dumps(result, ensure_ascii=False))
