"""Synthetic Snappy blocks only. No real cache/key/WeChat/network access."""
import json

import pytest
from v8serialize import dumps

from wechat_crawler.chromium_indexeddb_values import SNAPPY_PREFIX, unwrap_indexeddb_value
from wechat_crawler.crawler.discoverers.content_flow_cache_discoverer import WechatContentFlowCacheDiscoverer
from wechat_crawler.manual_cache_source import read_manual_cache

BIZ = "MzI0MDg5ODA2NQ=="
URL = f"https://mp.weixin.qq.com/s?__biz={BIZ}&mid=123456&idx=1&sn=fixture_signature_123456"


def varint(n):
    result = bytearray()
    while n >= 128:
        result.append((n & 127) | 128)
        n >>= 7
    result.append(n)
    return bytes(result)


def wrapped(payload):
    # One-byte literals deliberately split V8 tags: the old raw scanner cannot
    # accidentally deserialize a contiguous literal from a compressed block.
    return SNAPPY_PREFIX + varint(len(payload)) + b"".join(b"\0" + bytes([b]) for b in payload)


def fixture_blob():
    content = {"user_name": "gh_fixture", "nick_name": "合成账号", "title": "合成标题",
               "content_noencode": "合成标题\n\n完整的合成正文，不是真实文章。", "create_time": "2026-10-27 12:00",
               "link": URL + "&key=DO_NOT_EXPORT&sessionid=DO_NOT_EXPORT"}
    outer = {URL: {"Url": URL, "Content": json.dumps(content, ensure_ascii=False)}}
    return wrapped(b"\xff\x15\xfe" + dumps({"name": "__batch_get_appmsg_data", "data": json.dumps(outer, ensure_ascii=False)}))


@pytest.mark.parametrize("copy", [b"\x01\x04", b"\x0e\x04\x00", b"\x0f\x04\x00\x00\x00"])
def test_all_raw_snappy_copy_modes(copy):
    assert unwrap_indexeddb_value(SNAPPY_PREFIX + b"\x08\x0cabcd" + copy) == b"abcdabcd"


def test_overlapping_copy():
    assert unwrap_indexeddb_value(SNAPPY_PREFIX + b"\x07\x08xab\x0e\x02\x00") == b"xababab"


@pytest.mark.parametrize("size", [1, 60, 61, 257, 65537])
def test_short_and_long_literals(size):
    value = b"x" * size
    n = size - 1
    if n < 60:
        literal = bytes([n << 2])
    else:
        width = max(1, (n.bit_length() + 7) // 8)
        literal = bytes([(59 + width) << 2]) + n.to_bytes(width, "little")
    assert unwrap_indexeddb_value(SNAPPY_PREFIX + varint(size) + literal + value) == value


@pytest.mark.parametrize("raw", [b"", b"\x80", b"\xff\xff\xff\xff\x10", b"\x03\x08ab", b"\x04\x01\x00",
    b"\x04\x01\x02", b"\x02\x08abc", b"\x02\x00a", b"\x01\x00aTRAIL", b"\x04\xf0"])
def test_malformed_streams_fail_closed(raw):
    with pytest.raises(ValueError):
        unwrap_indexeddb_value(SNAPPY_PREFIX + raw)


def test_output_length_limit_is_checked_before_allocation():
    with pytest.raises(ValueError, match="too_large"):
        unwrap_indexeddb_value(SNAPPY_PREFIX + varint(100_000_000))
    with pytest.raises(ValueError):
        unwrap_indexeddb_value(wrapped(b"abc"), max_output=2)


def test_uncompressed_v8_is_preserved():
    data = dumps({"name": "legacy"})
    assert unwrap_indexeddb_value(data) == data


def test_content_flow_compressed_value_still_redacts_and_checks_account():
    flow = WechatContentFlowCacheDiscoverer("unused", BIZ, "gh_fixture")
    batches = list(flow._decode_batches(fixture_blob()))
    assert len(batches) == 1
    article = flow._article_from_payload(URL, batches[0][URL])
    assert article and "完整的合成正文" in article.content_text
    assert "DO_NOT_EXPORT" not in article.source_url + article.raw_html
    other = WechatContentFlowCacheDiscoverer("unused", "OTHER", "gh_fixture")
    assert other._article_from_payload(URL, batches[0][URL]) is None


def test_manual_cache_accepts_compressed_blob_without_raw_operation_marker(tmp_path):
    path = tmp_path / "new-public-value"
    data = fixture_blob()
    assert b"__batch_get_appmsg_data" not in data
    path.write_bytes(data)
    result = read_manual_cache([{"biz": BIZ, "wechat_id": "gh_fixture"}], root=tmp_path, paths=[path])
    assert result["decoded_batches"] == 1 and len(result["records"]) == 1
    assert result["records"][0]["title"] == "合成标题"
    assert "DO_NOT_EXPORT" not in json.dumps(result)
    assert not result["all_publications_verified"]
    assert result["network_requests"] == result["wechat_input_events"] == 0


def test_corrupt_wrapper_never_falls_back_to_scanning_inner_literal():
    flow = WechatContentFlowCacheDiscoverer("unused", BIZ)
    assert list(flow._decode_batches(SNAPPY_PREFIX + b"\x01\x00a" + fixture_blob())) == []


@pytest.mark.parametrize("variant,expected_count", [("complete_inner", 1), ("wrong_biz", 0),
    ("wrong_account", 0), ("missing_inner_sn", 0), ("valid_outer_only", 1)])
def test_outer_index_without_sn_never_suppresses_or_supplies_inner_identity(tmp_path, variant, expected_count):
    flow = WechatContentFlowCacheDiscoverer("unused", BIZ)
    source = next(flow._decode_batches(fixture_blob()))
    wrapper = source[URL]
    inner = json.loads(wrapper["Content"])
    outer = URL.rsplit("&sn=", 1)[0]
    if variant == "wrong_biz":
        inner["link"] = inner["link"].replace(BIZ, "OTHER")
    elif variant == "wrong_account":
        inner["user_name"] = "gh_other"
    elif variant == "missing_inner_sn":
        inner["link"] = outer
    elif variant == "valid_outer_only":
        outer = URL
        inner.pop("link")
        wrapper.pop("Url")
    wrapper["Content"] = json.dumps(inner, ensure_ascii=False)
    data = wrapped(dumps({"name": "__batch_get_appmsg_data", "data": json.dumps({outer: wrapper}, ensure_ascii=False)}))
    path = tmp_path / "compressed-wrapper"
    path.write_bytes(data)
    result = read_manual_cache([{"biz": BIZ, "wechat_id": "gh_fixture"}], root=tmp_path, paths=[path])
    assert len(result["records"]) == expected_count
    assert "DO_NOT_EXPORT" not in json.dumps(result)
    assert result["network_requests"] == result["wechat_input_events"] == 0
