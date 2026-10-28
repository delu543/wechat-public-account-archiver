"""Synthetic public-profile integration; no real WeChat, keys or network."""
import json

import pytest

from tests.cache_capture_fixtures import BIZ, blob, scope, url
from v8serialize import dumps
from tests.public_leveldb_fixtures import object_key, snapshot_mapping, table, vint
from wechat_crawler.cache_capture_window import read_window, read_windows
from wechat_crawler.public_indexeddb_inline import inline_values, is_object_key
from wechat_crawler.public_leveldb_snapshot import snapshot_values


def mapping_for(tmp_path, rows, **kwargs):
    folder = tmp_path / "IndexedDB" / "weixin_xworker_0.indexeddb.leveldb"
    return folder, snapshot_mapping(folder, rows, **kwargs)


def persist(mapping):
    for path, data in mapping.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def test_object_prefix_filters_metadata_and_other_indexes():
    assert is_object_key(object_key())
    assert not is_object_key(object_key(index=2))
    assert not is_object_key(b"\0\0\1\1arbitrary")
    assert not is_object_key(b"\0\1")


def test_only_latest_inline_values_survive_sequence_and_tombstones():
    key, other = object_key(), object_key(2)
    result = inline_values([(10, key, b"\1" + blob((9,))), (12, key, b"\1" + blob((27,))),
                            (13, other, None), (11, other, b"\1" + blob((8,)))])
    assert len(result["values"]) == 1 and result["deleted_object_keys"] == 1
    assert not result["values"][0].startswith(b"\xff\x11\2")


def test_conflicting_equal_sequence_is_not_selected_arbitrarily():
    with pytest.raises(ValueError, match="conflicting"):
        inline_values([(3, object_key(), b"\1" + blob((9,))), (3, object_key(), b"\1" + blob((27,)))])


def test_external_reference_cannot_revive_previous_inline_body():
    key = object_key()
    result = inline_values([(1, key, b"\1" + blob()), (2, key, b"\1\xff\x11\1" + vint(500) + vint(0))])
    assert result["values"] == [] and result["external_blob_references"] == 1


@pytest.mark.parametrize("value", [b"\x80", b"\1not-v8", b"\1\xff\x11\1\x80", b"\1\xff\x11\2\1\0aTAIL"])
def test_invalid_inline_envelopes_fail_closed(value):
    with pytest.raises(ValueError):
        inline_values([(1, object_key(), value)])


def test_snapshot_uses_manifest_live_tables_and_newer_log(tmp_path):
    folder, mapping = mapping_for(tmp_path, [(object_key(), b"\1" + blob((27,)))],
        table_rows=[(1, object_key(), b"\1" + blob((8,)))],
        extra_names=[("000099.ldb", table([(500, object_key(), b"\1" + blob((9,)))]))])
    reads = []

    def read(path):
        reads.append(path.name)
        return mapping[path]

    result = snapshot_values(folder, read, names=[p.name for p in mapping])
    assert len(result["values"]) == result["live_logs"] == result["live_tables"] == 1
    assert "000099.ldb" not in reads


def test_snapshot_rotation_is_not_an_accepted_window(tmp_path):
    folder, mapping = mapping_for(tmp_path, [(object_key(), b"\1" + blob())])
    times = 0

    def read(path):
        nonlocal times
        if path.name == "CURRENT":
            times += 1
            return mapping[path] if times == 1 else b"MANIFEST-000004\n"
        return mapping[path]

    with pytest.raises(ValueError, match="changed_during_read"):
        snapshot_values(folder, read, names=[p.name for p in mapping])


def test_current_cannot_escape_fixed_namespace(tmp_path):
    folder, mapping = mapping_for(tmp_path, [])
    mapping[folder / "CURRENT"] = b"../../private\n"
    with pytest.raises(ValueError, match="public_current"):
        snapshot_values(folder, mapping.__getitem__, names=[p.name for p in mapping])
    with pytest.raises(ValueError, match="not_public"):
        snapshot_values(tmp_path / "message", lambda _: pytest.fail("must not read"))


def test_current_table_size_must_match_manifest(tmp_path):
    folder, mapping = mapping_for(tmp_path, [], table_rows=[(1, object_key(), b"\1" + blob())])
    mapping[folder / "000003.ldb"] += b"extra"
    with pytest.raises(ValueError, match="size_mismatch"):
        snapshot_values(folder, mapping.__getitem__, names=[p.name for p in mapping])


def test_public_window_consumes_compressed_inline_values_not_old_versions(tmp_path):
    _, mapping = mapping_for(tmp_path, [(object_key(), b"\1" + blob((27, 20)))],
        table_rows=[(1, object_key(), b"\1" + blob((9, 8)))])
    persist(mapping)
    result = read_window(scope(), root=tmp_path)
    assert result["status"] == "read" and len(result["records"]) == 2
    assert result["inline_current_values"] == 1 and result["read_errors"] == 0
    assert {r["publish_time"][:10] for r in result["records"]} == {"2026-10-27", "2026-10-20"}
    assert "DO_NOT_EXPORT" not in json.dumps(result)
    assert not result["all_publications_verified"]
    assert result["network_requests"] == result["private_key_reads"] == result["wechat_input_events"] == 0


def test_public_window_merges_blob_and_current_inline_identity(tmp_path):
    _, mapping = mapping_for(tmp_path, [(object_key(), b"\1" + blob((27, 20)))])
    persist(mapping)
    separate = tmp_path / "IndexedDB/weixin_xworker_0.indexeddb.blob/1/00/2"
    separate.parent.mkdir(parents=True)
    separate.write_bytes(blob((20, 13)))
    result = read_window(scope(), root=tmp_path)
    assert len(result["records"]) == 3 and result["metadata_conflicts"] == 0


def test_bad_log_never_yields_a_fragment_as_an_article(tmp_path):
    folder, mapping = mapping_for(tmp_path, [(object_key(), b"\1" + blob((27,)))])
    mapping[folder / "000002.log"] = mapping[folder / "000002.log"][:-1]
    persist(mapping)
    result = read_window(scope(), root=tmp_path)
    assert result["records"] == [] and result["read_errors"] > 0
    assert not result["all_publications_verified"]


def test_tombstone_prevents_sstable_article_resurrection(tmp_path):
    _, mapping = mapping_for(tmp_path, [(object_key(), None)],
        table_rows=[(1, object_key(), b"\1" + blob((27,)))])
    persist(mapping)
    result = read_window(scope(), root=tmp_path)
    assert result["records"] == [] and result["inline_deleted_keys"] == 1


def test_other_account_inline_body_is_not_returned(tmp_path):
    _, mapping = mapping_for(tmp_path, [(object_key(), b"\1" + blob(raw_override={"user_name": "gh_other"}))])
    persist(mapping)
    result = read_window(scope(), root=tmp_path)
    assert result["records"] == [] and result["decoded_batches"] == 1


def test_symlinked_inline_file_is_not_read(tmp_path):
    folder, mapping = mapping_for(tmp_path, [(object_key(), b"\1" + blob())])
    log = mapping.pop(folder / "000002.log")
    persist(mapping)
    outside = tmp_path / "not-public"
    outside.write_bytes(log)
    (folder / "000002.log").symlink_to(outside)
    result = read_window(scope(), root=tmp_path)
    assert result["records"] == [] and result["read_errors"] > 0


def test_two_accounts_share_exactly_one_physical_blob_read(tmp_path, monkeypatch):
    from wechat_crawler.crawler.discoverers.wechat_cache_discoverer import WechatCacheDiscoverer
    from wechat_crawler.crawler.discoverers.content_flow_cache_discoverer import WechatContentFlowCacheDiscoverer
    flow = WechatContentFlowCacheDiscoverer("unused", BIZ)
    data = next(flow._decode_batches(blob(days=(27,), compressed=False)))
    second_scope = dict(scope(), biz="fixture_second_biz", wechat_id="gh_second", account_name="合成号二")
    second_url = url(20, second_scope["biz"])
    second_item = {"user_name":"gh_second","title":"第二号正文","link":second_url,
                   "create_time":"2026-10-20 12:00","content_noencode":"第二号完整合成内容"}
    data[second_url] = {"Url":second_url,"Content":json.dumps(second_item,ensure_ascii=False)}
    path = tmp_path / "one-public-blob"
    path.write_bytes(dumps({"name":"__batch_get_appmsg_data","data":json.dumps(data,ensure_ascii=False)}))
    calls, original = [], WechatCacheDiscoverer._read_bytes
    def read(self, value):
        calls.append(value)
        return original(self,value)
    monkeypatch.setattr(WechatCacheDiscoverer,"_read_bytes",read)
    result = read_windows([scope(),second_scope], root=tmp_path, paths=[path])
    assert calls == [path] and result["files_read"] == 1
    assert {r["biz"] for r in result["records"]} == {BIZ,second_scope["biz"]}
    assert len(result["records"]) == 2 and result["metadata_conflicts"] == 0
    assert "DO_NOT_EXPORT" not in json.dumps(result)


@pytest.mark.parametrize("accounts", [[], [scope(),scope()], [{"biz":"invalid"}]])
def test_invalid_shared_scope_stops_before_cache_read(tmp_path, accounts):
    with pytest.raises(ValueError,match="accounts"):
        read_windows(accounts,root=tmp_path)
