from __future__ import annotations

from wechat_crawler.crawler.models import ParsedArticle
from wechat_crawler.crawler.storage import Storage
from wechat_crawler.web_app import ArchiveWebService


def make_article(url: str) -> ParsedArticle:
    return ParsedArticle(
        source_url=url,
        normalized_url=url,
        title="自动化测试文章",
        account_name="自动化测试号",
        account_id="MzAutomation==",
        wechat_id="gh_automation",
        author="作者",
        publish_time="2026-07-10 09:00:00",
        summary="摘要",
        content_html="<p>正文</p>",
        content_text="正文",
        raw_html="<html>正文</html>",
        content_hash="automation-hash",
    )


def prepare_service(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    storage.init_db()
    run_id = storage.start_run("test")
    url = "https://mp.weixin.qq.com/s/automation"
    url_id = storage.upsert_article_url(url, "test", run_id=run_id)
    storage.save_article(make_article(url), url_id, run_id)
    storage.finish_run(run_id, "completed")
    service = ArchiveWebService(
        storage,
        tmp_path / "output",
        config={"automation": {"weekday": 6, "hour": 20, "minute": 0}},
    )
    return storage, service


def test_web_automation_adopts_existing_account_and_reports_overview(tmp_path):
    _, service = prepare_service(tmp_path)
    subscriptions = service.list_automation_subscriptions()
    assert len(subscriptions) == 1
    assert subscriptions[0]["onboarding_mode"] == "adopt_existing"
    assert subscriptions[0]["pending_export_count"] == 0
    overview = service.automation_overview()
    assert overview["subscription_count"] == 1
    assert overview["enabled_count"] == 1
    assert overview["database_path"].endswith("archive.sqlite")
    assert overview["output_dir"] == str((tmp_path / "output").resolve())
    assert overview["wechat_session_cache"]["status"] in {"missing", "accessible"}
    health = service.health_status()
    assert health["status"] == "ok"
    assert health["database"].endswith("archive.sqlite")


def test_web_automation_can_pause_and_update_schedule(tmp_path):
    _, service = prepare_service(tmp_path)
    subscription_id = service.list_automation_subscriptions()[0]["id"]
    paused = service.set_subscription_enabled(subscription_id, False)
    assert paused["enabled"] is False
    schedule = service.update_schedule(2, 9, 30)
    assert schedule["weekday"] == 2
    assert "09:30:00" in schedule["next_run_at"]


def test_web_automation_starts_persistent_manual_job(tmp_path, monkeypatch):
    _, service = prepare_service(tmp_path)
    subscription_id = service.list_automation_subscriptions()[0]["id"]

    class DeferredThread:
        def __init__(self, target, args, daemon):
            self.target = target
            self.args = args
            self.daemon = daemon

        def start(self):
            return None

    monkeypatch.setattr("wechat_crawler.web_app.threading.Thread", DeferredThread)
    job = service.start_subscription_run(subscription_id)
    assert job["status"] == "scheduled"
    assert job["mode"] == "incremental"
    assert service.get_automation_job(job["job_id"])["job_id"] == job["job_id"]


def test_web_automation_can_start_pending_export_recovery(tmp_path, monkeypatch):
    _, service = prepare_service(tmp_path)
    subscription_id = service.list_automation_subscriptions()[0]["id"]
    started = []

    class DeferredThread:
        def __init__(self, target, args, daemon):
            started.append((target, args, daemon))

        def start(self):
            return None

    monkeypatch.setattr("wechat_crawler.web_app.threading.Thread", DeferredThread)

    response = service.start_pending_export(subscription_id)

    assert response == {"status": "started", "subscription_id": subscription_id}
    assert started[0][1] == (subscription_id,)


def test_status_widget_is_compact_and_local(tmp_path, monkeypatch):
    _, service = prepare_service(tmp_path)
    monkeypatch.setattr(
        "wechat_crawler.web_app.ArchiveCleanup.prune",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("full cleanup scan")),
    )
    payload = service.status_widget()

    assert payload["overview"]["subscription_count"] == 1
    assert payload["current_job"] is None
    assert payload["latest_word"] is None
    assert payload["output_dir"] == str((tmp_path / "output").resolve())
    assert payload["overview"]["reclaimable_bytes"] is None


def test_web_automation_opens_only_the_fixed_output_directory(tmp_path, monkeypatch):
    _, service = prepare_service(tmp_path)
    calls = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        "wechat_crawler.web_app.subprocess.run",
        lambda args, **kwargs: calls.append((args, kwargs)) or Result(),
    )

    payload = service.open_output_directory()

    assert payload == {
        "status": "opened",
        "output_dir": str((tmp_path / "output").resolve()),
    }
    assert calls[0][0] == ["/usr/bin/open", str((tmp_path / "output").resolve())]
