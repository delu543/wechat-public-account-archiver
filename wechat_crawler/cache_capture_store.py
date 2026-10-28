"""Owned, isolated public staging journal. Never opens the product DB for writes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3

from .cache_capture_validation import canonical_url, digest, record_value, scope_value

APP_ID = 0x57504343


class CaptureStore:
    def __init__(self, path, scope):
        self.path, self.scope = Path(path).absolute(), scope_value(scope)
        if self.path.suffix != ".sqlite" or self.path.resolve() != self.path:
            raise ValueError("unsafe_stage_path")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        new = not self.path.exists()
        if new:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self.conn = sqlite3.connect(self.path, timeout=3)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA trusted_schema=OFF")
        self.conn.execute("PRAGMA synchronous=FULL")
        try:
            if new:
                with self.conn:
                    self.conn.execute("PRAGMA application_id=" + str(APP_ID))
                    self.conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)")
                    self.conn.execute("CREATE TABLE windows (id INTEGER PRIMARY KEY,fingerprint TEXT UNIQUE NOT NULL,keys_json TEXT NOT NULL,health_json TEXT NOT NULL)")
                    self.conn.execute("CREATE TABLE articles (url TEXT PRIMARY KEY,body_hash TEXT NOT NULL,body_ready INTEGER NOT NULL,data TEXT NOT NULL)")
                    self.conn.execute("CREATE TABLE variants (url TEXT NOT NULL,digest TEXT NOT NULL,body_hash TEXT NOT NULL,body_ready INTEGER NOT NULL,data TEXT NOT NULL,PRIMARY KEY(url,digest))")
                    self.conn.execute("CREATE TABLE exports (fingerprint TEXT PRIMARY KEY,path TEXT NOT NULL,sha256 TEXT NOT NULL)")
                    self.conn.execute("INSERT INTO meta VALUES ('scope',?)", (json.dumps(self.scope, sort_keys=True),))
            if self.conn.execute("PRAGMA application_id").fetchone()[0] != APP_ID:
                raise ValueError("not_owned_capture_database")
            stored = self.conn.execute("SELECT value FROM meta WHERE key='scope'").fetchone()
            if not stored or json.loads(stored[0]) != self.scope:
                raise ValueError("capture_scope_changed")
        except Exception:
            self.conn.close()
            raise

    def close(self):
        self.conn.close()

    def bind_newest(self, url, *, confirmation):
        """Bind an observed anchor once; never infer human consent from cache."""
        url = canonical_url(url, self.scope["biz"])
        if (not isinstance(confirmation, dict) or confirmation.get("confirmed_by") != "user"
            or canonical_url(confirmation.get("url"), self.scope["biz"]) != url):
            raise ValueError("human_anchor_confirmation_required")
        expected = confirmation.get("scope_sha256")
        current_hash = digest(self.scope)
        previous = self.conn.execute("SELECT value FROM meta WHERE key='anchor_previous_scope_sha256'").fetchone()
        if self.scope["newest_url"] == url:
            if expected == current_hash or (previous and expected == previous[0]):
                return dict(self.scope)
            raise ValueError("anchor_confirmation_scope_changed")
        if self.scope["newest_url"] is not None or expected != current_hash:
            raise ValueError("anchor_confirmation_scope_changed")
        row = self.conn.execute("SELECT data FROM articles WHERE url=?", (url,)).fetchone()
        if not row:
            raise ValueError("anchor_not_observed")
        day = json.loads(row[0])["publish_time"][:10]
        if not self.scope["since"] <= day <= self.scope["until"]:
            raise ValueError("anchor_outside_interval")
        updated = scope_value(dict(self.scope, newest_url=url))
        old_json = json.dumps(self.scope, sort_keys=True)
        with self.conn:
            result = self.conn.execute("UPDATE meta SET value=? WHERE key='scope' AND value=?",
                                       (json.dumps(updated, sort_keys=True), old_json))
            if result.rowcount != 1:
                raise ValueError("capture_scope_changed")
            self.conn.execute("INSERT OR REPLACE INTO meta VALUES ('anchor_previous_scope_sha256',?)", (current_hash,))
        self.scope = updated
        return dict(updated)

    def ingest(self, snapshot, *, before_commit=None):
        if snapshot.get("status") != "read":
            return {"status": "waiting_cache", "added": 0, "snapshot_committed": False}
        records = {}
        for item in snapshot.get("records", []):
            record = record_value(item, self.scope)
            day = record["publish_time"][:10]
            if (self.scope["since"] <= day <= self.scope["until"]
                or (record["url"] in self.scope["old_urls"] and day < self.scope["since"])):
                key = record["url"]
                if key in records and records[key] != record:
                    raise ValueError("conflicting_snapshot_members")
                records[key] = record
        health = {key: snapshot.get(key) for key in ("read_errors", "unsupported_values", "metadata_conflicts", "scan_within_bounds")}
        fingerprint = digest({"records": sorted(records.values(), key=lambda r: r["url"]), "health": health})
        if self.conn.execute("SELECT 1 FROM windows WHERE fingerprint=?", (fingerprint,)).fetchone():
            return {"status": "unchanged", "added": 0, "snapshot_committed": True}
        added = 0
        with self.conn:
            self.conn.execute("INSERT INTO windows(fingerprint,keys_json,health_json) VALUES (?,?,?)",
                              (fingerprint, json.dumps(sorted(records)), json.dumps(health, sort_keys=True)))
            for url, record in records.items():
                payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
                old = self.conn.execute("SELECT data FROM articles WHERE url=?", (url,)).fetchone()
                self.conn.execute("INSERT OR IGNORE INTO variants VALUES (?,?,?,?,?)",
                                  (url, digest(record), record["content_hash"], record["body_ready"], payload))
                if not old:
                    self.conn.execute("INSERT INTO articles VALUES (?,?,?,?)", (url, record["content_hash"], record["body_ready"], payload))
                    added += 1
                elif not json.loads(old[0])["body_ready"] and record["body_ready"]:
                    self.conn.execute("UPDATE articles SET body_hash=?,body_ready=1,data=? WHERE url=?",
                                      (record["content_hash"], payload, url))
            if before_commit:
                before_commit()
        return {"status": "captured", "added": added, "snapshot_committed": True}

    def records(self):
        return [json.loads(row[0]) for row in self.conn.execute("SELECT data FROM articles ORDER BY url")]

    def windows(self):
        return [{"id": r[0], "keys": json.loads(r[1]), "health": json.loads(r[2])}
                for r in self.conn.execute("SELECT id,keys_json,health_json FROM windows ORDER BY id")]

    def conflicts(self):
        values = {}
        for row in self.conn.execute("SELECT url,data FROM variants WHERE body_ready=1"):
            record = json.loads(row[1])
            values.setdefault(row[0], set()).add((record["content_hash"], record["publish_time"], record["title"]))
        return sorted(url for url, choices in values.items() if len(choices) > 1)

    def export_record(self, fingerprint):
        return self.conn.execute("SELECT path,sha256 FROM exports WHERE fingerprint=?", (fingerprint,)).fetchone()

    def register_export(self, fingerprint, path, sha256):
        with self.conn:
            self.conn.execute("INSERT INTO exports VALUES (?,?,?)", (fingerprint, str(path), sha256))
