"""Verified-index-only Word preparation from owned public staging data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sqlite3

from .biz_push_reader import public_identity
from .cache_capture_coverage import capture_coverage
from .cache_capture_store import APP_ID, CaptureStore
from .cache_capture_validation import digest, scope_value
from .crawler.normalizer import safe_path_part
from .incremental_word import IncrementalWordExporter
from .main import load_config
from .passive_weekly import verify_word
from .passive_weekly_plan import make_plan


def load_scope(path):
    path = Path(path).absolute()
    if path.resolve() != path or not path.is_file():
        raise ValueError("owned_stage_required")
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA query_only=ON")
        if conn.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
            raise ValueError("owned_stage_required")
        return scope_value(json.loads(conn.execute("SELECT value FROM meta WHERE key='scope'").fetchone()[0]))
    finally:
        conn.close()


def export_verified(store, account, settings, index, confirmation=None, *, exporter=None, verify=verify_word):
    coverage = capture_coverage(store, expected_index=index, confirmation=confirmation)
    if not coverage["full_interval_verified"]:
        return {"status": "coverage_incomplete", "coverage": coverage, "word_path": None,
                "exported_count": 0, "product_checkpoint_advanced": False}
    if account["biz"] != store.scope["biz"] or account["wechat_id"] != store.scope["wechat_id"]:
        raise ValueError("product_account_changed")
    frozen = settings["delivered_baseline_max_article_id"]
    incomplete = set(account.get("incomplete_article_ids", []))
    retained = {public_identity(r["url"]): r for r in store.records()}
    unresolved = []
    for old in account["_articles"]:
        if old["id"] not in incomplete:
            continue
        candidate = retained.get(public_identity(old["source_url"], account["biz"]))
        if (not candidate or not candidate["body_ready"]
            or candidate["content_hash"] == old.get("content_hash")
            or len(candidate["content_text"]) <= (old.get("body_chars") or 0)):
            unresolved.append(old)
    if unresolved:
        return {"status": "incomplete_baseline_not_recovered", "coverage": coverage,
                "unresolved_incomplete_article_ids": sorted({r["id"] for r in unresolved}),
                "unresolved_incomplete_urls": sorted({r["source_url"] for r in unresolved}),
                "word_path": None, "exported_count": 0, "product_checkpoint_advanced": False}
    delivered = [r for r in account["_articles"] if r["id"] not in incomplete
                 and (r["batch_id"] is not None or r["id"] <= frozen)]
    old_urls = {key for r in delivered if (key := public_identity(r["source_url"], account["biz"]))}
    old_hashes = {r["content_hash"] for r in delivered if r.get("content_hash")}
    articles = [r for r in store.records() if store.scope["since"] <= r["publish_time"][:10] <= store.scope["until"]
                and public_identity(r["url"]) not in old_urls and r["content_hash"] not in old_hashes]
    if not articles:
        return {"status": "no_new_verified", "exported_count": 0, "word_path": None,
                "coverage": coverage, "product_checkpoint_advanced": False}
    articles.sort(key=lambda r: (r["publish_time"], r["url"]))
    for article in articles:
        article["source_url"] = article["url"]
    fingerprint = digest({"scope": store.scope, "index": index, "members": articles})
    previous = store.export_record(fingerprint)
    if previous:
        path = Path(previous[0])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != previous[1]:
            raise ValueError("registered_word_changed_or_missing")
        verify(exporter or IncrementalWordExporter(), path, articles)
        return {"status": "word_ready_for_render_review", "word_path": str(path), "reused": True,
                "exported_count": len(articles), "coverage": coverage, "delivery_verified": False,
                "product_checkpoint_advanced": False}
    folder = store.path.parent / "Word"
    path = folder / (f'{safe_path_part(store.scope["account_name"])}_{store.scope["since"]}至'
                     f'{store.scope["until"]}_完整增量_{fingerprint[:12]}.docx')
    if path.exists() or path.with_suffix(path.suffix + ".tmp").exists() or folder.is_symlink():
        raise ValueError("refuse_to_overwrite_existing_word")
    engine = exporter or IncrementalWordExporter()
    engine.export_account(articles, path, account_name=store.scope["account_name"],
                          mode="range_incremental", cycle_key=store.scope["since"] + "至" + store.scope["until"])
    verify(engine, path, articles)
    store.register_export(fingerprint, path, hashlib.sha256(path.read_bytes()).hexdigest())
    return {"status": "word_ready_for_render_review", "word_path": str(path), "reused": False,
            "exported_count": len(articles), "coverage": coverage, "delivery_verified": False,
            "product_checkpoint_advanced": False}


def main(argv=None):
    from .cache_capture import bounded_json
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--verified-index", required=True)
    parser.add_argument("--index-confirmation", help="Human receipt only for a human-verified index; verified pagination needs none.")
    args = parser.parse_args(argv)
    store = None
    try:
        config, scope = load_config(args.config), load_scope(args.stage)
        account = make_plan(config["database"]["path"], config["passive_weekly"], scope["until"], biz=scope["biz"])["accounts"][0]
        store = CaptureStore(args.stage, scope)
        result = export_verified(store, account, config["passive_weekly"],
                                 bounded_json(args.verified_index),
                                 bounded_json(args.index_confirmation) if args.index_confirmation else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in ("no_new_verified", "word_ready_for_render_review") else 2
    except Exception:
        print(json.dumps({"status": "blocked_verified_word", "product_checkpoint_advanced": False}))
        return 2
    finally:
        if store:
            store.close()


if __name__ == "__main__":
    raise SystemExit(main())
