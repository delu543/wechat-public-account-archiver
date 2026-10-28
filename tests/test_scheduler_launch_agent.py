from __future__ import annotations

import plistlib
from datetime import datetime, timezone
from pathlib import Path

import yaml

from wechat_crawler.launch_agent import LABEL, STATUS_LABEL, LaunchAgentManager
from wechat_crawler.scheduler import WeeklyScheduler


class FakeRunner:
    def __init__(self):
        self.calls = []

    def run_due(self, now=None):
        self.calls.append(now)
        return []


def test_scheduler_tick_calls_runner_once():
    runner = FakeRunner()
    scheduler = WeeklyScheduler(runner, check_interval_seconds=5)
    now = datetime(2026, 7, 19, 20, tzinfo=timezone.utc)
    scheduler.tick(now)
    assert runner.calls == [now]


def test_launch_agent_writes_atomic_product_configuration(tmp_path, monkeypatch):
    project = tmp_path / "project"
    (project / ".venv" / "bin").mkdir(parents=True)
    (project / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
    (project / "main.py").write_text("", encoding="utf-8")
    (project / "wechat_crawler").mkdir()
    (project / "wechat_crawler" / "__init__.py").write_text("", encoding="utf-8")
    manager = LaunchAgentManager(project, product_root=tmp_path / "Application Support" / "product")
    manager.plist_path = tmp_path / "LaunchAgents" / f"{LABEL}.plist"

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr("wechat_crawler.launch_agent.subprocess.run", lambda *args, **kwargs: Result())
    manager.status_plist_path = tmp_path / "LaunchAgents" / f"{STATUS_LABEL}.plist"
    path = manager.install(port=8877, install_status_item=False)
    with path.open("rb") as handle:
        payload = plistlib.load(handle)
    assert payload["Label"] == LABEL
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["ProgramArguments"][-1] == "8877"
    assert payload["WorkingDirectory"] == str(manager.runtime_root)
    assert payload["ProgramArguments"][0] == str(manager.runtime_root / ".venv" / "bin" / "python")
    installed_config = yaml.safe_load((manager.runtime_root / "config.yaml").read_text(encoding="utf-8"))
    assert installed_config["database"]["path"] == str(manager.database_path)


def test_status_agent_targets_installed_native_app(tmp_path):
    project = tmp_path / "project"
    manager = LaunchAgentManager(project, product_root=tmp_path / "product")
    payload = manager._status_agent_payload()
    assert payload["Label"] == STATUS_LABEL
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"] is True
    assert payload["LimitLoadToSessionType"] == "Aqua"
    assert payload["ProgramArguments"] == [
        str(manager.status_app_path / "Contents" / "MacOS" / "WeChatArchiveStatus")
    ]


def test_status_app_ready_preserves_only_bundle_built_from_current_source(tmp_path, monkeypatch):
    project = tmp_path / "project"
    source = project / "wechat_crawler" / "macos_status" / "StatusApp.swift"
    source.parent.mkdir(parents=True)
    source.write_text("// current status app", encoding="utf-8")
    manager = LaunchAgentManager(project, product_root=tmp_path / "product")
    executable = manager.status_app_path / "Contents" / "MacOS" / "WeChatArchiveStatus"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o755)
    info_path = manager.status_app_path / "Contents" / "Info.plist"
    with info_path.open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": STATUS_LABEL,
                "WeChatArchiveSourceSHA256": manager._status_source_hash(),
            },
            handle,
        )

    class SignatureResult:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        "wechat_crawler.launch_agent.subprocess.run",
        lambda *args, **kwargs: SignatureResult(),
    )

    assert manager._status_app_ready() is True
    source.write_text("// updated status app", encoding="utf-8")
    assert manager._status_app_ready() is False


def test_status_app_never_requests_accessibility_permission():
    source = (
        Path(__file__).resolve().parents[1]
        / "wechat_crawler"
        / "macos_status"
        / "StatusApp.swift"
    ).read_text(encoding="utf-8")

    assert "AXIsProcessTrusted" not in source
    assert "kAXTrustedCheckOptionPrompt" not in source
    assert "openPublicAccountArticle" not in source
    assert "starting authorized foreground recovery" in source
    assert "startForegroundSessionPreparation" in source
    assert "UNUserNotificationCenter" in source
    assert "已经弄好了" in source
    assert '"output_folder"' in source
    assert '"automation-prepare-session"' in source
    assert '"--resume-needs-session"' not in source
    assert '"\\(subscriptionId)/resume-after-session"' in source
    assert "NSStatusItem.squareLength" in source
    assert "statusItem.isVisible = true" in source
    assert "NSWorkspace.didWakeNotification" in source
    assert '"didShowVisibilityPopover-v2"' in source
    assert "maxSessionPreparationAttempts = 3" in source
    assert "sessionPreparationCooldown: TimeInterval = 60" in source
    assert "completion(task.terminationStatus == 0)" in source
    assert 'NSButton(title: "暂停全部爬取"' in source
    assert '"继续全部爬取"' in source
    assert "/api/automation/control" in source
    assert "foregroundSessionProcess" in source
    assert "cancelForegroundSessionPreparation" in source
