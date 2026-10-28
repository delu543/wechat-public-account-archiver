from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from .automation_store import AutomationStore
from .cleanup import ArchiveCleanup
from .crawler.discoverers.wechat_cache_discoverer import WechatCacheDiscoverer
from .crawler.normalizer import safe_path_part
from .crawler.storage import Storage
from .incremental_word import IncrementalWordExporter
from .macos_wechat import WechatSessionRefresher


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class CrawlResult:
    status: str
    message: str = ""
    discovered_count: int = 0
    crawled_count: int = 0
    failed_count: int = 0
    restricted_count: int = 0
    unresolved_by_reason: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SyncResult:
    job_id: str
    account_name: str
    status: str
    exported_count: int
    word_path: str | None
    message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AccountCrawler(Protocol):
    def run(
        self,
        subscription: Mapping[str, Any],
        job: Mapping[str, Any],
        progress: Callable[[str, int, dict[str, int]], None],
    ) -> CrawlResult: ...


class ExistingCrawlerAdapter:
    """Run the proven URL history workflow while mirroring progress to SQLite."""

    def __init__(self, storage: Storage, output_dir: Path, config: dict[str, Any]) -> None:
        self.storage = storage
        self.output_dir = output_dir
        self.config = config
        self.session_refresher = WechatSessionRefresher()

    def run(
        self,
        subscription: Mapping[str, Any],
        job: Mapping[str, Any],
        progress: Callable[[str, int, dict[str, int]], None],
    ) -> CrawlResult:
        from .web_app import ArchiveJob, ArchiveWebService

        # Every scheduled/manual incremental cycle must begin at the newest
        # history page. A CLI-only resume offset must never leak into weekly
        # automation, otherwise new articles before that offset are skipped.
        service_config = dict(self.config)
        service_config["history"] = dict(self.config.get("history", {}))
        service_config["history"]["start_offset"] = 0
        service = ArchiveWebService(self.storage, self.output_dir, service_config)
        with self.storage.connect() as conn:
            account = conn.execute(
                "SELECT id, account_name, biz, wechat_id FROM accounts WHERE id = ?",
                (subscription["account_id"],),
            ).fetchone()
        if account is None:
            return CrawlResult(status="failed", message="subscription account not found")
        def execute() -> ArchiveJob:
            runtime = ArchiveJob(
                job_id=str(job["id"]),
                input_url=str(subscription["seed_url"]),
                since_date=job["since_date"],
            )
            target = service._run_reverse_job
            kwargs: dict[str, Any] = {}
            if account["biz"]:
                target = service._run_known_account_job
                kwargs = {
                    "account_id": int(account["id"]),
                    "account_name": str(account["account_name"]),
                    "biz": str(account["biz"]),
                    "wechat_id": account["wechat_id"],
                }
            thread = threading.Thread(target=target, args=(runtime,), kwargs=kwargs, daemon=False)
            thread.start()
            while thread.is_alive():
                progress(
                    self._phase(runtime.phase),
                    int(runtime.progress),
                    {
                        "discovered_count": runtime.discovered_count,
                        "crawled_count": runtime.crawled_count,
                        "failed_count": runtime.failed_count,
                        "restricted_count": runtime.restricted_count,
                    },
                )
                thread.join(timeout=0.5)
            return runtime

        # The stored public article URL and exact biz are always the fast path.
        # Visible WeChat automation is only a recovery step for an expired session.
        runtime = execute()
        automation_cfg = self.config.get("automation", {})
        if runtime.status == "needs_session" and automation_cfg.get("prepare_wechat_session", False):
            history_cfg = self.config.get("history", {})
            session_cache = WechatCacheDiscoverer(
                history_cfg.get(
                    "app_data_dir",
                    "~/Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data",
                ),
                target_biz=str(account["biz"]),
                target_wechat_id=account["wechat_id"],
            )
            preparation = self.session_refresher.prepare_account(
                str(account["account_name"]),
                seed_url=str(subscription["seed_url"]),
                session_fingerprint=session_cache.session_fingerprint,
                session_timeout_seconds=float(
                    automation_cfg.get("session_ready_timeout_seconds", 15.0)
                ),
            )
            if preparation.session_verified:
                progress("preflight", 16, {})
                runtime = execute()
            else:
                runtime.message = f"{runtime.message}\n自动微信恢复未通过验证：{preparation.message}"
        return CrawlResult(
            status=runtime.status,
            message=runtime.message,
            discovered_count=runtime.discovered_count,
            crawled_count=runtime.crawled_count,
            failed_count=runtime.failed_count,
            restricted_count=runtime.restricted_count,
            unresolved_by_reason=dict(runtime.unresolved_by_reason),
        )

    @staticmethod
    def _phase(label: str) -> str:
        if "发现" in label:
            return "discover"
        if "快速" in label:
            return "crawl_fast"
        if "失败" in label:
            return "retry_failed"
        if "不可用" in label:
            return "retry_unavailable"
        if "导出" in label:
            return "export_markdown"
        return "preflight"


class WeeklySyncRunner:
    def __init__(
        self,
        storage: Storage,
        output_dir: str | Path,
        config: dict[str, Any],
        *,
        crawler: AccountCrawler | None = None,
        word_exporter: IncrementalWordExporter | None = None,
    ) -> None:
        self.storage = storage
        self.output_dir = Path(output_dir)
        self.config = config
        self.automation = AutomationStore(storage)
        self.automation.init_db()
        self.crawler = crawler or ExistingCrawlerAdapter(storage, self.output_dir, config)
        self.word_exporter = word_exporter or IncrementalWordExporter()
        self.cleanup = ArchiveCleanup(storage, self.output_dir)

    def _ensure_automation_active(self) -> None:
        if self.automation.is_automation_paused():
            raise RuntimeError("全部爬取已暂停；请先在菜单栏插件中点击“继续”")

    def run_job(self, job_id: str) -> SyncResult:
        self._ensure_automation_active()
        try:
            return self._run_job_impl(job_id)
        except Exception as exc:
            LOG.exception("weekly account job failed: %s", job_id)
            job = self.automation.job(job_id)
            if job is None:
                raise
            message = str(exc).strip() or exc.__class__.__name__
            self.automation.update_job(
                job_id,
                status="failed",
                phase="failed",
                error_message=message,
                finished=True,
            )
            self.automation.defer_subscription(
                int(job["subscription_id"]),
                "error",
                message,
                delay=timedelta(hours=24),
            )
            _subscription, account = self._context(job)
            return SyncResult(job_id, str(account["account_name"]), "failed", 0, None, message)
        finally:
            try:
                self.cleanup.prune(execute=True, include_raw_html=False)
            except Exception:
                pass

    def _run_job_impl(self, job_id: str) -> SyncResult:
        job = self.automation.job(job_id)
        if job is None:
            raise KeyError(f"job not found: {job_id}")
        subscription, account = self._context(job)
        account_name = str(account["account_name"])
        if job["status"] == "completed":
            batch = self._batch_for_job(job_id)
            return SyncResult(
                job_id=job_id,
                account_name=account_name,
                status="completed",
                exported_count=int(batch["article_count"]) if batch else 0,
                word_path=str(batch["word_path"]) if batch and batch["word_path"] else None,
                message="任务此前已完成，未重复执行",
            )

        self.automation.update_job(job_id, status="preflight", phase="preflight", progress=2)

        def persist(phase: str, progress: int, counts: dict[str, int]) -> None:
            self.automation.update_job(
                job_id,
                status=phase,
                phase=phase,
                progress=progress,
                counts=counts,
            )

        outcome = self.crawler.run(subscription, job, persist)
        counts = {
            "discovered_count": outcome.discovered_count,
            "crawled_count": outcome.crawled_count,
            "failed_count": outcome.failed_count,
            "restricted_count": outcome.restricted_count,
        }
        checkpoint = {"unresolved_by_reason": dict(outcome.unresolved_by_reason)}
        self.automation.update_job(job_id, counts=counts, checkpoint=checkpoint)
        if outcome.status == "paused":
            self.automation.update_job(
                job_id,
                status="paused",
                phase="paused",
                counts=counts,
                checkpoint=checkpoint,
                error_message=outcome.message or "全部爬取已由用户暂停",
            )
            return SyncResult(
                job_id,
                account_name,
                "paused",
                0,
                None,
                outcome.message or "全部爬取已由用户暂停",
            )
        if outcome.status == "needs_session":
            self.automation.update_job(
                job_id,
                status="needs_session",
                phase="needs_session",
                progress=15,
                counts=counts,
                checkpoint=checkpoint,
                error_message=outcome.message,
            )
            retry_hours = max(1.0, float(self.config.get("automation", {}).get("session_retry_hours", 12)))
            self.automation.defer_subscription(
                int(subscription["id"]),
                "needs_session",
                outcome.message,
                delay=timedelta(hours=retry_hours),
            )
            return SyncResult(job_id, account_name, "needs_session", 0, None, outcome.message)
        if outcome.status == "blocked":
            self.automation.update_job(
                job_id,
                status="blocked",
                phase="blocked",
                counts=counts,
                checkpoint=checkpoint,
                error_message=outcome.message,
                finished=True,
            )
            # All current subscriptions use the same local WeChat public
            # session. A verification/access restriction is not evidence that
            # another account can safely retry through that shared session.
            for affected in self.automation.list_subscriptions():
                if affected["enabled"] or int(affected["id"]) == int(subscription["id"]):
                    self.automation.set_subscription_state(
                        int(affected["id"]), "blocked", f"共享微信公开会话已暂停：{outcome.message}"
                    )
            return SyncResult(job_id, account_name, "blocked", 0, None, outcome.message)
        if outcome.status != "completed":
            self.automation.update_job(
                job_id,
                status="failed",
                phase="failed",
                counts=counts,
                checkpoint=checkpoint,
                error_message=outcome.message,
                finished=True,
            )
            self.automation.defer_subscription(
                int(subscription["id"]),
                "error",
                outcome.message,
                delay=timedelta(hours=24),
            )
            return SyncResult(job_id, account_name, "failed", 0, None, outcome.message)

        return self._export_completed_job(job, account_name, outcome.message)

    def export_pending_subscription_now(self, subscription_id: int) -> SyncResult:
        """Finalize already-captured articles without rerunning discovery.

        Callers use this recovery path only after the visible WeChat/public URL
        pass has established the increment boundary. Export-batch membership
        still provides exactly-once protection.
        """
        current = datetime.now().astimezone()
        cycle = f"recovery-{current:%Y%m%d-%H%M%S}-{int(time.time_ns() % 1_000_000):06d}"
        job = self.automation.create_job(
            subscription_id,
            cycle,
            trigger="recovery_export",
            scheduled_for=current,
        )
        self.automation.update_job(
            str(job["id"]),
            status="export_word",
            phase="export_word",
            progress=95,
        )
        _subscription, account = self._context(job)
        try:
            return self._export_completed_job(
                job,
                str(account["account_name"]),
                "已导出可见会话中确认补齐的新增文章",
            )
        finally:
            try:
                self.cleanup.prune(execute=True, include_raw_html=False)
            except Exception:
                pass

    def _export_completed_job(
        self,
        job: Mapping[str, Any],
        account_name: str,
        message: str,
    ) -> SyncResult:
        job_id = str(job["id"])
        existing_batch = self._batch_for_job(job_id)
        if existing_batch is not None:
            exported_count = int(existing_batch["article_count"])
            self.automation.mark_job_completed(job_id, exported_count)
            return SyncResult(
                job_id,
                account_name,
                "completed",
                exported_count,
                existing_batch["word_path"],
                "已恢复此前验证成功的 Word 批次",
            )

        candidates = self.automation.unexported_articles(int(job["account_id"]))
        word_path: Path | None = None
        if candidates:
            self.automation.update_job(job_id, status="export_word", phase="export_word", progress=95)
            folder = self._weekly_folder(job)
            mode = str(job["mode"])
            suffix = "首次全量" if mode == "initial_full" else "本周增量"
            period = "" if mode == "initial_full" else f"_{self._weekly_period_label(job['scheduled_for'])}"
            filename = f"{safe_path_part(account_name, 'account')}{period}_{suffix}.docx"
            target = folder / filename
            if target.exists():
                cycle_suffix = safe_path_part(str(job["cycle_key"]), "cycle")
                target = folder / (
                    f"{safe_path_part(account_name, 'account')}{period}_{suffix}_{cycle_suffix}.docx"
                )
            word_path = self.word_exporter.export_account(
                candidates,
                target,
                account_name=account_name,
                mode=mode,
                cycle_key=str(job["cycle_key"]),
            )
            batch_kind = "initial_full" if mode == "initial_full" else "weekly_incremental"
            self.automation.complete_export_batch(
                account_id=int(job["account_id"]),
                cycle_key=str(job["cycle_key"]),
                kind=batch_kind,
                article_ids=[int(row["id"]) for row in candidates],
                word_path=str(word_path.resolve()),
                job_id=job_id,
                period_start=job["since_date"],
                period_end=str(job["scheduled_for"])[:10],
            )
        self.automation.update_job(job_id, status="cleanup", phase="cleanup", progress=98)
        self.cleanup.prune(
            article_ids=[int(row["id"]) for row in candidates],
            execute=True,
        )
        self.automation.mark_job_completed(job_id, len(candidates))
        return SyncResult(
            job_id,
            account_name,
            "completed",
            len(candidates),
            str(word_path.resolve()) if word_path else None,
            message,
        )

    def run_due(self, *, now: datetime | None = None) -> list[SyncResult]:
        if self.automation.is_automation_paused():
            return []
        current = now or datetime.now().astimezone()
        cycle_key = self.automation.cycle_key(current)
        subscriptions = self.automation.due_subscriptions(current)
        results: list[SyncResult] = []
        jobs: list[Mapping[str, Any]] = []
        for subscription in subscriptions:
            resumable = self.automation.latest_resumable_job(int(subscription["id"]))
            job = resumable or self.automation.create_job(
                int(subscription["id"]),
                cycle_key,
                trigger="schedule",
                scheduled_for=current,
            )
            jobs.append(job)
        grouped_jobs: dict[str, list[Mapping[str, Any]]] = {}
        for job in jobs:
            grouped_jobs.setdefault(str(job["cycle_key"]), []).append(job)
        for prepared_cycle_key, prepared_jobs in grouped_jobs.items():
            if self.automation.cycle(prepared_cycle_key) is not None:
                continue
            scheduled_for = datetime.fromisoformat(str(prepared_jobs[0]["scheduled_for"]))
            output_folder = self.output_dir / "weekly" / scheduled_for.date().isoformat()
            self.automation.begin_cycle(
                prepared_cycle_key,
                scheduled_for=scheduled_for,
                output_folder=str(output_folder.resolve()),
                expected_account_count=len(prepared_jobs),
            )
        for job in jobs:
            if self.automation.is_automation_paused():
                break
            subscription, account = self._context(job)
            if subscription["state"] == "blocked" and job["status"] != "completed":
                message = str(subscription["last_error"] or "共享微信公开会话访问受限")
                self.automation.update_job(
                    str(job["id"]), status="blocked", phase="blocked", error_message=message, finished=True
                )
                results.append(SyncResult(str(job["id"]), str(account["account_name"]), "blocked", 0, None, message))
                continue
            if job["status"] not in ("completed", "blocked"):
                results.append(self.run_job(str(job["id"])))
        cycle_keys = {str(job["cycle_key"]) for job in jobs}
        cycle_keys.update(str(row["cycle_key"]) for row in self.automation.open_cycles())
        for pending_cycle_key in sorted(cycle_keys):
            self._finalize_cycle(pending_cycle_key)
        return results

    def run_subscription_now(self, subscription_id: int) -> SyncResult:
        self._ensure_automation_active()
        current = datetime.now().astimezone()
        cycle = f"manual-{current:%Y%m%d-%H%M%S}-{int(time.time_ns() % 1_000_000):06d}"
        job = self.automation.create_job(
            subscription_id,
            cycle,
            trigger="manual",
            scheduled_for=current,
        )
        return self.run_job(str(job["id"]))

    def resume_latest_scheduled(self, subscription_id: int) -> SyncResult:
        self._ensure_automation_active()
        job = self.automation.latest_resumable_job(subscription_id)
        if job is None:
            return self.run_subscription_now(subscription_id)
        result = self.run_job(str(job["id"]))
        self._finalize_cycle(str(job["cycle_key"]))
        return result

    def _context(self, job: Mapping[str, Any]) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        with self.storage.connect() as conn:
            subscription = conn.execute(
                "SELECT * FROM subscriptions WHERE id = ?", (job["subscription_id"],)
            ).fetchone()
            account = conn.execute(
                "SELECT * FROM accounts WHERE id = ?", (job["account_id"],)
            ).fetchone()
        if subscription is None or account is None:
            raise KeyError("job subscription or account no longer exists")
        return subscription, account

    def _batch_for_job(self, job_id: str) -> Mapping[str, Any] | None:
        with self.storage.connect() as conn:
            return conn.execute(
                "SELECT * FROM export_batches WHERE job_id = ? AND status = 'completed'", (job_id,)
            ).fetchone()

    def _weekly_folder(self, job: Mapping[str, Any]) -> Path:
        date_part = str(job["scheduled_for"])[:10]
        folder = self.output_dir / "weekly" / date_part
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @staticmethod
    def _weekly_period_label(scheduled_for: Any) -> str:
        scheduled = datetime.fromisoformat(str(scheduled_for))
        period_end = scheduled.date()
        period_start = period_end - timedelta(days=6)
        return f"{period_start:%Y-%m-%d}至{period_end:%Y-%m-%d}"

    def _finalize_cycle(self, cycle_key: str) -> None:
        cycle = self.automation.cycle(cycle_key)
        if cycle is None or cycle["status"] == "completed":
            return
        jobs = self.automation.cycle_jobs(cycle_key)
        expected = int(cycle["expected_account_count"])
        if len(jobs) < expected or any(row["status"] != "completed" for row in jobs):
            self.automation.update_cycle_progress(cycle_key)
            return
        results = [
            SyncResult(
                job_id=str(row["id"]),
                account_name=str(row["account_name"]),
                status="completed",
                exported_count=int(row["exported_count"] or 0),
                word_path=str(row["word_path"]) if row["word_path"] else None,
                message="本轮增量已完成",
            )
            for row in jobs
        ]
        scheduled_for = datetime.fromisoformat(str(cycle["scheduled_for"]))
        report_path = self._write_run_report(scheduled_for, cycle_key, results)
        self.automation.mark_cycle_completed(cycle_key, report_path=str(report_path.resolve()))

    def _write_run_report(
        self,
        current: datetime,
        cycle_key: str,
        results: list[SyncResult],
    ) -> Path:
        folder = self.output_dir / "weekly" / current.date().isoformat()
        folder.mkdir(parents=True, exist_ok=True)
        result_dicts = [item.to_dict() for item in results]
        report = {
            "cycle_key": cycle_key,
            "generated_at": current.isoformat(timespec="seconds"),
            "results": result_dicts,
        }
        temporary = folder / "run-report.json.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(folder / "run-report.json")
        self.word_exporter.export_summary(
            result_dicts,
            folder / f"{self._weekly_period_label(current)}_本周公众号增量汇总.docx",
            cycle_key=cycle_key,
            generated_at=datetime.now().astimezone(),
        )
        return folder / "run-report.json"
