from wechat_crawler.crawler.exporter import MarkdownExporter
from wechat_crawler.crawler.models import ParsedArticle
from wechat_crawler.crawler.storage import Storage
from wechat_crawler.web_app import ArchiveJob, ArchiveWebService


def make_article(source_url: str) -> ParsedArticle:
    return ParsedArticle(
        source_url=source_url,
        normalized_url=source_url,
        title="平台测试文章",
        account_name="平台测试号",
        account_id="MzWebExample==",
        wechat_id="gh_web_example",
        author="作者B",
        publish_time="2026-07-01 09:00",
        summary="平台测试摘要",
        content_html="<p>平台正文</p>",
        content_text="平台正文",
        image_urls=[],
        cover_image_url=None,
        raw_html="<html></html>",
        content_hash="b" * 64,
        status="parsed",
        error_message=None,
    )


def test_web_service_lists_exported_accounts_and_articles(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    output_dir = tmp_path / "output"
    storage.init_db()
    run_id = storage.start_run("test")
    url = "https://mp.weixin.qq.com/s?__biz=MzWebExample==&mid=1&idx=1&sn=abc"
    url_id = storage.upsert_article_url(url, "url", run_id=run_id, account_hint="平台测试号")
    article_id = storage.save_article(make_article(url), article_url_id=url_id, run_id=run_id)
    storage.finish_run(run_id, "completed")
    MarkdownExporter(storage, output_dir).export_all(force=True)

    service = ArchiveWebService(storage, output_dir, config={})

    def fail_if_full_articles_are_loaded(account_id):
        raise AssertionError("list endpoints must not load full article bodies")

    storage.articles_for_account = fail_if_full_articles_are_loaded

    accounts = service.list_accounts()
    assert accounts[0]["account_name"] == "平台测试号"
    assert accounts[0]["article_count"] == 1
    assert accounts[0]["folder_name"] == "平台测试号"

    articles = service.list_articles(int(accounts[0]["id"]), limit=20, offset=0)
    assert articles["total"] == 1
    assert articles["items"][0]["id"] == article_id
    assert articles["items"][0]["title"] == "平台测试文章"
    assert "source_url" not in articles["items"][0]

    article = service.get_article(article_id)
    assert article["title"] == "平台测试文章"
    assert "平台正文" in article["body_markdown"]


def test_archive_job_reports_progress_and_eta():
    job = ArchiveJob(
        job_id="job-1",
        input_url="https://mp.weixin.qq.com/s/example",
        since_date="2026-01-01",
    )
    job.discovery_mode = "URL历史接口"
    job.crawl_stage = "快速抓取正文"
    job.unresolved_by_reason = {"captcha": 2}
    job.mark_running("抓取正文", total=10)
    job.mark_progress(done=4)

    snapshot = job.to_dict()

    assert snapshot["status"] == "running"
    assert snapshot["phase"] == "抓取正文"
    assert snapshot["done"] == 4
    assert snapshot["total"] == 10
    assert snapshot["progress"] >= 35
    assert snapshot["eta_seconds"] is not None
    assert snapshot["discovery_mode"] == "URL历史接口"
    assert snapshot["crawl_stage"] == "快速抓取正文"
    assert snapshot["unresolved_by_reason"] == {"captcha": 2}
    assert snapshot["since_date"] == "2026-01-01"


def test_archive_job_reports_completed_articles_skipped_by_deduplication():
    job = ArchiveJob(job_id="job-1", input_url="https://mp.weixin.qq.com/s/example")
    job.discovered_count = 14
    job.crawled_count = 8
    job.mark_done()

    assert job.to_dict()["already_archived_count"] == 6


def test_archive_job_does_not_report_premature_deduplication_while_running():
    job = ArchiveJob(job_id="job-1", input_url="https://mp.weixin.qq.com/s/example")
    job.discovered_count = 14
    job.mark_running("快速抓取正文", total=8)

    assert job.to_dict()["already_archived_count"] == 0


def test_storage_reports_deduplicated_article_update_as_not_created(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    storage.init_db()
    run_id = storage.start_run("test")
    value = make_article("https://mp.weixin.qq.com/s/deduplicated")
    url_id = storage.upsert_article_url(value.source_url, "url", run_id=run_id)

    first_id, first_created = storage.save_article_with_status(value, url_id, run_id)
    second_id, second_created = storage.save_article_with_status(value, url_id, run_id)

    assert first_created is True
    assert second_created is False
    assert second_id == first_id


def test_archive_job_reports_session_recovery_steps():
    job = ArchiveJob(job_id="job-1", input_url="https://mp.weixin.qq.com/s/example")
    job.mark_needs_session("需要刷新本机微信可见会话")

    snapshot = job.to_dict()

    assert snapshot["status"] == "needs_session"
    assert snapshot["phase"] == "等待微信会话"
    assert snapshot["recovery_action"]
    assert "已先尝试保存的文章链接和 biz" in snapshot["recovery_action"]
    assert len(snapshot["recovery_steps"]) >= 3
    assert any("自动搜索" in step for step in snapshot["recovery_steps"])


def test_web_service_paginates_large_article_lists(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    output_dir = tmp_path / "output"
    storage.init_db()
    run_id = storage.start_run("test")
    for index in range(3):
        url = f"https://mp.weixin.qq.com/s?__biz=MzWebExample==&mid={index}&idx=1&sn=abc{index}"
        url_id = storage.upsert_article_url(url, "url", run_id=run_id, account_hint="平台测试号")
        article = make_article(url)
        article.title = f"平台测试文章 {index}"
        article.content_hash = f"{index}" * 64
        storage.save_article(article, article_url_id=url_id, run_id=run_id)
    storage.finish_run(run_id, "completed")
    service = ArchiveWebService(storage, output_dir, config={})
    account_id = int(service.list_accounts()[0]["id"])

    first_page = service.list_articles(account_id, limit=2, offset=0)
    second_page = service.list_articles(account_id, limit=2, offset=2)

    assert first_page["total"] == 3
    assert first_page["limit"] == 2
    assert first_page["offset"] == 0
    assert [item["title"] for item in first_page["items"]] == ["平台测试文章 2", "平台测试文章 1"]
    assert [item["title"] for item in second_page["items"]] == ["平台测试文章 0"]


def test_web_service_reports_unresolved_url_reasons(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    output_dir = tmp_path / "output"
    storage.init_db()
    account_id = storage.upsert_account("平台测试号", biz="MzWebExample==", wechat_id="gh_web_example")
    run_id = storage.start_run("test")
    failed_id = storage.upsert_article_url(
        "https://mp.weixin.qq.com/s?__biz=MzWebExample==&mid=10&idx=1&sn=abc",
        "url",
        run_id=run_id,
        account_hint="平台测试号",
        account_id=account_id,
    )
    restricted_id = storage.upsert_article_url(
        "https://mp.weixin.qq.com/s?__biz=MzWebExample==&mid=11&idx=1&sn=def",
        "url",
        run_id=run_id,
        account_hint="平台测试号",
        account_id=account_id,
    )
    storage.mark_url_status(failed_id, "failed", "missing article title")
    storage.mark_url_status(restricted_id, "restricted", "captcha")

    service = ArchiveWebService(storage, output_dir, config={})
    job = ArchiveJob(job_id="job-1", input_url="https://mp.weixin.qq.com/s/example")
    service._refresh_unresolved_counts(job, account_id)

    assert job.failed_count == 1
    assert job.restricted_count == 1
    assert job.unresolved_by_reason == {"missing article title": 1, "captcha": 1}


def test_web_service_limits_unresolved_counts_to_current_history_urls(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    storage.init_db()
    account_id = storage.upsert_account("平台测试号", biz="MzWebExample==", wechat_id="gh_web_example")
    run_id = storage.start_run("test")
    old_id = storage.upsert_article_url(
        "https://mp.weixin.qq.com/s?__biz=MzWebExample==&mid=10&idx=1&sn=old",
        "url",
        run_id=run_id,
        account_id=account_id,
    )
    current_id = storage.upsert_article_url(
        "https://mp.weixin.qq.com/s?__biz=MzWebExample==&mid=11&idx=1&sn=current",
        "url",
        run_id=run_id,
        account_id=account_id,
    )
    storage.mark_url_status(old_id, "restricted", "unavailable")
    storage.mark_url_status(current_id, "restricted", "captcha")

    service = ArchiveWebService(storage, tmp_path / "output", config={})
    job = ArchiveJob(job_id="job-1", input_url="https://mp.weixin.qq.com/s/example")
    job.target_url_ids.add(current_id)
    service._refresh_unresolved_counts(job, account_id)

    assert job.failed_count == 0
    assert job.restricted_count == 1
    assert job.unresolved_by_reason == {"captcha": 1}


def test_web_service_retries_only_current_history_unavailable_urls(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "archive.sqlite")
    storage.init_db()
    account_id = storage.upsert_account("平台测试号", biz="MzWebExample==", wechat_id="gh_web_example")
    run_id = storage.start_run("test")
    old_url = "https://mp.weixin.qq.com/s?__biz=MzWebExample==&mid=10&idx=1&sn=old"
    current_url = "https://mp.weixin.qq.com/s?__biz=MzWebExample==&mid=11&idx=1&sn=current"
    old_id = storage.upsert_article_url(old_url, "url", run_id=run_id, account_id=account_id)
    current_id = storage.upsert_article_url(current_url, "url", run_id=run_id, account_id=account_id)
    storage.mark_url_status(old_id, "restricted", "unavailable")
    storage.mark_url_status(current_id, "restricted", "unavailable")
    fetched: list[str] = []

    class RestrictedResult:
        status = "restricted"
        error_message = "unavailable"

    class Fetcher:
        def fetch(self, url):
            fetched.append(url)
            return RestrictedResult()

    service = ArchiveWebService(storage, tmp_path / "output", config={})
    monkeypatch.setattr(service, "_build_fetcher", lambda **kwargs: Fetcher())
    job = ArchiveJob(
        job_id="job-1",
        input_url="https://mp.weixin.qq.com/s/example",
        since_date="2026-07-01",
    )
    job.target_url_ids.add(current_id)

    service._retry_unavailable(job, account_id, "MzWebExample==", expected_biz="MzWebExample==")

    assert fetched == [current_url]


def test_web_service_validates_start_date_before_starting_thread(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "archive.sqlite")
    storage.init_db()
    service = ArchiveWebService(storage, tmp_path / "output", config={})

    class DeferredThread:
        def __init__(self, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            return None

    monkeypatch.setattr("wechat_crawler.web_app.threading.Thread", DeferredThread)

    job = service.start_reverse_job("https://mp.weixin.qq.com/s/example", since_date="2026/1/2")

    assert job.since_date == "2026-01-02"

    try:
        service.start_reverse_job("https://mp.weixin.qq.com/s/example", since_date="2026-02-30")
    except ValueError as exc:
        assert "YYYY-MM-DD" in str(exc)
    else:
        raise AssertionError("invalid date should be rejected")
