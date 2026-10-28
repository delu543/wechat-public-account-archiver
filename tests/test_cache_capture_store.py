import json
import sqlite3

import pytest

from cache_capture_fixtures import BIZ, record, scope, url, window
from wechat_crawler.cache_capture_store import CaptureStore
from wechat_crawler.cache_capture_validation import digest


def test_replacing_cache_windows_accumulate_and_resume(tmp_path):
    path = tmp_path / "capture.sqlite"
    store = CaptureStore(path, scope())
    assert store.ingest(window(27, 20))["added"] == 2
    store.close()
    store = CaptureStore(path, scope())
    assert store.ingest(window(20, 13))["added"] == 1
    assert store.ingest(window(13, 8))["added"] == 1
    assert len(store.records()) == 4 and len(store.windows()) == 3
    assert store.ingest(window(13, 8))["status"] == "unchanged"
    assert len(store.records()) == 4
    store.close()


def test_snapshot_and_members_commit_atomically(tmp_path):
    store = CaptureStore(tmp_path / "capture.sqlite", scope())
    def crash():
        raise RuntimeError("simulated_before_commit")
    with pytest.raises(RuntimeError):
        store.ingest(window(27, 20), before_commit=crash)
    assert store.records() == store.windows() == []
    assert store.ingest(window(27, 20))["added"] == 2
    store.close()


def test_scope_change_cannot_merge_other_account_or_time_range(tmp_path):
    path = tmp_path / "capture.sqlite"
    store = CaptureStore(path, scope())
    store.ingest(window(27))
    store.close()
    with pytest.raises(ValueError, match="scope_changed"):
        CaptureStore(path, dict(scope(), until="2026-10-28"))


def test_foreign_record_aborts_before_any_snapshot_commit(tmp_path):
    store = CaptureStore(tmp_path / "capture.sqlite", scope())
    value = window(27)
    value["records"].append(dict(record(20), biz="OTHER"))
    with pytest.raises(ValueError):
        store.ingest(value)
    assert store.records() == store.windows() == []
    store.close()


def test_conflicting_nonempty_versions_preserved_not_overwritten(tmp_path):
    store = CaptureStore(tmp_path / "capture.sqlite", scope())
    store.ingest(window(27))
    value = window(27)
    value["records"][0]["content_text"] = "不同非空正文"
    store.ingest(value)
    assert store.conflicts() == [store.records()[0]["url"]]
    assert store.records()[0]["content_text"] != "不同非空正文"
    store.close()


def test_incomplete_body_can_be_upgraded_without_false_conflict(tmp_path):
    store = CaptureStore(tmp_path / "capture.sqlite", scope())
    value = window(27)
    value["records"][0].update(content_text="", body_ready=False)
    store.ingest(value)
    store.ingest(window(27))
    assert store.records()[0]["body_ready"] and store.conflicts() == []
    store.close()


def test_private_fields_and_transport_urls_never_persist(tmp_path):
    store = CaptureStore(tmp_path / "capture.sqlite", scope())
    value = window(27)
    value["records"][0].update(key="DO_NOT_EXPORT", raw_html="DO_NOT_EXPORT",
        content_text=url(27) + "&key=DO_NOT_EXPORT", content_html="DO_NOT_EXPORT")
    store.ingest(value)
    assert "DO_NOT_EXPORT" not in json.dumps(store.records())
    store.close()


def test_existing_nonowned_database_is_preserved(tmp_path):
    path = tmp_path / "user.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE valuable (value TEXT)")
        conn.execute("INSERT INTO valuable VALUES ('keep')")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="not_owned"):
        CaptureStore(path, scope())
    assert path.read_bytes() == before


def test_source_unavailable_is_not_recorded_as_zero_increment(tmp_path):
    store = CaptureStore(tmp_path / "capture.sqlite", scope())
    assert store.ingest({"status": "waiting_cache"})["snapshot_committed"] is False
    assert store.windows() == []
    store.close()


def test_later_human_anchor_binding_keeps_existing_batches_and_resumes(tmp_path):
    initial = dict(scope(), newest_url=None)
    path = tmp_path / "capture.sqlite"
    store = CaptureStore(path, initial)
    store.ingest(window(27, 20))
    before = store.records(), store.windows()
    receipt = {"confirmed_by":"user", "scope_sha256":digest(store.scope), "url":url(27)}
    updated = store.bind_newest(url(27), confirmation=receipt)
    assert store.records() == before[0] and store.windows() == before[1]
    assert store.bind_newest(url(27), confirmation=receipt) == updated
    store.close()
    resumed = CaptureStore(path, updated)
    assert len(resumed.records()) == 2 and len(resumed.windows()) == 1
    resumed.close()


@pytest.mark.parametrize("kind", ["not_user", "wrong_scope", "not_observed"])
def test_anchor_binding_requires_exact_human_receipt_and_observed_article(tmp_path, kind):
    store = CaptureStore(tmp_path / "capture.sqlite", dict(scope(), newest_url=None))
    store.ingest(window(27))
    chosen = url(27) if kind != "not_observed" else url(20)
    receipt = {"confirmed_by":"user", "scope_sha256":digest(store.scope), "url":chosen}
    if kind == "not_user":
        receipt["confirmed_by"] = "cache"
    if kind == "wrong_scope":
        receipt["scope_sha256"] = "wrong"
    with pytest.raises(ValueError):
        store.bind_newest(chosen, confirmation=receipt)
    assert store.scope["newest_url"] is None and len(store.records()) == 1
    store.close()


def test_known_anchor_cannot_be_silently_replaced(tmp_path):
    store = CaptureStore(tmp_path / "capture.sqlite", scope())
    store.ingest(window(27, 20))
    receipt = {"confirmed_by":"user", "scope_sha256":digest(store.scope), "url":url(20)}
    with pytest.raises(ValueError):
        store.bind_newest(url(20), confirmation=receipt)
    assert store.scope == scope()
    store.close()
