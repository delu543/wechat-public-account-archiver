import pytest

from wechat_crawler.crawler.date_range import (
    article_in_range,
    extract_publish_date,
    normalize_date,
    validate_date_range,
)


def test_date_inputs_are_normalized_and_validated():
    assert normalize_date("2026/7/2", "since") == "2026-07-02"
    assert validate_date_range("2026-01-01", "2026-07-02") == ("2026-01-01", "2026-07-02")

    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        normalize_date("2026-02-30", "since")
    with pytest.raises(ValueError, match="on or before"):
        validate_date_range("2026-07-02", "2026-01-01")


def test_article_range_is_inclusive_and_rejects_unknown_dates():
    assert extract_publish_date("2026-07-02 09:30") == "2026-07-02"
    assert article_in_range("2026-07-02 09:30", "2026-07-02")
    assert not article_in_range("2026-07-01 23:59", "2026-07-02")
    assert not article_in_range(None, "2026-07-02")
