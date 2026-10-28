"""Synthetic native V8 blobs only; no live cache or private DB/key access."""
import json
from types import SimpleNamespace

from v8serialize import dumps

from wechat_crawler import manual_cache_source as cache
from wechat_crawler.weekly_source_router import WeeklySourceRouter, public_source_health
from wechat_crawler.biz_push_reader import public_identity

BIZ = "MzI4MzExMDU1Nw=="
ACCOUNTS = [{"biz": BIZ, "wechat_id": "gh_fixture"}]


def url(mid, biz=BIZ):
    return f"https://mp.weixin.qq.com/s?__biz={biz}&mid={mid}&idx=1&sn=fixture_signature_{mid}"


def blob(path, entries):
    outer = {url(mid): {"Url": url(mid), "Content": json.dumps({"user_name": "gh_fixture", "link": url(mid) + "&key=DO_NOT_EXPORT",
        "title": "标题" + str(mid), "create_time": day})} for mid, day in entries}
    path.write_bytes(b"\xff\x15\xfe" + dumps({"name": "__batch_get_appmsg_data", "data": json.dumps(outer)}))


def test_fresh_manual_refresh_blob_finds_new_cards_without_bodies_and_deduplicates(tmp_path):
    path = tmp_path / "IndexedDB/weixin_xworker_0.indexeddb.blob/1/00/1"
    path.parent.mkdir(parents=True)
    blob(path, [(1, "2026-10-08 12:00")])
    before = cache.read_manual_cache(ACCOUNTS, root=tmp_path)
    assert len(before["records"]) == 1
    blob(path, [(1, "2026-10-08 12:00"), (2, "2026-10-26 12:00")])
    duplicate = path.with_name("2")
    blob(duplicate, [(1, "2026-10-08 12:00")])
    after = cache.read_manual_cache(ACCOUNTS, root=tmp_path)
    assert len(after["records"]) == 2 and after["key_health"] == "not_used"
    assert {public_identity(r["url"])[1] for r in after["records"]} == {"1", "2"}
    assert all(r["title"] and r["publish_time"].endswith("+08:00") for r in after["records"])
    assert "DO_NOT_EXPORT" not in json.dumps(after) and "key=" not in json.dumps(after)
    assert after["network_requests"] == after["wechat_input_events"] == 0
    assert after["all_publications_verified"] is False


def test_legacy_main_and_multi_cards_keep_common_publication_time_not_update_time():
    payload = {"list": [{"comm_msg_info": {"datetime": 1791453600}, "app_msg_ext_info": {
        "content_url": url(1), "title": "main", "update_time": 1999999999,
        "multi_app_msg_item_list": [{"content_url": url(2), "title": "second"}]}}]}
    records = list(cache.cards(payload, ACCOUNTS))
    assert len(records) == 2 and len({r["publish_time"] for r in records}) == 1
    assert not records[0]["publish_time"].startswith("2033")


def test_cross_biz_wrong_account_short_link_and_missing_sn_never_join_nearby_fields():
    payload = {"list": [{"url": url(1, "OTHER")}, {"url": url(2), "user_name": "gh_other"},
        {"url": "https://mp.weixin.qq.com/s/short"}, {"url": url(3).split("&sn=")[0], "sn": "outside-url"}]}
    assert list(cache.cards(payload, ACCOUNTS)) == []


def test_conflicting_metadata_is_reported_not_resolved_by_mtime(tmp_path):
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    first.write_text(json.dumps({"list": [{"url": url(1), "title": "a", "create_time": "2026-10-08"}]}))
    second.write_text(json.dumps({"list": [{"url": url(1), "title": "b", "create_time": "2026-10-26"}]}))
    result = cache.read_manual_cache(ACCOUNTS, root=tmp_path, paths=[first, second])
    assert result["metadata_conflicts"] == 1 and result["records"][0]["publish_time"] is None
    assert result["accounts"][0]["undecoded_rows"] == 1 and not result["accounts"][0]["all_publications_verified"]


def test_empty_or_symlink_cache_is_not_zero_new_success(tmp_path):
    result = cache.read_manual_cache(ACCOUNTS, root=tmp_path, paths=[])
    assert result["status"] == "blocked" and result["coverage_measured"] is False
    target = tmp_path / "owned.json"
    target.write_text("{}")
    link = tmp_path / "link.json"
    link.symlink_to(target)
    result = cache.read_manual_cache(ACCOUNTS, root=tmp_path, paths=[link])
    assert result["status"] == "blocked" and result["cache_read_errors"] == 1


def test_explicit_cache_router_never_calls_key_helper_or_setup():
    calls = []
    def child(module, request, **kwargs):
        calls.append(module)
        assert module == "wechat_crawler.manual_cache_source"
        return {"status": "read", "records": []}
    WeeklySourceRouter({}, "cache", child=child).read(ACCOUNTS)
    assert calls == ["wechat_crawler.manual_cache_source"]


def test_biz_salt_failure_is_one_notice_without_cache_fallback_or_retry():
    calls = []
    def child(module, request, **kwargs):
        calls.append(module)
        return {"status": "blocked", "reason": "biz_salt_changed"}
    result = WeeklySourceRouter({}, "biz", child=child).read(ACCOUNTS * 9)
    health = public_source_health({"source_reason": result["reason"]}, "biz")
    assert calls == ["wechat_crawler.biz_push_reader"]
    assert result["key_health"] == "reinitialization_required"
    assert health["required_actions"] == ["需重新初始化"]


def test_cache_fresh_mtime_and_samples_still_do_not_prove_all_publications(tmp_path):
    from wechat_crawler.passive_weekly_plan import coverage_for
    path = tmp_path / "cards.json"
    path.write_text(json.dumps({"list": [{"url": url(1), "title": "a", "create_time": "2026-10-26"}]}))
    source = cache.read_manual_cache(ACCOUNTS, root=tmp_path, paths=[path])
    account = dict(ACCOUNTS[0], _sample_keys={public_identity(url(1))}, since="2026-10-08", until="2026-10-26")
    report = coverage_for(account, source, source["records"])
    assert report["source_fresh"] and report["known_sample_matched"] == 1
    assert not report["publication_interval_proved"] and not report["complete"]


def test_cli_cache_check_does_not_initialize_product_or_call_native(tmp_path, monkeypatch, capsys):
    from test_passive_weekly import prepare
    from wechat_crawler import main as cli
    storage, _, settings = prepare(tmp_path, paused=True)
    monkeypatch.setattr(cli, "load_config", lambda p: {"database": {"path": str(storage.db_path)},
        "output": {"path": str(tmp_path / "out")}, "passive_weekly": settings})
    monkeypatch.setattr(WeeklySourceRouter, "read", lambda s, a: {"status": "read", "coverage_measured": False})
    assert cli.main(["weekly-run", "--source", "cache", "--check"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["source_mode"] == "cache" and result["key_health"] == "not_used"
    assert result["automation_paused"] and result["product_writes"] == 0
