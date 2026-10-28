"""Offline coverage: access restrictions must not trigger visible recovery."""
from datetime import datetime, timezone

import httpx
import pytest

from wechat_crawler.automation_store import AutomationStore
from wechat_crawler.crawler.discoverers import history_discoverer as history
from wechat_crawler.crawler.discoverers.wechat_cache_discoverer import CacheArticleCandidate
from wechat_crawler.crawler.storage import Storage
from wechat_crawler.web_app import ArchiveWebService
from wechat_crawler.weekly_sync import ExistingCrawlerAdapter, WeeklySyncRunner


@pytest.fixture(autouse=True)
def no_external_requests(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("offline regression must not request WeChat")
    monkeypatch.setattr(httpx.Client, "get", forbidden)


def response(status=200, *, captcha=False):
    url = "https://mp.weixin.qq.com/mp/wappoc_appmsgcaptcha" if captcha else "https://mp.weixin.qq.com/s"
    return httpx.Response(
        status, request=httpx.Request("GET", url),
        text="请输入验证码" if captcha else "window.appmsg_token = 'synthetic-token';",
    )


@pytest.mark.parametrize("status,captcha", [(401, False), (403, False), (429, False), (200, True)])
def test_session_restriction_stops_before_next_candidate(tmp_path, monkeypatch, status, captcha):
    candidates = [CacheArticleCandidate(
        public_url=f"https://mp.weixin.qq.com/s?__biz=ExactBiz%3D%3D&mid={mid}&idx=1",
        fetch_url=f"https://mp.weixin.qq.com/s?__biz=ExactBiz%3D%3D&mid={mid}&idx=1&uin=fake&key=fake{mid}",
        source_file="synthetic", biz="ExactBiz==", mid=str(mid), idx="1", sn=None, chksm=None,
    ) for mid in (2, 1)]
    monkeypatch.setattr(history.WechatCacheDiscoverer, "discover", lambda *args, **kwargs: candidates)
    calls = []

    def get(*args, **kwargs):
        calls.append(1)
        return response(status, captcha=captcha) if len(calls) == 1 else response()

    monkeypatch.setattr(httpx.Client, "get", get)
    discoverer = history.WechatHistoryDiscoverer(tmp_path, "ExactBiz==", "gh_test", delay_seconds=0)
    with pytest.raises(history.HistoryDiscoveryError) as caught:
        discoverer.load_session()
    assert calls == [1]
    assert type(caught.value).__name__ == "HistoryAccessRestrictedError"
    assert not history.is_session_recovery_error(caught.value)
    assert not history.is_transient_history_error(caught.value)


@pytest.mark.parametrize("status,captcha", [(401, False), (403, False), (429, False), (200, True)])
def test_page_restriction_does_not_reload_or_repeat_request(tmp_path, monkeypatch, status, captcha):
    discoverer = history.WechatHistoryDiscoverer(tmp_path, "ExactBiz==", "gh_test", delay_seconds=0)
    loads, requests = [], []

    def load():
        loads.append(1)
        return history.HistorySession("ExactBiz==", "fake", "fake", "fake", "fake")

    def get(*args, **kwargs):
        requests.append(1)
        return response(status, captcha=captcha)

    monkeypatch.setattr(discoverer, "load_session", load)
    monkeypatch.setattr(httpx.Client, "get", get)
    with pytest.raises(history.HistoryDiscoveryError) as caught:
        list(discoverer.discover_pages())
    assert loads == [1]
    assert requests == [1]
    assert type(caught.value).__name__ == "HistoryAccessRestrictedError"


def test_verification_text_is_not_an_automatic_recovery_instruction():
    assert not history.is_session_recovery_error("signed article URL requires verification")
    assert not history.is_session_recovery_error("profile history request requires verification")
    assert history.is_session_recovery_error("missing appmsg_token in signed article page")
    assert history.is_session_recovery_error("profile history returned ret=-3 errmsg=invalid session")


def test_valid_data_session_does_not_require_window_capture_or_input(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "archive.sqlite")
    automation = AutomationStore(storage)
    automation.init_db()
    account_id = storage.upsert_account("测试号", biz="ExactBiz==", wechat_id="gh_test")
    subscription = automation.ensure_subscription(account_id, "https://mp.weixin.qq.com/s/test", adopt_existing=False)
    job = automation.create_job(int(subscription["id"]), "offline-protected-test")
    monkeypatch.setattr(history.WechatHistoryDiscoverer, "load_session", lambda self: history.HistorySession(
        "ExactBiz==", "fake", "fake", "fake", "fake",
    ))
    requests = []

    def get(self, url, **kwargs):
        assert url == "https://mp.weixin.qq.com/mp/profile_ext"
        assert kwargs["params"]["offset"] == "0"
        assert kwargs["params"]["__biz"] == "ExactBiz=="
        requests.append(1)
        return httpx.Response(200, request=httpx.Request("GET", url), json={
            "ret": 0, "general_msg_list": '{"list": []}', "can_msg_continue": 0,
        })

    def forbidden(*args, **kwargs):
        raise AssertionError("an available data session must not inspect or operate the protected window")

    monkeypatch.setattr(httpx.Client, "get", get)
    monkeypatch.setattr("wechat_crawler.macos_wechat.WechatWindowProbe.inspect", forbidden)
    monkeypatch.setattr("wechat_crawler.macos_wechat.WechatSessionRefresher.prepare_account", forbidden)
    monkeypatch.setattr("wechat_crawler.cleanup.ArchiveCleanup.prune", lambda *args, **kwargs: None)
    adapter = ExistingCrawlerAdapter(storage, tmp_path / "output", {"automation": {"prepare_wechat_session": True}})
    result = adapter.run(subscription, job, lambda *args: None)
    assert result.status == "completed"
    assert requests == [1]


def test_shared_restriction_blocks_cycle_without_ui_export_or_success_advance(tmp_path, monkeypatch):
    storage = Storage(tmp_path / "archive.sqlite")
    automation = AutomationStore(storage)
    automation.init_db()
    previous = "2026-10-08T10:00:00+00:00"
    for index in range(2):
        account_id = storage.upsert_account(f"公开测试{index}", biz=f"ExactBiz{index}==")
        automation.ensure_subscription(account_id, f"https://mp.weixin.qq.com/s/test{index}", adopt_existing=False)
    with storage.connect() as conn:
        conn.execute("UPDATE subscriptions SET next_run_at='2026-10-25T21:00:00+00:00', last_success_at=?", (previous,))

    calls = []

    def restricted(self, **kwargs):
        calls.append(self.biz)
        error_type = getattr(history, "HistoryAccessRestrictedError", history.HistoryDiscoveryError)
        raise error_type("profile history request requires verification")

    def forbidden(*args, **kwargs):
        raise AssertionError("restriction must not open WeChat or export Word")

    monkeypatch.setattr(history.WechatHistoryDiscoverer, "discover_pages", restricted)
    monkeypatch.setattr("wechat_crawler.macos_wechat.WechatSessionRefresher.prepare_account", forbidden)
    monkeypatch.setattr("wechat_crawler.incremental_word.IncrementalWordExporter.export_account", forbidden)
    monkeypatch.setattr("wechat_crawler.cleanup.ArchiveCleanup.prune", lambda *args, **kwargs: None)
    runner = WeeklySyncRunner(storage, tmp_path / "output", {"automation": {"prepare_wechat_session": True}})

    results = runner.run_due(now=datetime(2026, 10, 26, tzinfo=timezone.utc))

    assert len(calls) == 1
    assert [result.status for result in results] == ["blocked", "blocked"]
    assert all(result.word_path is None and result.exported_count == 0 for result in results)
    assert all(row["status"] == "blocked" for row in automation.list_jobs())
    assert all(row["state"] == "blocked" and row["enabled"] for row in automation.list_subscriptions())
    assert all(row["last_success_at"] == previous for row in automation.list_subscriptions())
    assert automation.due_subscriptions(datetime(2027, 1, 1, tzinfo=timezone.utc)) == []
    assert not automation.is_automation_paused()
    assert automation.latest_completed_cycle() is None
    widget = ArchiveWebService(storage, tmp_path / "output", {}).status_widget()
    assert widget["session_requests"] == []
    assert not list((tmp_path / "output").rglob("*.docx"))
