from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from wechat_crawler.automation_store import AutomationStore
from wechat_crawler.crawler.discoverers.wechat_cache_discoverer import WechatCacheDiscoverer
from wechat_crawler.crawler.models import ParsedArticle
from wechat_crawler.crawler.storage import Storage
from wechat_crawler.web_app import ArchiveJob, ArchiveWebService
from wechat_crawler.weekly_sync import ExistingCrawlerAdapter


SEED = "https://mp.weixin.qq.com/s/VU512lPD0QSTeGWDMh2ONQ"


@pytest.mark.parametrize("verified", [False, True])
def test_weekly_adapter_retries_only_after_verified_recovery(tmp_path, monkeypatch, verified):
    storage = Storage(tmp_path / "archive.sqlite")
    automation = AutomationStore(storage)
    automation.init_db()
    account_id = storage.upsert_account("测试号", biz="ExactBiz==", source_url=SEED)
    subscription = automation.ensure_subscription(account_id, SEED, adopt_existing=False)
    job = automation.create_job(
        int(subscription["id"]), "2026-W44",
        scheduled_for=datetime(2026, 10, 26, tzinfo=timezone.utc),
    )
    calls = []

    def known(self, runtime, **kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            runtime.mark_needs_session("expired")
        else:
            runtime.mark_done("recovered")

    monkeypatch.setattr(ArchiveWebService, "_run_known_account_job", known)
    adapter = ExistingCrawlerAdapter(storage, tmp_path / "output", {
        "automation": {"prepare_wechat_session": True},
        "history": {"app_data_dir": str(tmp_path / "wechat")},
    })
    adapter.session_refresher.prepare_account = lambda *args, **kwargs: SimpleNamespace(
        article_open_attempted=True, session_verified=verified,
        message="verified" if verified else "unchanged session",
    )

    result = adapter.run(subscription, job, lambda *args: None)

    assert len(calls) == (2 if verified else 1)
    assert result.status == ("completed" if verified else "needs_session")
    if not verified:
        assert "unchanged session" in result.message


@pytest.mark.parametrize("verified", [False, True])
def test_web_recovery_retains_seed_and_requires_verified_session(tmp_path, monkeypatch, verified):
    storage = Storage(tmp_path / "archive.sqlite")
    storage.init_db()
    service = ArchiveWebService(storage, tmp_path / "output", {
        "automation": {"prepare_wechat_session": True},
        "history": {"app_data_dir": str(tmp_path / "wechat")},
    })
    article = ParsedArticle(
        source_url=SEED, normalized_url=SEED, title="公开测试", account_name="测试号",
        account_id="ExactBiz==", wechat_id="gh_test", author=None,
        publish_time="2026-10-26 10:00:00", summary=None,
        content_html="<p>正文</p>", content_text="正文", content_hash="test-body",
    )
    monkeypatch.setattr("wechat_crawler.web_app.WechatArticleParser.parse", lambda *args: article)
    monkeypatch.setattr("wechat_crawler.web_app.ArchiveCleanup.prune", lambda *args, **kwargs: None)
    service._build_fetcher = lambda: SimpleNamespace(fetch=lambda url: SimpleNamespace(
        status="ok", text="<html></html>", final_url=url,
    ))
    calls = []
    preparations = []

    def discover(job, *args):
        calls.append(args)
        job.mark_needs_session("expired")
        return False

    def prepare(self, name, **kwargs):
        preparations.append((name, kwargs.get("seed_url")))
        return SimpleNamespace(article_open_attempted=True, session_verified=verified, message="recovery")

    service._discover_history = discover
    monkeypatch.setattr("wechat_crawler.web_app.WechatSessionRefresher.prepare_account", prepare)
    job = ArchiveJob(job_id="recovery", input_url=SEED, since_date="2026-10-20")

    service._run_reverse_job(job)

    assert preparations == [("测试号", SEED)]
    assert len(calls) == (2 if verified else 1)
    assert job.status == "needs_session"


def test_session_only_cache_scan_has_deadline_without_truncating_full_discovery(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        "wechat_crawler.crawler.discoverers.wechat_cache_discoverer.time.monotonic",
        lambda: clock[0],
    )
    discoverer = WechatCacheDiscoverer(tmp_path, target_biz="ExactBiz==", session_scan_timeout_seconds=1)
    paths = [tmp_path / str(index) for index in range(6)]
    discoverer._iter_files = lambda: iter(paths)
    reads = []

    def read(path):
        reads.append(path)
        clock[0] += 0.6
        return b"not an article"

    discoverer._read_bytes = read
    discoverer.discover(stop_when_session_fields=True)
    assert len(reads) == 2

    reads.clear()
    discoverer.discover()
    assert reads == paths
