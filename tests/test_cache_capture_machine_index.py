"""Synthetic pagination evidence, no live client, HTTP or real Word authoring."""
import copy

import pytest

from cache_capture_fixtures import index_and_confirmation, scope, url, window
from wechat_crawler import cache_capture, cache_capture_word
from wechat_crawler.cache_capture_coverage import capture_coverage
from wechat_crawler.cache_capture_store import CaptureStore
from wechat_crawler.cache_capture_validation import canonical_url, digest


def machine_index():
    index, _ = index_and_confirmation()
    index["source"] = "native_history_index"
    pages = []
    for offset, days, more in [(0, (27, 20), True), (2, (13, 8), False)]:
        payload = {"biz": scope()["biz"], "offset": offset,
                   "next_offset": offset + len(days) if more else None, "has_more": more,
                   "articles": [{"url": canonical_url(url(d), scope()["biz"]),
                                 "publication_day": f"2026-10-{d:02d}"} for d in days]}
        pages.append({"payload": payload, "sha256": digest(payload)})
    index["pagination"] = {"schema": 1, "content_types": "all", "order": "newest_first",
        "filters": [], "observed_at": "2026-10-28T00:00:00+08:00", "pages": pages,
        "stop_reason": "end_of_history"}
    return index


def rehash(index):
    for page in index["pagination"]["pages"]:
        page["sha256"] = digest(page["payload"])


def staged(tmp_path):
    target = dict(scope(), newest_url=None)
    store = CaptureStore(tmp_path / "capture.sqlite", target)
    store.ingest(window(27, 20))
    store.ingest(window(13, 8))  # No artificial middle overlap or human anchor.
    return store


def test_real_pagination_evidence_needs_no_human_receipt_or_cache_overlap(tmp_path):
    store = staged(tmp_path)
    report = capture_coverage(store, expected_index=machine_index())
    assert report["full_interval_verified"] and report["machine_pagination_verified"]
    assert report["index_status"] == "verified_pagination_index"
    assert not report["boundary_overlap_connected"]
    assert not report["product_checkpoint_advanced"]
    store.close()


@pytest.mark.parametrize("change", [
    {"content_types": "long_articles"}, {"order": "unknown"}, {"filters": ["originals"]},
    {"observed_at": "2026-10-25T00:00:00+08:00"}, {"observed_at": "3000-01-01T00:00:00Z"},
    {"observed_at": "2026-10-28T00:00:00"}, {"pages": []}, {"stop_reason": "page_limit"},
])
def test_filtered_stale_future_unbounded_or_partial_listing_cannot_pass(tmp_path, change):
    store = staged(tmp_path)
    index = machine_index()
    index["pagination"].update(change)
    report = capture_coverage(store, expected_index=index)
    assert not report["full_interval_verified"] and not report["machine_pagination_verified"]
    store.close()


@pytest.mark.parametrize("case", ["hash", "wrong_biz", "offset_gap", "bad_next", "duplicate",
                                  "wrong_day", "missing", "not_descending", "bool_offset", "signed_url"])
def test_each_page_identity_integrity_and_continuity_is_checked(tmp_path, case):
    store = staged(tmp_path)
    index = machine_index()
    pages = index["pagination"]["pages"]
    if case == "hash":
        pages[0]["sha256"] = "wrong"
    else:
        first, last = pages[0]["payload"], pages[1]["payload"]
        if case == "wrong_biz": first["biz"] = "OTHER"
        if case == "offset_gap": last["offset"] = 3
        if case == "bad_next": first["next_offset"] = 1
        if case == "duplicate": last["articles"][0] = copy.deepcopy(first["articles"][1])
        if case == "wrong_day": last["articles"][0]["publication_day"] = "2026-10-12"
        if case == "missing": last["articles"].pop()
        if case == "not_descending": first["articles"].reverse()
        if case == "bool_offset": first["offset"] = False
        if case == "signed_url": first["articles"][0]["url"] += "&key=DO_NOT_RETAIN"
        rehash(index)
    report = capture_coverage(store, expected_index=index)
    assert not report["full_interval_verified"]
    store.close()


def test_source_label_and_body_cache_alone_still_do_not_prove_full_coverage(tmp_path):
    store = staged(tmp_path)
    index = machine_index()
    del index["pagination"]
    report = capture_coverage(store, expected_index=index)
    assert report["index_status"] == "index_confirmation_required"
    assert not report["full_interval_verified"]
    store.close()


def test_stop_only_after_crossing_before_inclusive_since(tmp_path):
    store = staged(tmp_path)
    index = machine_index()
    evidence = index["pagination"]
    evidence["stop_reason"] = "crossed_before_since"
    last = evidence["pages"][-1]["payload"]
    last.update(has_more=True, next_offset=4)
    rehash(index)
    assert not capture_coverage(store, expected_index=index)["full_interval_verified"]
    last["articles"].append({"url": canonical_url(url(7), scope()["biz"]), "publication_day": "2026-10-07"})
    last["next_offset"] = 5
    rehash(index)
    assert capture_coverage(store, expected_index=index)["full_interval_verified"]
    store.close()


def test_machine_catalog_does_not_hide_missing_or_unhealthy_body(tmp_path):
    store = CaptureStore(tmp_path / "capture.sqlite", dict(scope(), newest_url=None))
    frame = window(27, 20, 13, 8)
    frame["records"][0]["body_ready"] = False
    store.ingest(frame)
    report = capture_coverage(store, expected_index=machine_index())
    assert report["machine_pagination_verified"] and report["missing_bodies"]
    assert not report["full_interval_verified"]
    store.close()


def test_word_entry_can_use_verified_catalog_without_human_signature(tmp_path):
    store = staged(tmp_path)
    class MockExporter:
        def export_account(self, articles, path, **kwargs):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"synthetic state-machine fixture, not a Word")
    result = cache_capture_word.export_verified(store, dict(scope(), _articles=[]),
        {"delivered_baseline_max_article_id": 10182}, machine_index(),
        exporter=MockExporter(), verify=lambda *a: None)
    assert result["exported_count"] == 4 and not result["delivery_verified"]
    assert result["status"] == "word_ready_for_render_review"
    store.close()


def test_cli_confirmation_file_is_optional_for_machine_index(tmp_path, monkeypatch):
    store = staged(tmp_path)
    path = str(store.path)
    store.close()
    monkeypatch.setattr(cache_capture_word, "load_config", lambda p: {"database": {"path": "unused"}, "passive_weekly": {}})
    monkeypatch.setattr(cache_capture_word, "make_plan", lambda *a, **k: {"accounts": [scope()]})
    seen = []
    def fake_export(store, account, settings, index, confirmation):
        seen.append(confirmation)
        return {"status": "no_new_verified"}
    monkeypatch.setattr(cache_capture, "bounded_json", lambda p: machine_index())
    monkeypatch.setattr(cache_capture_word, "export_verified", fake_export)
    assert cache_capture_word.main(["--stage", path, "--verified-index", "synthetic-index"]) == 0
    assert seen == [None]
