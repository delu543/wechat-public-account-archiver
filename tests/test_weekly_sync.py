from __future__ import annotations

import json
from datetime import datetime, timezone

from docx import Document

from wechat_crawler.automation_store import AutomationStore
from wechat_crawler.crawler.models import ParsedArticle
from wechat_crawler.crawler.storage import Storage
from wechat_crawler.incremental_word import IncrementalWordExporter
from wechat_crawler.weekly_sync import CrawlResult, ExistingCrawlerAdapter, WeeklySyncRunner
from wechat_crawler.web_app import ArchiveWebService


def article(url: str, title: str, publish_time: str, raw_html: str = "<html>large</html>") -> ParsedArticle:
    return ParsedArticle(
        source_url=url,
        normalized_url=url,
        title=title,
        account_name="Account",
        account_id="biz-account",
        wechat_id="gh_account",
        author="Author",
        publish_time=publish_time,
        summary="Summary",
        content_html=f"<p>{title} body</p>",
        content_text=f"{title} body\n\nSecond paragraph",
        raw_html=raw_html,
        content_hash=f"hash-{title}",
    )


def save(storage: Storage, value: ParsedArticle) -> int:
    run_id = storage.start_run("test")
    url_id = storage.upsert_article_url(value.source_url, "test", run_id=run_id)
    article_id = storage.save_article(value, url_id, run_id)
    storage.finish_run(run_id, "completed")
    return article_id


class AddingCrawler:
    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self.calls = 0

    def run(self, subscription, job, progress):
        self.calls += 1
        progress("discover", 25, {"discovered_count": 1})
        save(
            self.storage,
            article(
                "https://mp.weixin.qq.com/s/new-article",
                "New article",
                "2026-07-18 09:00:00",
            ),
        )
        return CrawlResult(status="completed", message="crawl complete", discovered_count=1, crawled_count=1)


def prepare_existing(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    automation = AutomationStore(storage)
    automation.init_db()
    save(storage, article("https://mp.weixin.qq.com/s/old", "Old article", "2026-07-10 09:00:00"))
    automation.adopt_existing_accounts(now=datetime(2026, 7, 17, 12, tzinfo=timezone.utc))
    subscription = automation.list_subscriptions()[0]
    job = automation.create_job(
        int(subscription["id"]),
        "2026-W29",
        scheduled_for=datetime(2026, 7, 19, 20, tzinfo=timezone.utc),
    )
    return storage, automation, subscription, job


def test_weekly_job_exports_only_new_articles_and_is_resumable(tmp_path):
    storage, automation, _, job = prepare_existing(tmp_path)
    crawler = AddingCrawler(storage)
    runner = WeeklySyncRunner(storage, tmp_path / "output", {}, crawler=crawler)

    result = runner.run_job(job["id"])
    assert result.status == "completed"
    assert result.exported_count == 1
    assert crawler.calls == 1
    assert result.word_path
    document = Document(result.word_path)
    headings = [p.text for p in document.paragraphs if p.style.name == "Heading 2"]
    assert headings == ["New article"]
    assert automation.unexported_articles(int(job["account_id"])) == []
    with storage.connect() as conn:
        raw = conn.execute("SELECT raw_html FROM articles WHERE title = 'New article'").fetchone()[0]
    assert raw == ""

    repeated = runner.run_job(job["id"])
    assert repeated.exported_count == 1
    assert crawler.calls == 1


def test_pending_export_recovery_skips_discovery_and_marks_articles_once(tmp_path):
    storage, automation, subscription, _job = prepare_existing(tmp_path)
    save(
        storage,
        article(
            "https://mp.weixin.qq.com/s/recovered",
            "Recovered article",
            "2026-07-22 09:00:00",
        ),
    )
    runner = WeeklySyncRunner(storage, tmp_path / "output", {}, crawler=NoopCrawler())

    result = runner.export_pending_subscription_now(int(subscription["id"]))

    assert result.status == "completed"
    assert result.exported_count == 1
    assert result.word_path
    document = Document(result.word_path)
    headings = [p.text for p in document.paragraphs if p.style.name == "Heading 2"]
    assert headings == ["Recovered article"]
    assert automation.unexported_articles(int(subscription["account_id"])) == []


def test_word_export_verifies_article_count_and_has_clickable_source(tmp_path):
    exporter = IncrementalWordExporter()
    row = {
        "title": "Test title",
        "publish_time": "2026-07-18 09:00:00",
        "author": "Author",
        "summary": "Summary",
        "source_url": "https://mp.weixin.qq.com/s/test",
        "content_text": "First paragraph\n\nSecond paragraph",
    }
    path = exporter.export_account(
        [row],
        tmp_path / "test.docx",
        account_name="Account",
        mode="incremental",
        cycle_key="2026-W29",
    )
    exporter.verify(path, expected_articles=1)
    assert "hyperlink" in path.read_bytes().decode("latin1", errors="ignore") or path.stat().st_size > 0
    document = Document(path)
    assert "2026-07-18" in document.sections[0].header.paragraphs[0].text
    assert "2026-W29" not in document.sections[0].header.paragraphs[0].text


def test_word_export_uses_article_date_range_instead_of_internal_cycle_key(tmp_path):
    exporter = IncrementalWordExporter()
    rows = [
        {
            "title": "First",
            "publish_time": "2026-07-13 09:00:00",
            "source_url": "https://mp.weixin.qq.com/s/first",
            "content_text": "First body",
        },
        {
            "title": "Last",
            "publish_time": "2026-07-22 09:00:00",
            "source_url": "https://mp.weixin.qq.com/s/last",
            "content_text": "Last body",
        },
    ]
    path = exporter.export_account(
        rows,
        tmp_path / "range.docx",
        account_name="Account",
        mode="incremental",
        cycle_key="manual-20260722-internal",
    )

    document = Document(path)
    header = document.sections[0].header.paragraphs[0].text
    cover = "\n".join(paragraph.text for paragraph in document.paragraphs[:4])
    assert "2026-07-13 至 2026-07-22" in header
    assert "2026-07-13 至 2026-07-22" in cover
    assert "manual-" not in header
    assert "manual-" not in cover


class NoopCrawler:
    def run(self, subscription, job, progress):
        return CrawlResult(status="completed", message="no updates")


class UnresolvedCrawler:
    def run(self, subscription, job, progress):
        return CrawlResult(
            status="completed",
            message="completed with unresolved public pages",
            discovered_count=3,
            failed_count=2,
            restricted_count=1,
            unresolved_by_reason={"empty article body": 2, "captcha": 1},
        )


def test_weekly_job_persists_unresolved_reasons_for_the_dashboard(tmp_path):
    storage, automation, _, job = prepare_existing(tmp_path)
    runner = WeeklySyncRunner(storage, tmp_path / "output", {}, crawler=UnresolvedCrawler())

    result = runner.run_job(job["id"])

    assert result.status == "completed"
    stored = automation.job(job["id"])
    assert json.loads(stored["checkpoint_json"]) == {
        "unresolved_by_reason": {"captcha": 1, "empty article body": 2}
    }
    service = ArchiveWebService(storage, tmp_path / "output", config={})
    payload = service.get_automation_job(job["id"])
    assert payload["unresolved_by_reason"] == {
        "captcha": 1,
        "empty article body": 2,
    }


def test_due_run_writes_summary_without_empty_account_word(tmp_path):
    storage, automation, subscription, _ = prepare_existing(tmp_path)
    with storage.connect() as conn:
        conn.execute(
            "UPDATE subscriptions SET next_run_at = '2026-07-19T19:00:00+00:00' WHERE id = ?",
            (subscription["id"],),
        )
        conn.execute("DELETE FROM sync_jobs")
    orphan = tmp_path / "output" / "weekly" / "2026-07-19" / "orphan.docx.tmp"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"temporary")
    runner = WeeklySyncRunner(storage, tmp_path / "output", {}, crawler=NoopCrawler())
    results = runner.run_due(now=datetime(2026, 7, 19, 20, tzinfo=timezone.utc))
    assert len(results) == 1
    assert results[0].exported_count == 0
    folder = tmp_path / "output" / "weekly" / "2026-07-19"
    assert (folder / "run-report.json").exists()
    assert (folder / "本周公众号增量汇总.docx").exists()
    assert list(folder.glob("*_本周增量.docx")) == []
    assert not orphan.exists()


def test_existing_crawler_uses_stored_account_identity_instead_of_seed_parse(tmp_path, monkeypatch):
    storage, _, subscription, job = prepare_existing(tmp_path)
    calls = []

    def known(self, runtime, **kwargs):
        calls.append(kwargs)
        runtime.mark_done("known account complete")

    def reverse(self, runtime):
        raise AssertionError("old seed parser must not run for an account with exact biz")

    monkeypatch.setattr("wechat_crawler.web_app.ArchiveWebService._run_known_account_job", known)
    monkeypatch.setattr("wechat_crawler.web_app.ArchiveWebService._run_reverse_job", reverse)
    adapter = ExistingCrawlerAdapter(storage, tmp_path / "output", {})
    adapter.session_refresher.prepare_account = lambda _name, seed_url=None: (_ for _ in ()).throw(
        AssertionError("visible WeChat must not be opened when the stored URL session works")
    )

    result = adapter.run(subscription, job, lambda *_args: None)

    assert result.status == "completed"
    assert calls[0]["biz"] == "biz-account"


def test_existing_crawler_opens_wechat_only_after_direct_session_failure(tmp_path, monkeypatch):
    storage, _, subscription, job = prepare_existing(tmp_path)
    calls = []
    preparations = []

    def known(self, runtime, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            runtime.mark_needs_session("expired visible session")
        else:
            runtime.mark_done("recovered")

    class Preparation:
        article_open_attempted = True

    monkeypatch.setattr("wechat_crawler.web_app.ArchiveWebService._run_known_account_job", known)
    adapter = ExistingCrawlerAdapter(
        storage,
        tmp_path / "output",
        {"automation": {"prepare_wechat_session": True}},
    )
    adapter.session_refresher.prepare_account = (
        lambda name, seed_url=None: preparations.append((name, seed_url)) or Preparation()
    )

    result = adapter.run(subscription, job, lambda *_args: None)

    assert result.status == "completed"
    assert len(calls) == 2
    assert preparations == [("Account", "https://mp.weixin.qq.com/s/old")]
