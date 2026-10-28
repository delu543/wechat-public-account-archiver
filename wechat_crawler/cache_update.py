"""Repeatable passive-cache check, bounded collection and discovered Word delivery.

Default is a read-only check of retained data. No UI, network, client credentials,
private database decryption, product writes or persistent pause changes.
"""
from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re

from .cache_capture import read_control
from .cache_capture_all import prepare_scopes, read_shared
from .cache_capture_many import collect_accounts
from .cache_capture_retain import retain_into
from .cache_capture_store import CaptureStore
from .cache_capture_word import load_scope
from .cache_delivery_receipts import delivered_members
from .cache_update_export import (batches, confirm_delivery, export_batch, output_lock,
                                  pending_batches, safe_path)
from .discovered_export_plan import build_plan


def add_arguments(parser):
    parser.add_argument("--until", help="Inclusive publication end date; default local today.")
    parser.add_argument("--account-biz", action="append", help="Exact registered account; repeatable.")
    parser.add_argument("--exclude-biz", action="append", default=[])
    parser.add_argument("--stage-root", help="Owned staging directory; defaults to output/cache-updates/stages.")
    parser.add_argument("--check", action="store_true", help="Default; inspect retained data/receipts, no writes or live read.")
    parser.add_argument("--collect", action="store_true", help="Bounded passive public-cache read while a human browses normally.")
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--allow-local-stage-while-paused", action="store_true",
                        help="Explicit one-run local capture exception; never resumes background jobs.")
    parser.add_argument("--export", action="store_true", help="Export available retained text, not an assertion of full coverage.")
    parser.add_argument("--accept-discovered", action="store_true", help="Accept labelled incomplete discoveries; retain gaps.")
    parser.add_argument("--confirm-delivery", help="Owned batch folder, only after actual rendered-page review.")
    parser.add_argument("--render-reviewed", action="store_true", help="Attest that rendered pages were actually reviewed.")


def paths_and_receipts(config, args):
    settings = config.get("cache_update", {})
    output = safe_path(Path(config["output"]["path"]) / "cache-updates")
    stage = safe_path(args.stage_root or settings.get("stage_root") or output / "stages")
    deliveries = output / "deliveries"
    receipts = list(settings.get("delivery_receipts", []))
    receipts.extend(str(p.parent / "delivery-manifest.json") for p in batches(deliveries)
                    if (p.parent / "verification.json").exists())
    return settings, stage, deliveries, delivered_members(receipts)


def retained_paths(scopes, paths, roots):
    """Newest compatible accumulated stage per exact account; never a fresh read."""
    found = dict(paths)
    seen, candidates = set(), []
    for root in roots:
        for path in sorted(safe_path(root).glob("*.sqlite")):
            if path not in seen:
                seen.add(path)
                candidates.append(path)
                if len(candidates) > 2048:
                    raise ValueError("retained_stage_budget")
    by_biz = {s["biz"]:s for s in scopes}
    chosen = {}
    for path in candidates:
        value = load_scope(path)
        scope = by_biz.get(value["biz"])
        if not scope:
            continue
        ignore = {"until", "newest_url"}
        if (value["until"] > scope["until"] or
            {k:v for k,v in value.items() if k not in ignore} != {k:v for k,v in scope.items() if k not in ignore}):
            continue
        biz = value["biz"]
        if biz not in chosen or value["until"] > chosen[biz][0]:
            chosen[biz] = (value["until"], path)
    for biz, (_, path) in chosen.items():
        if not found[biz].exists():
            found[biz] = path
    return found


def report(plan, pending):
    return {"status": "retained_cache_checked", "until": plan["until"],
        "automation_paused": plan["automation_paused"], "pending_batches": [str(p) for p in pending],
        "excluded_biz": plan["excluded_biz"], "accounts": [{
            **{k:a.get(k) for k in ("account_name", "biz", "since", "until", "retained_until", "retained_count",
                                   "candidate_count", "export_count", "delivered_overlap_count", "latest_exported_day", "gaps")},
            "suggested_refresh_through": a.get("latest_exported_day") or a["since"],
            "older_gap_start": a["since"] if a["gaps"] else None,
            "next_action": "export_retained" if a["articles"] else "refresh_if_new_publications_needed",
            "full_interval_verified": False, "zero_increment_claimed": False} for a in plan["accounts"]],
        "product_writes": 0, "product_checkpoint_advanced": False, "master_pause_changed": False,
        "network_requests": 0, "wechat_input_events": 0, "private_key_reads": 0}


def run(args, config, *, source=read_shared):
    if args.check and (args.collect or args.export or args.confirm_delivery):
        raise ValueError("check_is_read_only")
    if args.export and not args.accept_discovered:
        raise ValueError("discovered_acceptance_required")
    if args.confirm_delivery and (args.collect or args.export):
        raise ValueError("review_is_separate_from_capture")
    settings, stage, deliveries, delivered = paths_and_receipts(config, args)
    if args.confirm_delivery:
        folder = safe_path(args.confirm_delivery)
        if folder.parent != deliveries or folder not in pending_batches(deliveries) + [p.parent for p in batches(deliveries)]:
            raise ValueError("owned_delivery_required")
        with output_lock(deliveries):
            return confirm_delivery(folder, render_reviewed=args.render_reviewed)
    until = args.until or date.today().isoformat()
    date.fromisoformat(until)
    excluded = set(settings.get("excluded_biz", [])) | set(args.exclude_biz)
    scopes, paths = prepare_scopes(config, until, stage, args.account_biz)
    if args.account_biz and set(args.account_biz) & excluded:
        raise ValueError("selected_account_is_excluded_in_settings")
    scopes = [s for s in scopes if s["biz"] not in excluded]
    paths = {s["biz"]: paths[s["biz"]] for s in scopes}
    prior = retained_paths(scopes, paths, [stage] + list(settings.get("prior_stage_roots", [])))
    control = lambda: read_control(config["database"]["path"])
    initial = dict(control())
    def guard():
        if control() != initial:
            raise ValueError("control_changed")
    capture = None
    if args.collect:
        if not 0 < args.seconds <= 1800 or not 2 <= args.interval <= 30:
            raise ValueError("bounded_capture_required")
        if initial["paused"] and not args.allow_local_stage_while_paused:
            return {"status": "paused", "product_writes": 0, "master_pause_changed": False, "stage_writes": 0}
        with output_lock(deliveries):
            for scope in scopes:
                old, new = prior[scope["biz"]], paths[scope["biz"]]
                if old.exists() and old != new:
                    guard()
                    store = CaptureStore(new, scope)
                    try:
                        retain_into(old, store, before_commit=guard)
                    finally:
                        store.close()
            capture = collect_accounts(scopes, paths, source, control, seconds=args.seconds,
                interval=args.interval, max_polls=min(900, max(1, int(args.seconds / args.interval))),
                allow_local_stage_while_paused=args.allow_local_stage_while_paused)
        if capture["status"] in ("control_changed", "interrupted_resumable", "blocked_control_unknown"):
            return capture
        prior = paths
    def plan_now():
        return build_plan(config, stage, until, excluded, delivered=delivered, allow_empty=True,
                          selected=args.account_biz, retained_paths=prior)
    plan = plan_now()
    result = report(plan, pending_batches(deliveries))
    if capture:
        result["capture"] = capture
    if args.export:
        with output_lock(deliveries):
            # Recheck receipts under the same lock used by review/other runs.
            _, _, _, delivered = paths_and_receipts(config, args)
            plan = plan_now()
            guard()
            result = report(plan, pending_batches(deliveries)) | {"export": export_batch(plan, deliveries, before_write=guard)}
            if capture:
                result["capture"] = capture
    return result


def command(args, config):
    try:
        result = run(args, config)
    except Exception as exc:
        reason = str(exc)
        result = {"status": "blocked_cache_update", "reason": reason if re.fullmatch(r"[a-z_]+", reason) else "validation_failed",
                  "product_writes": 0, "master_pause_changed": False}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] in ("blocked_cache_update", "paused", "control_changed", "blocked_control_unknown") else 0
