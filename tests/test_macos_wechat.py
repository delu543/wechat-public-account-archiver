from __future__ import annotations

import argparse
import subprocess
from datetime import datetime

from wechat_crawler.automation_store import AutomationStore
from wechat_crawler.crawler.storage import Storage
from wechat_crawler.main import cmd_automation_prepare_session
from wechat_crawler.macos_wechat import (
    InProcessSystemEventsInput,
    WechatForegroundInput,
    WechatRuntimeStatus,
    WechatSessionRefresher,
    WechatWindow,
    WechatWindowProbe,
    parse_window_rows,
)


def completed(args, *, stdout="", returncode=0):
    return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="")


class VerifiedSearchProbe:
    def search_field_focused(self):
        return True


class FakeInput:
    def __init__(self):
        self.clicks = []
        self.keys = []

    def click(self, x, y):
        self.clicks.append((x, y))
        return True

    def press_key(self, key_code, flags=0):
        self.keys.append((key_code, flags))
        return True


def test_in_process_system_events_input_clicks_without_external_osascript():
    scripts = []
    driver = InProcessSystemEventsInput(
        sleeper=lambda _seconds: None,
        script_runner=lambda source: scripts.append(source) is None or True,
    )

    assert driver.click(355, 180) is True
    assert driver.press_key(9, 1 << 20) is True
    assert scripts == [
        'tell application "System Events" to click at {355, 180}',
        'tell application "System Events" to key code 9 using {command down}',
    ]


def test_wechat_foreground_input_uses_native_mouse_and_system_events_keyboard():
    mouse = FakeInput()
    keyboard = FakeInput()
    driver = WechatForegroundInput(
        sleeper=lambda _seconds: None,
        mouse=mouse,
        keyboard=keyboard,
    )

    assert driver.click(558, 119) is True
    assert driver.press_key(9, 1 << 20) is True
    assert mouse.clicks == [(558, 119)]
    assert keyboard.keys == [(9, 1 << 20)]


class FakeSyncResult:
    def __init__(self, status, message=""):
        self.status = status
        self.message = message

    def to_dict(self):
        return {"status": self.status, "message": self.message}


def prepare_waiting_subscription(tmp_path):
    storage = Storage(tmp_path / "archive.sqlite")
    storage.init_db()
    automation = AutomationStore(storage)
    automation.init_db()
    seed_url = "https://mp.weixin.qq.com/s/VU512lPD0QSTeGWDMh2ONQ"
    account_id = storage.upsert_account(
        "测试公众号",
        biz="MzPrepareOnly==",
        wechat_id="gh_prepare_only",
        source_url=seed_url,
    )
    subscription = automation.ensure_subscription(account_id, seed_url, adopt_existing=False)
    now = datetime.now().astimezone()
    job = automation.create_job(
        int(subscription["id"]),
        "2026-W35",
        trigger="schedule",
        scheduled_for=now,
    )
    automation.begin_cycle(
        "2026-W35",
        scheduled_for=now,
        output_folder=str(tmp_path / "output" / "weekly" / "2026-08-30"),
        expected_account_count=1,
    )
    automation.update_job(
        str(job["id"]),
        status="needs_session",
        phase="needs_session",
        progress=15,
    )
    config = {
        "output": {"path": str(tmp_path / "output")},
        "automation": {"session_ready_timeout_seconds": 12},
        "history": {"app_data_dir": str(tmp_path / "wechat-app-data")},
    }
    return storage, config, seed_url


def test_prepare_session_cli_delegates_to_normal_direct_first_recovery(
    tmp_path,
    monkeypatch,
    capsys,
):
    storage, config, _seed_url = prepare_waiting_subscription(tmp_path)
    captured = {"runs": 0}

    class DirectFirstRunner:
        def __init__(self, _storage, _output_dir, runner_config):
            captured["config"] = runner_config

        def resume_latest_scheduled(self, subscription_id):
            captured["runs"] += 1
            captured["subscription_id"] = subscription_id
            return FakeSyncResult("completed", "direct-first flow completed")

    monkeypatch.setattr("wechat_crawler.weekly_sync.WeeklySyncRunner", DirectFirstRunner)

    exit_code = cmd_automation_prepare_session(
        argparse.Namespace(account_biz="MzPrepareOnly=="),
        config,
        storage,
    )

    assert exit_code == 0
    assert captured["runs"] == 1
    assert captured["config"]["automation"]["prepare_wechat_session"] is True
    output = capsys.readouterr().out
    assert '"status": "completed"' in output


def test_parse_window_rows_ignores_invalid_lines():
    windows = parse_window_rows(
        "公众号\t435\t65\t600\t800\tfalse\n"
        "bad row\n"
        "微信\t0\t33\t624\t864\ttrue\n"
    )

    assert windows == [
        WechatWindow("公众号", 435, 65, 600, 800, False),
        WechatWindow("微信", 0, 33, 624, 864, True),
    ]


def test_probe_distinguishes_outer_window_and_renderer(monkeypatch):
    monkeypatch.setattr("wechat_crawler.macos_wechat.sys.platform", "darwin")
    scripts = []

    def runner(command, **_kwargs):
        if command[0] == "ps":
            return completed(
                command,
                stdout=(
                    "100 1 /Applications/微信.app/Contents/MacOS/WeChat\n"
                    "101 100 /Applications/微信.app/Contents/MacOS/WeChatAppEx.app/Contents/MacOS/WeChatAppEx\n"
                ),
            )
        if command[0] == "ioreg":
            return completed(command, stdout='"IOConsoleLocked" = No\n')
        scripts.append(command[-1])
        return completed(command, stdout="WeChat\t0\t33\t624\t864\tfalse\n")

    status = WechatWindowProbe(runner).inspect()

    assert status.state == "ready"
    assert status.app_running is True
    assert status.renderer_running is True
    assert status.capture_strategy == "outer_ax_plus_renderer_process"
    assert status.windows[0].title == "WeChat"
    assert 'bundle identifier is "com.tencent.xinWeChat"' in scripts[0]


def test_probe_falls_back_to_window_server_metadata(monkeypatch):
    monkeypatch.setattr("wechat_crawler.macos_wechat.sys.platform", "darwin")

    def runner(command, **_kwargs):
        if command[0] == "ps":
            return completed(
                command,
                stdout=(
                    "100 1 /Applications/微信.app/Contents/MacOS/WeChat\n"
                    "101 100 /Applications/微信.app/Contents/MacOS/WeChatAppEx.app/Contents/MacOS/WeChatAppEx\n"
                ),
            )
        if command[0] == "ioreg":
            return completed(command, stdout='"IOConsoleLocked" = No\n')
        if command[0] == "osascript":
            return completed(command, stdout="")
        assert command[0] == "swift"
        return completed(command, stdout="公众号\t435\t65\t600\t800\tfalse\n")

    status = WechatWindowProbe(runner).inspect()

    assert status.state == "ready"
    assert status.windows == (WechatWindow("公众号", 435, 65, 600, 800, False),)


def test_probe_prefers_in_process_accessibility_windows(monkeypatch):
    monkeypatch.setattr("wechat_crawler.macos_wechat.sys.platform", "darwin")
    commands = []

    class NativeWindows:
        def __init__(self):
            self.activated = []

        def windows(self, pid):
            assert pid == 100
            return [WechatWindow("微信", 0, 33, 624, 864, False)]

        def activate_window(self, pid, title):
            self.activated.append((pid, title))
            return True

    native = NativeWindows()

    def runner(command, **_kwargs):
        commands.append(command)
        if command[0] == "ps":
            return completed(
                command,
                stdout=(
                    "100 1 /Applications/微信.app/Contents/MacOS/WeChat\n"
                    "101 100 /Applications/微信.app/Contents/MacOS/WeChatAppEx.app/Contents/MacOS/WeChatAppEx\n"
                ),
            )
        if command[0] == "ioreg":
            return completed(command, stdout='"IOConsoleLocked" = No\n')
        raise AssertionError(command)

    probe = WechatWindowProbe(runner, native_windows=native)

    assert probe.inspect().state == "ready"
    assert probe.activate_window("微信") is True
    assert native.activated == [(100, "微信")]
    assert not any(command[0] == "osascript" for command in commands)


def test_probe_defers_while_screen_is_locked(monkeypatch):
    monkeypatch.setattr("wechat_crawler.macos_wechat.sys.platform", "darwin")

    def runner(command, **_kwargs):
        if command[0] == "ps":
            return completed(
                command,
                stdout=(
                    "100 1 /Applications/微信.app/Contents/MacOS/WeChat\n"
                    "101 100 /Applications/微信.app/Contents/MacOS/WeChatAppEx.app/Contents/MacOS/WeChatAppEx\n"
                ),
            )
        if command[0] == "ioreg":
            return completed(command, stdout='"IOConsoleLocked" = Yes\n')
        if command[0] == "osascript":
            return completed(command, stdout="")
        return completed(command, stdout="")

    status = WechatWindowProbe(runner).inspect()

    assert status.state == "screen_locked"
    assert status.screen_locked is True
    assert status.capture_strategy == "deferred_until_unlock"


def test_probe_defers_when_wechat_only_exposes_login_window(monkeypatch):
    monkeypatch.setattr("wechat_crawler.macos_wechat.sys.platform", "darwin")

    def runner(command, **_kwargs):
        if command[0] == "ps":
            return completed(
                command,
                stdout=(
                    "100 1 /Applications/微信.app/Contents/MacOS/WeChat\n"
                    "101 100 /Applications/微信.app/Contents/MacOS/WeChatAppEx.app/Contents/MacOS/WeChatAppEx\n"
                ),
            )
        if command[0] == "ioreg":
            return completed(command, stdout='"IOConsoleLocked" = No\n')
        if command[0] == "osascript":
            return completed(command, stdout="微信\t594\t275\t280\t380\tfalse\n")
        raise AssertionError(command)

    status = WechatWindowProbe(runner).inspect()

    assert status.state == "needs_login"
    assert status.capture_strategy == "deferred_until_login"


def test_probe_activation_does_not_enumerate_accessibility_windows(monkeypatch):
    monkeypatch.setattr("wechat_crawler.macos_wechat.sys.platform", "darwin")
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        return completed(command)

    assert WechatWindowProbe(runner).activate_main_window() is True
    assert commands == [["open", "-b", "com.tencent.xinWeChat"]]


def test_probe_activates_named_window_through_wechat_menu(monkeypatch):
    monkeypatch.setattr("wechat_crawler.macos_wechat.sys.platform", "darwin")
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        return completed(command)

    assert WechatWindowProbe(runner).activate_window("公众号") is True
    assert commands[0][0:2] == ["ps", "-axo"]
    assert commands[1][0:2] == ["osascript", "-e"]
    assert commands[1][-1] == "公众号"


def test_session_refresher_leaves_existing_public_windows_open():
    input_driver = FakeInput()
    clipboard_writes = []
    inspections = 0
    seed_url = "https://mp.weixin.qq.com/s/VU512lPD0QSTeGWDMh2ONQ"

    class Probe(VerifiedSearchProbe):
        def inspect(self):
            nonlocal inspections
            inspections += 1
            return WechatRuntimeStatus(
                "ready",
                "ready",
                True,
                True,
                (
                    WechatWindow("微信", 0, 0, 624, 864),
                    WechatWindow("公众号", 400, 60, 600, 800),
                    WechatWindow("微信 (窗口)", 0, 33, 1470, 864),
                ),
                "outer_ax_plus_renderer_process",
            )

        def activate_main_window(self):
            return False

        def activate_window(self, title):
            return True

    def runner(command, **kwargs):
        if command[0] == "pbpaste":
            return subprocess.CompletedProcess(command, 0, stdout=b"original", stderr=b"")
        if command[0] == "pbcopy":
            clipboard_writes.append(kwargs["input"])
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        raise AssertionError(command)

    refresher = WechatSessionRefresher(
        Probe(),
        runner=runner,
        sleeper=lambda _seconds: None,
        input_driver=input_driver,
    )

    result = refresher.prepare_account("子说一点", seed_url=seed_url)

    assert result.status == "attempted"
    assert clipboard_writes == [seed_url.encode("utf-8"), b"original"]
    assert (13, 1 << 20) not in input_driver.keys
    assert input_driver.clicks == [(250, 51), (558, 119), (922, 119), (352, 243)]
    assert input_driver.keys == [
        (0, 1 << 20),
        (9, 1 << 20),
    ]


def test_session_refresher_raises_hidden_public_search_after_main_submit_misses():
    input_driver = FakeInput()
    clipboard_writes = []
    hidden_search_raised = False
    seed_url = "https://mp.weixin.qq.com/s/VU512lPD0QSTeGWDMh2ONQ"

    class Probe(VerifiedSearchProbe):
        def inspect(self):
            windows = (WechatWindow("微信", 0, 0, 624, 864),)
            if hidden_search_raised:
                windows += (WechatWindow("微信 (窗口)", 0, 33, 1470, 864),)
            return WechatRuntimeStatus(
                "ready",
                "ready",
                True,
                True,
                windows,
                "outer_ax_plus_renderer_process",
            )

        def activate_main_window(self):
            return True

        def activate_window(self, title):
            nonlocal hidden_search_raised
            if title == "微信 (窗口)":
                hidden_search_raised = True
            return True

    def runner(command, **kwargs):
        if command[0] == "pbpaste":
            return subprocess.CompletedProcess(command, 0, stdout=b"original", stderr=b"")
        if command[0] == "pbcopy":
            clipboard_writes.append(kwargs["input"])
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        raise AssertionError(command)

    result = WechatSessionRefresher(
        Probe(),
        runner=runner,
        sleeper=lambda _seconds: None,
        input_driver=input_driver,
    ).prepare_account("子说一点", seed_url=seed_url)

    assert result.status == "attempted"
    assert result.article_open_attempted is True
    assert hidden_search_raised is True
    assert clipboard_writes == [seed_url.encode("utf-8"), b"original"]
    assert input_driver.clicks == [
        (143, 30),
        (250, 51),
        (558, 119),
        (922, 119),
        (352, 243),
    ]
    assert input_driver.keys == [
        (0, 1 << 20),
        (9, 1 << 20),
        (36, 0),
        (0, 1 << 20),
        (9, 1 << 20),
    ]


def test_session_refresher_restores_clipboard_when_public_search_window_is_missing():
    clipboard_writes = []
    input_driver = FakeInput()
    activated_windows = []

    class Probe(VerifiedSearchProbe):
        def inspect(self):
            return WechatRuntimeStatus(
                "ready",
                "ready",
                True,
                True,
                (WechatWindow("微信", 0, 0, 624, 864),),
                "outer_ax_plus_renderer_process",
            )

        def activate_main_window(self):
            return True

        def activate_window(self, title):
            activated_windows.append(title)
            return True

        def _run_osascript(self, _script):
            return True

    def runner(command, **kwargs):
        if command[0] == "pbpaste":
            return subprocess.CompletedProcess(command, 0, stdout=b"original", stderr=b"")
        if command[0] == "pbcopy":
            clipboard_writes.append(kwargs["input"])
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        raise AssertionError(command)

    result = WechatSessionRefresher(
        Probe(),
        runner=runner,
        sleeper=lambda _seconds: None,
        input_driver=input_driver,
    ).prepare_account("子说一点")

    assert result.status == "unverified"
    assert activated_windows == ["微信"]
    assert input_driver.clicks == [(143, 30)]
    assert (3, 1 << 20) not in input_driver.keys
    assert clipboard_writes == ["子说一点".encode("utf-8"), b"original"]


def test_session_refresher_activates_hidden_wechat_once_before_rechecking():
    input_driver = FakeInput()
    inspections = 0
    activations = 0

    class Probe(VerifiedSearchProbe):
        def inspect(self):
            nonlocal inspections
            inspections += 1
            if inspections == 1:
                return WechatRuntimeStatus(
                    "no_visible_window",
                    "hidden",
                    True,
                    True,
                    (),
                    "renderer_process_probe",
                )
            return WechatRuntimeStatus(
                "ready",
                "ready",
                True,
                True,
                (WechatWindow("微信", 0, 0, 624, 864),),
                "outer_ax_plus_renderer_process",
            )

        def activate_main_window(self):
            nonlocal activations
            activations += 1
            return True

    def runner(command, **kwargs):
        if command[0] == "pbpaste":
            return subprocess.CompletedProcess(command, 0, stdout=b"original", stderr=b"")
        if command[0] == "pbcopy":
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        raise AssertionError(command)

    result = WechatSessionRefresher(
        Probe(),
        runner=runner,
        sleeper=lambda _seconds: None,
        input_driver=input_driver,
    ).prepare_account("子说一点")

    assert result.status == "unverified"
    assert inspections > 1
    assert activations == 2


def test_session_refresher_prefers_public_article_seed_url_over_account_name():
    clipboard_writes = []
    input_driver = FakeInput()
    inspections = 0
    seed_url = "https://mp.weixin.qq.com/s/VU512lPD0QSTeGWDMh2ONQ"

    fingerprints = iter(["old", "new"])

    def fingerprint():
        return next(fingerprints)

    class Probe(VerifiedSearchProbe):
        def inspect(self):
            nonlocal inspections
            inspections += 1
            windows = (WechatWindow("微信", 0, 0, 624, 864),)
            if inspections > 1:
                windows += (WechatWindow("微信 (窗口)", 100, 20, 624, 858),)
            return WechatRuntimeStatus(
                "ready",
                "ready",
                True,
                True,
                windows,
                "outer_ax_plus_renderer_process",
            )

        def activate_main_window(self):
            return True

        def activate_window(self, _title):
            return True

    def runner(command, **kwargs):
        if command[0] == "pbpaste":
            return subprocess.CompletedProcess(command, 0, stdout=b"original", stderr=b"")
        if command[0] == "pbcopy":
            clipboard_writes.append(kwargs["input"])
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        raise AssertionError(command)

    result = WechatSessionRefresher(
        Probe(),
        runner=runner,
        sleeper=lambda _seconds: None,
        input_driver=input_driver,
    ).prepare_account(
        "子说一点",
        seed_url=seed_url,
        session_fingerprint=fingerprint,
        session_timeout_seconds=1,
        session_poll_interval_seconds=0.01,
    )

    assert result.status == "attempted"
    assert result.article_open_attempted is True
    assert inspections > 2  # Guard every event, not only entry and first search.
    assert result.session_verified is True
    assert clipboard_writes == [seed_url.encode("utf-8"), b"original"]
    assert input_driver.clicks == [(143, 30), (249, 230)]
    assert input_driver.keys == [
        (0, 1 << 20),
        (9, 1 << 20),
        (36, 0),
    ]
    assert (125, 0) not in input_driver.keys


def test_session_refresher_keeps_public_web_click_inside_wide_result_column():
    input_driver = FakeInput()

    class Probe(VerifiedSearchProbe):
        def inspect(self):
            return WechatRuntimeStatus(
                "ready", "ready", True, True,
                (WechatWindow("微信 (窗口)", 0, 33, 1470, 864),),
                "outer_ax_plus_renderer_process",
            )

    refresher = WechatSessionRefresher(
        probe=Probe(),
        sleeper=lambda _seconds: None,
        input_driver=input_driver,
    )

    assert refresher._click_public_web_result(
        WechatWindow("微信 (窗口)", 0, 33, 1470, 864)
    ) is True
    assert input_driver.clicks == [(352, 243)]


def test_session_refresher_uses_account_name_when_seed_url_is_not_public_wechat_article():
    clipboard_writes = []
    input_driver = FakeInput()

    class Probe(VerifiedSearchProbe):
        def inspect(self):
            return WechatRuntimeStatus(
                "ready",
                "ready",
                True,
                True,
                (WechatWindow("微信", 0, 0, 624, 864),),
                "outer_ax_plus_renderer_process",
            )

        def activate_main_window(self):
            return True

    def runner(command, **kwargs):
        if command[0] == "pbpaste":
            return subprocess.CompletedProcess(command, 0, stdout=b"original", stderr=b"")
        if command[0] == "pbcopy":
            clipboard_writes.append(kwargs["input"])
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        raise AssertionError(command)

    WechatSessionRefresher(
        Probe(),
        runner=runner,
        sleeper=lambda _seconds: None,
        input_driver=input_driver,
    ).prepare_account("子说一点", seed_url="https://example.com/not-wechat")

    assert clipboard_writes == ["子说一点".encode("utf-8"), b"original"]


def test_session_refresher_waits_then_selects_public_search_row():
    input_driver = FakeInput()
    inspections = 0

    class Probe(VerifiedSearchProbe):
        def inspect(self):
            nonlocal inspections
            inspections += 1
            return WechatRuntimeStatus(
                "ready",
                "ready",
                True,
                True,
                (WechatWindow("微信", 0, 0, 624, 864),),
                "outer_ax_plus_renderer_process",
            )

        def activate_main_window(self):
            return True

        def _run_osascript(self, _script):
            return True

    def runner(command, **kwargs):
        if command[0] == "pbpaste":
            return subprocess.CompletedProcess(command, 0, stdout=b"original", stderr=b"")
        if command[0] == "pbcopy":
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        raise AssertionError(command)

    WechatSessionRefresher(
        Probe(),
        runner=runner,
        sleeper=lambda _seconds: None,
        input_driver=input_driver,
    ).prepare_account("子说一点")

    assert input_driver.keys.count((125, 0)) == 2
    assert (36, 0) in input_driver.keys


def test_session_refresher_opens_first_public_article_search_result():
    input_driver = FakeInput()

    class Probe(VerifiedSearchProbe):
        def inspect(self):
            return WechatRuntimeStatus(
                "ready",
                "ready",
                True,
                True,
                (
                    WechatWindow("微信", 0, 0, 624, 864),
                    WechatWindow("微信 (窗口)", 100, 20, 624, 858),
                    WechatWindow("公众号", 400, 60, 600, 800),
                ),
                "outer_ax_plus_renderer_process",
            )

        def activate_main_window(self):
            return True

        def activate_window(self, _title):
            return True

    def runner(command, **kwargs):
        if command[0] == "pbpaste":
            return subprocess.CompletedProcess(command, 0, stdout=b"original", stderr=b"")
        if command[0] == "pbcopy":
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        raise AssertionError(command)

    result = WechatSessionRefresher(
        Probe(),
        runner=runner,
        sleeper=lambda _seconds: None,
        input_driver=input_driver,
    ).prepare_account("香港S叔")

    assert result.status == "attempted"
    assert input_driver.clicks == [
        (143, 30),
        (337, 105),
        (599, 105),
        (390, 150),
        (399, 268),
        (634, 288),
        (700, 404),
    ]
    assert input_driver.keys.count((0, 1 << 20)) == 2
    assert input_driver.keys.count((9, 1 << 20)) == 2


def test_session_refresher_tries_expanded_profile_header_after_compact_layout():
    input_driver = FakeInput()
    fingerprint_calls = 0

    def fingerprint():
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        return "new" if fingerprint_calls >= 4 else "old"

    class Probe(VerifiedSearchProbe):
        def inspect(self):
            return WechatRuntimeStatus(
                "ready",
                "ready",
                True,
                True,
                (
                    WechatWindow("微信", 0, 0, 624, 864),
                    WechatWindow("微信 (窗口)", 100, 20, 624, 858),
                    WechatWindow("公众号", 400, 60, 600, 800),
                ),
                "outer_ax_plus_renderer_process",
            )

        def activate_main_window(self):
            return True

        def activate_window(self, _title):
            return True

    def runner(command, **kwargs):
        if command[0] == "pbpaste":
            return subprocess.CompletedProcess(command, 0, stdout=b"original", stderr=b"")
        if command[0] == "pbcopy":
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        raise AssertionError(command)

    result = WechatSessionRefresher(
        Probe(),
        runner=runner,
        sleeper=lambda _seconds: None,
        input_driver=input_driver,
    ).prepare_account(
        "香港S叔",
        session_fingerprint=fingerprint,
        session_timeout_seconds=0.04,
        session_poll_interval_seconds=0.01,
    )

    assert result.status == "attempted"
    assert result.article_open_attempted is True
    assert input_driver.clicks[-6:] == [
        (634, 288),
        (700, 404),
        (634, 308),
        (700, 424),
        (634, 328),
        (700, 444),
    ]


def test_session_refresher_waits_for_exact_account_session_change():
    input_driver = FakeInput()
    fingerprints = iter(["old", "old", "new"])

    class Probe(VerifiedSearchProbe):
        def inspect(self):
            return WechatRuntimeStatus(
                "ready",
                "ready",
                True,
                True,
                    (
                        WechatWindow("微信", 0, 0, 624, 864),
                        WechatWindow("微信 (窗口)", 100, 20, 624, 858),
                        WechatWindow("公众号", 400, 60, 600, 800),
                    ),
                "outer_ax_plus_renderer_process",
            )

        def activate_main_window(self):
            return True

        def activate_window(self, _title):
            return True

    def runner(command, **kwargs):
        if command[0] == "pbpaste":
            return subprocess.CompletedProcess(command, 0, stdout=b"original", stderr=b"")
        if command[0] == "pbcopy":
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        raise AssertionError(command)

    result = WechatSessionRefresher(
        Probe(),
        runner=runner,
        sleeper=lambda _seconds: None,
        input_driver=input_driver,
    ).prepare_account(
        "子说一点",
        session_fingerprint=lambda: next(fingerprints),
        session_timeout_seconds=1,
        session_poll_interval_seconds=0.01,
    )

    assert result.status == "attempted"
    assert result.article_open_attempted is True
    assert "精确公众号主页" in result.message
    assert "最新页" in result.message


def test_session_refresher_does_not_claim_success_without_session_change():
    input_driver = FakeInput()

    class Probe(VerifiedSearchProbe):
        def inspect(self):
            return WechatRuntimeStatus(
                "ready",
                "ready",
                True,
                True,
                    (
                        WechatWindow("微信", 0, 0, 624, 864),
                        WechatWindow("微信 (窗口)", 100, 20, 624, 858),
                        WechatWindow("公众号", 400, 60, 600, 800),
                    ),
                "outer_ax_plus_renderer_process",
            )

        def activate_main_window(self):
            return True

        def activate_window(self, _title):
            return True

    def runner(command, **kwargs):
        if command[0] == "pbpaste":
            return subprocess.CompletedProcess(command, 0, stdout=b"original", stderr=b"")
        if command[0] == "pbcopy":
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        raise AssertionError(command)

    result = WechatSessionRefresher(
        Probe(),
        runner=runner,
        sleeper=lambda _seconds: None,
        input_driver=input_driver,
    ).prepare_account(
        "子说一点",
        session_fingerprint=lambda: "unchanged",
        session_timeout_seconds=0.02,
        session_poll_interval_seconds=0.01,
    )

    assert result.status == "unverified"
    assert result.article_open_attempted is True
    assert "尚未确认" in result.message


def test_session_refresher_requires_public_search_and_profile_windows():
    input_driver = FakeInput()

    class Probe(VerifiedSearchProbe):
        def inspect(self):
            return WechatRuntimeStatus(
                "ready",
                "ready",
                True,
                True,
                (
                    WechatWindow("微信", 0, 33, 624, 864),
                    WechatWindow("香港S叔", 436, 112, 598, 640),
                ),
                "outer_ax_plus_renderer_process",
            )

        def activate_main_window(self):
            return True

    def runner(command, **kwargs):
        if command[0] == "pbpaste":
            return subprocess.CompletedProcess(command, 0, stdout=b"original", stderr=b"")
        if command[0] == "pbcopy":
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        raise AssertionError(command)

    result = WechatSessionRefresher(
        Probe(),
        runner=runner,
        sleeper=lambda _seconds: None,
        input_driver=input_driver,
    ).prepare_account("香港S叔")

    assert result.status == "unverified"
    assert input_driver.clicks[0] == (49, 188)
    assert input_driver.clicks[1] == (143, 63)
    assert len(input_driver.clicks) == 2
