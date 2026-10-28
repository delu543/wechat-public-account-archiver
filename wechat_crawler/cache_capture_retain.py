"""Carry owned public evidence into a longer batch without changing old files.

An old window's membership is preserved exactly, never unioned to manufacture
overlap. Retained evidence is not a new source refresh or a full-list proof.
"""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .cache_capture_store import APP_ID
from .cache_capture_validation import scope_value


def retain_into(source_path, destination, *, before_commit=None):
    source_path = Path(source_path).absolute()
    if (source_path.resolve() != source_path or not source_path.is_file()
        or source_path == destination.path):
        raise ValueError("separate_owned_source_required")
    conn = sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA trusted_schema=OFF")
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        if conn.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
            raise ValueError("owned_public_source_required")
        source = scope_value(json.loads(conn.execute("SELECT value FROM meta WHERE key='scope'").fetchone()[0]))
        expected = {k:v for k,v in destination.scope.items() if k not in ("until", "newest_url")}
        actual = {k:v for k,v in source.items() if k not in ("until", "newest_url")}
        if actual != expected or source["until"] > destination.scope["until"]:
            raise ValueError("retained_capture_scope_mismatch")
        for table, expression, limit in (("articles","length(data)",20000),
            ("variants","length(data)",40000), ("windows","length(keys_json)+length(health_json)",5000)):
            count, size = conn.execute(f"SELECT count(*),coalesce(sum({expression}),0) FROM {table}").fetchone()
            if count > limit or size > 128 * 1024 * 1024:
                raise ValueError("retained_public_evidence_budget")
        versions = {}
        for url, data in conn.execute("SELECT url,data FROM variants WHERE body_ready=1"):
            value = json.loads(data)
            versions.setdefault(url,set()).add((value["content_hash"],value["publish_time"],value["title"]))
        if any(len(values) > 1 for values in versions.values()):
            raise ValueError("retained_source_conflicts_unresolved")
        articles = {url:json.loads(data) for url,data in conn.execute("SELECT url,data FROM articles")}
        windows = [(json.loads(keys),json.loads(health)) for keys,health in conn.execute(
            "SELECT keys_json,health_json FROM windows ORDER BY id")]
    finally:
        conn.close()
    seen = {key for keys,_ in windows for key in keys}
    if seen != set(articles):
        raise ValueError("retained_window_membership_mismatch")
    added = committed = 0
    for keys, health in windows:
        snapshot = dict(health, status="read", records=[articles[key] for key in keys])
        event = destination.ingest(snapshot, before_commit=before_commit)
        added += event["added"]
        committed += event["status"] == "captured"
    return {"status":"retained_owned_public_evidence", "source_until":source["until"],
            "destination_until":destination.scope["until"], "source_records":len(articles),
            "source_windows":len(windows), "added_records":added, "committed_windows":committed,
            "source_modified":False, "fresh_source_read":False, "full_interval_verified":False,
            "product_writes":0, "word_exports":0, "master_pause_changed":False}
