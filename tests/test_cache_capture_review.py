"""Synthetic acceptance reports, with no live cache/network/product mutation."""
import copy
import json

import pytest

from tests.cache_capture_fixtures import record, scope
from wechat_crawler.cache_capture_review import main, review_account
from wechat_crawler.cache_capture_validation import record_value


def fixture():
    target = scope()
    rows = [record_value(record(day), target) for day in (8, 20, 27)]
    old = {"id": 1, "title": rows[0]["title"], "source_url": rows[0]["url"],
           "content_hash": rows[0]["content_hash"], "body_chars": len(rows[0]["content_text"]), "batch_id": None}
    account = {"_articles": [old], "incomplete_article_ids": []}
    return target, account, rows, {"delivered_baseline_max_article_id": 1}


def test_unknown_account_is_not_zero_increment_or_refresh_all():
    target, account, _, settings = fixture()
    result = review_account(target, account, None, settings)
    assert result["retained"] is result["new_candidates"] is None
    assert result["issues"] == ["specific_account_source_missing"]
    assert not result["zero_increment_claimed"]


def test_ready_cache_keeps_observed_counts_separate_from_complete():
    target, account, rows, settings = fixture()
    result = review_account(target, account, rows, settings)
    assert result["retained"] == 3 and result["new_candidates"] == 2
    assert result["observed_new_bodies"] == 2 and result["old_boundary_seen"]
    assert not result["full_interval_verified"] and not result["zero_increment_claimed"]
    assert all("content_text" not in r for r in result["articles"])


def test_old_preview_is_not_declared_ready_despite_cache_body_flag():
    target, account, rows, settings = fixture()
    old = dict(account["_articles"][0], id=2, source_url=rows[1]["url"],
               title=rows[1]["title"], content_hash=rows[1]["content_hash"], body_chars=1)
    account["_articles"].append(old)
    account["incomplete_article_ids"] = [2]
    result = review_account(target, account, rows, settings)
    assert result["new_candidates"] == 2 and result["observed_new_bodies"] == 1
    assert result["unresolved_previews"][0]["article_id"] == 2
    assert "known_preview_not_recovered" in result["issues"]


def test_longer_changed_preview_removes_only_that_issue_not_catalog_gate():
    target, account, rows, settings = fixture()
    old = dict(account["_articles"][0], id=2, source_url=rows[1]["url"], title=rows[1]["title"],
               content_hash="old-partial-body", body_chars=1)
    account["_articles"].append(old)
    account["incomplete_article_ids"] = [2]
    result = review_account(target, account, rows, settings)
    assert result["unresolved_previews"] == []
    assert not result["full_interval_verified"]


def test_delivered_hash_and_completed_batch_both_suppress_duplicates():
    target, account, rows, settings = fixture()
    account["_articles"].append(dict(account["_articles"][0], id=999, batch_id=1,
                                    source_url=rows[1]["url"], content_hash=rows[1]["content_hash"]))
    rows[2]["content_hash"] = rows[0]["content_hash"]
    result = review_account(target, account, rows, settings)
    assert result["new_candidates"] == 0
    assert not result["zero_increment_claimed"]  # Only observed cache was checked.


@pytest.mark.parametrize("mutation", ["foreign", "duplicate", "wrong_gh"])
def test_invalid_staged_identity_fails_closed(mutation):
    target, account, rows, settings = fixture()
    if mutation == "foreign": rows[1]["url"] = rows[1]["url"].replace("MzI0MDg5ODA2NQ", "OTHER_BIZ")
    if mutation == "duplicate": rows.append(copy.deepcopy(rows[0]))
    if mutation == "wrong_gh": rows[1]["wechat_id"] = "gh_other"
    with pytest.raises(ValueError, match="staged_identity"):
        review_account(target, account, rows, settings)


def test_missing_body_is_specific_and_never_hides_old_boundary():
    target, account, rows, settings = fixture()
    rows[1]["body_ready"] = False
    result = review_account(target, account, rows[1:], settings)
    assert result["old_boundary_seen"] is False
    assert set(result["issues"]) == {"body_unverified", "old_boundary_not_observed"}


def test_cli_report_refuses_overwrite_and_preserves_existing_data(tmp_path, monkeypatch, capsys):
    from wechat_crawler import cache_capture_review as module
    report = {"status": "review_requires_catalog", "accounts": [], "product_writes": 0, "word_exports": 0}
    monkeypatch.setattr(module, "load_config", lambda _: {})
    monkeypatch.setattr(module, "review_stages", lambda *args: report)
    path = tmp_path / "new-report.json"
    args = ["--until", "2026-10-28", "--stage-root", str(tmp_path), "--output", str(path)]
    assert main(args) == 0
    assert json.loads(path.read_text()) == report
    before = path.read_bytes()
    assert main(args) == 2 and path.read_bytes() == before
    assert "blocked_readonly_review" in capsys.readouterr().out
