from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from .crawler.models import now_iso
from .crawler.storage import Storage


ACTIVE_JOB_STATES = (
    "scheduled",
    "preflight",
    "discover",
    "crawl_fast",
    "retry_failed",
    "retry_unavailable",
    "export_markdown",
    "export_word",
    "cleanup",
)


class AutomationStore:
    """Persistent subscriptions, resumable jobs, and exactly-once export membership."""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage

    @property
    def db_path(self) -> Path:
        return self.storage.db_path

    def init_db(self) -> None:
        self.storage.init_db()
        with self.storage.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL UNIQUE REFERENCES accounts(id),
                    seed_url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
                    onboarding_mode TEXT NOT NULL
                        CHECK(onboarding_mode IN ('adopt_existing', 'initial_full', 'incremental')),
                    initial_full_completed_at TEXT,
                    baseline_max_article_id INTEGER,
                    lookback_days INTEGER NOT NULL DEFAULT 7 CHECK(lookback_days >= 0),
                    schedule_weekday INTEGER NOT NULL DEFAULT 6
                        CHECK(schedule_weekday BETWEEN 0 AND 6),
                    schedule_hour INTEGER NOT NULL DEFAULT 21
                        CHECK(schedule_hour BETWEEN 0 AND 23),
                    schedule_minute INTEGER NOT NULL DEFAULT 0
                        CHECK(schedule_minute BETWEEN 0 AND 59),
                    last_success_at TEXT,
                    next_run_at TEXT,
                    state TEXT NOT NULL DEFAULT 'active',
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sync_jobs (
                    id TEXT PRIMARY KEY,
                    subscription_id INTEGER NOT NULL REFERENCES subscriptions(id),
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    cycle_key TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    mode TEXT NOT NULL CHECK(mode IN ('initial_full', 'incremental')),
                    since_date TEXT,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    progress INTEGER NOT NULL DEFAULT 0,
                    discovered_count INTEGER NOT NULL DEFAULT 0,
                    crawled_count INTEGER NOT NULL DEFAULT 0,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    restricted_count INTEGER NOT NULL DEFAULT 0,
                    exported_count INTEGER NOT NULL DEFAULT 0,
                    checkpoint_json TEXT NOT NULL DEFAULT '{}',
                    error_message TEXT,
                    scheduled_for TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(account_id, cycle_key)
                );

                CREATE TABLE IF NOT EXISTS export_batches (
                    id TEXT PRIMARY KEY,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    job_id TEXT REFERENCES sync_jobs(id),
                    cycle_key TEXT NOT NULL,
                    kind TEXT NOT NULL
                        CHECK(kind IN ('baseline', 'initial_full', 'weekly_incremental')),
                    status TEXT NOT NULL CHECK(status IN ('preparing', 'completed', 'failed')),
                    period_start TEXT,
                    period_end TEXT,
                    word_path TEXT,
                    article_count INTEGER NOT NULL DEFAULT 0,
                    article_fingerprint TEXT NOT NULL,
                    error_message TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    UNIQUE(account_id, cycle_key, kind),
                    UNIQUE(account_id, article_fingerprint, kind)
                );

                CREATE TABLE IF NOT EXISTS export_batch_articles (
                    batch_id TEXT NOT NULL REFERENCES export_batches(id) ON DELETE CASCADE,
                    article_id INTEGER NOT NULL UNIQUE REFERENCES articles(id),
                    ordinal INTEGER NOT NULL,
                    PRIMARY KEY(batch_id, article_id)
                );

                CREATE TABLE IF NOT EXISTS automation_cycles (
                    cycle_key TEXT PRIMARY KEY,
                    scheduled_for TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('running', 'needs_attention', 'completed', 'superseded')),
                    output_folder TEXT NOT NULL,
                    expected_account_count INTEGER NOT NULL CHECK(expected_account_count >= 0),
                    completed_account_count INTEGER NOT NULL DEFAULT 0,
                    attention_account_count INTEGER NOT NULL DEFAULT 0,
                    exported_count INTEGER NOT NULL DEFAULT 0,
                    report_path TEXT,
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS automation_control (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    paused INTEGER NOT NULL DEFAULT 0 CHECK(paused IN (0, 1)),
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_subscriptions_due
                    ON subscriptions(enabled, next_run_at);
                CREATE INDEX IF NOT EXISTS idx_sync_jobs_status
                    ON sync_jobs(status, scheduled_for);
                CREATE INDEX IF NOT EXISTS idx_export_batches_account
                    ON export_batches(account_id, completed_at);
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO automation_control(id, paused, updated_at)
                VALUES (1, 0, ?)
                """,
                (now_iso(),),
            )

    def automation_control(self) -> dict[str, Any]:
        with self.storage.connect() as conn:
            row = conn.execute(
                "SELECT paused, updated_at FROM automation_control WHERE id = 1"
            ).fetchone()
        if row is None:
            self.init_db()
            return self.automation_control()
        return {
            "paused": bool(row["paused"]),
            "updated_at": row["updated_at"],
        }

    def is_automation_paused(self) -> bool:
        return bool(self.automation_control()["paused"])

    def set_automation_paused(self, paused: bool) -> dict[str, Any]:
        updated_at = now_iso()
        with self.storage.connect() as conn:
            conn.execute(
                """
                INSERT INTO automation_control(id, paused, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    paused = excluded.paused,
                    updated_at = excluded.updated_at
                """,
                (int(paused), updated_at),
            )
        return {"paused": bool(paused), "updated_at": updated_at}

    def adopt_existing_accounts(
        self,
        *,
        schedule_weekday: int = 6,
        schedule_hour: int = 21,
        schedule_minute: int = 0,
        now: datetime | None = None,
    ) -> list[int]:
        """Adopt every populated account and mark its current articles as baseline."""
        adopted: list[int] = []
        current = now or datetime.now().astimezone()
        with self.storage.connect() as conn:
            accounts = conn.execute(
                """
                SELECT a.id,
                       COUNT(ar.id) AS article_count,
                       MAX(ar.id) AS max_article_id,
                       COALESCE(
                           (SELECT source_url FROM articles newest
                            WHERE newest.account_id = a.id
                            ORDER BY COALESCE(newest.publish_time, '') DESC, newest.id DESC
                            LIMIT 1),
                           ''
                       ) AS seed_url
                FROM accounts a
                JOIN articles ar ON ar.account_id = a.id
                LEFT JOIN subscriptions s ON s.account_id = a.id
                WHERE s.id IS NULL
                GROUP BY a.id
                ORDER BY a.id
                """
            ).fetchall()
            for account in accounts:
                account_id = int(account["id"])
                now_text = current.isoformat(timespec="seconds")
                next_run = self.next_weekly_run(
                    current,
                    schedule_weekday,
                    schedule_hour,
                    schedule_minute,
                ).isoformat(timespec="seconds")
                cursor = conn.execute(
                    """
                    INSERT INTO subscriptions(
                        account_id, seed_url, enabled, onboarding_mode,
                        initial_full_completed_at, baseline_max_article_id,
                        schedule_weekday, schedule_hour, schedule_minute,
                        next_run_at, state, created_at, updated_at
                    ) VALUES (?, ?, 1, 'adopt_existing', ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        account_id,
                        account["seed_url"],
                        now_text,
                        int(account["max_article_id"]),
                        schedule_weekday,
                        schedule_hour,
                        schedule_minute,
                        next_run,
                        now_text,
                        now_text,
                    ),
                )
                subscription_id = int(cursor.lastrowid)
                article_ids = self._unexported_article_ids_with_conn(conn, account_id)
                self._complete_batch_with_conn(
                    conn,
                    account_id=account_id,
                    cycle_key="adopt-existing",
                    kind="baseline",
                    article_ids=article_ids,
                    word_path=None,
                    job_id=None,
                    period_start=None,
                    period_end=None,
                    completed_at=now_text,
                )
                adopted.append(subscription_id)
        return adopted

    def ensure_subscription(
        self,
        account_id: int,
        seed_url: str,
        *,
        adopt_existing: bool | None = None,
        enabled: bool = True,
        lookback_days: int = 7,
        schedule_weekday: int = 6,
        schedule_hour: int = 21,
        schedule_minute: int = 0,
        now: datetime | None = None,
    ) -> sqlite3.Row:
        current = now or datetime.now().astimezone()
        now_text = current.isoformat(timespec="seconds")
        next_run = self.next_weekly_run(
            current,
            schedule_weekday,
            schedule_hour,
            schedule_minute,
        ).isoformat(timespec="seconds")
        with self.storage.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM subscriptions WHERE account_id = ?", (account_id,)
            ).fetchone()
            if existing is not None:
                conn.execute(
                    """
                    UPDATE subscriptions
                    SET seed_url = ?, enabled = ?, lookback_days = ?,
                        schedule_weekday = ?, schedule_hour = ?, schedule_minute = ?,
                        updated_at = ?
                    WHERE account_id = ?
                    """,
                    (
                        seed_url,
                        int(enabled),
                        lookback_days,
                        schedule_weekday,
                        schedule_hour,
                        schedule_minute,
                        now_text,
                        account_id,
                    ),
                )
            else:
                article_count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM articles WHERE account_id = ?", (account_id,)
                    ).fetchone()[0]
                )
                should_adopt = bool(article_count) if adopt_existing is None else adopt_existing
                mode = "adopt_existing" if should_adopt else "initial_full"
                max_article_id = conn.execute(
                    "SELECT MAX(id) FROM articles WHERE account_id = ?", (account_id,)
                ).fetchone()[0]
                conn.execute(
                    """
                    INSERT INTO subscriptions(
                        account_id, seed_url, enabled, onboarding_mode,
                        initial_full_completed_at, baseline_max_article_id,
                        lookback_days, schedule_weekday, schedule_hour, schedule_minute,
                        next_run_at, state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        account_id,
                        seed_url,
                        int(enabled),
                        mode,
                        now_text if should_adopt else None,
                        int(max_article_id) if should_adopt and max_article_id is not None else None,
                        lookback_days,
                        schedule_weekday,
                        schedule_hour,
                        schedule_minute,
                        next_run,
                        now_text,
                        now_text,
                    ),
                )
                if should_adopt and article_count:
                    article_ids = self._unexported_article_ids_with_conn(conn, account_id)
                    self._complete_batch_with_conn(
                        conn,
                        account_id=account_id,
                        cycle_key="adopt-existing",
                        kind="baseline",
                        article_ids=article_ids,
                        word_path=None,
                        job_id=None,
                        period_start=None,
                        period_end=None,
                        completed_at=now_text,
                    )
            return conn.execute(
                "SELECT * FROM subscriptions WHERE account_id = ?", (account_id,)
            ).fetchone()

    def activate_current_baseline(
        self,
        *,
        schedule_weekday: int = 6,
        schedule_hour: int = 21,
        schedule_minute: int = 0,
        enabled: bool = True,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Make every successful current article the explicit automation baseline.

        This is intentionally separate from normal startup. It is safe to use
        when the user chooses a new starting point, but normal restarts must not
        silently absorb genuinely new, not-yet-exported articles.
        """
        current = now or datetime.now().astimezone()
        with self.storage.connect() as conn:
            baseline_counts = {
                int(row["account_id"]): int(row["pending_count"])
                for row in conn.execute(
                    """
                    SELECT ar.account_id, COUNT(*) AS pending_count
                    FROM articles ar
                    LEFT JOIN export_batch_articles eba ON eba.article_id = ar.id
                    WHERE eba.article_id IS NULL
                    GROUP BY ar.account_id
                    """
                )
            }
        self.adopt_existing_accounts(
            schedule_weekday=schedule_weekday,
            schedule_hour=schedule_hour,
            schedule_minute=schedule_minute,
            now=current,
        )
        now_text = current.isoformat(timespec="seconds")
        next_run = self.next_weekly_run(
            current,
            schedule_weekday,
            schedule_hour,
            schedule_minute,
        ).isoformat(timespec="seconds")
        cycle_key = f"activate-current-{current:%Y%m%d-%H%M%S}"
        activated: list[dict[str, Any]] = []
        with self.storage.connect() as conn:
            subscriptions = conn.execute(
                """
                SELECT s.id, s.account_id, a.account_name,
                       COUNT(ar.id) AS article_count,
                       MAX(ar.id) AS max_article_id,
                       MAX(ar.publish_time) AS latest_publish_time
                FROM subscriptions s
                JOIN accounts a ON a.id = s.account_id
                LEFT JOIN articles ar ON ar.account_id = s.account_id
                GROUP BY s.id
                ORDER BY a.account_name, a.id
                """
            ).fetchall()
            for subscription in subscriptions:
                account_id = int(subscription["account_id"])
                baseline_ids = self._unexported_article_ids_with_conn(conn, account_id)
                if baseline_ids:
                    self._complete_batch_with_conn(
                        conn,
                        account_id=account_id,
                        cycle_key=cycle_key,
                        kind="baseline",
                        article_ids=baseline_ids,
                        word_path=None,
                        job_id=None,
                        period_start=None,
                        period_end=str(subscription["latest_publish_time"] or "")[:10] or None,
                        completed_at=now_text,
                    )
                conn.execute(
                    """
                    UPDATE automation_cycles
                    SET status = 'superseded', updated_at = ?
                    WHERE status != 'completed' AND cycle_key IN (
                        SELECT cycle_key FROM sync_jobs
                        WHERE subscription_id = ?
                          AND status NOT IN ('completed', 'superseded')
                    )
                    """,
                    (now_text, subscription["id"]),
                )
                conn.execute(
                    """
                    UPDATE sync_jobs
                    SET status = 'superseded', phase = 'superseded', progress = 100,
                        error_message = '当前成功导入内容已由用户确认为新的自动增量基线',
                        finished_at = COALESCE(finished_at, ?), updated_at = ?
                    WHERE subscription_id = ?
                      AND status NOT IN ('completed', 'superseded')
                    """,
                    (now_text, now_text, subscription["id"]),
                )
                conn.execute(
                    """
                    UPDATE subscriptions
                    SET enabled = ?, onboarding_mode = 'incremental',
                        initial_full_completed_at = COALESCE(initial_full_completed_at, ?),
                        baseline_max_article_id = ?,
                        schedule_weekday = ?, schedule_hour = ?, schedule_minute = ?,
                        last_success_at = ?, next_run_at = ?, state = 'active',
                        last_error = NULL, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        int(enabled),
                        now_text,
                        subscription["max_article_id"],
                        schedule_weekday,
                        schedule_hour,
                        schedule_minute,
                        now_text,
                        next_run,
                        now_text,
                        subscription["id"],
                    ),
                )
                activated.append(
                    {
                        "subscription_id": int(subscription["id"]),
                        "account_id": account_id,
                        "account_name": str(subscription["account_name"]),
                        "article_count": int(subscription["article_count"] or 0),
                        "baseline_article_count": baseline_counts.get(account_id, 0),
                        "latest_publish_time": subscription["latest_publish_time"],
                        "next_run_at": next_run,
                    }
                )
        return activated

    def list_subscriptions(self) -> list[sqlite3.Row]:
        with self.storage.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT s.*, a.account_name, a.biz, a.wechat_id,
                           COUNT(ar.id) AS article_count,
                           MAX(ar.publish_time) AS latest_publish_time,
                           SUM(CASE WHEN eba.article_id IS NULL THEN 1 ELSE 0 END) AS pending_export_count
                    FROM subscriptions s
                    JOIN accounts a ON a.id = s.account_id
                    LEFT JOIN articles ar ON ar.account_id = s.account_id
                    LEFT JOIN export_batch_articles eba ON eba.article_id = ar.id
                    GROUP BY s.id
                    ORDER BY a.account_name, a.id
                    """
                ).fetchall()
            )

    def has_subscriptions(self) -> bool:
        with self.storage.connect() as conn:
            return bool(conn.execute("SELECT EXISTS(SELECT 1 FROM subscriptions)").fetchone()[0])

    def due_subscriptions(self, now: datetime | None = None) -> list[sqlite3.Row]:
        current = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
        with self.storage.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT s.*, a.account_name, a.biz
                    FROM subscriptions s
                    JOIN accounts a ON a.id = s.account_id
                    WHERE s.enabled = 1
                      AND s.state NOT IN ('blocked')
                      AND (s.next_run_at IS NULL OR s.next_run_at <= ?)
                    ORDER BY COALESCE(s.next_run_at, ''), s.id
                    """,
                    (current,),
                ).fetchall()
            )

    def unexported_articles(self, account_id: int) -> list[sqlite3.Row]:
        with self.storage.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT ar.*, a.account_name, a.biz, a.wechat_id
                    FROM articles ar
                    JOIN accounts a ON a.id = ar.account_id
                    LEFT JOIN export_batch_articles eba ON eba.article_id = ar.id
                    WHERE ar.account_id = ? AND eba.article_id IS NULL
                    ORDER BY COALESCE(ar.publish_time, ''), ar.id
                    """,
                    (account_id,),
                ).fetchall()
            )

    def create_job(
        self,
        subscription_id: int,
        cycle_key: str,
        *,
        trigger: str = "schedule",
        scheduled_for: datetime | None = None,
    ) -> sqlite3.Row:
        current = scheduled_for or datetime.now().astimezone()
        now_text = now_iso()
        with self.storage.connect() as conn:
            subscription = conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?", (subscription_id,)
            ).fetchone()
            if subscription is None:
                raise KeyError(f"subscription not found: {subscription_id}")
            existing = conn.execute(
                "SELECT * FROM sync_jobs WHERE account_id = ? AND cycle_key = ?",
                (subscription["account_id"], cycle_key),
            ).fetchone()
            if existing is not None:
                return existing
            mode = (
                "initial_full"
                if subscription["onboarding_mode"] == "initial_full"
                and not subscription["initial_full_completed_at"]
                else "incremental"
            )
            since_date = None
            if mode == "incremental":
                latest = conn.execute(
                    "SELECT MAX(substr(publish_time, 1, 10)) FROM articles WHERE account_id = ?",
                    (subscription["account_id"],),
                ).fetchone()[0]
                if latest:
                    try:
                        since_date = (
                            datetime.strptime(str(latest), "%Y-%m-%d").date()
                            - timedelta(days=int(subscription["lookback_days"]))
                        ).isoformat()
                    except ValueError:
                        since_date = None
            job_id = uuid.uuid4().hex
            conn.execute(
                """
                INSERT INTO sync_jobs(
                    id, subscription_id, account_id, cycle_key, trigger, mode,
                    since_date, status, phase, scheduled_for, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'scheduled', 'scheduled', ?, ?, ?)
                """,
                (
                    job_id,
                    subscription_id,
                    subscription["account_id"],
                    cycle_key,
                    trigger,
                    mode,
                    since_date,
                    current.isoformat(timespec="seconds"),
                    now_text,
                    now_text,
                ),
            )
            return conn.execute("SELECT * FROM sync_jobs WHERE id = ?", (job_id,)).fetchone()

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        phase: str | None = None,
        progress: int | None = None,
        counts: dict[str, int] | None = None,
        checkpoint: dict[str, Any] | None = None,
        error_message: str | None = None,
        finished: bool = False,
    ) -> None:
        columns: list[str] = ["updated_at = ?"]
        params: list[Any] = [now_iso()]
        for name, value in (("status", status), ("phase", phase), ("progress", progress)):
            if value is not None:
                columns.append(f"{name} = ?")
                params.append(value)
        allowed_counts = {
            "discovered_count",
            "crawled_count",
            "failed_count",
            "restricted_count",
            "exported_count",
        }
        for name, value in (counts or {}).items():
            if name not in allowed_counts:
                raise ValueError(f"unsupported count: {name}")
            columns.append(f"{name} = ?")
            params.append(int(value))
        if checkpoint is not None:
            columns.append("checkpoint_json = ?")
            params.append(json.dumps(checkpoint, ensure_ascii=False, sort_keys=True))
        if error_message is not None:
            columns.append("error_message = ?")
            params.append(error_message)
        if finished:
            columns.append("finished_at = ?")
            params.append(now_iso())
        params.append(job_id)
        with self.storage.connect() as conn:
            conn.execute(f"UPDATE sync_jobs SET {', '.join(columns)} WHERE id = ?", params)

    def job(self, job_id: str) -> sqlite3.Row | None:
        with self.storage.connect() as conn:
            return conn.execute("SELECT * FROM sync_jobs WHERE id = ?", (job_id,)).fetchone()

    def list_jobs(self, limit: int = 50) -> list[sqlite3.Row]:
        with self.storage.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT j.*, a.account_name, a.biz
                    FROM sync_jobs j
                    JOIN accounts a ON a.id = j.account_id
                    ORDER BY j.created_at DESC LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            )

    def latest_resumable_job(self, subscription_id: int) -> sqlite3.Row | None:
        """Return the newest scheduled job that is waiting for a safe retry."""
        with self.storage.connect() as conn:
            return conn.execute(
                """
                SELECT j.* FROM sync_jobs j
                JOIN automation_cycles c ON c.cycle_key = j.cycle_key
                WHERE j.subscription_id = ? AND j.trigger = 'schedule'
                  AND j.status IN ('needs_session', 'failed', 'paused')
                  AND c.status != 'completed'
                ORDER BY j.scheduled_for DESC, j.created_at DESC
                LIMIT 1
                """,
                (subscription_id,),
            ).fetchone()

    def begin_cycle(
        self,
        cycle_key: str,
        *,
        scheduled_for: datetime,
        output_folder: str,
        expected_account_count: int,
    ) -> sqlite3.Row:
        now_text = now_iso()
        with self.storage.connect() as conn:
            conn.execute(
                """
                INSERT INTO automation_cycles(
                    cycle_key, scheduled_for, status, output_folder,
                    expected_account_count, created_at, updated_at
                ) VALUES (?, ?, 'running', ?, ?, ?, ?)
                ON CONFLICT(cycle_key) DO UPDATE SET
                    expected_account_count = MAX(
                        automation_cycles.expected_account_count,
                        excluded.expected_account_count
                    ),
                    updated_at = excluded.updated_at
                """,
                (
                    cycle_key,
                    scheduled_for.isoformat(timespec="seconds"),
                    output_folder,
                    max(0, int(expected_account_count)),
                    now_text,
                    now_text,
                ),
            )
            return conn.execute(
                "SELECT * FROM automation_cycles WHERE cycle_key = ?", (cycle_key,)
            ).fetchone()

    def cycle(self, cycle_key: str) -> sqlite3.Row | None:
        with self.storage.connect() as conn:
            return conn.execute(
                "SELECT * FROM automation_cycles WHERE cycle_key = ?", (cycle_key,)
            ).fetchone()

    def cycle_jobs(self, cycle_key: str) -> list[sqlite3.Row]:
        with self.storage.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT j.*, a.account_name, a.biz, b.word_path
                    FROM sync_jobs j
                    JOIN accounts a ON a.id = j.account_id
                    LEFT JOIN export_batches b
                      ON b.job_id = j.id AND b.status = 'completed'
                    WHERE j.cycle_key = ? AND j.trigger = 'schedule'
                    ORDER BY a.account_name, a.id
                    """,
                    (cycle_key,),
                ).fetchall()
            )

    def update_cycle_progress(self, cycle_key: str) -> sqlite3.Row:
        cycle = self.cycle(cycle_key)
        if cycle is None:
            raise KeyError(f"automation cycle not found: {cycle_key}")
        jobs = self.cycle_jobs(cycle_key)
        completed = sum(row["status"] == "completed" for row in jobs)
        attention = sum(row["status"] in ("failed", "blocked", "needs_session") for row in jobs)
        exported = sum(int(row["exported_count"] or 0) for row in jobs)
        status = "needs_attention" if attention else "running"
        with self.storage.connect() as conn:
            conn.execute(
                """
                UPDATE automation_cycles
                SET status = ?, completed_account_count = ?,
                    attention_account_count = ?, exported_count = ?, updated_at = ?
                WHERE cycle_key = ? AND status != 'completed'
                """,
                (status, completed, attention, exported, now_iso(), cycle_key),
            )
            return conn.execute(
                "SELECT * FROM automation_cycles WHERE cycle_key = ?", (cycle_key,)
            ).fetchone()

    def mark_cycle_completed(self, cycle_key: str, *, report_path: str) -> sqlite3.Row:
        cycle = self.cycle(cycle_key)
        if cycle is None:
            raise KeyError(f"automation cycle not found: {cycle_key}")
        jobs = self.cycle_jobs(cycle_key)
        expected = int(cycle["expected_account_count"])
        if len(jobs) < expected or any(row["status"] != "completed" for row in jobs):
            raise ValueError("automation cycle still has unfinished accounts")
        completed_at = now_iso()
        exported = sum(int(row["exported_count"] or 0) for row in jobs)
        with self.storage.connect() as conn:
            conn.execute(
                """
                UPDATE automation_cycles
                SET status = 'completed', completed_account_count = ?,
                    attention_account_count = 0, exported_count = ?,
                    report_path = ?, completed_at = COALESCE(completed_at, ?),
                    updated_at = ?
                WHERE cycle_key = ?
                """,
                (len(jobs), exported, report_path, completed_at, completed_at, cycle_key),
            )
            return conn.execute(
                "SELECT * FROM automation_cycles WHERE cycle_key = ?", (cycle_key,)
            ).fetchone()

    def latest_completed_cycle(self) -> sqlite3.Row | None:
        with self.storage.connect() as conn:
            return conn.execute(
                """
                SELECT * FROM automation_cycles
                WHERE status = 'completed'
                ORDER BY completed_at DESC, scheduled_for DESC
                LIMIT 1
                """
            ).fetchone()

    def open_cycles(self) -> list[sqlite3.Row]:
        with self.storage.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT * FROM automation_cycles
                    WHERE status NOT IN ('completed', 'superseded')
                    ORDER BY scheduled_for, cycle_key
                    """
                ).fetchall()
            )

    def complete_export_batch(
        self,
        *,
        account_id: int,
        cycle_key: str,
        kind: str,
        article_ids: Sequence[int],
        word_path: str | None,
        job_id: str | None = None,
        period_start: str | None = None,
        period_end: str | None = None,
    ) -> sqlite3.Row:
        with self.storage.connect() as conn:
            batch_id = self._complete_batch_with_conn(
                conn,
                account_id=account_id,
                cycle_key=cycle_key,
                kind=kind,
                article_ids=article_ids,
                word_path=word_path,
                job_id=job_id,
                period_start=period_start,
                period_end=period_end,
                completed_at=now_iso(),
            )
            return conn.execute("SELECT * FROM export_batches WHERE id = ?", (batch_id,)).fetchone()

    def mark_job_completed(self, job_id: str, exported_count: int) -> None:
        now_text = now_iso()
        with self.storage.connect() as conn:
            job = conn.execute("SELECT * FROM sync_jobs WHERE id = ?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(f"job not found: {job_id}")
            subscription = conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?", (job["subscription_id"],)
            ).fetchone()
            next_run = self.next_weekly_run(
                datetime.now().astimezone(),
                int(subscription["schedule_weekday"]),
                int(subscription["schedule_hour"]),
                int(subscription["schedule_minute"]),
            ).isoformat(timespec="seconds")
            conn.execute(
                """
                UPDATE sync_jobs
                SET status = 'completed', phase = 'completed', progress = 100,
                    exported_count = ?, finished_at = ?, updated_at = ?, error_message = NULL
                WHERE id = ?
                """,
                (exported_count, now_text, now_text, job_id),
            )
            conn.execute(
                """
                UPDATE subscriptions
                SET onboarding_mode = 'incremental',
                    initial_full_completed_at = CASE
                        WHEN onboarding_mode = 'initial_full' THEN ?
                        ELSE initial_full_completed_at
                    END,
                    last_success_at = ?, next_run_at = ?, state = 'active',
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now_text, now_text, next_run, now_text, job["subscription_id"]),
            )

    def set_subscription_state(self, subscription_id: int, state: str, error: str | None) -> None:
        with self.storage.connect() as conn:
            conn.execute(
                """
                UPDATE subscriptions SET state = ?, last_error = ?, updated_at = ? WHERE id = ?
                """,
                (state, error, now_iso(), subscription_id),
            )

    def defer_subscription(
        self,
        subscription_id: int,
        state: str,
        error: str | None,
        *,
        delay: timedelta,
        now: datetime | None = None,
    ) -> str:
        current = now or datetime.now().astimezone()
        retry_at = (current + delay).isoformat(timespec="seconds")
        with self.storage.connect() as conn:
            conn.execute(
                """
                UPDATE subscriptions
                SET state = ?, last_error = ?, next_run_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (state, error, retry_at, now_iso(), subscription_id),
            )
        return retry_at

    def recover_interrupted_jobs(
        self,
        *,
        older_than: timedelta = timedelta(hours=6),
        now: datetime | None = None,
    ) -> int:
        current = now or datetime.now().astimezone()
        recovered = 0
        with self.storage.connect() as conn:
            placeholders = ",".join("?" for _ in ACTIVE_JOB_STATES)
            rows = conn.execute(
                f"SELECT id, updated_at FROM sync_jobs WHERE status IN ({placeholders})",
                ACTIVE_JOB_STATES,
            ).fetchall()
            for row in rows:
                try:
                    updated = datetime.fromisoformat(str(row["updated_at"]))
                    if updated.tzinfo is None:
                        updated = updated.astimezone()
                except (TypeError, ValueError):
                    continue
                if current - updated < older_than:
                    continue
                cursor = conn.execute(
                    """
                    UPDATE sync_jobs
                    SET status = 'failed', phase = 'failed',
                        error_message = '任务进程中断，已在启动时安全收尾',
                        finished_at = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        current.isoformat(timespec="seconds"),
                        current.isoformat(timespec="seconds"),
                        row["id"],
                    ),
                )
                recovered += int(cursor.rowcount)
        return recovered

    @staticmethod
    def cycle_key(value: datetime | None = None) -> str:
        current = value or datetime.now().astimezone()
        iso_year, iso_week, _ = current.isocalendar()
        return f"{iso_year}-W{iso_week:02d}"

    @staticmethod
    def next_weekly_run(
        after: datetime,
        weekday: int,
        hour: int,
        minute: int,
    ) -> datetime:
        if after.tzinfo is None:
            after = after.astimezone()
        days = (weekday - after.weekday()) % 7
        candidate = after.replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=days)
        if candidate <= after:
            candidate += timedelta(days=7)
        return candidate

    def _complete_batch_with_conn(
        self,
        conn: sqlite3.Connection,
        *,
        account_id: int,
        cycle_key: str,
        kind: str,
        article_ids: Iterable[int],
        word_path: str | None,
        job_id: str | None,
        period_start: str | None,
        period_end: str | None,
        completed_at: str,
    ) -> str:
        unique_ids = sorted({int(article_id) for article_id in article_ids})
        fingerprint = hashlib.sha256(
            ",".join(str(article_id) for article_id in unique_ids).encode("ascii")
        ).hexdigest()
        existing = conn.execute(
            """
            SELECT id FROM export_batches
            WHERE account_id = ? AND cycle_key = ? AND kind = ?
            """,
            (account_id, cycle_key, kind),
        ).fetchone()
        if existing is not None:
            return str(existing["id"])
        if unique_ids:
            placeholders = ",".join("?" for _ in unique_ids)
            rows = conn.execute(
                f"SELECT id, account_id FROM articles WHERE id IN ({placeholders})", unique_ids
            ).fetchall()
            if len(rows) != len(unique_ids) or any(int(row["account_id"]) != account_id for row in rows):
                raise ValueError("all export articles must exist and belong to the target account")
        batch_id = uuid.uuid4().hex
        conn.execute(
            """
            INSERT INTO export_batches(
                id, account_id, job_id, cycle_key, kind, status,
                period_start, period_end, word_path, article_count,
                article_fingerprint, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                batch_id,
                account_id,
                job_id,
                cycle_key,
                kind,
                period_start,
                period_end,
                word_path,
                len(unique_ids),
                fingerprint,
                completed_at,
                completed_at,
            ),
        )
        conn.executemany(
            "INSERT INTO export_batch_articles(batch_id, article_id, ordinal) VALUES (?, ?, ?)",
            ((batch_id, article_id, ordinal) for ordinal, article_id in enumerate(unique_ids, 1)),
        )
        return batch_id

    @staticmethod
    def _unexported_article_ids_with_conn(
        conn: sqlite3.Connection,
        account_id: int,
    ) -> list[int]:
        return [
            int(row["id"])
            for row in conn.execute(
                """
                SELECT ar.id
                FROM articles ar
                LEFT JOIN export_batch_articles eba ON eba.article_id = ar.id
                WHERE ar.account_id = ? AND eba.article_id IS NULL
                ORDER BY ar.id
                """,
                (account_id,),
            )
        ]
