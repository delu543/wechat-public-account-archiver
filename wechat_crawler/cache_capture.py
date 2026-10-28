"""python -m wechat_crawler.cache_capture: passive, bounded public staging."""
from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from .biz_push_reader import public_identity
from .cache_capture_session import collect
from .cache_capture_validation import scope_value
from .main import load_config
from .passive_weekly_plan import make_plan, publication_day, readonly_database
from .passive_weekly_sources import child_json


def read_window(scope):
    return child_json("wechat_crawler.cache_capture_window", {"scope": scope},
                      argument="--public-window-only", timeout=30)


def account_scope(config, biz, until, *, since=None, newest_url=None):
    plan = make_plan(config["database"]["path"], config.get("passive_weekly", {}), until, biz=biz)
    if len(plan["accounts"]) != 1:
        raise ValueError("exact_single_subscription_required")
    account = plan["accounts"][0]
    start = since or account["latest_archived_beijing"] or account["since"]
    gap = account.get("recovery_gap")
    if gap and gap["since"] < start:
        start = gap["since"]
    incomplete = set(account.get("incomplete_article_ids", []))
    for row in account["_articles"]:
        if row["id"] in incomplete and (day := publication_day(row["publish_time"])):
            start = min(start, day)
    earlier = [(publication_day(r["publish_time"]), r) for r in account["_articles"]
               if r["status"] == "parsed" and publication_day(r["publish_time"])
               and publication_day(r["publish_time"]) <= start and public_identity(r["source_url"], biz)]
    old_day = max((d for d, _ in earlier), default=None)
    old_urls = [r["source_url"] for d, r in earlier if d == old_day]
    scope = scope_value(dict(account, since=start, until=until, newest_url=newest_url, old_urls=old_urls))
    return scope


def read_control(database):
    with readonly_database(database) as conn:
        row = conn.execute("SELECT paused,updated_at FROM automation_control WHERE id=1").fetchone()
    return {"paused": bool(row[0]), "updated_at": row[1]} if row else {"paused": None, "updated_at": None}


def bounded_json(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("invalid_public_index_file")
    return json.loads(path.read_text(encoding="utf-8"))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--account-biz", required=True)
    parser.add_argument("--since")
    parser.add_argument("--until", default=datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat())
    parser.add_argument("--newest-url", help="Exact already-known latest public article, not a client signed URL.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--collect", action="store_true")
    parser.add_argument("--stage", help="New or matching-owned isolated .sqlite; never the product database.")
    parser.add_argument("--seconds", type=int, default=900)
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--allow-local-stage-while-paused", action="store_true",
                        help="Requires human authorization for this public-cache-only staging run; never resumes automation.")
    parser.add_argument("--verified-index")
    parser.add_argument("--index-confirmation")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        scope = account_scope(config, args.account_biz, args.until, since=args.since, newest_url=args.newest_url)
        control = lambda: read_control(config["database"]["path"])
        if args.plan:
            result = {"status": "plan_only", "scope": scope, "control": control(), "product_writes": 0, "stage_writes": 0}
        elif args.check:
            snapshot = read_window(scope)
            result = {"status": snapshot["status"], "scope": scope, "control": control(),
                      "records": [{k: r[k] for k in ("url", "title", "publish_time", "body_ready")} for r in snapshot.get("records", [])],
                      "source_reason": snapshot.get("reason"), "read_errors": snapshot.get("read_errors"),
                      "scan_within_bounds": snapshot.get("scan_within_bounds"),
                      "full_interval_verified": False, "stage_writes": 0, "product_writes": 0}
        else:
            if not args.stage or not 2 <= args.interval <= 30:
                raise ValueError("stage_and_bounded_interval_required")
            stage, product = Path(args.stage).expanduser().absolute(), Path(config["database"]["path"]).expanduser().resolve()
            if stage.resolve() == product:
                raise ValueError("product_cannot_be_capture_stage")
            index = bounded_json(args.verified_index) if args.verified_index else None
            receipt = bounded_json(args.index_confirmation) if args.index_confirmation else None
            result = collect(scope, stage, lambda: read_window(scope), control, seconds=args.seconds,
                interval=args.interval, max_polls=min(900, max(1, int(args.seconds / args.interval) + 1)),
                allow_local_stage_while_paused=args.allow_local_stage_while_paused,
                progress=lambda event: print(json.dumps(event, ensure_ascii=False), flush=True),
                expected_index=index, confirmation=receipt)
        result.update(network_requests=0, wechat_input_events=0, private_key_reads=0, master_pause_changed=False)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in ("plan_only", "read", "verified_interval_captured") else 2
    except Exception:
        print(json.dumps({"status": "blocked_capture_local_validation", "network_requests": 0,
                          "wechat_input_events": 0, "product_writes": 0, "master_pause_changed": False}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
