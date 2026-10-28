import copy

import pytest

from cache_capture_fixtures import index_and_confirmation, record, scope, url, window
from wechat_crawler.cache_capture_coverage import capture_coverage
from wechat_crawler.cache_capture_store import CaptureStore
from wechat_crawler.cache_capture_validation import digest


def populated(tmp_path, *, middle=True):
    store = CaptureStore(tmp_path / "capture.sqlite", scope())
    store.ingest(window(27, 20))
    if middle:
        store.ingest(window(20, 13))
    store.ingest(window(13, 8))
    return store


def test_endpoints_and_overlap_do_not_manufacture_full_index(tmp_path):
    store = populated(tmp_path)
    report = capture_coverage(store)
    assert report["newest_boundary_seen"] and report["old_boundary_seen"]
    assert report["boundary_overlap_connected"]
    assert not report["full_interval_verified"] and not report["boundary_chain_is_full_list_proof"]
    assert not report["zero_increment_claimed"]
    store.close()


def test_confirmed_index_plus_continuous_overlap_and_bodies_passes(tmp_path):
    store = populated(tmp_path)
    index, receipt = index_and_confirmation()
    report = capture_coverage(store, expected_index=index, confirmation=receipt)
    assert report["full_interval_verified"]
    assert report["expected_count"] == 4 and report["missing_urls"] == []
    assert not report["product_checkpoint_advanced"]
    store.close()


@pytest.mark.parametrize("receipt_change", [None, {"confirmed_by": "cache"}, {"index_sha256": "wrong"},
    {"scope_sha256": "wrong"}])
def test_cache_or_stale_receipt_cannot_authorize_complete_index(tmp_path, receipt_change):
    store = populated(tmp_path)
    index, receipt = index_and_confirmation()
    receipt = None if receipt_change is None else dict(receipt, **receipt_change)
    report = capture_coverage(store, expected_index=index, confirmation=receipt)
    assert not report["full_interval_verified"] and report["index_status"] == "index_confirmation_required"
    store.close()


def test_missing_middle_snapshot_fails_even_with_both_endpoints(tmp_path):
    store = populated(tmp_path, middle=False)
    index, receipt = index_and_confirmation()
    report = capture_coverage(store, expected_index=index, confirmation=receipt)
    assert report["old_boundary_seen"] and report["newest_boundary_seen"]
    assert not report["boundary_overlap_connected"] and not report["full_interval_verified"]
    store.close()


def test_missing_published_article_is_explicit(tmp_path):
    store = populated(tmp_path)
    index, receipt = index_and_confirmation((8, 13, 17, 20, 27))
    report = capture_coverage(store, expected_index=index, confirmation=receipt)
    assert report["missing_urls"] and not report["full_interval_verified"]
    store.close()


def test_observed_article_omitted_from_index_blocks_acceptance(tmp_path):
    store = populated(tmp_path)
    index, receipt = index_and_confirmation((8, 13, 27))
    report = capture_coverage(store, expected_index=index, confirmation=receipt)
    assert report["unexpected_urls"] and not report["full_interval_verified"]
    store.close()


@pytest.mark.parametrize("case", ["body_missing", "body_conflict", "duplicate_body", "source_read_error", "bounded_limit"])
def test_incomplete_body_conflicts_and_source_gaps_block_full_delivery(tmp_path, case):
    store = CaptureStore(tmp_path / "capture.sqlite", scope())
    first = window(27, 20)
    if case == "body_missing":
        first["records"][0]["body_ready"] = False
    if case == "duplicate_body":
        first["records"][0]["content_text"] = first["records"][1]["content_text"]
    if case == "source_read_error":
        first["read_errors"] = 1
    if case == "bounded_limit":
        first["scan_within_bounds"] = False
    store.ingest(first)
    store.ingest(window(20, 13))
    store.ingest(window(13, 8))
    if case == "body_conflict":
        changed = window(27)
        changed["records"][0]["content_text"] = "改过的正文"
        store.ingest(changed)
    index, receipt = index_and_confirmation()
    assert not capture_coverage(store, expected_index=index, confirmation=receipt)["full_interval_verified"]
    store.close()


@pytest.mark.parametrize("change", [{"source": "untrusted_cache_claim"}, {"biz": "OTHER"},
    {"until": "2026-10-28"}, {"articles": [{"url": url(27), "publication_day": "2026-10-1X"}]}])
def test_invalid_index_scope_and_days_fail_closed(tmp_path, change):
    store = populated(tmp_path)
    index, receipt = index_and_confirmation()
    index.update(change)
    receipt["index_sha256"] = digest(index)
    report = capture_coverage(store, expected_index=index, confirmation=receipt)
    assert report["index_status"] == "invalid_complete_index" and not report["full_interval_verified"]
    store.close()


def test_clean_recapture_recovers_a_prior_transient_source_gap(tmp_path):
    store = CaptureStore(tmp_path / "capture.sqlite", scope())
    bad = window(27, 20)
    bad["read_errors"] = 1
    store.ingest(bad)
    for frame in [window(27, 20), window(20, 13), window(13, 8)]:
        store.ingest(frame)
    index, receipt = index_and_confirmation()
    report = capture_coverage(store, expected_index=index, confirmation=receipt)
    assert report["unhealthy_windows"]  # Retain history, but do not poison verified recapture forever.
    assert report["full_interval_verified"]
    store.close()
