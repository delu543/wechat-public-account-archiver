from pathlib import Path

import pytest

from cache_capture_fixtures import index_and_confirmation, scope, window
from wechat_crawler import cache_capture as cli
from wechat_crawler.cache_capture_session import collect
from wechat_crawler.cache_capture_store import CaptureStore
from wechat_crawler.cache_capture_word import export_verified, load_scope

CONTROL = {"paused": True, "updated_at": "existing-pause"}


def test_paused_default_stops_before_cache_read_or_stage_write(tmp_path):
    path = tmp_path / "capture.sqlite"
    def forbidden():
        raise AssertionError("cache must not be read")
    result = collect(scope(), path, forbidden, lambda: CONTROL)
    assert result["status"] == "paused" and not path.exists()


def test_explicit_local_staging_is_bounded_and_keeps_global_pause(tmp_path):
    snapshots = iter([window(27, 20), window(20, 13), window(13, 8)])
    result = collect(scope(), tmp_path / "capture.sqlite", lambda: next(snapshots), lambda: CONTROL,
        allow_local_stage_while_paused=True, max_polls=3, interval=0, clock=lambda: 0, sleep=lambda _: None)
    assert result["status"] == "poll_limit_reached"
    assert result["coverage"]["records_captured"] == 4
    assert result["automation_paused"] and not result["master_pause_changed"]
    assert not result["coverage"]["full_interval_verified"]


def test_control_change_during_read_stops_before_commit(tmp_path):
    state = dict(CONTROL)
    def source():
        state["updated_at"] = "new-user-pause"
        return window(27)
    path = tmp_path / "capture.sqlite"
    result = collect(scope(), path, source, lambda: dict(state), allow_local_stage_while_paused=True,
        max_polls=2, interval=0, clock=lambda: 0)
    assert result["status"] == "control_changed"
    assert result["coverage"]["records_captured"] == 0


def test_interruption_resumes_without_repeating_members(tmp_path):
    path = tmp_path / "capture.sqlite"
    calls = []
    def source():
        if calls:
            raise KeyboardInterrupt()
        calls.append(1)
        return window(27, 20)
    first = collect(scope(), path, source, lambda: CONTROL, allow_local_stage_while_paused=True,
        max_polls=3, interval=0, clock=lambda: 0, sleep=lambda _: None)
    assert first["status"] == "interrupted_resumable"
    second = collect(scope(), path, lambda: window(20, 13), lambda: CONTROL,
        allow_local_stage_while_paused=True, max_polls=1, interval=0, clock=lambda: 0)
    assert second["coverage"]["records_captured"] == 3


def test_plan_does_not_read_wechat_or_create_stage(monkeypatch, capsys):
    monkeypatch.setattr(cli, "load_config", lambda p: {"database": {"path": "unused"}})
    monkeypatch.setattr(cli, "account_scope", lambda *a, **k: scope())
    monkeypatch.setattr(cli, "read_control", lambda p: CONTROL)
    monkeypatch.setattr(cli, "read_window", lambda *a: (_ for _ in ()).throw(AssertionError()))
    assert cli.main(["--account-biz", scope()["biz"], "--plan"]) == 0
    assert '"stage_writes": 0' in capsys.readouterr().out


class FakeExporter:
    """State-machine mock, not an authored/rendered Word document."""
    def export_account(self, articles, path, **kwargs):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"synthetic-export-state-machine-only")


def ready_store(tmp_path):
    store = CaptureStore(tmp_path / "capture.sqlite", scope())
    for sample in [window(27, 20), window(20, 13), window(13, 8)]:
        store.ingest(sample)
    return store


def account():
    return dict(scope(), _articles=[], incomplete_article_ids=[])


def test_unverified_range_never_exports_word(tmp_path):
    store = ready_store(tmp_path)
    result = export_verified(store, account(), {"delivered_baseline_max_article_id": 10182}, None, None,
        exporter=FakeExporter(), verify=lambda *a: None)
    assert result["status"] == "coverage_incomplete" and not (tmp_path / "Word").exists()
    store.close()


def test_verified_word_is_idempotent_and_waits_for_render_review(tmp_path):
    store = ready_store(tmp_path)
    index, receipt = index_and_confirmation()
    args = (store, account(), {"delivered_baseline_max_article_id": 10182}, index, receipt)
    first = export_verified(*args, exporter=FakeExporter(), verify=lambda *a: None)
    second = export_verified(*args, exporter=FakeExporter(), verify=lambda *a: None)
    assert first["exported_count"] == 4 and second["reused"]
    assert first["status"] == "word_ready_for_render_review" and not first["delivery_verified"]
    assert not first["product_checkpoint_advanced"]
    assert load_scope(store.path) == scope()
    Path(first["word_path"]).write_bytes(b"user modification")
    with pytest.raises(ValueError, match="changed_or_missing"):
        export_verified(*args, exporter=FakeExporter(), verify=lambda *a: None)
    store.close()


def test_mutable_control_return_cannot_hide_pause_change(tmp_path):
    state = dict(CONTROL)
    def source():
        state["updated_at"] = "new-pause"
        return window(27)
    result = collect(scope(), tmp_path / "capture.sqlite", source, lambda: state,
        allow_local_stage_while_paused=True, max_polls=1, clock=lambda: 0)
    assert result["status"] == "control_changed" and result["coverage"]["records_captured"] == 0


def test_pause_between_validation_and_commit_rolls_back_snapshot(tmp_path, monkeypatch):
    state = dict(CONTROL)
    original = CaptureStore.ingest
    def update_control(self, snapshot, **kwargs):
        state["updated_at"] = "pause-before-commit"
        return original(self, snapshot, **kwargs)
    monkeypatch.setattr(CaptureStore, "ingest", update_control)
    result = collect(scope(), tmp_path / "capture.sqlite", lambda: window(27), lambda: dict(state),
        allow_local_stage_while_paused=True, max_polls=1, clock=lambda: 0)
    assert result["status"] == "control_changed" and result["coverage"]["records_captured"] == 0


def test_completed_baseline_has_zero_new_and_no_empty_word(tmp_path):
    store = ready_store(tmp_path)
    archived = [dict(r, id=i, source_url=r["url"], batch_id=None) for i, r in enumerate(store.records(), 1)]
    index, receipt = index_and_confirmation()
    result = export_verified(store, dict(account(), _articles=archived),
        {"delivered_baseline_max_article_id": 10182}, index, receipt, exporter=FakeExporter(), verify=lambda *a: None)
    assert result["status"] == "no_new_verified" and not (tmp_path / "Word").exists()
    store.close()


@pytest.mark.parametrize("unchanged", [True, False])
def test_known_incomplete_preview_cannot_be_reexported_as_complete(tmp_path, unchanged):
    store = ready_store(tmp_path)
    row = store.records()[0]
    old = dict(row, id=10172, source_url=row["url"], batch_id=None, status="parsed",
        body_chars=len(row["content_text"]) if unchanged else len(row["content_text"]) + 10,
        content_hash=row["content_hash"] if unchanged else "longer-old-preview-hash")
    target = dict(account(), _articles=[old], incomplete_article_ids=[10172])
    index, receipt = index_and_confirmation()
    result = export_verified(store, target, {"delivered_baseline_max_article_id":10182}, index, receipt,
        exporter=FakeExporter(), verify=lambda *a:None)
    assert result["status"] == "incomplete_baseline_not_recovered"
    assert result["unresolved_incomplete_article_ids"] == [10172]
    assert result["exported_count"] == 0 and not (tmp_path / "Word").exists()
    store.close()


def test_recovered_preview_is_not_suppressed_by_old_batch_membership(tmp_path):
    store = ready_store(tmp_path)
    row = store.records()[0]
    old = dict(row, id=10172, source_url=row["url"], batch_id=999, status="parsed",
        body_chars=1, content_hash="different-old-preview-hash")
    target = dict(account(), _articles=[old], incomplete_article_ids=[10172])
    index, receipt = index_and_confirmation()
    result = export_verified(store, target, {"delivered_baseline_max_article_id":10182}, index, receipt,
        exporter=FakeExporter(), verify=lambda *a:None)
    assert result["status"] == "word_ready_for_render_review" and result["exported_count"] == 4
    assert not result["delivery_verified"] and not result["product_checkpoint_advanced"]
    store.close()
