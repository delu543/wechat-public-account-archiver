from argparse import Namespace

from wechat_crawler.main import fetch_config_from_args


def test_fast_crawl_uses_low_delay_and_no_retries():
    config = {
        "fetch": {
            "timeout_seconds": 20,
            "min_delay_seconds": 2.0,
            "max_delay_seconds": 5.0,
            "max_retries": 2,
        }
    }
    args = Namespace(fast=True, min_delay=None, max_delay=None, max_retries=None)

    fetch_config = fetch_config_from_args(config, args)

    assert fetch_config.min_delay_seconds == 0.15
    assert fetch_config.max_delay_seconds == 0.45
    assert fetch_config.max_retries == 0


def test_crawl_delay_overrides_win_over_fast_defaults():
    config = {"fetch": {"timeout_seconds": 20, "min_delay_seconds": 2.0, "max_delay_seconds": 5.0, "max_retries": 2}}
    args = Namespace(fast=True, min_delay=0.5, max_delay=1.0, max_retries=1)

    fetch_config = fetch_config_from_args(config, args)

    assert fetch_config.min_delay_seconds == 0.5
    assert fetch_config.max_delay_seconds == 1.0
    assert fetch_config.max_retries == 1
