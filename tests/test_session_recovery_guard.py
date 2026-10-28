import subprocess

import pytest

from wechat_crawler.macos_wechat import (
    WechatRuntimeStatus,
    WechatSessionRefresher,
    WechatWindow,
)


SEED = "https://mp.weixin.qq.com/s/VU512lPD0QSTeGWDMh2ONQ"
SEARCH = WechatWindow("微信 (窗口)", 0, 33, 1470, 864)


class Probe:
    def __init__(self):
        self.state = "ready"
        self.windows = (WechatWindow("微信", 0, 33, 630, 864), SEARCH)

    def inspect(self):
        return WechatRuntimeStatus(
            self.state, self.state, True, True, self.windows,
            "outer_ax_plus_renderer_process", self.state == "screen_locked",
        )

    def activate_window(self, _title):
        return True

    def activate_main_window(self):
        return True

    def search_field_focused(self):
        return True


class Input:
    def __init__(self):
        self.events = []
        self.after_click = lambda: None

    def click(self, x, y):
        self.events.append(("click", x, y))
        self.after_click()
        return True

    def press_key(self, key_code, flags=0):
        self.events.append(("key", key_code, flags))
        return True


def make_refresher(probe=None):
    probe = probe or Probe()
    driver = Input()
    clipboard = []

    def runner(command, **kwargs):
        if command[0] == "pbpaste":
            return subprocess.CompletedProcess(command, 0, stdout=b"original")
        assert command[0] == "pbcopy"
        clipboard.append(kwargs["input"])
        return subprocess.CompletedProcess(command, 0, stdout=b"")

    refresher = WechatSessionRefresher(
        probe, runner=runner, sleeper=lambda _seconds: None, input_driver=driver,
    )
    return refresher, probe, driver, clipboard


@pytest.mark.parametrize("changed", [False, True])
def test_public_seed_requires_observed_exact_account_session_change(changed):
    refresher, _, _, clipboard = make_refresher()
    values = iter(["old", "new" if changed else "old"])
    result = refresher.prepare_account(
        "测试号", seed_url=SEED, session_fingerprint=lambda: next(values, "old"),
        session_timeout_seconds=0,
    )

    assert result.session_verified is changed
    assert result.status == ("attempted" if changed else "unverified")
    assert result.article_open_attempted is True
    assert clipboard[-1] == b"original"


def test_login_during_baseline_scan_stops_before_any_input():
    refresher, probe, driver, _ = make_refresher()

    def fingerprint():
        probe.state = "needs_login"
        return "old"

    result = refresher.prepare_account("测试号", seed_url=SEED, session_fingerprint=fingerprint)

    assert result.status == "needs_login"
    assert result.session_verified is False
    assert driver.events == []


def test_login_after_seed_click_is_not_a_success_or_another_ui_attempt():
    refresher, probe, driver, clipboard = make_refresher()

    def after_click():
        # Last public-search input is the first web card.
        if len([event for event in driver.events if event[0] == "click"]) == 4:
            probe.state = "needs_login"

    driver.after_click = after_click
    result = refresher.prepare_account(
        "测试号", seed_url=SEED, session_fingerprint=lambda: "old",
        session_timeout_seconds=0,
    )

    assert result.status == "needs_login"
    assert result.article_open_attempted is True
    assert result.session_verified is False
    assert len(driver.events) == 6
    assert clipboard[-1] == b"original"


def test_screen_lock_between_native_events_aborts_the_sequence():
    refresher, probe, driver, clipboard = make_refresher()
    driver.after_click = lambda: setattr(probe, "state", "screen_locked")
    result = refresher.prepare_account("测试号", seed_url=SEED)

    assert result.status == "screen_locked"
    assert result.session_verified is False
    assert len(driver.events) == 1
    assert clipboard[-1] == b"original"


def test_click_uses_fresh_bounds_instead_of_retained_search_geometry():
    refresher, probe, driver, _ = make_refresher()
    probe.windows = (WechatWindow("微信 (窗口)", 80, 90, 624, 858),)

    assert refresher._click_public_web_result(SEARCH) is True
    assert driver.events == [("click", 229, 300)]


def test_missing_target_never_clicks_old_window_bounds():
    refresher, probe, driver, _ = make_refresher()
    probe.windows = (WechatWindow("微信", 0, 33, 630, 864),)

    assert refresher._click_public_web_result(SEARCH) is False
    assert driver.events == []


@pytest.mark.parametrize("focus_lost_after_select_all", [False, True])
def test_unverifiable_main_search_focus_never_receives_paste(focus_lost_after_select_all):
    refresher, probe, driver, clipboard = make_refresher()
    probe.windows = (WechatWindow("微信", 0, 33, 630, 864),)
    checks = iter([True, False] if focus_lost_after_select_all else [False])
    probe.search_field_focused = lambda: next(checks, False)

    result = refresher.prepare_account("测试号", seed_url=SEED)

    assert result.status == "unverified"
    assert result.session_verified is False
    assert "搜索焦点" in result.message
    assert ("key", 9, 1 << 20) not in driver.events
    assert ("key", 36, 0) not in driver.events
    assert clipboard[-1] == b"original"
