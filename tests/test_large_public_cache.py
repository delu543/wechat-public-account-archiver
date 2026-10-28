"""Large synthetic public batches; no real cache, keys, UI or network."""
import json

import pytest
from v8serialize import dumps

from tests.cache_capture_fixtures import blob, scope
from wechat_crawler.cache_capture_window import read_window
from wechat_crawler.chromium_indexeddb_values import MAX_VALUE_BYTES, SNAPPY_PREFIX, unwrap_indexeddb_value

MIB = 1024 * 1024


def vint(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    return bytes(out) + bytes([value])


def literal(value):
    if not value:
        return b""
    size = len(value) - 1
    if size < 60:
        return bytes([size << 2]) + value
    width = (size.bit_length() + 7) // 8
    return bytes([(59 + width) << 2]) + size.to_bytes(width, "little") + value


def repeated_fixture(prefix, count, suffix=b""):
    # Exact raw-Snappy copies with offset one, not a generic compressor.
    copies, remainder = divmod(count - 1, 64)
    return (SNAPPY_PREFIX + vint(len(prefix) + count + len(suffix))
            + literal(prefix + b"x") + b"\xfe\x01\x00" * copies
            + (bytes([((remainder - 1) << 2) | 2]) + b"\x01\x00" if remainder else b"")
            + literal(suffix))


def large_batch(size):
    from wechat_crawler.crawler.discoverers.content_flow_cache_discoverer import WechatContentFlowCacheDiscoverer
    batch = next(WechatContentFlowCacheDiscoverer("unused", scope()["biz"])._decode_batches(blob(compressed=False)))
    raw = bytes(dumps({"name": "__batch_get_appmsg_data", "data": json.dumps(batch), "padding": "x" * size}))
    start = raw.index(b"x" * 1024)
    return repeated_fixture(raw[:start], size, raw[start + size:])


def test_legacy_default_still_rejects_over_twenty_mib():
    with pytest.raises(ValueError, match="too_large"):
        unwrap_indexeddb_value(SNAPPY_PREFIX + vint(21 * MIB))


def test_explicit_bounded_batch_output_accepts_large_compressed_value():
    data = repeated_fixture(b"", 21 * MIB + 1)
    assert len(data) < MAX_VALUE_BYTES
    result = unwrap_indexeddb_value(data, max_output=40 * MIB)
    assert len(result) == 21 * MIB + 1 and result == b"x" * len(result)


@pytest.mark.parametrize("limit", [-1, 40 * MIB + 1, True, 1.5])
def test_invalid_output_limits_fail_before_decode(limit):
    with pytest.raises(ValueError, match="invalid_output_limit"):
        unwrap_indexeddb_value(SNAPPY_PREFIX + b"\0", max_output=limit)


def test_advertised_batch_above_forty_mib_is_rejected_without_allocation():
    with pytest.raises(ValueError, match="too_large"):
        unwrap_indexeddb_value(SNAPPY_PREFIX + vint(40 * MIB + 1), max_output=40 * MIB)


def test_compressed_input_limit_is_not_relaxed():
    with pytest.raises(ValueError, match="too_large"):
        unwrap_indexeddb_value(SNAPPY_PREFIX + b"x" * MAX_VALUE_BYTES, max_output=40 * MIB)


def test_large_native_batch_is_parsed_once_and_account_isolated(tmp_path):
    path = tmp_path / "synthetic-large-public-blob"
    path.write_bytes(large_batch(30 * MIB))
    result = read_window(scope(), root=tmp_path, paths=[path])
    assert result["read_errors"] == 0 and result["decoded_batches"] == 1
    assert len(result["records"]) == 2 and {r["biz"] for r in result["records"]} == {scope()["biz"]}
    assert all(r["body_ready"] for r in result["records"])
    assert "DO_NOT_EXPORT" not in json.dumps(result)
    assert not result["all_publications_verified"]
    assert result["network_requests"] == result["wechat_input_events"] == result["private_key_reads"] == 0


def test_snapshot_keeps_forty_mib_total_budget(tmp_path):
    data = large_batch(21 * MIB)
    paths = [tmp_path / "first", tmp_path / "second"]
    for path in paths:
        path.write_bytes(data)
    result = read_window(scope(), root=tmp_path, paths=paths)
    assert len(result["records"]) == 2 and result["read_errors"] == 1
    assert not result["scan_within_bounds"]
    assert not result["all_publications_verified"]


def test_corrupt_large_batch_never_scans_embedded_valid_value(tmp_path):
    path = tmp_path / "corrupt-large"
    path.write_bytes(repeated_fixture(b"", 21 * MIB) + blob())
    result = read_window(scope(), root=tmp_path, paths=[path])
    assert result["records"] == [] and result["read_errors"] == 1
    assert not result["all_publications_verified"]


def test_native_padding_decodes_the_same_value_only_once(monkeypatch):
    from wechat_crawler.crawler.discoverers import content_flow_cache_discoverer as module
    raw = b"\xff\x15\xfe" + b"\0" * 12 + bytes(blob(compressed=False))
    calls, original = [], module.loads
    def counted(value):
        calls.append(value)
        return original(value)
    monkeypatch.setattr(module, "loads", counted)
    batches = list(module.WechatContentFlowCacheDiscoverer("unused", scope()["biz"])._decode_batches(raw))
    assert len(batches) == 1
    assert len(calls) == 4  # Three non-padding envelope bytes and one V8 value.


def test_padding_does_not_hide_two_distinct_serialized_batches():
    from wechat_crawler.crawler.discoverers.content_flow_cache_discoverer import WechatContentFlowCacheDiscoverer
    raw = b"\0" * 12 + bytes(blob((27,), compressed=False)) + b"\0" * 3 + bytes(blob((20,), compressed=False))
    batches = list(WechatContentFlowCacheDiscoverer("unused", scope()["biz"])._decode_batches(raw))
    assert len(batches) == 2
    assert [next(iter(b)).split("&mid=")[1].split("&")[0] for b in batches] == ["2247000027", "2247000020"]
