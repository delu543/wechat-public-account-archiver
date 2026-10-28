"""Read-only consolidated acceptance and minimal human-action report.

Loaded cache entries are evidence, not a full publication index. This command
never refreshes WeChat, fetches pages, exports Word or advances a checkpoint.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from .biz_push_reader import public_identity, permanent_url
from .cache_capture_all import prepare_scopes
from .cache_capture_store import APP_ID
from .main import load_config
from .passive_weekly_plan import make_plan, readonly_database


def review_account(scope, account, records, settings):
    base = {"account_name": scope["account_name"], "biz": scope["biz"],
            "since": scope["since"], "until": scope["until"],
            "full_interval_verified": False, "zero_increment_claimed": False}
    if records is None:
        return dict(base, source_status="not_observed", retained=None, new_candidates=None,
                    old_boundary_seen=None, articles=[], unresolved_previews=[],
                    issues=["specific_account_source_missing"])
    frozen = settings["delivered_baseline_max_article_id"]
    incomplete = set(account.get("incomplete_article_ids", []))
    delivered = [r for r in account["_articles"] if r["id"] not in incomplete
                 and (r.get("batch_id") is not None or r["id"] <= frozen)]
    old_keys = {public_identity(r["source_url"], scope["biz"]) for r in delivered}
    old_hashes = {r["content_hash"] for r in delivered if r.get("content_hash")}
    keyed = {}
    for record in records:
        key = public_identity(record.get("url"), scope["biz"])
        if not key or record.get("wechat_id") != scope["wechat_id"] or key in keyed:
            raise ValueError("invalid_staged_identity")
        keyed[key] = record
    unresolved = []
    for old in account["_articles"]:
        if old["id"] not in incomplete:
            continue
        key = public_identity(old["source_url"], scope["biz"])
        record = keyed.get(key)
        if (not record or record.get("body_ready") is not True
            or record.get("content_hash") == old.get("content_hash")
            or len(record["content_text"]) <= (old.get("body_chars") or 0)):
            unresolved.append({"article_id": old["id"], "title": old["title"],
                               "url": permanent_url(key), "reason": "known_preview_not_recovered"})
    preview_keys = {public_identity(r["url"]) for r in unresolved}
    articles = []
    for key, record in keyed.items():
        day = record["publish_time"][:10]
        in_range = scope["since"] <= day <= scope["until"]
        overlap = key in old_keys or record.get("content_hash") in old_hashes
        status = ("old_boundary_reference" if not in_range else "already_delivered" if overlap
                  else "known_preview_not_recovered" if key in preview_keys
                  else "body_unverified" if record.get("body_ready") is not True else "observed_new")
        articles.append({"url": permanent_url(key), "title": record["title"],
                         "publication_day": day, "status": status,
                         "body_chars": len(record["content_text"])})
    articles.sort(key=lambda r: (r["publication_day"], r["url"]), reverse=True)
    counts = Counter(r["status"] for r in articles)
    old = {public_identity(u, scope["biz"]) for u in scope["old_urls"]}
    old_seen = bool(old and old <= set(keyed))
    issues = []
    if not old_seen:
        issues.append("old_boundary_not_observed")
    if unresolved:
        issues.append("known_preview_not_recovered")
    if counts["body_unverified"]:
        issues.append("body_unverified")
    return dict(base, source_status="retained_public_cache", retained=len(records),
                new_candidates=counts["observed_new"] + counts["known_preview_not_recovered"] + counts["body_unverified"],
                observed_new_bodies=counts["observed_new"], old_boundary_seen=old_seen,
                status_counts=dict(counts), articles=articles, unresolved_previews=unresolved, issues=issues)


def review_stages(config, until, root):
    root = Path(root).expanduser().absolute()
    if root.resolve() != root:
        raise ValueError("symlinked_stage_root")
    scopes, paths = prepare_scopes(config, until, root)
    plan = make_plan(config["database"]["path"], config["passive_weekly"], until)
    accounts = {a["biz"]: a for a in plan["accounts"]}
    reviews = []
    for scope in scopes:
        path, records = paths[scope["biz"]], None
        if path.exists():
            with readonly_database(path) as conn:
                if conn.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
                    raise ValueError("owned_stage_required")
                rows = conn.execute("SELECT data FROM articles LIMIT 20001").fetchall()
                if len(rows) > 20000 or sum(len(r[0]) for r in rows) > 128 * 1024 * 1024:
                    raise ValueError("staged_review_budget")
                records = [json.loads(r[0]) for r in rows]
        reviews.append(review_account(scope, accounts[scope["biz"]], records, config["passive_weekly"]))
    return {"status": "review_requires_catalog", "until": until, "accounts": reviews,
            "retained_total": sum(a["retained"] or 0 for a in reviews),
            "new_candidates_total": sum(a["new_candidates"] or 0 for a in reviews),
            "specific_source_gaps": [a["account_name"] for a in reviews if a["source_status"] == "not_observed"],
            "specific_body_gaps": [dict(r, account_name=a["account_name"]) for a in reviews for r in a["unresolved_previews"]],
            "catalog_requirement": "independent_paginated_publication_index",
            "loaded_cache_is_full_history": False, "default_action_is_refresh_all_homepages": False,
            "full_interval_verified": False, "word_exports": 0, "product_writes": 0,
            "network_requests": 0, "wechat_input_events": 0, "private_key_reads": 0,
            "automation_paused": plan["paused"], "pause_updated_at": plan["pause_updated_at"],
            "master_pause_changed": False, "product_checkpoint_advanced": False}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--until", required=True)
    parser.add_argument("--stage-root", required=True)
    parser.add_argument("--output", help="New metadata-only JSON report; refuses to overwrite.")
    args = parser.parse_args(argv)
    try:
        report = review_stages(load_config(args.config), args.until, args.stage_root)
        if args.output:
            path = Path(args.output).expanduser().absolute()
            if path.resolve() != path or not path.parent.is_dir():
                raise ValueError("separate_report_path_required")
            with path.open("x", encoding="utf-8") as handle:
                path.chmod(0o600)
                json.dump(report, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
        print(json.dumps({k:v for k,v in report.items() if k != "accounts"}, ensure_ascii=False))
        return 0  # A completed review is not a completed archive/export.
    except Exception:
        print(json.dumps({"status": "blocked_readonly_review", "product_writes": 0, "word_exports": 0}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
