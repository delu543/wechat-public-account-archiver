"""One shared public-cache read per poll, isolated owned staging per account."""
from __future__ import annotations

import time
from pathlib import Path

from .cache_capture_coverage import capture_coverage
from .cache_capture_store import CaptureStore
from .cache_capture_validation import scope_value


def collect_accounts(scopes, stage_paths, source, read_control, *, seconds=600,
                     interval=2, max_polls=300, allow_local_stage_while_paused=False,
                     clock=time.monotonic, sleep=time.sleep, progress=None):
    scopes = [scope_value(value) for value in scopes]
    accounts = {value["biz"]: value for value in scopes}
    if not accounts or len(accounts) != len(scopes) or len(accounts) > 32 or set(stage_paths) != set(accounts):
        raise ValueError("invalid_exact_capture_accounts")
    stage_paths = {biz: Path(path).absolute() for biz, path in stage_paths.items()}
    if len({path.resolve() for path in stage_paths.values()}) != len(accounts):
        raise ValueError("duplicate_capture_stage_paths")
    if not 0 < seconds <= 1800 or not 0 <= interval <= 30 or not 1 <= max_polls <= 900:
        raise ValueError("invalid_capture_bounds")
    control = dict(read_control())
    if not isinstance(control.get("paused"), bool) or not control.get("updated_at"):
        return {"status": "blocked_control_unknown", "stage_writes": 0}
    if control["paused"] and not allow_local_stage_while_paused:
        return {"status": "paused", "stage_writes": 0, "master_pause_changed": False}
    stores, committed, polls, started, stop = {}, 0, 0, clock(), "duration_finished"
    last_source, latest_errors, attempts, source_reads = "not_read", None, 0, 0

    class ControlChanged(RuntimeError):
        pass

    def guard():
        if read_control() != control:
            raise ControlChanged()

    def summaries():
        return [{"biz": value["biz"], "account_name": value["account_name"],
                 "since": value["since"], "until": value["until"],
                 "status": "retained_public_cache" if value["biz"] in stores else "waiting_target_cache",
                 "coverage": capture_coverage(stores[value["biz"]]) if value["biz"] in stores else None,
                 "new_article_count": None, "missing_article_count": None,
                 "full_interval_verified": False, "zero_increment_claimed": False}
                for value in scopes]

    try:
        # Existing stages are resumed before I/O but do not create missing ones.
        for value in scopes:
            path = stage_paths[value["biz"]]
            if path.exists():
                stores[value["biz"]] = CaptureStore(path, value)
        for _ in range(max_polls):
            if clock() - started >= seconds:
                break
            guard()
            attempts += 1
            snapshot = source(scopes)
            source_reads += 1
            guard()
            polls += 1
            last_source, latest_errors = snapshot.get("status"), snapshot.get("read_errors")
            if last_source == "read":
                grouped = {biz: [] for biz in accounts}
                for record in snapshot.get("records", []):
                    if record.get("biz") not in grouped:
                        raise ValueError("foreign_capture_source_account")
                    grouped[record["biz"]].append(record)
                for biz, records in grouped.items():
                    scope = accounts[biz]
                    eligible = [record for record in records if (
                        scope["since"] <= str(record.get("publish_time") or "")[:10] <= scope["until"]
                        or record.get("url") in scope["old_urls"])]
                    if not eligible:
                        continue
                    guard()
                    if biz not in stores:
                        stores[biz] = CaptureStore(stage_paths[biz], scope)
                    result = stores[biz].ingest(dict(snapshot, records=eligible), before_commit=guard)
                    committed += int(result["status"] == "captured")
            if progress:
                progress({"polls": polls, "source_status": last_source,
                          "accounts": summaries(), "master_pause_changed": False})
            if polls < max_polls:
                sleep(min(interval, max(0, seconds - (clock() - started))))
        else:
            stop = "poll_limit_reached"
    except ControlChanged:
        stop = "control_changed"
    except KeyboardInterrupt:
        stop = "interrupted_resumable"
    finally:
        try:
            result_accounts = summaries()
        finally:
            for store in stores.values():
                store.close()
    return {"status": stop, "polls": polls, "shared_source_reads": source_reads,
            "source_read_attempts": attempts,
            "windows_committed_this_session": committed, "last_source_status": last_source,
            "last_source_read_errors": latest_errors, "accounts": result_accounts,
            "automation_paused": control["paused"], "master_pause_changed": False,
            "local_stage_exception_explicit": bool(allow_local_stage_while_paused),
            "product_writes": 0, "word_exports": 0, "product_checkpoint_advanced": False,
            "network_requests": 0, "wechat_input_events": 0, "private_key_reads": 0}
