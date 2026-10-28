"""Resumable Word reservations and complete-only passive checkpoints."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import uuid

from .crawler.models import now_iso
from .passive_weekly_plan import publication_day


@contextmanager
def run_lock(database_path):
    path = Path(str(database_path) + ".passive-weekly.lock")
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


class PassiveWeeklyStore:
    def __init__(self, storage):
        self.storage = storage

    def init_db(self):
        with self.storage.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS passive_weekly_checkpoints(
                    biz TEXT PRIMARY KEY, verified_through TEXT NOT NULL,
                    report_json TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS passive_weekly_notices(
                    code TEXT PRIMARY KEY, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS passive_weekly_pending(
                    biz TEXT PRIMARY KEY, since_date TEXT NOT NULL, until_date TEXT NOT NULL,
                    state TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS passive_weekly_observed_progress(
                    biz TEXT PRIMARY KEY, latest_exported_day TEXT,
                    last_checked_until TEXT NOT NULL, report_json TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)

    def notice_once(self, code):
        with self.storage.connect() as conn:
            return conn.execute("INSERT OR IGNORE INTO passive_weekly_notices VALUES(?,?)",
                                (code, now_iso())).rowcount == 1

    def paused(self):
        with self.storage.connect() as conn:
            row = conn.execute("SELECT paused FROM automation_control WHERE id=1").fetchone()
            return row is None or bool(row[0])

    def batch(self, account_id, cycle):
        with self.storage.connect() as conn:
            row = conn.execute("SELECT * FROM export_batches WHERE account_id=? AND cycle_key=? "
                               "AND kind='weekly_incremental'", (account_id, cycle)).fetchone()
            # Discovery may change while a run is interrupted. Resume the one
            # preparing batch owned by this passive pipeline, not just today's
            # URL fingerprint. Never adopt another workflow's pending batch.
            pending = conn.execute("SELECT * FROM export_batches WHERE account_id=? AND cycle_key LIKE 'passive-%' "
                "AND status='preparing' AND kind='weekly_incremental' ORDER BY created_at", (account_id,)).fetchall()
            if len(pending) > 1:
                raise ValueError("multiple pending Word batches require reconciliation")
            if pending:
                row = pending[0]
            return dict(row) if row else None

    def extend_preparing(self, batch, account, article_ids, word_path):
        """Add arriving members before completion; preserve the old Word file."""
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT paused FROM automation_control WHERE id=1").fetchone()[0]:
                raise RuntimeError("automation_paused")
            current = conn.execute("SELECT * FROM export_batches WHERE id=? AND account_id=?",
                                   (batch["id"], account["account_id"])).fetchone()
            if not current or current["status"] != "preparing" or not current["cycle_key"].startswith("passive-"):
                raise ValueError("only this pipeline's unfinished batch may be extended")
            previous = {r[0] for r in conn.execute("SELECT article_id FROM export_batch_articles WHERE batch_id=?", (batch["id"],))}
            added = set(article_ids) - previous
            if not added:
                return dict(current)
            ids = sorted(previous | added)
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(f"SELECT id,account_id,status,length(content_text),publish_time FROM articles WHERE id IN ({placeholders})", ids).fetchall()
            if (str(word_path) == current["word_path"] or len(rows) != len(ids)
                    or any(r[1] != account["account_id"] or r[2] != "parsed" or not r[3]
                           or not (day := publication_day(r[4]))
                           or not account["since"] <= day <= account["until"] for r in rows)):
                raise ValueError("invalid extended Word members or output path")
            for ordinal, ident in enumerate(ids, 1):
                if ident in added:
                    conn.execute("INSERT INTO export_batch_articles VALUES(?,?,?)", (batch["id"], ident, ordinal))
                else:
                    conn.execute("UPDATE export_batch_articles SET ordinal=? WHERE batch_id=? AND article_id=?",
                                 (ordinal, batch["id"], ident))
            fingerprint = hashlib.sha256(",".join(map(str, ids)).encode("ascii")).hexdigest()
            conn.execute("UPDATE export_batches SET word_path=?,article_count=?,article_fingerprint=?,period_start=?,period_end=? WHERE id=?",
                         (str(word_path), len(ids), fingerprint, min(current["period_start"] or account["since"], account["since"]),
                          max(current["period_end"] or account["until"], account["until"]), batch["id"]))
            return dict(conn.execute("SELECT * FROM export_batches WHERE id=?", (batch["id"],)).fetchone())

    def begin_account(self, account):
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT paused FROM automation_control WHERE id=1").fetchone()[0]:
                raise RuntimeError("automation_paused")
            conn.execute("INSERT INTO passive_weekly_pending VALUES(?,?,?,'open',?) ON CONFLICT(biz) DO UPDATE SET "
                "since_date=CASE WHEN state='open' THEN MIN(since_date,excluded.since_date) ELSE excluded.since_date END,"
                "until_date=MAX(until_date,excluded.until_date),state='open',updated_at=excluded.updated_at",
                (account["biz"], account["since"], account["until"], now_iso()))

    def prepare(self, account, cycle, article_ids, word_path):
        ids = sorted(set(article_ids))
        if not ids:
            raise ValueError("empty Word reservation")
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT paused FROM automation_control WHERE id=1").fetchone()[0]:
                raise RuntimeError("automation_paused")
            existing = conn.execute("SELECT * FROM export_batches WHERE account_id=? AND cycle_key=? "
                                    "AND kind='weekly_incremental'", (account["account_id"], cycle)).fetchone()
            if existing:
                return dict(existing)
            placeholders = ",".join("?" for _ in ids)
            rows = conn.execute(f"SELECT id,account_id,status,length(content_text) FROM articles WHERE id IN ({placeholders})", ids).fetchall()
            if len(rows) != len(ids) or any(r[1] != account["account_id"] or r[2] != "parsed" or not r[3] for r in rows):
                raise ValueError("invalid exact-account Word members")
            batch_id, now = uuid.uuid4().hex, now_iso()
            fingerprint = hashlib.sha256(",".join(map(str, ids)).encode("ascii")).hexdigest()
            conn.execute("INSERT INTO export_batches(id,account_id,cycle_key,kind,status,period_start,period_end,"
                         "word_path,article_count,article_fingerprint,created_at) "
                         "VALUES(?,?,?,'weekly_incremental','preparing',?,?,?,?,?,?)",
                         (batch_id, account["account_id"], cycle, account["since"], account["until"],
                          str(word_path), len(ids), fingerprint, now))
            conn.executemany("INSERT INTO export_batch_articles(batch_id,article_id,ordinal) VALUES(?,?,?)",
                             [(batch_id, ident, index) for index, ident in enumerate(ids, 1)])
            return dict(conn.execute("SELECT * FROM export_batches WHERE id=?", (batch_id,)).fetchone())

    def members(self, batch_id):
        with self.storage.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT ar.* FROM articles ar JOIN export_batch_articles e ON e.article_id=ar.id "
                "WHERE e.batch_id=? ORDER BY ar.publish_time,ar.id", (batch_id,))]

    def eligible(self, account, baseline_max, allowed_ids):
        with self.storage.connect() as conn:
            rows = conn.execute("SELECT ar.* FROM articles ar LEFT JOIN export_batch_articles e ON e.article_id=ar.id "
                "WHERE ar.account_id=? AND ar.status='parsed' AND e.article_id IS NULL "
                "ORDER BY ar.publish_time,ar.id", (account["account_id"],)).fetchall()
            return [dict(r) for r in rows if r["id"] in allowed_ids
                    and (r["id"] > baseline_max or r["id"] in account["incomplete_article_ids"])
                    and (day := publication_day(r["publish_time"]))
                    and account["since"] <= day <= account["until"]]

    def finish(self, account, batch_id, coverage, *, complete):
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT paused FROM automation_control WHERE id=1").fetchone()[0]:
                raise RuntimeError("automation_paused")
            now = now_iso()
            if batch_id:
                conn.execute("UPDATE export_batches SET status='completed',completed_at=? WHERE id=?", (now, batch_id))
            if complete:
                if not coverage.get("complete"):
                    raise ValueError("cannot advance an unverified publication checkpoint")
                conn.execute("INSERT INTO passive_weekly_checkpoints VALUES(?,?,?,?) "
                    "ON CONFLICT(biz) DO UPDATE SET verified_through=MAX(verified_through,excluded.verified_through),"
                    "report_json=excluded.report_json,updated_at=excluded.updated_at",
                    (account["biz"], account["until"], json.dumps(coverage), now))
                conn.execute("UPDATE subscriptions SET last_success_at=?,state='active',last_error=NULL,updated_at=? WHERE id=?",
                             (now, now, account["id"]))
                conn.execute("UPDATE passive_weekly_pending SET state='completed',updated_at=? WHERE biz=? AND until_date<=?",
                             (now, account["biz"], account["until"]))

    def record_observed_export(self, account, batch_id, coverage, *, search_status=None):
        """Export watermark only. Never a publication cursor or gap resolution.

        Only a verified, registered Word's actual member dates can move the
        watermark. Empty checks record no new publication day. Pending/full
        checkpoints and subscription last_success remain untouched.
        """
        with self.storage.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute("SELECT paused FROM automation_control WHERE id=1").fetchone()[0]:
                raise RuntimeError("automation_paused")
            old = conn.execute("SELECT latest_exported_day,last_checked_until FROM passive_weekly_observed_progress WHERE biz=?",
                               (account["biz"],)).fetchone()
            days = []
            if batch_id:
                rows = conn.execute("SELECT ar.publish_time FROM articles ar JOIN export_batch_articles e ON e.article_id=ar.id "
                    "JOIN export_batches b ON b.id=e.batch_id WHERE b.id=? AND b.account_id=? AND ar.account_id=? "
                    "AND b.status='completed' AND b.cycle_key LIKE 'passive-%'",
                    (batch_id, account["account_id"], account["account_id"])).fetchall()
                days = [day for row in rows if (day := publication_day(row[0])) and day <= account["until"]]
            if old and old[0]:
                days.append(old[0])
            latest = max(days) if days else None
            checked = max(account["until"], old[1] if old else account["until"])
            report = {"scope": "observed_sources_only", "latest_exported_day": latest,
                "last_checked_until": checked, "full_interval_verified": False,
                "search_status": search_status, "coverage": coverage,
                "recovery_gap_retained": account.get("recovery_gap"), "pending_range_not_closed": True}
            conn.execute("INSERT INTO passive_weekly_observed_progress VALUES(?,?,?,?,?) "
                "ON CONFLICT(biz) DO UPDATE SET latest_exported_day=excluded.latest_exported_day,"
                "last_checked_until=excluded.last_checked_until,report_json=excluded.report_json,updated_at=excluded.updated_at",
                (account["biz"], latest, checked, json.dumps(report), now_iso()))
            return {k: report[k] for k in ("scope", "latest_exported_day", "last_checked_until", "full_interval_verified")}
