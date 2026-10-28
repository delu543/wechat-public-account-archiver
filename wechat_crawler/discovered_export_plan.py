"""Prepare explicitly accepted discovered-only output without product writes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from .cache_capture_all import prepare_scopes
from .cache_capture_review import review_account
from .cache_capture_store import APP_ID
from .cache_capture_word import load_scope
from .cache_capture_validation import digest
from .crawler.dedup import normalized_text_hash
from .main import load_config
from .passive_weekly_plan import make_plan, readonly_database


def select_discovered(scope, account, records, settings, *, conflicts=(), delivered=None):
    review = review_account(scope, account, records, settings)
    keyed = {r["url"]: r for r in records}
    selected, hashes, duplicates, overlap = [], {}, [], 0
    delivered = delivered or {"urls": {}, "hashes": set()}
    gaps = list(review["unresolved_previews"])
    for key in ("recovery_gap", "pending_range"):
        if account.get(key):
            gaps.append({"reason": "historical_interval_unverified", "range": account[key]})
    for item in sorted(review["articles"], key=lambda r: (r["publication_day"], r["url"])):
        if item["status"] != "observed_new":
            if item["status"] == "body_unverified":
                gaps.append(dict(item, reason="body_unverified"))
            continue
        row = keyed[item["url"]]
        if row["url"] in conflicts:
            gaps.append(dict(item, reason="conflicting_versions"))
            continue
        actual_hash = normalized_text_hash(row["content_text"])
        if not row["body_ready"] or not actual_hash or actual_hash != row["content_hash"]:
            raise ValueError("staged_body_changed")
        previous = delivered["urls"].get(row["url"])
        if previous and previous != actual_hash:
            gaps.append(dict(item, reason="previously_delivered_body_changed"))
            continue
        if previous or actual_hash in delivered["hashes"]:
            overlap += 1
            continue
        if actual_hash in hashes:
            duplicates.append({"url": row["url"], "title": row["title"], "same_body_as": hashes[actual_hash]})
            continue
        hashes[actual_hash] = row["url"]
        selected.append({k: row[k] for k in ("url", "biz", "wechat_id", "title", "title_kind",
                          "publish_time", "content_text", "content_hash")})
    return {"account_name": scope["account_name"], "biz": scope["biz"], "wechat_id": scope["wechat_id"],
            "since": scope["since"], "until": scope["until"], "articles": selected,
            "gaps": gaps, "duplicate_bodies": duplicates, "retained_count": len(records),
            "candidate_count": review["new_candidates"], "export_count": len(selected),
            "delivered_overlap_count": overlap, "latest_exported_day": delivered.get("latest_exported_day"),
            "full_interval_verified": False, "product_checkpoint_advanced": False}


def build_plan(config, stage_root, until, excluded, *, delivered=None, allow_empty=False, selected=None,
               retained_paths=None):
    scopes, paths = prepare_scopes(config, until, Path(stage_root).absolute(), selected)
    current = make_plan(config["database"]["path"], config["passive_weekly"], until)
    accounts = {a["biz"]: a for a in current["accounts"]}
    output = []
    for scope in scopes:
        if scope["biz"] in excluded:
            continue
        path = (retained_paths or {}).get(scope["biz"], paths[scope["biz"]])
        if path.exists():
            stored = load_scope(path)
            ignored = {"until", "newest_url"}
            if (stored["until"] > scope["until"] or
                {k:v for k,v in stored.items() if k not in ignored} !=
                {k:v for k,v in scope.items() if k not in ignored}):
                raise ValueError("retained_capture_scope_mismatch")
        if not path.exists() and allow_empty:
            output.append(dict(scope, articles=[], gaps=[{"reason":"specific_account_source_missing"}],
                export_count=0, retained_count=None, candidate_count=None, delivered_overlap_count=0,
                latest_exported_day=(delivered or {}).get(scope["biz"], {}).get("latest_exported_day"),
                duplicate_bodies=[], full_interval_verified=False, product_checkpoint_advanced=False))
            continue
        with readonly_database(path) as conn:
            if conn.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
                raise ValueError("owned_stage_required")
            raw_records = conn.execute("SELECT data FROM articles LIMIT 20001").fetchall()
            if len(raw_records) > 20000 or sum(len(r[0]) for r in raw_records) > 128 * 1024 * 1024:
                raise ValueError("staged_export_budget")
            records = [json.loads(r[0]) for r in raw_records]
            versions = {}
            count, size = conn.execute("SELECT count(*),coalesce(sum(length(data)),0) FROM variants").fetchone()
            if count > 40000 or size > 128 * 1024 * 1024:
                raise ValueError("staged_variant_budget")
            for url, raw in conn.execute("SELECT url,data FROM variants WHERE body_ready=1"):
                row = json.loads(raw)
                versions.setdefault(url, set()).add((row["content_hash"], row["publish_time"], row["title"]))
        conflicts = {u for u, values in versions.items() if len(values) > 1}
        output.append(select_discovered(scope, accounts[scope["biz"]], records,
            config["passive_weekly"], conflicts=conflicts, delivered=(delivered or {}).get(scope["biz"])))
        output[-1]["retained_until"] = stored["until"]
    if not output or (not allow_empty and any(not a["articles"] for a in output)):
        raise ValueError("empty_account_requires_explicit_reporting")
    result = {"schema": 1, "mode": "discovered_increment", "until": until,
              "accounts": output, "excluded_biz": sorted(excluded), "accepted_unknown_coverage": True,
              "automation_paused": current["paused"], "pause_updated_at": current["pause_updated_at"],
              "full_interval_verified": False, "product_checkpoint_advanced": False,
              "product_writes": 0, "network_requests": 0, "wechat_input_events": 0}
    result["fingerprint"] = digest(result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--stage-root", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--exclude-biz", action="append", default=[])
    parser.add_argument("--accept-discovered-coverage", action="store_true", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    result = build_plan(load_config(args.config), args.stage_root, args.until, set(args.exclude_biz))
    path = Path(args.output).absolute()
    if path.resolve() != path or not path.parent.is_dir():
        raise ValueError("owned_output_required")
    with path.open("x", encoding="utf-8") as handle:
        path.chmod(0o600)
        json.dump(result, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"accounts": [{"account_name": a["account_name"], "export_count": a["export_count"],
        "known_gaps": len(a["gaps"]), "body_duplicates": len(a["duplicate_bodies"])} for a in result["accounts"]],
        "fingerprint": result["fingerprint"], "product_writes": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
