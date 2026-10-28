import subprocess

import pytest

from wechat_crawler.macos_wechat import NativeAccessibilityWindows, WechatWindowProbe


class FocusMetadata(NativeAccessibilityWindows):
    def __init__(self, role, *, label="", subrole=""):
        self.reads = []
        self._ax = type("AX", (), {"AXUIElementCreateApplication": lambda self, pid: 1})()
        self._cf = type("CF", (), {"CFRelease": lambda self, value: None})()
        self.values = {"AXRole": role, "AXDescription": label,
                       "AXPlaceholderValue": "", "AXSubrole": subrole}

    def _load(self):
        return True

    def _copy_attribute(self, element, name):
        self.reads.append(name)
        return 2 if name == "AXFocusedUIElement" else None

    def _attribute_text(self, element, name):
        self.reads.append(name)
        return self.values.get(name, "")


@pytest.mark.parametrize("role,label,subrole,expected", [
    ("AXSearchField", "", "", True),
    ("AXTextField", "搜索", "", True),
    ("AXTextField", "Search", "", True),
    ("AXTextField", "", "AXSearchField", True),
    ("AXTextField", "", "", False),
    ("AXTextArea", "搜索", "", False),
    ("AXWindow", "", "", False),
])
def test_search_focus_uses_control_metadata_without_reading_private_values(role, label, subrole, expected):
    native = FocusMetadata(role, label=label, subrole=subrole)

    assert native.search_field_focused(123) is expected
    assert "AXValue" not in native.reads
    assert "AXChildren" not in native.reads


def test_focus_probe_includes_official_renderer_with_launch_arguments():
    checked = []

    class Native:
        def search_field_focused(self, pid):
            checked.append(pid)
            return pid == 101

    def runner(command, **kwargs):
        assert command[:2] == ["ps", "-axo"]
        return subprocess.CompletedProcess(command, 0, stdout="\n".join([
            "100 1 /Applications/微信.app/Contents/MacOS/WeChat",
            "101 100 /Applications/微信.app/Contents/MacOS/WeChatAppEx.app/Contents/MacOS/WeChatAppEx --log-level=2 --lang=zh-CN",
            "102 999 /Applications/微信.app/Contents/MacOS/WeChatAppEx.app/Contents/MacOS/WeChatAppEx --log-level=2",
            "103 100 /tmp/WeChatAppEx.app/Contents/MacOS/WeChatAppEx --log-level=2",
        ]), stderr="")

    probe = WechatWindowProbe(runner=runner, native_windows=Native())

    assert probe.search_field_focused() is True
    assert checked == [100, 101]


def test_main_process_identity_does_not_match_crash_report_annotations():
    annotation = (
        '102 1 /Applications/微信.app/Contents/Frameworks/crashpad_handler '
        '--annotation=app_path=/Applications/微信.app/Contents/MacOS/WeChat'
    )

    assert WechatWindowProbe._wechat_pid(annotation) is None
    assert WechatWindowProbe._wechat_pid(
        annotation + '\n100 1 /Applications/微信.app/Contents/MacOS/WeChat --flag=value'
    ) == 100


def test_renderer_identity_rejects_stale_foreign_and_helper_processes():
    output = '\n'.join([
        '100 1 /Applications/微信.app/Contents/MacOS/WeChat --flag=value',
        '101 100 /Applications/微信.app/Contents/MacOS/WeChatAppEx.app/Contents/MacOS/WeChatAppEx --lang=zh-CN',
        '102 99 /Applications/微信.app/Contents/MacOS/WeChatAppEx.app/Contents/MacOS/WeChatAppEx --lang=zh-CN',
        '103 100 /tmp/WeChatAppEx.app/Contents/MacOS/WeChatAppEx',
        '104 100 /Applications/微信.app/Contents/MacOS/WeChatAppEx.app/Contents/MacOS/WeChatAppExHelper',
    ])

    assert WechatWindowProbe._renderer_pids(output, 100) == [101]
    assert WechatWindowProbe._renderer_pids(output, 999) == []
