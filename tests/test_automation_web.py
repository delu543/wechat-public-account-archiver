from __future__ import annotations

from datetime import datetime

import pytest

from wechat_crawler.crawler.models import ParsedArticle
from wechat_crawler.crawler.storage import Storage
from wechat_crawler.web_app import ArchiveJob, ArchiveWebService


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


def test_web_startup_skips_full_adoption_scan_after_subscriptions_exist(tmp_path, monkeypatch):
    storage, _service = prepare_service(tmp_path)
    monkeypatch.setattr(
        "wechat_crawler.web_app.AutomationStore.adopt_existing_accounts",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("full adoption scan")),
    )

    restarted = ArchiveWebService(storage, tmp_path / "output", config={})

    assert restarted.automation.has_subscriptions() is True
    with storage.connect() as conn:
        indexes = {row["name"] for row in conn.execute("PRAGMA index_list(articles)")}
    assert "idx_articles_account_publish" in indexes


def test_web_automation_can_pause_and_update_schedule(tmp_path):
    _, service = prepare_service(tmp_path)
    subscription_id = service.list_automation_subscriptions()[0]["id"]
    paused = service.set_subscription_enabled(subscription_id, False)
    assert paused["enabled"] is False
    schedule = service.update_schedule(2, 9, 30)
    assert schedule["weekday"] == 2
    assert "09:30:00" in schedule["next_run_at"]


def test_web_global_pause_blocks_new_crawls_but_keeps_status_service_available(tmp_path):
    storage, service = prepare_service(tmp_path)
    subscription_id = service.list_automation_subscriptions()[0]["id"]
    with storage.connect() as conn:
        conn.execute(
            "UPDATE subscriptions SET state = 'needs_session', last_error = 'session stale' WHERE id = ?",
            (subscription_id,),
        )

    paused = service.set_automation_paused(True)

    assert paused["paused"] is True
    overview = service.automation_overview()
    assert overview["automation_paused"] is True
    assert overview["wechat_window"]["state"] == "paused"
    assert overview["wechat_session_cache"]["status"] == "paused"
    assert service.health_status()["automation_paused"] is True
    assert service.status_widget()["session_requests"] == []
    with pytest.raises(ValueError, match="全部爬取已暂停"):
        service.start_subscription_run(subscription_id)
    with pytest.raises(ValueError, match="全部爬取已暂停"):
        service.start_reverse_job("https://mp.weixin.qq.com/s/new")

    resumed = service.set_automation_paused(False)
    assert resumed["paused"] is False
    assert len(service.status_widget()["session_requests"]) == 1


def test_running_archive_job_stops_at_master_pause_checkpoint(tmp_path):
    _storage, service = prepare_service(tmp_path)
    subscription = service.list_automation_subscriptions()[0]
    service.set_automation_paused(True)
    job = ArchiveJob(
        job_id="paused-job",
        input_url=subscription["seed_url"],
    )

    service._run_known_account_job(
        job,
        account_id=int(subscription["account_id"]),
        account_name=subscription["account_name"],
        biz=subscription["account_biz"],
        wechat_id=None,
    )

    assert job.status == "paused"
    assert "继续全部爬取" in job.message


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


def test_web_resumes_scheduled_job_after_foreground_session_without_ui_retry(
    tmp_path,
    monkeypatch,
):
    _, service = prepare_service(tmp_path)
    subscription_id = service.list_automation_subscriptions()[0]["id"]
    job = service.automation.create_job(
        subscription_id,
        "2026-W35",
        trigger="schedule",
        scheduled_for=datetime.now().astimezone(),
    )
    service.automation.begin_cycle(
        "2026-W35",
        scheduled_for=datetime.now().astimezone(),
        output_folder=str(tmp_path / "output" / "weekly" / "2026-08-30"),
        expected_account_count=1,
    )
    service.automation.update_job(
        str(job["id"]),
        status="needs_session",
        phase="needs_session",
        progress=15,
    )
    created = {}
    started = []

    class BackgroundOnlyRunner:
        def __init__(self, storage, output_dir, config):
            created["config"] = config

        def resume_latest_scheduled(self, subscription_id):
            return subscription_id

    class DeferredThread:
        def __init__(self, target, args, daemon):
            started.append((target, args, daemon))

        def start(self):
            return None

    monkeypatch.setattr("wechat_crawler.web_app.WeeklySyncRunner", BackgroundOnlyRunner)
    monkeypatch.setattr("wechat_crawler.web_app.threading.Thread", DeferredThread)

    response = service.resume_subscription_after_session(subscription_id)

    assert response["job_id"] == str(job["id"])
    assert response["status"] == "needs_session"
    assert created["config"]["automation"]["prepare_wechat_session"] is False
    assert started[0][1] == (subscription_id,)


def test_web_session_resume_is_idempotent_after_foreground_already_completed_job(
    tmp_path,
    monkeypatch,
):
    _, service = prepare_service(tmp_path)
    subscription_id = service.list_automation_subscriptions()[0]["id"]
    job = service.automation.create_job(
        subscription_id,
        "2026-W35",
        trigger="schedule",
        scheduled_for=datetime.now().astimezone(),
    )
    service.automation.begin_cycle(
        "2026-W35",
        scheduled_for=datetime.now().astimezone(),
        output_folder=str(tmp_path / "output" / "weekly" / "2026-08-30"),
        expected_account_count=1,
    )
    service.automation.update_job(
        str(job["id"]),
        status="completed",
        phase="completed",
        progress=100,
        finished=True,
    )

    class ThreadMustNotStart:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("completed foreground job must not start a duplicate worker")

    monkeypatch.setattr("wechat_crawler.web_app.threading.Thread", ThreadMustNotStart)

    response = service.resume_subscription_after_session(subscription_id)

    assert response["job_id"] == str(job["id"])
    assert response["status"] == "completed"


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
    assert payload["latest_completion"] is None
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
