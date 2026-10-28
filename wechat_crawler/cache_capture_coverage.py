"""Boundary overlap is necessary, never a substitute for a complete index."""
from __future__ import annotations

from datetime import date
from .cache_capture_index import verified_pagination
from .cache_capture_validation import canonical_url, digest


def capture_coverage(store, *, expected_index=None, confirmation=None):
    scope = store.scope
    records = {r["url"]: r for r in store.records()}
    windows = store.windows()
    observed = set(records)
    old = set(scope["old_urls"])
    newest = scope.get("newest_url")
    unhealthy = [w["id"] for w in windows if w["health"].get("read_errors")
                 or w["health"].get("unsupported_values") or w["health"].get("metadata_conflicts")
                 or w["health"].get("scan_within_bounds") is not True]
    healthy_windows = [w for w in windows if w["id"] not in unhealthy]
    healthy_observed = {u for w in healthy_windows for u in w["keys"]}
    connected = {newest} & healthy_observed if newest else set()
    previous = None
    while previous != connected:
        previous = set(connected)
        for window in healthy_windows:
            if connected.intersection(window["keys"]):
                connected.update(window["keys"])
    conflicting = store.conflicts()
    in_range = {u for u, r in records.items() if scope["since"] <= r["publish_time"][:10] <= scope["until"]}
    index_status, expected, machine_verified = "complete_index_required", {}, False
    if expected_index is not None:
        try:
            if (not isinstance(expected_index, dict) or expected_index.get("biz") != scope["biz"] or expected_index.get("since") != scope["since"]
                or expected_index.get("until") != scope["until"]
                or expected_index.get("source") not in ("native_history_index", "official_export", "human_verified_homepage_index")
                or expected_index.get("all_publications_for_interval") is not True
                or not isinstance(expected_index.get("articles"), list) or len(expected_index["articles"]) > 20000):
                raise ValueError()
            for item in expected_index["articles"]:
                url = canonical_url(item["url"], scope["biz"])
                day = date.fromisoformat(str(item["publication_day"])).isoformat()
                if url in expected or not scope["since"] <= day <= scope["until"]:
                    raise ValueError()
                expected[url] = day
            machine_verified = verified_pagination(expected_index, scope, expected)
            approved = (isinstance(confirmation, dict) and confirmation.get("confirmed_by") == "user"
                        and confirmation.get("index_sha256") == digest(expected_index)
                        and confirmation.get("scope_sha256") == digest(scope))
            index_status = ("verified_pagination_index" if machine_verified else
                            "confirmed_exact_index" if approved else "index_confirmation_required")
        except (ValueError, TypeError, KeyError):
            index_status, expected = "invalid_complete_index", {}
    missing = sorted(set(expected) - observed)
    accepted_index = index_status in ("confirmed_exact_index", "verified_pagination_index")
    extra = sorted(in_range - set(expected)) if accepted_index else []
    wrong_dates = sorted(u for u, day in expected.items() if u in records and records[u]["publish_time"][:10] != day)
    missing_bodies = sorted(u for u in expected if u in records and not records[u]["body_ready"])
    body_hashes = {}
    for url in set(expected) & observed:
        value = records[url]["content_hash"]
        body_hashes.setdefault(value, []).append(url)
    duplicate_bodies = [urls for value, urls in body_hashes.items() if value and len(urls) > 1]
    latest_seen = bool(newest and newest in observed)
    old_seen = bool(old and old <= observed)
    overlap_chain = bool(latest_seen and old_seen and old <= connected)
    unrecovered = sorted(set(expected) - healthy_observed)
    # An independent complete paginated catalog already proves the interval.
    # Its exact body reconciliation does not need another human anchor/receipt
    # or an overlap chain manufactured from unrelated cache windows.
    complete = bool(accepted_index and (overlap_chain or machine_verified) and not unrecovered
                    and not conflicting and not missing and not extra and not wrong_dates
                    and not missing_bodies and not duplicate_bodies)
    return {"records_captured": len(records), "windows_captured": len(windows),
            "in_requested_interval": len(in_range), "newest_boundary_seen": latest_seen,
            "old_boundary_seen": old_seen, "boundary_overlap_connected": overlap_chain,
            "boundary_chain_is_full_list_proof": False, "index_status": index_status,
            "machine_pagination_verified": machine_verified,
            "index_sha256": digest(expected_index) if expected_index is not None else None,
            "scope_sha256": digest(scope), "expected_count": len(expected) if expected_index is not None else None,
            "missing_urls": missing, "unexpected_urls": extra, "publication_conflicts": wrong_dates,
            "missing_bodies": missing_bodies, "conflicting_versions": conflicting,
            "duplicate_body_groups": duplicate_bodies, "unhealthy_windows": unhealthy,
            "unrecovered_urls": unrecovered,
            "full_interval_verified": complete, "zero_increment_claimed": False,
            "product_checkpoint_advanced": False}
