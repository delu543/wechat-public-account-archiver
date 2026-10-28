"""Read-only old-stage preservation; synthetic evidence only."""
import hashlib

import pytest

from cache_capture_fixtures import record, scope, window
from wechat_crawler.cache_capture_coverage import capture_coverage
from wechat_crawler.cache_capture_retain import retain_into
from wechat_crawler.cache_capture_store import CaptureStore


def old_stage(tmp_path):
    path = tmp_path / "old.sqlite"
    store = CaptureStore(path, scope())
    store.ingest(window(27,20))
    store.ingest(window(8))
    store.close()
    return path


def test_extension_keeps_old_hash_and_disconnected_windows(tmp_path):
    source = old_stage(tmp_path)
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    store = CaptureStore(tmp_path / "new.sqlite", dict(scope(),until="2026-10-28",newest_url=None))
    first = retain_into(source,store)
    second = retain_into(source,store)
    assert first["added_records"] == 3 and second["added_records"] == 0
    assert len(store.windows()) == 2 and len(store.records()) == 3
    assert not capture_coverage(store)["boundary_overlap_connected"]
    assert not first["fresh_source_read"] and not first["full_interval_verified"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before
    store.close()


@pytest.mark.parametrize("change", [{"since":"2026-10-07"},{"until":"2026-10-26"},{"account_name":"another"}])
def test_incompatible_or_narrower_extension_is_rejected(tmp_path,change):
    source = old_stage(tmp_path)
    store = CaptureStore(tmp_path / "new.sqlite",dict(scope(),**change))
    with pytest.raises(ValueError,match="scope_mismatch"):
        retain_into(source,store)
    assert store.records() == []
    store.close()


def test_conflicting_prior_versions_are_not_silently_resolved(tmp_path):
    source = old_stage(tmp_path)
    prior = CaptureStore(source,scope())
    changed = window(27)
    changed["records"] = [dict(record(27),content_text="different full body")]
    prior.ingest(changed)
    prior.close()
    store = CaptureStore(tmp_path / "new.sqlite",dict(scope(),until="2026-10-28"))
    with pytest.raises(ValueError,match="conflicts_unresolved"):
        retain_into(source,store)
    assert store.records() == []
    store.close()


def test_pause_guard_rolls_back_destination_and_leaves_source(tmp_path):
    source = old_stage(tmp_path)
    before = source.read_bytes()
    store = CaptureStore(tmp_path / "new.sqlite",dict(scope(),until="2026-10-28"))
    def pause():
        raise RuntimeError("control changed")
    with pytest.raises(RuntimeError,match="control changed"):
        retain_into(source,store,before_commit=pause)
    assert store.records() == [] and store.windows() == []
    assert source.read_bytes() == before
    store.close()
