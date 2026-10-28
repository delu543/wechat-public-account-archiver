import json

import pytest

from cache_capture_fixtures import BIZ, blob, scope, url
from wechat_crawler.cache_capture_window import read_window, records_from_batch
from wechat_crawler.cache_capture_window import public_text
from wechat_crawler.crawler.discoverers.content_flow_cache_discoverer import WechatContentFlowCacheDiscoverer


@pytest.mark.parametrize("compressed", [False, True])
def test_window_captures_only_own_public_body_and_real_title(tmp_path, compressed):
    file = tmp_path / "IndexedDB/weixin_xworker_0.indexeddb.blob/1/00/1"
    file.parent.mkdir(parents=True)
    file.write_bytes(blob(compressed=compressed))
    value = read_window(scope(), root=tmp_path)
    assert value["status"] == "read" and len(value["records"]) == 2
    assert value["records"][0]["title"] == "独立原标题27"
    assert value["records"][0]["content_text"].startswith("正文第一段")
    assert all(r["body_ready"] for r in value["records"])
    assert "DO_NOT_EXPORT" not in json.dumps(value)
    assert not value["all_publications_verified"]


@pytest.mark.parametrize("override", [{"user_name": "gh_other"}, {"link": url(27, "OTHER")},
    {"link": url(27).rsplit("&sn=", 1)[0]}])
def test_foreign_or_incomplete_inner_link_cannot_borrow_identity(tmp_path, override):
    file = tmp_path / "value"
    file.write_bytes(blob(days=(27,), raw_override=override))
    value = read_window(scope(), root=tmp_path, paths=[file])
    assert value["records"] == []


def test_conflicting_wrapper_identity_is_rejected():
    flow = WechatContentFlowCacheDiscoverer("unused", BIZ)
    data = next(flow._decode_batches(blob(days=(27,))))
    next(iter(data.values()))["Url"] = url(20)
    assert list(records_from_batch(data, scope())) == []


@pytest.mark.parametrize("extra,ready", [({}, False), ({"is_truncated": True}, False),
    ({"is_content_complete": False}, False)])
def test_title_only_and_truncated_are_not_complete_bodies(tmp_path, extra, ready):
    file = tmp_path / "value"
    override = dict(content_noencode="", title="标题作为旧解析器回退", **extra)
    file.write_bytes(blob(days=(27,), raw_override=override))
    value = read_window(scope(), root=tmp_path, paths=[file])
    assert len(value["records"]) == 1 and value["records"][0]["body_ready"] is ready


def test_corrupt_compressed_blob_is_reported_not_zero_new(tmp_path):
    file = tmp_path / "bad"
    file.write_bytes(b"\xff\x11\x02\xff\xff")
    value = read_window(scope(), root=tmp_path, paths=[file])
    assert value["status"] == "waiting_cache" and value["read_errors"] == 1
    assert not value["all_publications_verified"]


def test_symlinks_and_outside_root_are_not_read(tmp_path):
    file = tmp_path / "real"
    file.write_bytes(blob())
    link = tmp_path / "link"
    link.symlink_to(file)
    value = read_window(scope(), root=tmp_path, paths=[link, tmp_path.parent / "unrelated"])
    assert value["records"] == [] and value["read_errors"] == 2


def test_changing_current_blob_produces_different_window_not_zero_success(tmp_path):
    file = tmp_path / "current"
    file.write_bytes(blob(days=(27, 20)))
    first = read_window(scope(), root=tmp_path, paths=[file])
    file.write_bytes(blob(days=(13, 8)))
    second = read_window(scope(), root=tmp_path, paths=[file])
    assert first["fingerprint"] != second["fingerprint"]
    assert len(first["records"]) == len(second["records"]) == 2


@pytest.mark.parametrize("extra", [{"is_truncated": True}, {"is_content_complete": False}, {"is_pay": True}])
def test_explicit_restriction_blocks_even_nonempty_body(tmp_path, extra):
    path = tmp_path / "value"
    path.write_bytes(blob(days=(27,), raw_override=extra))
    value = read_window(scope(), root=tmp_path, paths=[path])
    assert not value["records"][0]["body_ready"]


@pytest.mark.parametrize("value", ["https://mp.weixin.qq.com/s/abcdefghijklmnopqrstuv?key=DO_NOT_EXPORT",
    "https://mp.weixin.qq.com/mp/profile_ext?__biz=public&uin=DO_NOT_EXPORT&album_id=123"])
def test_nonpermanent_mp_url_transport_values_are_not_retained(value):
    assert "DO_NOT_EXPORT" not in public_text(value)
