import copy

import pytest

from cache_capture_fixtures import record, scope
from wechat_crawler.cache_capture_validation import record_value
from wechat_crawler.discovered_export_plan import select_discovered


def rows():
    return [record_value(record(d), scope()) for d in (8, 13, 20, 27)]


def account():
    return dict(scope(), _articles=[], incomplete_article_ids=[])


def test_discovered_plan_is_not_full_coverage_or_product_progress():
    result = select_discovered(scope(), account(), rows(), {"delivered_baseline_max_article_id": 10})
    assert result["export_count"] == 4 and not result["full_interval_verified"]
    assert not result["product_checkpoint_advanced"]


def test_known_preview_is_a_gap_not_a_body_or_delivered_overlap():
    values, target = rows(), account()
    first = values[0]
    target["incomplete_article_ids"] = [8]
    target["_articles"] = [{"id":8, "source_url":first["url"], "title":first["title"],
                            "content_hash":first["content_hash"], "body_chars":len(first["content_text"]), "batch_id":1}]
    result = select_discovered(scope(), target, values, {"delivered_baseline_max_article_id":10})
    assert result["export_count"] == 3 and len(result["gaps"]) == 1


def test_short_body_is_valid_and_duplicates_are_retained_as_metadata():
    values = rows()
    values[0] = record_value(record(8, body="这是正文。"), scope())
    values[1] = record_value(record(13, body="这是正文。"), scope())
    result = select_discovered(scope(), account(), values, {"delivered_baseline_max_article_id":10})
    assert result["export_count"] == 3 and len(result["duplicate_bodies"]) == 1


@pytest.mark.parametrize("case", ["wrong_hash", "wrong_account"])
def test_tampered_stage_cannot_be_exported(case):
    values = rows()
    values[0]["content_hash" if case == "wrong_hash" else "wechat_id"] = "WRONG"
    with pytest.raises(ValueError):
        select_discovered(scope(), account(), values, {"delivered_baseline_max_article_id":10})


def test_missing_body_and_version_conflict_stay_in_gap_list():
    values = rows()
    values[0]["body_ready"] = False
    result = select_discovered(scope(), account(), values, {"delivered_baseline_max_article_id":10},
                               conflicts={values[1]["url"]})
    assert result["export_count"] == 2 and len(result["gaps"]) == 2


def test_exported_overlap_does_not_return_as_new():
    target, values = account(), rows()
    target["_articles"] = [{"id":8,"source_url":values[0]["url"],"content_hash":values[0]["content_hash"],"batch_id":1}]
    original = copy.deepcopy(values)
    result = select_discovered(scope(), target, values, {"delivered_baseline_max_article_id":10})
    assert result["export_count"] == 3 and values == original
