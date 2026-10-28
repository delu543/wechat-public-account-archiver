from __future__ import annotations

from datetime import datetime, timedelta, timezone

from wechat_crawler.automation_store import AutomationStore
from wechat_crawler.crawler.models import ParsedArticle
from wechat_crawler.crawler.storage import Storage


def make_article(url: str, account_name: str, biz: str, title: str, content: str) -> ParsedArticle:
    return ParsedArticle(
        source_url=url,
        normalized_url=url,
        title=title,
        account_name=account_name,
        account_id=biz,
        wechat_id=None,
        author="author",
        publish_time="2026-07-10 10:00:00",
        summary=None,
        content_html=f"<p>{content}</p>",
        content_text=content,
        raw_html=f"<html><p>{content}</p></html>",
        content_hash="shared-hash" if content == "shared" else f"hash-{biz}-{title}",
    )


def save(storage: Storage, article: ParsedArticle) -> int:
    run_id = storage.start_run("test")
    url_id = storage.upsert_article_url(article.source_url, "test", run_id=run_id)
    article_id = storage.save_article(article, url_id, run_id)
    storage.finish_run(run_id, "completed")
    return article_id


def test_existing_accounts_are_adopted_as_baseline_idempotently(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    automation = AutomationStore(storage)
    automation.init_db()
    save(storage, make_article("https://mp.weixin.qq.com/s/one", "A", "biz-a", "one", "body"))

    adopted = automation.adopt_existing_accounts(now=datetime(2026, 7, 17, 12, tzinfo=timezone.utc))
    assert len(adopted) == 1
    assert automation.adopt_existing_accounts() == []
    subscriptions = automation.list_subscriptions()
    assert subscriptions[0]["onboarding_mode"] == "adopt_existing"
    assert subscriptions[0]["pending_export_count"] == 0
    assert automation.unexported_articles(int(subscriptions[0]["account_id"])) == []


def test_adopting_account_with_existing_word_membership_does_not_duplicate_article(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    automation = AutomationStore(storage)
    automation.init_db()
    article_id = save(
        storage,
        make_article("https://mp.weixin.qq.com/s/exported", "A", "biz-a", "exported", "body"),
    )
    account_id = storage.account_id_by_biz("biz-a")
    automation.complete_export_batch(
        account_id=account_id,
        cycle_key="historical-full",
        kind="initial_full",
        article_ids=[article_id],
        word_path="output/full/A.docx",
    )

    adopted = automation.adopt_existing_accounts(
        now=datetime(2026, 8, 24, 14, tzinfo=timezone.utc)
    )

    assert len(adopted) == 1
    subscription = automation.list_subscriptions()[0]
    assert subscription["pending_export_count"] == 0
    with storage.connect() as conn:
        memberships = conn.execute(
            "SELECT COUNT(*) FROM export_batch_articles WHERE article_id = ?", (article_id,)
        ).fetchone()[0]
    assert memberships == 1


def test_activate_current_baseline_absorbs_pending_and_resets_weekly_schedule(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    automation = AutomationStore(storage)
    automation.init_db()
    save(storage, make_article("https://mp.weixin.qq.com/s/old", "A", "biz-a", "old", "old"))
    automation.adopt_existing_accounts(now=datetime(2026, 8, 20, 12, tzinfo=timezone.utc))
    subscription = automation.list_subscriptions()[0]
    pending_id = save(
        storage,
        make_article("https://mp.weixin.qq.com/s/current", "A", "biz-a", "current", "current"),
    )
    job = automation.create_job(
        int(subscription["id"]),
        "legacy-cycle",
        trigger="schedule",
        scheduled_for=datetime(2026, 8, 23, 20, tzinfo=timezone.utc),
    )
    automation.update_job(job["id"], status="needs_session", phase="needs_session")
    with storage.connect() as conn:
        conn.execute(
            "UPDATE subscriptions SET enabled = 0, state = 'needs_session' WHERE id = ?",
            (subscription["id"],),
        )

    activated = automation.activate_current_baseline(
        schedule_weekday=6,
        schedule_hour=21,
        now=datetime(2026, 8, 24, 14, tzinfo=timezone.utc),
    )

    assert activated[0]["baseline_article_count"] == 1
    assert activated[0]["next_run_at"] == "2026-08-30T21:00:00+00:00"
    assert automation.unexported_articles(int(subscription["account_id"])) == []
    refreshed = automation.list_subscriptions()[0]
    assert refreshed["enabled"] == 1
    assert refreshed["onboarding_mode"] == "incremental"
    assert refreshed["schedule_hour"] == 21
    assert refreshed["state"] == "active"
    assert automation.job(job["id"])["status"] == "superseded"
    with storage.connect() as conn:
        membership = conn.execute(
            "SELECT COUNT(*) FROM export_batch_articles WHERE article_id = ?", (pending_id,)
        ).fetchone()[0]
    assert membership == 1

    repeated = automation.activate_current_baseline(
        schedule_weekday=6,
        schedule_hour=21,
        now=datetime(2026, 8, 24, 14, 1, tzinfo=timezone.utc),
    )
    assert repeated[0]["baseline_article_count"] == 0


def test_new_account_starts_full_then_exports_each_article_once(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    automation = AutomationStore(storage)
    automation.init_db()
    account_id = storage.upsert_account("New", biz="new-biz", source_url="https://mp.weixin.qq.com/s/seed")
    subscription = automation.ensure_subscription(account_id, "https://mp.weixin.qq.com/s/seed")
    assert subscription["onboarding_mode"] == "initial_full"

    article_id = save(
        storage,
        make_article("https://mp.weixin.qq.com/s/new", "New", "new-biz", "new", "new body"),
    )
    candidates = automation.unexported_articles(account_id)
    assert [row["id"] for row in candidates] == [article_id]

    job = automation.create_job(int(subscription["id"]), "2026-W29")
    assert job["mode"] == "initial_full"
    batch = automation.complete_export_batch(
        account_id=account_id,
        cycle_key="2026-W29",
        kind="initial_full",
        article_ids=[article_id, article_id],
        word_path="output/weekly/2026-07-19/New.docx",
        job_id=job["id"],
    )
    repeated = automation.complete_export_batch(
        account_id=account_id,
        cycle_key="2026-W29",
        kind="initial_full",
        article_ids=[article_id],
        word_path="ignored.docx",
        job_id=job["id"],
    )
    assert repeated["id"] == batch["id"]
    assert batch["article_count"] == 1
    assert automation.unexported_articles(account_id) == []


def test_jobs_are_persistent_and_deduplicated_by_account_cycle(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    automation = AutomationStore(storage)
    automation.init_db()
    account_id = storage.upsert_account("New", biz="new-biz", source_url="https://mp.weixin.qq.com/s/seed")
    subscription = automation.ensure_subscription(account_id, "https://mp.weixin.qq.com/s/seed")

    first = automation.create_job(int(subscription["id"]), "2026-W29")
    second = automation.create_job(int(subscription["id"]), "2026-W29")
    assert first["id"] == second["id"]

    automation.update_job(
        first["id"],
        status="discover",
        phase="discover",
        progress=20,
        counts={"discovered_count": 12},
        checkpoint={"offset": 20},
    )
    restored = AutomationStore(Storage(storage.db_path)).job(first["id"])
    assert restored["status"] == "discover"
    assert restored["discovered_count"] == 12
    assert restored["checkpoint_json"] == '{"offset": 20}'


def test_global_pause_is_persistent_and_independent_of_subscription_flags(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    automation = AutomationStore(storage)
    automation.init_db()
    account_id = storage.upsert_account(
        "A",
        biz="biz-a",
        source_url="https://mp.weixin.qq.com/s/a",
    )
    subscription = automation.ensure_subscription(
        account_id,
        "https://mp.weixin.qq.com/s/a",
    )

    assert automation.is_automation_paused() is False
    paused = automation.set_automation_paused(True)

    assert paused["paused"] is True
    assert AutomationStore(Storage(storage.db_path)).is_automation_paused() is True
    assert automation.list_subscriptions()[0]["enabled"] == subscription["enabled"] == 1

    resumed = automation.set_automation_paused(False)
    assert resumed["paused"] is False
    assert automation.is_automation_paused() is False


def test_same_content_hash_is_allowed_for_different_accounts(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    storage.init_db()
    first_id = save(storage, make_article("https://mp.weixin.qq.com/s/a", "A", "biz-a", "one", "shared"))
    second_id = save(storage, make_article("https://mp.weixin.qq.com/s/b", "B", "biz-b", "two", "shared"))
    assert first_id != second_id
    assert storage.stats()["articles"] == 2


def test_next_weekly_run_is_strictly_in_the_future():
    friday = datetime(2026, 7, 17, 12, tzinfo=timezone.utc)
    sunday = AutomationStore.next_weekly_run(friday, 6, 20, 0)
    assert sunday.isoformat() == "2026-07-19T20:00:00+00:00"
    after_sunday = datetime(2026, 7, 19, 21, tzinfo=timezone.utc)
    assert AutomationStore.next_weekly_run(after_sunday, 6, 20, 0).date().isoformat() == "2026-07-26"


def test_needs_session_is_deferred_instead_of_remaining_immediately_due(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    automation = AutomationStore(storage)
    automation.init_db()
    account_id = storage.upsert_account("A", biz="biz-a", source_url="https://mp.weixin.qq.com/s/a")
    subscription = automation.ensure_subscription(account_id, "https://mp.weixin.qq.com/s/a")
    current = datetime(2026, 7, 22, 12, tzinfo=timezone.utc)

    retry_at = automation.defer_subscription(
        int(subscription["id"]),
        "needs_session",
        "session unavailable",
        delay=timedelta(hours=12),
        now=current,
    )

    assert retry_at == "2026-07-23T00:00:00+00:00"
    assert automation.due_subscriptions(current + timedelta(hours=11)) == []
    assert len(automation.due_subscriptions(current + timedelta(hours=13))) == 1


def test_interrupted_sync_jobs_are_closed_without_deleting_history(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    automation = AutomationStore(storage)
    automation.init_db()
    account_id = storage.upsert_account("A", biz="biz-a", source_url="https://mp.weixin.qq.com/s/a")
    subscription = automation.ensure_subscription(account_id, "https://mp.weixin.qq.com/s/a")
    job = automation.create_job(int(subscription["id"]), "2026-W28")
    with storage.connect() as conn:
        conn.execute(
            "UPDATE sync_jobs SET status = 'discover', updated_at = '2026-07-20T00:00:00+00:00' WHERE id = ?",
            (job["id"],),
        )

    recovered = automation.recover_interrupted_jobs(
        older_than=timedelta(hours=6),
        now=datetime(2026, 7, 22, 12, tzinfo=timezone.utc),
    )

    assert recovered == 1
    restored = automation.job(job["id"])
    assert restored["status"] == "failed"
    assert "中断" in restored["error_message"]
