import json
from datetime import datetime, timedelta, timezone

from wechat_crawler.crawler.exporter import MarkdownExporter
from wechat_crawler.crawler.models import ParsedArticle
from wechat_crawler.crawler.storage import Storage


def make_article(source_url: str) -> ParsedArticle:
    return ParsedArticle(
        source_url=source_url,
        normalized_url=source_url,
        title="测试文章标题",
        account_name="测试公众号",
        account_id="MzIxExample==",
        wechat_id="wechat_id_001",
        author="作者A",
        publish_time="2024-01-02 08:00",
        summary="这是一段文章摘要",
        content_html="<p>正文第一段</p>",
        content_text="正文第一段",
        image_urls=["https://mmbiz.qpic.cn/body.png"],
        cover_image_url="https://mmbiz.qpic.cn/cover.jpg",
        raw_html="<html></html>",
        content_hash="a" * 64,
        status="parsed",
        error_message=None,
    )


def test_storage_deduplicates_urls_and_exporter_groups_by_account(tmp_path):
    db_path = tmp_path / "archive.sqlite"
    output_dir = tmp_path / "output"
    url = "https://mp.weixin.qq.com/s?__biz=MzIxExample==&mid=1&idx=1&sn=abc"
    storage = Storage(db_path)
    storage.init_db()
    run_id = storage.start_run("test")

    first_url_id = storage.upsert_article_url(url, discoverer="url", run_id=run_id)
    second_url_id = storage.upsert_article_url(url, discoverer="url", run_id=run_id)
    article_id = storage.save_article(make_article(url), article_url_id=first_url_id, run_id=run_id)
    storage.finish_run(run_id, "completed")

    assert first_url_id == second_url_id
    assert article_id > 0
    assert storage.stats()["article_urls"] == 1
    assert storage.stats()["articles"] == 1

    exported = MarkdownExporter(storage, output_dir).export_all(force=True)

    assert exported == 1
    metadata_path = output_dir / "测试公众号" / "metadata.json"
    account_metadata_path = output_dir / "测试公众号" / "account_metadata.json"
    article_dir = output_dir / "测试公众号" / "articles"
    files = list(article_dir.glob("2024-01-02_测试文章标题*.md"))
    assert metadata_path.exists()
    assert account_metadata_path.exists()
    assert len(files) == 1
    text = files[0].read_text(encoding="utf-8")
    assert "title: 测试文章标题" in text
    assert "account_id: MzIxExample==" in text
    assert "account_biz: MzIxExample==" in text
    assert "discovered_from: url" in text
    assert "source_url: https://mp.weixin.qq.com/s?" in text
    assert "正文第一段" in text

    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["account_name"] == "测试公众号"
    assert metadata["account_biz"] == "MzIxExample=="
    assert metadata["article_count"] == 1


def test_exporter_can_export_only_one_account(tmp_path):
    db_path = tmp_path / "archive.sqlite"
    output_dir = tmp_path / "output"
    storage = Storage(db_path)
    storage.init_db()
    run_id = storage.start_run("test")
    first_url = "https://mp.weixin.qq.com/s?__biz=MzIxExample==&mid=1&idx=1&sn=abc"
    second_url = "https://mp.weixin.qq.com/s?__biz=MzIyOther==&mid=2&idx=1&sn=def"
    first_url_id = storage.upsert_article_url(first_url, discoverer="url", run_id=run_id)
    second_url_id = storage.upsert_article_url(second_url, discoverer="url", run_id=run_id)
    first = make_article(first_url)
    second = make_article(second_url)
    second.account_name = "另一个公众号"
    second.account_id = "MzIyOther=="
    second.wechat_id = "wechat_id_002"
    second.title = "另一个标题"
    second.content_hash = "b" * 64
    storage.save_article(first, article_url_id=first_url_id, run_id=run_id)
    storage.save_article(second, article_url_id=second_url_id, run_id=run_id)
    storage.finish_run(run_id, "completed")
    account_id = storage.account_id_by_biz("MzIxExample==")

    exported = MarkdownExporter(storage, output_dir).export_all(force=True, account_id=account_id)

    assert exported == 1
    assert (output_dir / "测试公众号" / "metadata.json").exists()
    assert not (output_dir / "另一个公众号").exists()


def test_pending_urls_respect_date_hints_without_breaking_full_crawl(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    storage.init_db()
    account_id = storage.upsert_account("日期测试号", biz="MzDateTest==")
    run_id = storage.start_run("test")
    old_id = storage.upsert_article_url(
        "https://mp.weixin.qq.com/s?__biz=MzDateTest==&mid=1&idx=1&sn=old",
        "wechat_history",
        run_id=run_id,
        account_id=account_id,
        publish_date_hint="2025-12-31",
    )
    new_id = storage.upsert_article_url(
        "https://mp.weixin.qq.com/s?__biz=MzDateTest==&mid=2&idx=1&sn=new",
        "wechat_history",
        run_id=run_id,
        account_id=account_id,
        publish_date_hint="2026-01-01",
    )
    unknown_id = storage.upsert_article_url(
        "https://mp.weixin.qq.com/s?__biz=MzDateTest==&mid=3&idx=1&sn=unknown",
        "url",
        run_id=run_id,
        account_id=account_id,
    )
    storage.mark_url_status(old_id, "out_of_range", "publish_time=2025-12-31")

    bounded = storage.pending_urls(account="MzDateTest==", since="2026-01-01")
    full = storage.pending_urls(account="MzDateTest==")

    assert {int(row["id"]) for row in bounded} == {new_id, unknown_id}
    assert {int(row["id"]) for row in full} == {old_id, new_id, unknown_id}


def test_stale_crawl_runs_are_closed_and_retained(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    storage.init_db()
    run_id = storage.start_run("history")
    with storage.connect() as conn:
        conn.execute(
            "UPDATE crawl_runs SET started_at = '2026-07-20T00:00:00+00:00' WHERE id = ?",
            (run_id,),
        )

    count = storage.recover_interrupted_runs(
        older_than=timedelta(hours=6),
        now=datetime(2026, 7, 22, 12, tzinfo=timezone.utc),
    )

    assert count == 1
    with storage.connect() as conn:
        row = conn.execute(
            "SELECT status, notes, finished_at FROM crawl_runs WHERE id = ?",
            (run_id,),
        ).fetchone()
    assert row["status"] == "failed"
    assert "interrupted" in row["notes"]
    assert row["finished_at"]
