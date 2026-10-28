"""One-command passive incremental archive, gated before network/body writes."""
from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path

from docx import Document

from .biz_push_reader import public_identity
from .crawler.normalizer import safe_path_part
from .crawler.storage import Storage
from .incremental_word import IncrementalWordExporter
from .passive_weekly_body import crawl_records
from .passive_weekly_plan import coverage_for, make_plan, public_plan
from .sogou_pc_source import pc_search_candidates
from .weekly_source_router import WeeklySourceRouter
from .passive_weekly_store import PassiveWeeklyStore, run_lock


def verify_word(exporter, path, articles):
    exporter.verify(path, expected_articles=len(articles))
    keys = [public_identity(a["source_url"]) for a in articles]
    hashes = [a["content_hash"] for a in articles if a["content_hash"]]
    if None in keys or len(set(keys)) != len(keys) or len(set(hashes)) != len(hashes):
        raise ValueError("duplicate_or_invalid_word_members")
    doc = Document(path)
    links = [public_identity(rel.target_ref) for rel in doc.part.rels.values()
             if rel.reltype.endswith("/hyperlink") and rel.is_external]
    if set(keys) != set(links) or len(links) != len(keys):
        raise ValueError("Word source link verification failed")


class PassiveWeeklyRunner:
    def __init__(self, database_path, output_path, settings=None, *, sources=None,
                 fetcher=None, exporter=None, searcher=None):
        self.database_path = Path(database_path).expanduser()
        self.output_path = Path(output_path).expanduser()
        self.settings = settings or {}
        self.sources = sources or WeeklySourceRouter(self.settings)
        self.fetcher = fetcher
        self.exporter = exporter or IncrementalWordExporter()
        self.searcher = searcher or pc_search_candidates

    def inspect(self, until, *, biz=None, source_check=False):
        plan = make_plan(self.database_path, self.settings, until, biz=biz)
        report = public_plan(plan)
        report.update(status="plan_only", network_requests=0, wechat_input_events=0,
                      product_writes=0, runtime_deployed=False)
        if source_check:
            source = self.sources.read(plan["accounts"])
            report.update(status="health_checked", source_status=source.get("status"),
                          source_reason=source.get("reason"),
                          key_available=source.get("key_available"),
                          key_health=source.get("key_health") or ("setup_required_unverified" if source.get("reason") == "biz_setup_required" else "not_checked"),
                          first_page_hmac_verified=source.get("first_page_hmac_verified"),
                          coverage_measured=bool(source.get("coverage_measured")))
            for account, row in zip(plan["accounts"], report["accounts"]):
                row["coverage"] = coverage_for(account, source, source.get("records", []),
                    freshness_hours=self.settings.get("freshness_hours", 36))
            if source.get("reason") == "biz_setup_required":
                report["required_actions"] = ["仅公众号推送库需要一次专用初始化；本命令不会自动取密钥"]
        return report

    def run(self, until, *, biz=None, allow_partial=False, search_gaps=False, observed=False):
        plan = make_plan(self.database_path, self.settings, until, biz=biz)
        report = public_plan(plan)
        report["discovery_scope"] = "observed_sources_only" if observed else "all_publications"
        if plan["paused"]:
            return dict(report, status="paused", reason="automation_paused", product_writes=0)
        with run_lock(self.database_path):
            return self._run_locked(until, biz, allow_partial, search_gaps, observed)

    def _run_locked(self, until, biz, allow_partial, search_gaps, observed):
        plan = make_plan(self.database_path, self.settings, until, biz=biz)
        report = public_plan(plan)
        report["discovery_scope"] = "observed_sources_only" if observed else "all_publications"
        store = PassiveWeeklyStore(Storage(self.database_path))
        if store.paused():
            return dict(report, status="paused", reason="automation_paused", product_writes=0)
        if not plan["accounts"]:
            return dict(report, status="no_enabled_subscriptions", product_writes=0)
        source = self.sources.read(plan["accounts"])
        report.update(source_status=source.get("status"), source_reason=source.get("reason"),
                      key_available=source.get("key_available"),
                      key_health=source.get("key_health") or ("setup_required_unverified" if source.get("reason") == "biz_setup_required" else "not_checked"),
                      first_page_hmac_verified=source.get("first_page_hmac_verified"),
                      coverage_measured=bool(source.get("coverage_measured")))
        if source.get("status") != "read":
            # One top-level setup item, never nine account-level prompts. No
            # key refresh/capture/init is reachable from this workflow.
            return dict(report, status="blocked_source", product_writes=0,
                        required_actions=[source.get("reason") or "source_unavailable"])
        records = list(source.get("records", []))
        search_status, search_stop, search_wechat_stop = {}, False, False
        if search_gaps:
            for account in plan["accounts"]:
                if store.paused():
                    break
                if search_stop:
                    search_status[account["biz"]] = "skipped_after_verification"
                    continue
                found = self.searcher(account)  # one ordinary PC query/account
                search_status[account["biz"]] = found.get("status")
                search_stop = found.get("status") == "restricted"
                search_wechat_stop = search_stop and found.get("restriction_provider") == "wechat"
                candidates = found.get("candidates", []) + found.get("records", [])
                records += [dict(r, source="sogou_pc", target_biz=account["biz"]) for r in candidates]
        # Opaque search redirect tokens are never written into reports/cycle IDs.
        fingerprint = hashlib.sha256(json.dumps(sorted({r["url"] for r in records if public_identity(r.get("url"))})).encode()).hexdigest()[:12]
        cycle = "passive-" + until + "-" + fingerprint
        start = min(a["since"] for a in plan["accounts"])
        folder = self.output_path / "passive-weekly" / f"截止{until}_{fingerprint}"
        previous_report = folder / "运行结果.json"
        if previous_report.exists() and previous_report.stat().st_size < 16 * 1024 * 1024:
            previous = json.loads(previous_report.read_text(encoding="utf-8"))
            previous_start = previous.get("range_start")
            if previous_start and date.fromisoformat(previous_start).isoformat() <= until:
                start = min(start, previous_start)
        report["range_start"] = start
        store.init_db()
        results, shared_stop = [], search_wechat_stop
        for account in plan["accounts"]:
            coverage = coverage_for(account, source, records, freshness_hours=self.settings.get("freshness_hours", 36))
            row = {"account_name": account["account_name"], "biz": account["biz"], "since": account["since"],
                   "until": until, "status": "coverage_incomplete", "coverage": coverage,
                   "exported_count": 0, "word_path": None,
                   "discovery_scope": report["discovery_scope"], "full_interval_verified": False}
            results.append(row)
            if account["biz"] in search_status:
                row["search_status"] = search_status[account["biz"]]
            if shared_stop or store.paused():
                row["status"] = "blocked_shared_restriction" if shared_stop else "paused"
                continue
            if not coverage["complete"] and not allow_partial and not observed:
                continue
            try:
                store.begin_account(account)
                selected = [r for r in records if r.get("source") != "sogou_pc" or r.get("target_biz") == account["biz"]]
                crawl = crawl_records(store.storage, account, selected, store.paused, fetcher=self.fetcher,
                                      search_stopped=search_stop)
                shared_stop = crawl["shared_restriction"]
                search_stop = search_stop or crawl["search_restriction"]
                row.update({k: v for k, v in crawl.items() if k != "article_ids"})
                complete = bool(not observed and coverage["complete"] and not crawl["failed"] and not crawl["restricted"]
                                and not crawl["paused"]
                                and set(account["incomplete_article_ids"]) <= crawl["article_ids"])
                row["status"] = "completed" if complete else "incomplete"
                if crawl["paused"] or shared_stop:
                    row["status"] = "paused" if crawl["paused"] else "blocked_shared_restriction"
                    continue
                if not complete and not allow_partial and not observed:
                    continue
                batch = store.batch(account["account_id"], cycle)
                articles = store.eligible(account, self.settings.get("delivered_baseline_max_article_id", 0), crawl["article_ids"])
                reused = bool(batch and batch["status"] == "completed" and not articles)
                if articles and batch and batch["status"] == "preparing":
                    ids = sorted([a["id"] for a in articles] + [a["id"] for a in store.members(batch["id"])])
                    extension = hashlib.sha256(json.dumps(ids).encode()).hexdigest()[:10]
                    label = "已发现增量" if observed else ("增量" if complete else "已获取部分")
                    path = folder / f'{safe_path_part(account["account_name"])}_{account["since"]}至{until}_{label}_续接{extension}.docx'
                    batch = store.extend_preparing(batch, account, [a["id"] for a in articles], path)
                    row["resumed_batch"] = True
                if articles and (not batch or batch["status"] == "completed"):
                    ids = [a["id"] for a in articles]
                    supplement = hashlib.sha256(json.dumps(sorted(ids)).encode()).hexdigest()[:10]
                    batch_cycle = cycle + ":" + supplement if batch else cycle
                    label = "已发现增量" if observed else ("增量" if complete else "已获取部分")
                    path = folder / f'{safe_path_part(account["account_name"])}_{account["since"]}至{until}_{label}.docx'
                    if path.exists():
                        path = path.with_name(path.stem + "_补充" + supplement + path.suffix)
                    batch = store.prepare(account, batch_cycle, ids, path)
                if batch:
                    articles, path = store.members(batch["id"]), Path(batch["word_path"])
                    if not path.exists():
                        suffix = " 已发现增量（仅已发现内容，非完整发表验收）" if observed else (" 已获取部分" if not complete else "")
                        self.exporter.export_account(articles, path, account_name=account["account_name"] + suffix,
                                                     mode="range_incremental", cycle_key=f'{account["since"]}至{until}')
                    verify_word(self.exporter, path, articles)
                    row.update(word_path=str(path), exported_count=0 if reused else len(articles),
                               word_article_count=len(articles), reused_word=reused)
                store.finish(account, batch["id"] if batch else None, coverage, complete=complete)
                row["full_interval_verified"] = complete
                if observed:
                    row["observed_export_progress"] = store.record_observed_export(account, batch["id"] if batch else None,
                        coverage, search_status=row.get("search_status"))
                    problem = crawl["failed"] or crawl["restricted"] or crawl["search_unresolved"]
                    row["status"] = "observed_partial" if problem else ("observed_exported" if batch else "observed_checked")
            except Exception:
                row["status"] = "paused" if store.paused() else "failed_resumable"
                row["reason"] = "account_stage_failed_no_success_checkpoint"
        report.update(accounts=results, cycle=cycle,
                      status="completed" if results and all(r["status"] == "completed" for r in results) else "needs_attention")
        if observed and results and all(r["status"] in ("observed_exported", "observed_checked") for r in results):
            report["status"] = "observed_completed"
        if observed:
            report["coverage_notice"] = "仅已发现增量：完整发表覆盖未知；缺口/待验区间保留，全量验收节点未推进"
        if search_stop:
            report["required_actions"] = ["搜狗核对遇验证后已停止；未改用移动端、筛选参数或微信界面"]
        if any(r["word_path"] or r["status"] == "completed" for r in results):
            display = [dict(r, status=("已发现增量（非全量验收）" if observed else
                                      ("已完成" if r["status"] == "completed" else "未完整验收")),
                            exported_count=r.get("word_article_count", r["exported_count"]),
                            word_path=r["word_path"] or ("无新增" if r["status"] == "completed" else "未输出 详见运行结果")) for r in results]
            summary_version = hashlib.sha256(json.dumps([
                {key: r[key] for key in ("account_name", "status", "exported_count", "word_path")}
                for r in display], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]
            summary_label = "公众号已发现增量汇总" if observed else "公众号增量汇总"
            summary = folder / f"{start}至{until}_{summary_label}_{summary_version}.docx"
            if not summary.exists():
                scope_label = " 已发现增量（非全量验收）" if observed else ""
                self.exporter.export_summary(display, summary, cycle_key=f"{start}至{until}" + scope_label)
            report["summary_path"] = str(summary)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "运行结果.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)
        report["report_path"] = str(path)
        return report
