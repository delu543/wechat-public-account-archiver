"""Finite passive capture session; local staging never unpauses automation."""
from __future__ import annotations

import time

from .cache_capture_coverage import capture_coverage
from .cache_capture_store import CaptureStore


def collect(scope, stage_path, read_snapshot, read_control, *, seconds=900, interval=2,
            max_polls=450, allow_local_stage_while_paused=False, clock=time.monotonic,
            sleep=time.sleep, progress=None, expected_index=None, confirmation=None):
    if not 0 < seconds <= 1800 or not 0 <= interval <= 30 or not 1 <= max_polls <= 900:
        raise ValueError("invalid_capture_bounds")
    control = dict(read_control())
    if not isinstance(control.get("paused"), bool) or not control.get("updated_at"):
        return {"status": "blocked_control_unknown", "stage_writes": 0}
    if control["paused"] and not allow_local_stage_while_paused:
        return {"status": "paused", "stage_writes": 0, "master_pause_changed": False}
    started, polls, events, stop = clock(), 0, 0, "duration_finished"
    store = CaptureStore(stage_path, scope)
    last_source = "not_read"
    class ControlChanged(RuntimeError):
        pass
    def commit_guard():
        if read_control() != control:
            raise ControlChanged()
    try:
        for _ in range(max_polls):
            if clock() - started >= seconds:
                break
            current = read_control()
            if current != control:
                stop = "control_changed"
                break
            snapshot = read_snapshot()
            # A pause/control change during I/O must stop BEFORE a stage commit.
            if read_control() != control:
                stop = "control_changed"
                break
            polls += 1
            last_source = snapshot.get("status")
            try:
                event = store.ingest(snapshot, before_commit=commit_guard)
            except ControlChanged:
                stop = "control_changed"
                break
            events += event["status"] == "captured"
            coverage = capture_coverage(store, expected_index=expected_index, confirmation=confirmation)
            update = {"status": event["status"], "polls": polls, "added": event["added"],
                      "coverage": coverage, "network_requests": 0, "wechat_input_events": 0}
            if progress:
                progress(update)
            if coverage["full_interval_verified"]:
                stop = "verified_interval_captured"
                break
            if polls < max_polls:
                sleep(min(interval, max(0, seconds - (clock() - started))))
        else:
            stop = "poll_limit_reached"
    except KeyboardInterrupt:
        stop = "interrupted_resumable"
    finally:
        coverage = capture_coverage(store, expected_index=expected_index, confirmation=confirmation)
        store.close()
    return {"status": stop, "polls": polls, "windows_committed_this_session": events,
            "last_source_status": last_source, "coverage": coverage, "stage_path": str(stage_path),
            "local_stage_exception_explicit": bool(allow_local_stage_while_paused),
            "automation_paused": control["paused"], "master_pause_changed": False,
            "product_writes": 0, "network_requests": 0, "wechat_input_events": 0,
            "word_exports": 0, "private_key_reads": 0}
