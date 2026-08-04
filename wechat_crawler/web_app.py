from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from markdownify import markdownify as html_to_markdown

from .automation_store import AutomationStore
from .cleanup import ArchiveCleanup
from .crawler.date_range import article_in_range, extract_publish_date, normalize_date, publish_timestamp_to_date
from .crawler.discoverers import WechatHistoryDiscoverer
from .crawler.discoverers.wechat_cache_discoverer import WechatCacheDiscoverer
from .crawler.discoverers.history_discoverer import (
    HistoryDiscoveryError,
    SESSION_RECOVERY_STEPS,
    is_session_recovery_error,
    session_recovery_guidance,
)
from .crawler.exporter import MarkdownExporter
from .crawler.fetcher import ArticleFetcher, FetchConfig
from .crawler.models import now_iso
from .crawler.normalizer import safe_path_part
from .crawler.parser import ParseError, WechatArticleParser
from .crawler.storage import Storage
from .macos_wechat import WechatSessionRefresher, WechatWindowProbe
from .weekly_sync import WeeklySyncRunner


@dataclass
class ArchiveJob:
    job_id: str
    input_url: str
    since_date: str | None = None
    status: str = "queued"
    phase: str = "等待中"
    message: str = ""
    progress: int = 0
    done: int = 0
    total: int = 0
    eta_seconds: int | None = None
    account_name: str | None = None
    account_biz: str | None = None
    discovery_mode: str = "等待中"
    crawl_stage: str = ""
    discovered_count: int = 0
    crawled_count: int = 0
    failed_count: int = 0
    restricted_count: int = 0
    exported_count: int = 0
    target_url_ids: set[int] = field(default_factory=set, repr=False)
    unresolved_by_reason: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    recovery_action: str | None = None
    recovery_steps: list[str] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def mark_running(self, phase: str, total: int = 0, base_progress: int = 35) -> None:
        self.status = "running"
        self.phase = phase
        self.total = max(0, total)
        self.done = 0
        self.progress = base_progress if total else max(self.progress, base_progress)
        self.eta_seconds = None

    def mark_progress(self, done: int, total: int | None = None, base: int = 35, span: int = 55) -> None:
        self.done = max(0, done)
        if total is not None:
            self.total = max(0, total)
        if self.total:
            ratio = min(1.0, self.done / self.total)
            self.progress = min(99, int(base + ratio * span))
            elapsed = max(0.1, time.time() - self.started_at)
            if self.done:
                seconds_per_item = elapsed / self.done
                self.eta_seconds = int(max(0, (self.total - self.done) * seconds_per_item))
        else:
            self.progress = max(self.progress, base)

    def mark_done(self, message: str = "完成") -> None:
        self.status = "completed"
        self.phase = "完成"
        self.message = message
        self.progress = 100
        self.eta_seconds = 0
        self.finished_at = time.time()

    def mark_failed(self, message: str) -> None:
        self.status = "failed"
        self.phase = "失败"
        self.message = message
        self.finished_at = time.time()

    def mark_needs_session(self, message: str) -> None:
        self.status = "needs_session"
        self.phase = "等待微信会话"
        self.message = message
        self.progress = max(self.progress, 15)
        self.eta_seconds = None
        self.discovery_mode = "等待文章详情页会话"
        self.recovery_action = (
            "已先尝试保存的文章链接和 biz，并自动尝试微信正文页恢复；当前会话仍不可用。"
            "任务已保留进度，不会重复导出或改用第三方搜索。"
        )
        self.recovery_steps = list(SESSION_RECOVERY_STEPS)
        self.finished_at = time.time()

    def to_dict(self) -> dict[str, Any]:
        already_archived_count = 0
        if self.status == "completed":
            already_archived_count = max(
                0,
                self.discovered_count
                - self.crawled_count
                - self.failed_count
                - self.restricted_count,
            )
        return {
            "job_id": self.job_id,
            "input_url": self.input_url,
            "since_date": self.since_date,
            "status": self.status,
            "phase": self.phase,
            "message": self.message,
            "progress": self.progress,
            "done": self.done,
            "total": self.total,
            "eta_seconds": self.eta_seconds,
            "account_name": self.account_name,
            "account_biz": self.account_biz,
            "discovery_mode": self.discovery_mode,
            "crawl_stage": self.crawl_stage,
            "discovered_count": self.discovered_count,
            "already_archived_count": already_archived_count,
            "crawled_count": self.crawled_count,
            "failed_count": self.failed_count,
            "restricted_count": self.restricted_count,
            "exported_count": self.exported_count,
            "unresolved_by_reason": dict(self.unresolved_by_reason),
            "warnings": list(self.warnings),
            "recovery_action": self.recovery_action,
            "recovery_steps": list(self.recovery_steps),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class ArchiveWebService:
    def __init__(self, storage: Storage, output_dir: str | Path, config: dict[str, Any]) -> None:
        self.storage = storage
        self.output_dir = Path(output_dir)
        self.config = config
        self.jobs: dict[str, ArchiveJob] = {}
        self._lock = threading.Lock()
        self.automation = AutomationStore(storage)
        self.automation.init_db()
        self.recovered_crawl_runs = self.storage.recover_interrupted_runs()
        self.recovered_sync_jobs = self.automation.recover_interrupted_jobs()
        automation_cfg = self.config.get("automation", {})
        self.automation.adopt_existing_accounts(
            schedule_weekday=int(automation_cfg.get("weekday", 6)),
            schedule_hour=int(automation_cfg.get("hour", 20)),
            schedule_minute=int(automation_cfg.get("minute", 0)),
        )
        self.weekly_runner = WeeklySyncRunner(storage, self.output_dir, config)
        self.scheduler_running = False

    def list_accounts(self) -> list[dict[str, Any]]:
        accounts = []
        article_counts = self.storage.account_article_counts()
        for account in self.storage.iter_accounts():
            account_id = int(account["id"])
            folder_name = safe_path_part(account["account_name"], f"account-{account['id']}")
            accounts.append(
                {
                    "id": account_id,
                    "account_name": account["account_name"],
                    "account_biz": account["biz"],
                    "wechat_id": account["wechat_id"],
                    "article_count": article_counts.get(account_id, 0),
                    "folder_name": folder_name,
                    "folder_path": str(self.output_dir / folder_name),
                    "metadata_path": str(self.output_dir / folder_name / "account_metadata.json"),
                }
            )
        return accounts

    def list_articles(self, account_id: int, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        limit = min(max(1, limit), 200)
        offset = max(0, offset)
        articles = []
        total = self.storage.article_count_for_account(account_id)
        for article in self.storage.article_summaries_for_account(account_id, limit=limit, offset=offset):
            articles.append(
                {
                    "id": int(article["id"]),
                    "title": article["title"],
                    "publish_time": article["publish_time"],
                    "author": article["author"],
                    "discovered_from": article["discovered_from"] or "",
                }
            )
        return {
            "items": articles,
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def get_article(self, article_id: int) -> dict[str, Any]:
        with self.storage.connect() as conn:
            row = conn.execute(
                """
                SELECT a.*, acc.account_name, acc.biz, acc.wechat_id, u.discoverer AS discovered_from
                FROM articles a
                JOIN accounts acc ON acc.id = a.account_id
                LEFT JOIN article_urls u ON u.id = a.article_url_id
                WHERE a.id = ?
                """,
                (article_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"article not found: {article_id}")
        body_markdown = html_to_markdown(row["content_html"] or "", heading_style="ATX").strip()
        if not body_markdown:
            body_markdown = row["content_text"] or ""
        return {
            "id": int(row["id"]),
            "title": row["title"],
            "account_name": row["account_name"],
            "account_biz": row["biz"],
            "wechat_id": row["wechat_id"],
            "author": row["author"],
            "publish_time": row["publish_time"],
            "source_url": row["source_url"],
            "summary": row["summary"],
            "cover_image_url": row["cover_image_url"],
            "content_hash": row["content_hash"],
            "discovered_from": row["discovered_from"] or "",
            "body_markdown": body_markdown,
        }

    def start_reverse_job(self, input_url: str, since_date: str | None = None) -> ArchiveJob:
        normalized_since = normalize_date(since_date, "since")
        job = ArchiveJob(job_id=str(uuid.uuid4()), input_url=input_url, since_date=normalized_since)
        with self._lock:
            self.jobs[job.job_id] = job
        thread = threading.Thread(target=self._run_reverse_job, args=(job,), daemon=True)
        thread.start()
        return job

    def list_jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [job.to_dict() for job in self.jobs.values()]

    def automation_overview(self, *, include_cleanup_stats: bool = True) -> dict[str, Any]:
        subscriptions = self.automation.list_subscriptions()
        enabled = [row for row in subscriptions if int(row["enabled"])]
        next_runs = [str(row["next_run_at"]) for row in enabled if row["next_run_at"]]
        latest_jobs = self.automation.list_jobs(limit=20)
        running = [
            row for row in latest_jobs
            if row["status"] not in ("completed", "failed", "blocked", "needs_session")
        ]
        reclaimable_bytes: int | None = None
        if include_cleanup_stats:
            cleanup = ArchiveCleanup(self.storage, self.output_dir).prune(execute=False)
            reclaimable_bytes = cleanup.raw_html_bytes + cleanup.temporary_bytes
        return {
            "database_path": str(self.storage.db_path.resolve()),
            "output_dir": str(self.output_dir.resolve()),
            "database_bytes": self.storage.db_path.stat().st_size if self.storage.db_path.exists() else 0,
            "subscription_count": len(subscriptions),
            "enabled_count": len(enabled),
            "pending_export_count": sum(int(row["pending_export_count"] or 0) for row in subscriptions),
            "next_run_at": min(next_runs) if next_runs else None,
            "scheduler_running": self.scheduler_running,
            "running_job_count": len(running),
            "reclaimable_bytes": reclaimable_bytes,
            "schedule": self._schedule_payload(enabled or subscriptions),
            "wechat_session_cache": self._wechat_session_cache_status(),
            "wechat_window": WechatWindowProbe().inspect().to_dict(),
            "recovered_interrupted_runs": self.recovered_crawl_runs + self.recovered_sync_jobs,
        }

    def health_status(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "database": str(self.storage.db_path.resolve()),
            "scheduler_running": self.scheduler_running,
        }

    def _wechat_session_cache_status(self) -> dict[str, str]:
        history_cfg = self.config.get("history", {})
        path = Path(
            history_cfg.get(
                "app_data_dir",
                "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data",
            )
        ).expanduser()
        if "com.tencent.xinWeChat" in path.parts:
            try:
                probe = subprocess.run(
                    ["/bin/test", "-d", str(path)],
                    capture_output=True,
                    timeout=0.8,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return {
                    "status": "permission_denied",
                    "message": "微信会话目录受 macOS 隐私保护；前台恢复会快速重试，不会阻塞",
                }
            except OSError as exc:
                return {"status": "unavailable", "message": f"微信会话暂不可用：{exc}"}
            if probe.returncode == 0:
                return {"status": "accessible", "message": "微信会话目录可读取"}
            return {"status": "missing", "message": "未找到微信会话目录"}
        try:
            if not path.is_dir():
                return {"status": "missing", "message": "未找到微信会话目录"}
            if os.access(path, os.R_OK | os.X_OK):
                return {"status": "accessible", "message": "微信会话目录可读取"}
            return {"status": "permission_denied", "message": "后台进程无权读取微信会话"}
        except PermissionError:
            return {"status": "permission_denied", "message": "后台进程无权读取微信会话"}
        except OSError as exc:
            return {"status": "unavailable", "message": f"微信会话暂不可用：{exc.strerror or exc}"}

    def list_automation_subscriptions(self) -> list[dict[str, Any]]:
        return [
            {
                "id": int(row["id"]),
                "account_id": int(row["account_id"]),
                "account_name": row["account_name"],
                "account_biz": row["biz"],
                "seed_url": row["seed_url"],
                "enabled": bool(row["enabled"]),
                "onboarding_mode": row["onboarding_mode"],
                "article_count": int(row["article_count"] or 0),
                "pending_export_count": int(row["pending_export_count"] or 0),
                "latest_publish_time": row["latest_publish_time"],
                "last_success_at": row["last_success_at"],
                "next_run_at": row["next_run_at"],
                "state": row["state"],
                "last_error": row["last_error"],
            }
            for row in self.automation.list_subscriptions()
        ]

    def list_automation_jobs(self, limit: int = 50) -> list[dict[str, Any]]:
        return [self._sync_job_payload(row) for row in self.automation.list_jobs(limit=limit)]

    def get_automation_job(self, job_id: str) -> dict[str, Any]:
        row = self.automation.job(job_id)
        if row is None:
            raise KeyError(f"automation job not found: {job_id}")
        with self.storage.connect() as conn:
            enriched = conn.execute(
                """
                SELECT j.*, a.account_name, a.biz
                FROM sync_jobs j JOIN accounts a ON a.id = j.account_id
                WHERE j.id = ?
                """,
                (job_id,),
            ).fetchone()
        return self._sync_job_payload(enriched)

    def list_export_batches(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.storage.connect() as conn:
            rows = conn.execute(
                """
                SELECT b.*, a.account_name
                FROM export_batches b
                JOIN accounts a ON a.id = b.account_id
                WHERE b.status = 'completed' AND b.kind != 'baseline'
                ORDER BY b.completed_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "account_name": row["account_name"],
                "kind": row["kind"],
                "cycle_key": row["cycle_key"],
                "article_count": int(row["article_count"]),
                "completed_at": row["completed_at"],
                "word_path": row["word_path"],
                "download_url": f"/api/automation/batches/{row['id']}/download" if row["word_path"] else None,
            }
            for row in rows
        ]

    def status_widget(self) -> dict[str, Any]:
        overview = self.automation_overview(include_cleanup_stats=False)
        subscriptions = self.list_automation_subscriptions()
        jobs = self.list_automation_jobs(limit=20)
        batches = self.list_export_batches(limit=1)
        active = next(
            (
                job
                for job in jobs
                if job["status"] not in ("completed", "failed", "blocked", "needs_session")
            ),
            None,
        )
        attention = [
            row
            for row in subscriptions
            if row["enabled"] and row["state"] in ("error", "needs_session", "blocked")
        ]
        attention_message = ""
        if attention:
            first = attention[0]
            attention_message = f"{first['account_name']}：{first['last_error'] or first['state']}"
        session_requests = [
            {
                "subscription_id": row["id"],
                "account_name": row["account_name"],
                "account_biz": row["account_biz"],
                "request_key": f"{row['id']}:{row['state']}:{row['next_run_at'] or ''}",
            }
            for row in attention
            if row["state"] == "needs_session"
        ]
        if session_requests and overview["wechat_window"]["state"] == "screen_locked":
            attention_message = "Mac 屏幕已锁定；已保留任务，解锁后将继续，不会操作登录界面"
        return {
            "overview": overview,
            "attention_count": len(attention),
            "attention_message": attention_message,
            "current_job": active,
            "latest_word": batches[0] if batches else None,
            "output_dir": str(self.output_dir.resolve()),
            "session_requests": session_requests,
        }

    def export_batch_file(self, batch_id: str) -> Path:
        with self.storage.connect() as conn:
            row = conn.execute(
                "SELECT word_path FROM export_batches WHERE id = ? AND status = 'completed'",
                (batch_id,),
            ).fetchone()
        if row is None or not row["word_path"]:
            raise KeyError(f"Word batch not found: {batch_id}")
        target = Path(row["word_path"]).resolve()
        try:
            target.relative_to(self.output_dir.resolve())
        except ValueError as exc:
            raise KeyError("Word batch path is outside the output directory") from exc
        if not target.is_file():
            raise KeyError(f"Word file not found: {target.name}")
        return target

    def start_subscription_run(self, subscription_id: int) -> dict[str, Any]:
        with self.storage.connect() as conn:
            subscription = conn.execute(
                "SELECT id FROM subscriptions WHERE id = ?", (subscription_id,)
            ).fetchone()
        if subscription is None:
            raise KeyError(f"subscription not found: {subscription_id}")
        current = datetime.now().astimezone()
        cycle = f"manual-{current:%Y%m%d-%H%M%S}-{time.time_ns() % 1_000_000:06d}"
        job = self.automation.create_job(
            subscription_id,
            cycle,
            trigger="manual",
            scheduled_for=current,
        )
        thread = threading.Thread(
            target=self.weekly_runner.run_job,
            args=(str(job["id"]),),
            daemon=True,
        )
        thread.start()
        return self.get_automation_job(str(job["id"]))

    def start_pending_export(self, subscription_id: int) -> dict[str, Any]:
        with self.storage.connect() as conn:
            subscription = conn.execute(
                "SELECT id FROM subscriptions WHERE id = ?", (subscription_id,)
            ).fetchone()
        if subscription is None:
            raise KeyError(f"subscription not found: {subscription_id}")
        thread = threading.Thread(
            target=self.weekly_runner.export_pending_subscription_now,
            args=(subscription_id,),
            daemon=True,
        )
        thread.start()
        return {"status": "started", "subscription_id": subscription_id}

    def set_subscription_enabled(self, subscription_id: int, enabled: bool) -> dict[str, Any]:
        with self.storage.connect() as conn:
            cursor = conn.execute(
                "UPDATE subscriptions SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), now_iso(), subscription_id),
            )
            if not cursor.rowcount:
                raise KeyError(f"subscription not found: {subscription_id}")
        return next(
            item for item in self.list_automation_subscriptions() if item["id"] == subscription_id
        )

    def update_schedule(self, weekday: int, hour: int, minute: int) -> dict[str, Any]:
        if weekday not in range(7) or hour not in range(24) or minute not in range(60):
            raise ValueError("invalid weekly schedule")
        current = datetime.now().astimezone()
        next_run = self.automation.next_weekly_run(current, weekday, hour, minute).isoformat(timespec="seconds")
        with self.storage.connect() as conn:
            conn.execute(
                """
                UPDATE subscriptions
                SET schedule_weekday = ?, schedule_hour = ?, schedule_minute = ?,
                    next_run_at = ?, updated_at = ?
                """,
                (weekday, hour, minute, next_run, now_iso()),
            )
        return {"weekday": weekday, "hour": hour, "minute": minute, "next_run_at": next_run}

    def cleanup_preview(self, execute: bool = False) -> dict[str, Any]:
        return ArchiveCleanup(self.storage, self.output_dir).prune(execute=execute).to_dict()

    def open_output_directory(self) -> dict[str, str]:
        target = self.output_dir.resolve()
        target.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["/usr/bin/open", str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(
                (result.stderr or result.stdout or "无法打开输出目录").strip()
            )
        return {"status": "opened", "output_dir": str(target)}

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.jobs.get(job_id)
            if not job:
                raise KeyError(f"job not found: {job_id}")
            return job.to_dict()

    def _run_reverse_job(self, job: ArchiveJob) -> None:
        try:
            job.mark_running("解析账号", base_progress=3)
            job.discovery_mode = "入口文章解析"
            parser = WechatArticleParser()
            fetcher = self._build_fetcher()
            run_id = self.storage.start_run("web-reverse-input", notes=f"input_url={job.input_url}")
            url_id = self.storage.upsert_article_url(job.input_url, "web_reverse_input", run_id=run_id, source_clue=job.input_url)
            result = fetcher.fetch(job.input_url)
            if result.status == "restricted":
                self.storage.mark_url_status(url_id, "restricted", result.error_message)
                self.storage.finish_run(run_id, "blocked", notes=result.error_message)
                job.mark_failed(result.error_message or "入口文章受限")
                return
            if result.status != "ok":
                self.storage.mark_url_status(url_id, "failed", result.error_message)
                self.storage.finish_run(run_id, "failed", notes=result.error_message)
                job.mark_failed(result.error_message or "入口文章抓取失败")
                return
            article = parser.parse(result.text, result.final_url or job.input_url)
            account_id = self.storage.upsert_account(
                article.account_name,
                biz=article.account_id,
                wechat_id=article.wechat_id,
                source_url=article.source_url,
            )
            article_count_before = self.storage.article_count_for_account(account_id)
            self._ensure_automation_subscription(account_id, job.input_url, article_count_before)
            publish_date_hint = extract_publish_date(article.publish_time)
            self.storage.update_article_url_metadata(
                url_id,
                account_id=account_id,
                publish_date_hint=publish_date_hint or "",
            )
            if article_in_range(article.publish_time, job.since_date):
                article_id = self.storage.save_article(article, article_url_id=url_id, run_id=run_id)
                self.storage.finish_run(run_id, "completed", notes=f"article_id={article_id} since={job.since_date or ''}")
            else:
                self.storage.mark_url_status(url_id, "out_of_range", f"publish_time={article.publish_time or ''}")
                self.storage.finish_run(run_id, "completed", notes=f"entry_out_of_range=1 since={job.since_date or ''}")
            job.account_name = article.account_name
            job.account_biz = article.account_id
            job.progress = 10

            if article.account_id:
                job.discovery_mode = "URL历史接口"
                if not self._discover_history(job, account_id, article.account_name, article.account_id, article.wechat_id):
                    automation_cfg = self.config.get("automation", {})
                    if job.status != "needs_session" or not automation_cfg.get("prepare_wechat_session", False):
                        return
                    history_cfg = self.config.get("history", {})
                    session_cache = WechatCacheDiscoverer(
                        history_cfg.get(
                            "app_data_dir",
                            "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data",
                        ),
                        target_biz=article.account_id,
                        target_wechat_id=article.wechat_id,
                    )
                    preparation = WechatSessionRefresher().prepare_account(
                        article.account_name,
                        session_fingerprint=session_cache.session_fingerprint,
                        session_timeout_seconds=float(
                            automation_cfg.get("session_ready_timeout_seconds", 15.0)
                        ),
                    )
                    if not preparation.article_open_attempted:
                        job.message = f"{job.message}\n自动微信恢复失败：{preparation.message}"
                        return
                    job.status = "running"
                    job.phase = "刷新微信会话"
                    job.progress = 16
                    job.finished_at = None
                    job.message = preparation.message
                    if not self._discover_history(
                        job,
                        account_id,
                        article.account_name,
                        article.account_id,
                        article.wechat_id,
                    ):
                        return
            else:
                job.warnings.append("入口文章没有暴露 biz，已跳过历史发现")

            self._crawl_account_staged(job, account_id, article.account_id or article.account_name, expected_biz=article.account_id)
            job.phase = "导出 Markdown"
            job.progress = 95
            job.exported_count = MarkdownExporter(self.storage, self.output_dir).export_all(
                force=True,
                account_id=account_id,
            )
            range_text = f"；范围从 {job.since_date} 起" if job.since_date else "；范围为全部历史"
            job.mark_done(f"已导出 {job.exported_count} 篇文章；本次保存/更新 {job.crawled_count} 篇{range_text}")
        except Exception as exc:
            job.mark_failed(str(exc))
        finally:
            ArchiveCleanup(self.storage, self.output_dir).prune(execute=True)

    def _run_known_account_job(
        self,
        job: ArchiveJob,
        *,
        account_id: int,
        account_name: str,
        biz: str,
        wechat_id: str | None,
    ) -> None:
        """Run history discovery without re-parsing an old seed article."""
        try:
            job.mark_running("使用已确认账号身份", base_progress=5)
            job.account_name = account_name
            job.account_biz = biz
            job.discovery_mode = "URL历史接口"
            if not self._discover_history(job, account_id, account_name, biz, wechat_id):
                return
            self._crawl_account_staged(job, account_id, biz, expected_biz=biz)
            job.phase = "导出 Markdown"
            job.progress = 95
            job.exported_count = MarkdownExporter(self.storage, self.output_dir).export_all(
                force=True,
                account_id=account_id,
            )
            range_text = f"；范围从 {job.since_date} 起" if job.since_date else "；范围为全部历史"
            job.mark_done(f"已导出 {job.exported_count} 篇文章；本次保存/更新 {job.crawled_count} 篇{range_text}")
        except Exception as exc:
            job.mark_failed(str(exc))
        finally:
            ArchiveCleanup(self.storage, self.output_dir).prune(execute=True)

    def _discover_history(
        self,
        job: ArchiveJob,
        account_id: int,
        account_name: str,
        biz: str,
        wechat_id: str | None,
    ) -> bool:
        job.phase = "发现历史文章"
        job.progress = 15
        history_cfg = self.config.get("history", {})
        run_id = self.storage.start_run(
            "web-history",
            notes=f"biz={biz} wechat_id={wechat_id or ''} since={job.since_date or ''}",
        )
        discoverer = WechatHistoryDiscoverer(
            history_cfg.get("app_data_dir", "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data"),
            biz=biz,
            wechat_id=wechat_id or "",
            delay_seconds=float(history_cfg.get("delay_seconds", 2.0)),
            count=int(history_cfg.get("count", 10)),
        )
        pages = 0
        try:
            for offset, items, page in discoverer.discover_pages(
                max_pages=history_cfg.get("max_pages"),
                max_items=history_cfg.get("max_items"),
                start_offset=int(history_cfg.get("start_offset", 0) or 0),
                since_date=job.since_date,
            ):
                pages += 1
                for item in items:
                    article_url_id = self.storage.upsert_article_url(
                        item.public_url,
                        discoverer=discoverer.name,
                        run_id=run_id,
                        source_clue=f"history:{wechat_id or biz}:offset:{offset}",
                        account_hint=account_name,
                        account_id=account_id,
                        publish_date_hint=publish_timestamp_to_date(item.publish_timestamp),
                    )
                    job.target_url_ids.add(int(article_url_id))
                    job.discovered_count += 1
                job.message = f"已发现 {job.discovered_count} 条历史 URL"
                job.progress = min(35, 15 + pages)
                if not page.can_continue:
                    break
            self.storage.finish_run(run_id, "completed", notes=f"pages={pages} discovered={job.discovered_count}")
            return True
        except HistoryDiscoveryError as exc:
            self.storage.finish_run(run_id, "blocked", notes=str(exc))
            job.warnings.append(f"历史发现暂停：{exc}")
            if is_session_recovery_error(exc):
                job.mark_needs_session(session_recovery_guidance(account_name))
            else:
                job.mark_failed(f"历史发现被暂停：{exc}")
            return False
        except Exception as exc:
            self.storage.finish_run(run_id, "failed", notes=str(exc))
            raise

    def _crawl_account_staged(
        self,
        job: ArchiveJob,
        account_id: int,
        account_filter: str,
        expected_biz: str | None,
    ) -> None:
        job.discovery_mode = "URL历史接口"
        self._crawl_pending(
            job,
            account_id=account_id,
            account_filter=account_filter,
            retry_failed=False,
            phase="快速抓取正文",
            base=35,
            span=35,
            fetcher=self._build_fetcher(min_delay=0.15, max_delay=0.45, max_retries=0),
            run_mode="web-crawl-fast",
            expected_biz=expected_biz,
        )
        self._refresh_unresolved_counts(job, account_id)
        if job.failed_count:
            self._crawl_pending(
                job,
                account_id=account_id,
                account_filter=account_filter,
                retry_failed=True,
                phase="失败项低并发重试",
                base=70,
                span=15,
                fetcher=self._build_fetcher(min_delay=0.2, max_delay=0.5, max_retries=1),
                run_mode="web-crawl-retry",
                expected_biz=expected_biz,
            )
            self._refresh_unresolved_counts(job, account_id)
        if job.unresolved_by_reason.get("unavailable"):
            self._retry_unavailable(job, account_id, account_filter, expected_biz=expected_biz)
            self._refresh_unresolved_counts(job, account_id)

    def _crawl_pending(
        self,
        job: ArchiveJob,
        account_id: int,
        account_filter: str,
        retry_failed: bool,
        phase: str,
        base: int,
        span: int,
        fetcher: ArticleFetcher,
        run_mode: str,
        expected_biz: str | None,
    ) -> None:
        rows = self.storage.pending_urls(
            account=account_filter,
            retry_failed=retry_failed,
            since=job.since_date,
        )
        if job.target_url_ids:
            rows = [row for row in rows if int(row["id"]) in job.target_url_ids]
        job.crawl_stage = phase
        job.mark_running(phase, total=len(rows), base_progress=base)
        if not rows:
            job.progress = max(job.progress, min(99, base + span))
            return
        parser = WechatArticleParser()
        run_id = self.storage.start_run(run_mode, notes=f"account={account_filter} total={len(rows)} retry_failed={retry_failed}")
        for index, row in enumerate(rows, start=1):
            article_url_id = int(row["id"])
            result = fetcher.fetch(row["source_url"])
            if result.status == "restricted":
                self.storage.mark_url_status(article_url_id, "restricted", result.error_message)
                self.storage.log("warning", "restricted", result.error_message or "restricted", run_id, account_id=account_id, article_url_id=article_url_id)
            elif result.status != "ok":
                self.storage.mark_url_status(article_url_id, "failed", result.error_message)
                self.storage.log("error", "fetch_failed", result.error_message or "fetch failed", run_id, account_id=account_id, article_url_id=article_url_id)
            else:
                try:
                    article = parser.parse(result.text, result.final_url or row["source_url"])
                    if expected_biz and article.account_id != expected_biz:
                        actual = article.account_id or article.account_name or ""
                        self.storage.mark_url_status(
                            article_url_id,
                            "mismatch",
                            f"account mismatch: expected {account_filter}, got {actual}",
                        )
                        self.storage.log("error", "account_mismatch", f"account mismatch: expected {account_filter}, got {actual}", run_id, account_id=account_id, article_url_id=article_url_id)
                        job.mark_progress(index, total=len(rows), base=base, span=span)
                        continue
                    if not article_in_range(article.publish_time, job.since_date):
                        self.storage.update_article_url_metadata(
                            article_url_id,
                            publish_date_hint=extract_publish_date(article.publish_time) or "",
                        )
                        self.storage.mark_url_status(
                            article_url_id,
                            "out_of_range",
                            f"publish_time={article.publish_time or ''}",
                        )
                        self.storage.log(
                            "info",
                            "crawl_skipped_out_of_range",
                            f"publish_time={article.publish_time or ''}",
                            run_id,
                            account_id=account_id,
                            article_url_id=article_url_id,
                        )
                        job.mark_progress(index, total=len(rows), base=base, span=span)
                        continue
                    _article_id, created = self.storage.save_article_with_status(
                        article,
                        article_url_id=article_url_id,
                        run_id=run_id,
                    )
                    if created:
                        job.crawled_count += 1
                except ParseError as exc:
                    self.storage.mark_url_status(article_url_id, "failed", str(exc))
                    self.storage.log("error", "parse_failed", str(exc), run_id, account_id=account_id, article_url_id=article_url_id)
            if index % 10 == 0 or index == len(rows):
                self._refresh_unresolved_counts(job, account_id)
            job.mark_progress(index, total=len(rows), base=base, span=span)
            job.message = f"抓取 {index}/{len(rows)}"
        self.storage.finish_run(
            run_id,
            "completed",
            notes=f"crawled={job.crawled_count} failed={job.failed_count} restricted={job.restricted_count}",
        )

    def _retry_unavailable(
        self,
        job: ArchiveJob,
        account_id: int,
        account_filter: str,
        expected_biz: str | None,
    ) -> None:
        with self.storage.connect() as conn:
            target_clause = ""
            params: list[Any] = [account_id, job.since_date, job.since_date]
            if job.target_url_ids:
                placeholders = ",".join("?" for _ in job.target_url_ids)
                target_clause = f" AND id IN ({placeholders})"
                params.extend(sorted(job.target_url_ids))
            rows = conn.execute(
                f"""
                SELECT * FROM article_urls
                WHERE account_id = ? AND status = 'restricted' AND last_error = 'unavailable'
                  AND (? IS NULL OR publish_date_hint IS NULL OR publish_date_hint >= ?)
                  {target_clause}
                ORDER BY id
                """,
                params,
            ).fetchall()
        job.crawl_stage = "不可用项保守重试"
        job.mark_running("不可用项保守重试", total=len(rows), base_progress=85)
        if not rows:
            return
        run_id = self.storage.start_run("web-restricted-unavailable-retry", notes=f"account={account_filter} total={len(rows)}")
        fetcher = self._build_fetcher(min_delay=0.5, max_delay=1.2, max_retries=1)
        parser = WechatArticleParser()
        for index, row in enumerate(rows, start=1):
            result = fetcher.fetch(row["source_url"])
            if result.status == "restricted":
                self.storage.mark_url_status(row["id"], "restricted", result.error_message)
                self.storage.log("warning", "restricted_retry_still_restricted", result.error_message or "restricted", run_id, account_id=account_id, article_url_id=row["id"])
            elif result.status != "ok":
                self.storage.mark_url_status(row["id"], "failed", result.error_message)
                self.storage.log("error", "restricted_retry_fetch_failed", result.error_message or "fetch failed", run_id, account_id=account_id, article_url_id=row["id"])
            else:
                try:
                    article = parser.parse(result.text, result.final_url or row["source_url"])
                except ParseError as exc:
                    self.storage.mark_url_status(row["id"], "failed", str(exc))
                    self.storage.log("error", "restricted_retry_parse_failed", str(exc), run_id, account_id=account_id, article_url_id=row["id"])
                else:
                    if expected_biz and article.account_id != expected_biz:
                        self.storage.mark_url_status(row["id"], "mismatch", f"account mismatch: expected {account_filter}, got {article.account_id or article.account_name or ''}")
                    elif not article_in_range(article.publish_time, job.since_date):
                        self.storage.update_article_url_metadata(
                            int(row["id"]),
                            publish_date_hint=extract_publish_date(article.publish_time) or "",
                        )
                        self.storage.mark_url_status(
                            row["id"],
                            "out_of_range",
                            f"publish_time={article.publish_time or ''}",
                        )
                    else:
                        _article_id, created = self.storage.save_article_with_status(
                            article,
                            article_url_id=row["id"],
                            run_id=run_id,
                        )
                        if created:
                            job.crawled_count += 1
            if index % 5 == 0 or index == len(rows):
                self._refresh_unresolved_counts(job, account_id)
            job.mark_progress(index, total=len(rows), base=85, span=8)
            job.message = f"不可用重试 {index}/{len(rows)}"
        self.storage.finish_run(run_id, "completed", notes=f"retried={len(rows)}")

    def _refresh_unresolved_counts(self, job: ArchiveJob, account_id: int) -> None:
        with self.storage.connect() as conn:
            target_clause = ""
            params: list[Any] = [account_id]
            if job.target_url_ids:
                placeholders = ",".join("?" for _ in job.target_url_ids)
                target_clause = f" AND id IN ({placeholders})"
                params.extend(sorted(job.target_url_ids))
            rows = conn.execute(
                f"""
                SELECT status, COALESCE(last_error, '') AS reason, COUNT(*) AS count
                FROM article_urls
                WHERE account_id = ? AND status IN ('failed', 'restricted', 'mismatch')
                  {target_clause}
                GROUP BY status, COALESCE(last_error, '')
                """,
                params,
            ).fetchall()
        failed = 0
        restricted = 0
        by_reason: dict[str, int] = {}
        for row in rows:
            status = row["status"]
            count = int(row["count"])
            reason = row["reason"] or status
            if status == "restricted":
                restricted += count
            else:
                failed += count
            by_reason[reason] = by_reason.get(reason, 0) + count
        job.failed_count = failed
        job.restricted_count = restricted
        job.unresolved_by_reason = by_reason

    def _build_fetcher(
        self,
        min_delay: float | None = None,
        max_delay: float | None = None,
        max_retries: int | None = None,
    ) -> ArticleFetcher:
        fetch_cfg = self.config.get("fetch", {})
        configured_min = float(fetch_cfg.get("min_delay_seconds", 2.0))
        configured_max = float(fetch_cfg.get("max_delay_seconds", 5.0))
        return ArticleFetcher(
            FetchConfig(
                timeout_seconds=float(fetch_cfg.get("timeout_seconds", 20.0)),
                min_delay_seconds=configured_min if min_delay is None else min_delay,
                max_delay_seconds=configured_max if max_delay is None else max_delay,
                max_retries=int(fetch_cfg.get("max_retries", 2)) if max_retries is None else max_retries,
                user_agent=fetch_cfg.get("user_agent", FetchConfig.user_agent),
            )
        )

    def _ensure_automation_subscription(self, account_id: int, seed_url: str, article_count_before: int) -> None:
        with self.storage.connect() as conn:
            existing = conn.execute(
                "SELECT id FROM subscriptions WHERE account_id = ?", (account_id,)
            ).fetchone()
            if existing is not None:
                conn.execute(
                    "UPDATE subscriptions SET seed_url = ?, updated_at = ? WHERE id = ?",
                    (seed_url, now_iso(), existing["id"]),
                )
                return
        automation_cfg = self.config.get("automation", {})
        self.automation.ensure_subscription(
            account_id,
            seed_url,
            adopt_existing=article_count_before > 0,
            schedule_weekday=int(automation_cfg.get("weekday", 6)),
            schedule_hour=int(automation_cfg.get("hour", 20)),
            schedule_minute=int(automation_cfg.get("minute", 0)),
        )

    @staticmethod
    def _schedule_payload(rows: list[Any]) -> dict[str, Any]:
        if not rows:
            return {"weekday": 6, "hour": 20, "minute": 0}
        row = rows[0]
        return {
            "weekday": int(row["schedule_weekday"]),
            "hour": int(row["schedule_hour"]),
            "minute": int(row["schedule_minute"]),
        }

    @staticmethod
    def _sync_job_payload(row: Any) -> dict[str, Any]:
        progress = int(row["progress"] or 0)
        discovered_count = int(row["discovered_count"] or 0)
        crawled_count = int(row["crawled_count"] or 0)
        failed_count = int(row["failed_count"] or 0)
        restricted_count = int(row["restricted_count"] or 0)
        already_archived_count = 0
        if row["status"] == "completed":
            already_archived_count = max(
                0,
                discovered_count - crawled_count - failed_count - restricted_count,
            )
        eta_seconds = None
        if 0 < progress < 100:
            try:
                started = datetime.fromisoformat(str(row["started_at"] or row["created_at"]))
                elapsed = max(1.0, (datetime.now().astimezone() - started).total_seconds())
                eta_seconds = int(elapsed * (100 - progress) / progress)
            except (TypeError, ValueError):
                eta_seconds = None
        unresolved_by_reason: dict[str, int] = {}
        try:
            checkpoint = json.loads(row["checkpoint_json"] or "{}")
            unresolved_by_reason = {
                str(reason): int(count)
                for reason, count in (checkpoint.get("unresolved_by_reason") or {}).items()
                if int(count) > 0
            }
        except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
            unresolved_by_reason = {}
        return {
            "job_id": row["id"],
            "account_name": row["account_name"],
            "account_biz": row["biz"],
            "cycle_key": row["cycle_key"],
            "mode": row["mode"],
            "since_date": row["since_date"],
            "status": row["status"],
            "phase": row["phase"],
            "progress": progress,
            "eta_seconds": eta_seconds,
            "discovered_count": discovered_count,
            "already_archived_count": already_archived_count,
            "crawled_count": crawled_count,
            "failed_count": failed_count,
            "restricted_count": restricted_count,
            "exported_count": int(row["exported_count"] or 0),
            "unresolved_by_reason": unresolved_by_reason,
            "message": row["error_message"] or "",
            "created_at": row["created_at"],
            "finished_at": row["finished_at"],
        }


class ArchiveRequestHandler(SimpleHTTPRequestHandler):
    service: ArchiveWebService
    static_dir: Path

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        if isinstance(code, int) and code < 400:
            return
        super().log_request(code, size)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/health":
                self._send_json(self.service.health_status())
            elif parsed.path == "/api/accounts":
                self._send_json(self.service.list_accounts())
            elif parsed.path.startswith("/api/accounts/") and parsed.path.endswith("/articles"):
                account_id = int(parsed.path.split("/")[3])
                qs = parse_qs(parsed.query)
                limit = int(qs.get("limit", ["100"])[0])
                offset = int(qs.get("offset", ["0"])[0])
                self._send_json(self.service.list_articles(account_id, limit=limit, offset=offset))
            elif parsed.path.startswith("/api/articles/"):
                article_id = int(parsed.path.split("/")[3])
                self._send_json(self.service.get_article(article_id))
            elif parsed.path == "/api/jobs":
                self._send_json(self.service.list_jobs())
            elif parsed.path.startswith("/api/jobs/"):
                self._send_json(self.service.get_job(parsed.path.split("/")[3]))
            elif parsed.path == "/api/automation/overview":
                self._send_json(self.service.automation_overview())
            elif parsed.path == "/api/automation/subscriptions":
                self._send_json(self.service.list_automation_subscriptions())
            elif parsed.path == "/api/automation/jobs":
                self._send_json(self.service.list_automation_jobs())
            elif parsed.path.startswith("/api/automation/jobs/"):
                self._send_json(self.service.get_automation_job(parsed.path.split("/")[4]))
            elif parsed.path == "/api/automation/batches":
                self._send_json(self.service.list_export_batches())
            elif parsed.path == "/api/automation/status-widget":
                self._send_json(self.service.status_widget())
            elif parsed.path.startswith("/api/automation/batches/") and parsed.path.endswith("/download"):
                batch_id = parsed.path.split("/")[4]
                self._send_file(self.service.export_batch_file(batch_id))
            else:
                self._serve_static(parsed.path)
        except KeyError as exc:
            self._send_json({"error": str(exc)}, status=404)
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=400)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            payload = self._read_json()
            if parsed.path == "/api/jobs":
                input_url = str(payload.get("url") or "").strip()
                since_date = str(payload.get("since") or "").strip() or None
                if not input_url:
                    raise ValueError("url is required")
                job = self.service.start_reverse_job(input_url, since_date=since_date)
                self._send_json(job.to_dict(), status=202)
            elif parsed.path.startswith("/api/automation/subscriptions/") and parsed.path.endswith("/run"):
                subscription_id = int(parsed.path.split("/")[4])
                self._send_json(self.service.start_subscription_run(subscription_id), status=202)
            elif parsed.path.startswith("/api/automation/subscriptions/") and parsed.path.endswith("/export-pending"):
                subscription_id = int(parsed.path.split("/")[4])
                self._send_json(self.service.start_pending_export(subscription_id), status=202)
            elif parsed.path.startswith("/api/automation/subscriptions/") and parsed.path.endswith("/toggle"):
                subscription_id = int(parsed.path.split("/")[4])
                self._send_json(
                    self.service.set_subscription_enabled(subscription_id, bool(payload.get("enabled")))
                )
            elif parsed.path == "/api/automation/settings":
                self._send_json(
                    self.service.update_schedule(
                        int(payload.get("weekday")),
                        int(payload.get("hour")),
                        int(payload.get("minute")),
                    )
                )
            elif parsed.path == "/api/automation/cleanup":
                self._send_json(self.service.cleanup_preview(execute=bool(payload.get("execute"))))
            elif parsed.path == "/api/automation/open-output":
                self._send_json(self.service.open_output_directory())
            else:
                self._send_json({"error": "not found"}, status=404)
        except KeyError as exc:
            self._send_json({"error": str(exc)}, status=404)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, status=400)
        except (OSError, RuntimeError) as exc:
            self._send_json({"error": str(exc)}, status=500)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        return json.loads(self.rfile.read(length).decode("utf-8") or "{}")

    def _send_file(self, path: Path) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(path.name)}")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _serve_static(self, path: str) -> None:
        if path in ("", "/"):
            target = self.static_dir / "index.html"
        elif path.startswith("/static/"):
            target = self.static_dir / path.removeprefix("/static/")
        else:
            target = self.static_dir / "index.html"
        if not target.exists() or not target.is_file():
            self._send_json({"error": "not found"}, status=404)
            return
        content_type = "text/html; charset=utf-8"
        if target.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif target.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def run_server(
    storage: Storage,
    output_dir: str | Path,
    config: dict[str, Any],
    host: str = "127.0.0.1",
    port: int = 8765,
) -> ThreadingHTTPServer:
    service = ArchiveWebService(storage, output_dir, config=config)
    static_dir = Path(__file__).parent / "web" / "static"

    class Handler(ArchiveRequestHandler):
        pass

    Handler.service = service
    Handler.static_dir = static_dir
    server = ThreadingHTTPServer((host, port), Handler)
    scheduler = None
    automation_cfg = config.get("automation", {})
    if automation_cfg.get("enabled", True):
        from .scheduler import WeeklyScheduler

        scheduler = WeeklyScheduler(
            service.weekly_runner,
            check_interval_seconds=float(automation_cfg.get("check_interval_seconds", 60)),
        )
        scheduler.start()
        service.scheduler_running = True
    print(f"local archive web UI: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if scheduler is not None:
            scheduler.stop()
            service.scheduler_running = False
    return server
