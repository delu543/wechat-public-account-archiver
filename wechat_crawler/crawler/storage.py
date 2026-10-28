from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .date_range import extract_publish_date
from .models import ParsedArticle, now_iso
from .normalizer import normalize_url, source_domain, stable_url_key


class Storage:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)

    def connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def init_db(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_key TEXT NOT NULL UNIQUE,
                    account_name TEXT NOT NULL,
                    wechat_id TEXT,
                    biz TEXT,
                    source TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS crawl_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    notes TEXT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT
                );

                CREATE TABLE IF NOT EXISTS article_urls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_url TEXT NOT NULL,
                    normalized_url TEXT NOT NULL,
                    url_key TEXT NOT NULL UNIQUE,
                    account_id INTEGER REFERENCES accounts(id),
                    account_hint TEXT,
                    discoverer TEXT NOT NULL,
                    source_clue TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    last_error TEXT,
                    first_seen_at TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    last_run_id INTEGER REFERENCES crawl_runs(id)
                );

                CREATE TABLE IF NOT EXISTS article_url_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    article_url_id INTEGER NOT NULL REFERENCES article_urls(id),
                    run_id INTEGER REFERENCES crawl_runs(id),
                    discoverer TEXT NOT NULL,
                    source_clue TEXT,
                    seen_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    account_id INTEGER NOT NULL REFERENCES accounts(id),
                    article_url_id INTEGER REFERENCES article_urls(id),
                    source_url TEXT NOT NULL,
                    normalized_url TEXT NOT NULL,
                    title TEXT NOT NULL,
                    author TEXT,
                    publish_time TEXT,
                    summary TEXT,
                    content_html TEXT NOT NULL,
                    content_text TEXT NOT NULL,
                    image_urls TEXT NOT NULL,
                    cover_image_url TEXT,
                    raw_html TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT,
                    crawl_time TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS crawl_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id INTEGER REFERENCES crawl_runs(id),
                    level TEXT NOT NULL,
                    event TEXT NOT NULL,
                    account_id INTEGER REFERENCES accounts(id),
                    article_url_id INTEGER REFERENCES article_urls(id),
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_url
                    ON articles(article_url_id)
                    WHERE article_url_id IS NOT NULL;
                DROP INDEX IF EXISTS idx_articles_hash;
                CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_account_hash
                    ON articles(account_id, content_hash)
                    WHERE content_hash IS NOT NULL AND content_hash != '';
                CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_identity
                    ON articles(account_id, title, publish_time)
                    WHERE title != '' AND publish_time IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_articles_account_publish
                    ON articles(account_id, publish_time, id);
                """
            )
            self._ensure_column(conn, "article_urls", "publish_date_hint TEXT")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_article_urls_publish_date_hint "
                "ON article_urls(publish_date_hint)"
            )

    def start_run(self, mode: str, notes: str | None = None) -> int:
        now = now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO crawl_runs(mode, status, notes, started_at) VALUES (?, ?, ?, ?)",
                (mode, "running", notes, now),
            )
            return int(cur.lastrowid)

    def finish_run(self, run_id: int, status: str, notes: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE crawl_runs SET status = ?, notes = COALESCE(?, notes), finished_at = ? WHERE id = ?",
                (status, notes, now_iso(), run_id),
            )

    def recover_interrupted_runs(
        self,
        *,
        older_than: timedelta = timedelta(hours=6),
        now: datetime | None = None,
    ) -> int:
        """Close stale running rows while preserving their audit history."""
        current = now or datetime.now().astimezone()
        recovered = 0
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT id, started_at, notes FROM crawl_runs WHERE status = 'running'"
            ).fetchall()
            for row in rows:
                try:
                    started = datetime.fromisoformat(str(row["started_at"]))
                    if started.tzinfo is None:
                        started = started.astimezone()
                except (TypeError, ValueError):
                    continue
                if current - started < older_than:
                    continue
                note = str(row["notes"] or "").strip()
                suffix = "recovered after interrupted process"
                recovered_note = f"{note}; {suffix}" if note else suffix
                cursor = conn.execute(
                    """
                    UPDATE crawl_runs
                    SET status = 'failed', notes = ?, finished_at = ?
                    WHERE id = ? AND status = 'running'
                    """,
                    (recovered_note, current.isoformat(timespec="seconds"), int(row["id"])),
                )
                recovered += int(cursor.rowcount)
        return recovered

    def log(
        self,
        level: str,
        event: str,
        message: str,
        run_id: int | None = None,
        account_id: int | None = None,
        article_url_id: int | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO crawl_logs(run_id, level, event, account_id, article_url_id, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (run_id, level, event, account_id, article_url_id, message, now_iso()),
            )

    def upsert_article_url(
        self,
        source_url: str,
        discoverer: str,
        run_id: int | None = None,
        source_clue: str | None = None,
        account_hint: str | None = None,
        account_id: int | None = None,
        publish_date_hint: str | None = None,
    ) -> int:
        normalized = normalize_url(source_url)
        key = stable_url_key(source_url)
        now = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO article_urls(
                    source_url, normalized_url, url_key, account_id, account_hint, discoverer,
                    source_clue, status, first_seen_at, last_seen_at, last_run_id, publish_date_hint
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                ON CONFLICT(url_key) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    last_run_id = excluded.last_run_id,
                    account_hint = COALESCE(article_urls.account_hint, excluded.account_hint),
                    account_id = COALESCE(article_urls.account_id, excluded.account_id),
                    publish_date_hint = COALESCE(excluded.publish_date_hint, article_urls.publish_date_hint)
                """,
                (
                    source_url,
                    normalized,
                    key,
                    account_id,
                    account_hint,
                    discoverer,
                    source_clue,
                    now,
                    now,
                    run_id,
                    publish_date_hint,
                ),
            )
            row = conn.execute("SELECT id FROM article_urls WHERE url_key = ?", (key,)).fetchone()
            article_url_id = int(row["id"])
            conn.execute(
                """
                INSERT INTO article_url_sources(article_url_id, run_id, discoverer, source_clue, seen_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (article_url_id, run_id, discoverer, source_clue, now),
            )
            return article_url_id

    def mark_url_status(self, article_url_id: int, status: str, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE article_urls SET status = ?, last_error = ? WHERE id = ?",
                (status, error, article_url_id),
            )

    def update_article_url_metadata(
        self,
        article_url_id: int,
        *,
        account_id: int | None = None,
        publish_date_hint: str | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE article_urls
                SET account_id = COALESCE(?, account_id),
                    publish_date_hint = COALESCE(?, publish_date_hint)
                WHERE id = ?
                """,
                (account_id, publish_date_hint, article_url_id),
            )

    def upsert_account(
        self,
        account_name: str,
        biz: str | None = None,
        wechat_id: str | None = None,
        source_url: str | None = None,
    ) -> int:
        key = self._account_key(account_name, biz, source_url)
        now = now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO accounts(account_key, account_name, wechat_id, biz, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_key) DO UPDATE SET
                    account_name = excluded.account_name,
                    wechat_id = COALESCE(excluded.wechat_id, accounts.wechat_id),
                    biz = COALESCE(excluded.biz, accounts.biz),
                    updated_at = excluded.updated_at
                """,
                (key, account_name, wechat_id, biz, source_domain(source_url or ""), now, now),
            )
            row = conn.execute("SELECT id FROM accounts WHERE account_key = ?", (key,)).fetchone()
            return int(row["id"])

    def save_article(self, article: ParsedArticle, article_url_id: int | None, run_id: int | None) -> int:
        article_id, _created = self.save_article_with_status(article, article_url_id, run_id)
        return article_id

    def save_article_with_status(
        self,
        article: ParsedArticle,
        article_url_id: int | None,
        run_id: int | None,
    ) -> tuple[int, bool]:
        account_id = self.upsert_account(
            article.account_name,
            biz=article.account_id,
            wechat_id=article.wechat_id,
            source_url=article.source_url,
        )
        now = now_iso()
        values = (
            account_id,
            article_url_id,
            article.source_url,
            article.normalized_url,
            article.title,
            article.author,
            article.publish_time,
            article.summary,
            article.content_html,
            article.content_text,
            json.dumps(article.image_urls, ensure_ascii=False),
            article.cover_image_url,
            article.raw_html,
            article.content_hash,
            article.status,
            article.error_message,
            article.crawl_time,
            now,
            now,
        )
        with self.connect() as conn:
            created = True
            try:
                cur = conn.execute(
                    """
                    INSERT INTO articles(
                        account_id, article_url_id, source_url, normalized_url, title, author,
                        publish_time, summary, content_html, content_text, image_urls,
                        cover_image_url, raw_html, content_hash, status, error_message,
                        crawl_time, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    values,
                )
                article_id = int(cur.lastrowid)
            except sqlite3.IntegrityError:
                created = False
                row = self._find_existing_article(conn, article, article_url_id, account_id)
                if row is None:
                    raise
                article_id = int(row["id"])
                conn.execute(
                    """
                    UPDATE articles SET
                        account_id = ?, article_url_id = COALESCE(article_url_id, ?),
                        source_url = ?, normalized_url = ?, title = ?, author = ?,
                        publish_time = ?, summary = ?, content_html = ?, content_text = ?,
                        image_urls = ?, cover_image_url = ?, raw_html = ?, content_hash = ?,
                        status = ?, error_message = ?, crawl_time = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    values[:17] + (now, article_id),
                )
            if article_url_id is not None:
                conn.execute(
                    """
                    UPDATE article_urls
                    SET account_id = ?, status = 'crawled', last_error = NULL,
                        publish_date_hint = COALESCE(?, publish_date_hint)
                    WHERE id = ?
                    """,
                    (account_id, extract_publish_date(article.publish_time), article_url_id),
                )
            self._log_with_conn(
                conn,
                "info",
                "article_saved",
                f"saved article {article.title}",
                run_id=run_id,
                account_id=account_id,
                article_url_id=article_url_id,
            )
            return article_id, created

    def pending_urls(
        self,
        account: str | None = None,
        limit: int | None = None,
        retry_failed: bool = False,
        since: str | None = None,
        until: str | None = None,
    ) -> list[sqlite3.Row]:
        statuses = ("pending", "discovered", "out_of_range")
        if retry_failed:
            statuses = ("pending", "discovered", "out_of_range", "failed")
        placeholders = ",".join("?" for _ in statuses)
        params: list[Any] = list(statuses)
        where = [f"u.status IN ({placeholders})"]
        if account:
            where.append("(a.account_name = ? OR a.biz = ? OR u.account_hint = ?)")
            params.extend([account, account, account])
        if since:
            where.append("(u.publish_date_hint IS NULL OR u.publish_date_hint >= ?)")
            params.append(since)
        if until:
            where.append("(u.publish_date_hint IS NULL OR u.publish_date_hint <= ?)")
            params.append(until)
        sql = (
            "SELECT u.*, a.account_name, a.biz FROM article_urls u "
            "LEFT JOIN accounts a ON a.id = u.account_id "
            f"WHERE {' AND '.join(where)} ORDER BY u.id ASC"
        )
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        with self.connect() as conn:
            return list(conn.execute(sql, params).fetchall())

    def stats(self) -> dict[str, int]:
        with self.connect() as conn:
            return {
                "accounts": self._count(conn, "accounts"),
                "article_urls": self._count(conn, "article_urls"),
                "articles": self._count(conn, "articles"),
                "crawl_runs": self._count(conn, "crawl_runs"),
                "crawl_logs": self._count(conn, "crawl_logs"),
                "pending_urls": int(
                    conn.execute(
                        "SELECT COUNT(*) FROM article_urls WHERE status IN ('pending', 'discovered')"
                    ).fetchone()[0]
                ),
            }

    def iter_accounts(self) -> Iterable[sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM accounts ORDER BY account_name, id").fetchall()
            return list(rows)

    def account_by_id(self, account_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()

    def account_id_by_biz(self, biz: str) -> int | None:
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM accounts WHERE biz = ?", (biz,)).fetchone()
            return int(row["id"]) if row else None

    def account_article_counts(self) -> dict[int, int]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT account_id, COUNT(*) AS count FROM articles GROUP BY account_id"
            ).fetchall()
            return {int(row["account_id"]): int(row["count"]) for row in rows}

    def article_count_for_account(self, account_id: int) -> int:
        with self.connect() as conn:
            return int(
                conn.execute("SELECT COUNT(*) FROM articles WHERE account_id = ?", (account_id,)).fetchone()[0]
            )

    def article_summaries_for_account(
        self,
        account_id: int,
        limit: int = 100,
        offset: int = 0,
    ) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                        a.id,
                        a.title,
                        a.publish_time,
                        a.author,
                        u.discoverer AS discovered_from
                    FROM articles a
                    LEFT JOIN article_urls u ON u.id = a.article_url_id
                    WHERE a.account_id = ?
                    ORDER BY
                        CASE WHEN a.publish_time IS NULL OR a.publish_time = '' THEN 1 ELSE 0 END,
                        a.publish_time DESC,
                        a.id DESC
                    LIMIT ? OFFSET ?
                    """,
                    (account_id, limit, offset),
                ).fetchall()
            )

    def articles_for_account(self, account_id: int) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(
                conn.execute(
                    """
                    SELECT
                        a.*,
                        acc.account_name,
                        acc.biz,
                        acc.wechat_id,
                        u.discoverer AS discovered_from,
                        u.source_clue AS discovered_source_clue,
                        u.first_seen_at AS discovered_at
                    FROM articles a
                    JOIN accounts acc ON acc.id = a.account_id
                    LEFT JOIN article_urls u ON u.id = a.article_url_id
                    WHERE a.account_id = ?
                    ORDER BY COALESCE(a.publish_time, ''), a.id
                    """,
                    (account_id,),
                ).fetchall()
            )

    def _find_existing_article(
        self,
        conn: sqlite3.Connection,
        article: ParsedArticle,
        article_url_id: int | None,
        account_id: int,
    ) -> sqlite3.Row | None:
        if article_url_id is not None:
            row = conn.execute("SELECT id FROM articles WHERE article_url_id = ?", (article_url_id,)).fetchone()
            if row:
                return row
        row = conn.execute(
            "SELECT id FROM articles WHERE account_id = ? AND content_hash = ?",
            (account_id, article.content_hash),
        ).fetchone()
        if row:
            return row
        if article.publish_time:
            return conn.execute(
                "SELECT id FROM articles WHERE account_id = ? AND title = ? AND publish_time = ?",
                (account_id, article.title, article.publish_time),
            ).fetchone()
        return None

    def _account_key(self, account_name: str, biz: str | None, source_url: str | None) -> str:
        if biz:
            return f"biz:{biz}"
        return f"name:{account_name}|source:{source_domain(source_url or '')}"

    def _count(self, conn: sqlite3.Connection, table: str) -> int:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _ensure_column(self, conn: sqlite3.Connection, table: str, definition: str) -> None:
        column_name = definition.split()[0]
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column_name not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {definition}")

    def _log_with_conn(
        self,
        conn: sqlite3.Connection,
        level: str,
        event: str,
        message: str,
        run_id: int | None = None,
        account_id: int | None = None,
        article_url_id: int | None = None,
    ) -> None:
        conn.execute(
            """
            INSERT INTO crawl_logs(run_id, level, event, account_id, article_url_id, message, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, level, event, account_id, article_url_id, message, now_iso()),
        )
