"""Shared synthetic-only public cache fixtures; no live WeChat data."""
import json

from v8serialize import dumps

from wechat_crawler.cache_capture_validation import digest, scope_value
from wechat_crawler.chromium_indexeddb_values import SNAPPY_PREFIX

BIZ = "MzI0MDg5ODA2NQ=="


def url(day, biz=BIZ):
    return f"https://mp.weixin.qq.com/s?__biz={biz}&mid=22470000{day:02d}&idx=1&sn=synthetic_signature_{day:02d}"


def scope():
    return scope_value({"biz": BIZ, "wechat_id": "gh_fixture", "account_name": "合成公众号",
        "since": "2026-10-08", "until": "2026-10-27", "newest_url": url(27), "old_urls": [url(8)]})


def record(day, *, body=None, ready=True):
    return {"url": url(day), "biz": BIZ, "wechat_id": "gh_fixture", "account_name": "合成公众号",
        "title": "独立原标题" + str(day), "title_kind": "original_title",
        "publish_time": f"2026-10-{day:02d}T12:00:00+08:00", "content_text": body if body is not None else "正文段落 " + str(day),
        "body_ready": ready}


def window(*days):
    return {"status": "read", "records": [record(d) for d in days], "read_errors": 0,
            "unsupported_values": 0, "metadata_conflicts": 0, "scan_within_bounds": True}


def index_and_confirmation(days=(8, 13, 20, 27)):
    index = {"source": "human_verified_homepage_index", "all_publications_for_interval": True,
        "biz": BIZ, "since": "2026-10-08", "until": "2026-10-27",
        "articles": [{"url": url(d), "publication_day": f"2026-10-{d:02d}"} for d in days]}
    receipt = {"confirmed_by": "user", "index_sha256": digest(index), "scope_sha256": digest(scope())}
    return index, receipt


def blob(days=(27, 20), *, compressed=True, raw_override=None):
    batch = {}
    for day in days:
        link = url(day)
        inner = {"user_name": "gh_fixture", "nick_name": "合成公众号", "title": "独立原标题" + str(day),
            "create_time": f"2026-10-{day:02d} 12:00", "link": link + "&key=DO_NOT_EXPORT",
            "content_noencode": "正文第一段 " + str(day) + "\n\n正文第二段"}
        if raw_override:
            inner.update(raw_override)
        batch[link.rsplit("&sn=", 1)[0]] = {"Url": link, "Content": json.dumps(inner, ensure_ascii=False)}
    raw = dumps({"name": "__batch_get_appmsg_data", "data": json.dumps(batch, ensure_ascii=False)})
    if not compressed:
        return raw
    size, preamble = len(raw), bytearray()
    while size >= 128:
        preamble.append((size & 127) | 128)
        size >>= 7
    preamble.append(size)
    return SNAPPY_PREFIX + bytes(preamble) + b"".join(b"\0" + bytes([b]) for b in raw)
