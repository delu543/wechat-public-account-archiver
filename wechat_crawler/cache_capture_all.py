"""Single command to retain registered accounts while a human browses normally.

This is passive staging, not a complete-publication assertion or background
automation resume. Public account data never leave this computer.
"""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .cache_capture import account_scope, read_control
from .cache_capture_many import collect_accounts
from .cache_capture_retain import retain_into
from .cache_capture_store import CaptureStore
from .cache_capture_word import load_scope
from .main import load_config
from .passive_weekly_plan import make_plan
from .passive_weekly_sources import child_json


def read_shared(scopes):
    return child_json("wechat_crawler.cache_capture_window", {"scopes": scopes},
                      argument="--public-windows-only", timeout=30)


def prepare_scopes(config, until, root, selected=None):
    plan = make_plan(config["database"]["path"], config.get("passive_weekly", {}), until)
    accounts = plan["accounts"]
    if selected:
        accounts = [value for value in accounts if value["biz"] in selected]
        if {value["biz"] for value in accounts} != set(selected):
            raise ValueError("exact_enabled_subscriptions_required")
    scopes, paths = [], {}
    for account in accounts:
        scope = account_scope(config, account["biz"], until)
        token = hashlib.sha256(scope["biz"].encode()).hexdigest()[:12]
        path = root / f'{token}_{scope["since"]}_{until}.sqlite'
        if path.exists():
            stored = load_scope(path)
            if {k:v for k,v in stored.items() if k != "newest_url"} != {k:v for k,v in scope.items() if k != "newest_url"}:
                raise ValueError("existing_capture_scope_changed")
            scope = stored
        scopes.append(scope)
        paths[scope["biz"]] = path
    if not scopes:
        raise ValueError("no_enabled_subscriptions")
    return scopes, paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--until", default=datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat())
    parser.add_argument("--account-biz", action="append")
    parser.add_argument("--stage-root", required=True)
    parser.add_argument("--retain-stage", action="append", default=[],
                        help="Explicit owned old stage; read-only carry into the new batch, never a fresh-list proof.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--collect", action="store_true")
    parser.add_argument("--seconds", type=int, default=600)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--allow-local-stage-while-paused", action="store_true")
    args = parser.parse_args(argv)
    try:
        root = Path(args.stage_root).expanduser().absolute()
        if root.resolve() != root:
            raise ValueError("symlinked_stage_root")
        config = load_config(args.config)
        scopes, paths = prepare_scopes(config, args.until, root, args.account_biz)
        control = lambda: read_control(config["database"]["path"])
        if args.plan:
            result = {"status": "plan_only", "scopes": scopes, "control": control(),
                      "stage_paths": {biz:str(path) for biz,path in paths.items()}, "stage_writes": 0}
        elif args.check:
            snapshot = read_shared(scopes)
            result = {"status": snapshot["status"], "control": control(), "shared_source_reads": 1,
                "source_health": {k:v for k,v in snapshot.items() if k != "records"}, "stage_writes": 0,
                "accounts": [{"biz": scope["biz"], "account_name": scope["account_name"],
                    "observed_records": [{k:r[k] for k in ("url","title","publish_time","body_ready")}
                                         for r in snapshot.get("records",[]) if r["biz"] == scope["biz"]],
                    "new_article_count": None, "missing_article_count": None,
                    "full_interval_verified": False} for scope in scopes]}
        else:
            if not 2 <= args.interval <= 30:
                raise ValueError("bounded_interval_required")
            retained, initial = [], dict(control())
            if len(args.retain_stage) > 32:
                raise ValueError("retained_source_budget")
            if not initial.get("paused") or args.allow_local_stage_while_paused:
                def guard():
                    if control() != initial:
                        raise ValueError("control_changed")
                allowed = {scope["biz"]:scope for scope in scopes}
                for source in args.retain_stage:
                    guard()
                    old_scope = load_scope(source)
                    biz = old_scope["biz"]
                    if biz not in allowed or Path(source).resolve() == paths[biz].resolve():
                        raise ValueError("separate_registered_retained_source_required")
                    stage = CaptureStore(paths[biz],allowed[biz])
                    try:
                        retained.append(retain_into(source,stage,before_commit=guard))
                    finally:
                        stage.close()
            result = collect_accounts(scopes, paths, read_shared, control, seconds=args.seconds,
                interval=args.interval, max_polls=min(900, max(1, int(args.seconds / args.interval) + 1)),
                allow_local_stage_while_paused=args.allow_local_stage_while_paused,
                progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True))
            result["retained_prior_batches"] = retained
        result.update(product_writes=0, word_exports=0, network_requests=0, wechat_input_events=0,
                      private_key_reads=0, master_pause_changed=False, product_checkpoint_advanced=False)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in ("plan_only", "read", "poll_limit_reached", "duration_finished") else 2
    except Exception:
        print(json.dumps({"status": "blocked_passive_capture", "master_pause_changed": False,
                          "product_writes": 0, "word_exports": 0, "product_checkpoint_advanced": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
