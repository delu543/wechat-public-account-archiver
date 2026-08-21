from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from typing import Callable, Protocol, Sequence
from urllib.parse import urlparse


@dataclass(frozen=True)
class WechatWindow:
    title: str
    x: int
    y: int
    width: int
    height: int
    minimized: bool = False


@dataclass(frozen=True)
class WechatRuntimeStatus:
    state: str
    message: str
    app_running: bool
    renderer_running: bool
    windows: tuple[WechatWindow, ...]
    capture_strategy: str
    screen_locked: bool = False

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["windows"] = [asdict(window) for window in self.windows]
        return payload


RunCommand = Callable[..., subprocess.CompletedProcess]


class MacInput(Protocol):
    def click(self, x: int, y: int) -> bool: ...

    def press_key(self, key_code: int, flags: int = 0) -> bool: ...


class NativeMacInput:
    """Post foreground input through the already-authorized Python process."""

    COMMAND_FLAG = 1 << 20
    EVENT_TAP = 0
    MOUSE_LEFT = 0
    MOUSE_MOVED = 5
    MOUSE_DOWN = 1
    MOUSE_UP = 2

    class Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    def __init__(self, sleeper: Callable[[float], None] = time.sleep) -> None:
        self._sleep = sleeper
        self._cg = None
        self._cf = None

    def click(self, x: int, y: int) -> bool:
        if not self._load() or not self._event_access_allowed():
            return False
        point = self.Point(float(x), float(y))
        for event_type, delay in (
            (self.MOUSE_MOVED, 0.08),
            (self.MOUSE_DOWN, 0.06),
            (self.MOUSE_UP, 0.08),
        ):
            event = self._cg.CGEventCreateMouseEvent(
                None,
                event_type,
                point,
                self.MOUSE_LEFT,
            )
            if not event:
                return False
            self._cg.CGEventPost(self.EVENT_TAP, event)
            self._cf.CFRelease(event)
            self._sleep(delay)
        return True

    def press_key(self, key_code: int, flags: int = 0) -> bool:
        if not self._load() or not self._event_access_allowed():
            return False
        for is_down in (True, False):
            event = self._cg.CGEventCreateKeyboardEvent(None, key_code, is_down)
            if not event:
                return False
            if flags:
                self._cg.CGEventSetFlags(event, flags)
            self._cg.CGEventPost(self.EVENT_TAP, event)
            self._cf.CFRelease(event)
            self._sleep(0.05)
        return True

    def _event_access_allowed(self) -> bool:
        preflight = getattr(self._cg, "CGPreflightPostEventAccess", None)
        return preflight is None or bool(preflight())

    def _load(self) -> bool:
        if sys.platform != "darwin":
            return False
        if self._cg is not None and self._cf is not None:
            return True
        try:
            cg = ctypes.CDLL("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics")
            cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        except OSError:
            return False
        cg.CGEventCreateMouseEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            self.Point,
            ctypes.c_uint32,
        ]
        cg.CGEventCreateMouseEvent.restype = ctypes.c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint16,
            ctypes.c_bool,
        ]
        cg.CGEventCreateKeyboardEvent.restype = ctypes.c_void_p
        cg.CGEventSetFlags.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        cg.CGEventPost.argtypes = [ctypes.c_uint32, ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        preflight = getattr(cg, "CGPreflightPostEventAccess", None)
        if preflight is not None:
            preflight.restype = ctypes.c_bool
        self._cg = cg
        self._cf = cf
        return True


class WechatWindowProbe:
    """Inspect WeChat's outer AX window and separate content renderer safely."""

    def __init__(self, runner: RunCommand = subprocess.run) -> None:
        self._run = runner

    def inspect(self) -> WechatRuntimeStatus:
        if sys.platform != "darwin":
            return WechatRuntimeStatus(
                state="unsupported",
                message="微信窗口诊断只支持 macOS",
                app_running=False,
                renderer_running=False,
                windows=(),
                capture_strategy="none",
            )
        process_output = self._command_output(["ps", "-axo", "pid=,ppid=,command="])
        app_running = any(
            "/微信.app/Contents/MacOS/WeChat" in line and "WeChatAppEx.app" not in line
            for line in process_output.splitlines()
        )
        renderer_running = any(
            "/WeChatAppEx.app/Contents/MacOS/WeChatAppEx" in line
            and "Helper" not in line
            for line in process_output.splitlines()
        )
        screen_locked = self._screen_locked()
        windows = tuple(self._read_windows()) if app_running else ()
        login_shell = bool(windows) and all(
            window.width <= 360 and window.height <= 500 for window in windows
        )
        if not app_running:
            state = "not_running"
            message = "微信未运行；自动任务将暂停，不会尝试登录或修改网络设置"
            strategy = "none"
        elif screen_locked:
            state = "screen_locked"
            message = "Mac 屏幕已锁定；任务将保留并在解锁后重试，不会操作登录界面"
            strategy = "deferred_until_unlock"
        elif login_shell:
            state = "needs_login"
            message = "微信当前显示登录窗口；自动任务将暂停，等待用户正常登录"
            strategy = "deferred_until_login"
        elif not renderer_running:
            state = "starting"
            message = "微信外层进程已启动，内容渲染进程尚未就绪"
            strategy = "outer_window_only"
        elif not windows:
            state = "no_visible_window"
            message = "微信已登录进程存在，但没有可操作窗口；任务将保留并稍后重试"
            strategy = "renderer_process_probe"
        else:
            state = "ready"
            message = "微信外层窗口与内容渲染进程均已识别；抓取不依赖单窗口截图"
            strategy = "outer_ax_plus_renderer_process"
        return WechatRuntimeStatus(
            state=state,
            message=message,
            app_running=app_running,
            renderer_running=renderer_running,
            windows=windows,
            capture_strategy=strategy,
            screen_locked=screen_locked,
        )

    def _screen_locked(self) -> bool:
        output = self._command_output(["ioreg", "-n", "Root", "-d1"])
        return '"IOConsoleLocked" = Yes' in output or '"CGSSessionScreenIsLocked"=Yes' in output

    def activate_main_window(self) -> bool:
        if sys.platform != "darwin":
            return False
        try:
            result = self._run(
                ["open", "-b", "com.tencent.xinWeChat"],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def activate_window(self, title: str) -> bool:
        if sys.platform != "darwin" or not title:
            return False
        script = """
on run argv
    set targetTitle to item 1 of argv
    tell application "System Events"
        tell application process "WeChat"
            click menu item targetTitle of menu "窗口" of menu bar 1
        end tell
    end tell
end run
"""
        try:
            result = self._run(
                ["osascript", "-e", script, title],
                capture_output=True,
                text=True,
                timeout=4,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _read_windows(self) -> list[WechatWindow]:
        script = """
set outputText to ""
tell application "System Events"
    if exists (first application process whose bundle identifier is "com.tencent.xinWeChat") then
        set wechatProcess to first application process whose bundle identifier is "com.tencent.xinWeChat"
        tell wechatProcess
            repeat with currentWindow in windows
                set windowTitle to name of currentWindow as text
                set windowPosition to position of currentWindow
                set windowSize to size of currentWindow
                set isMinimized to value of attribute "AXMinimized" of currentWindow
                set outputText to outputText & windowTitle & tab & (item 1 of windowPosition) & tab & (item 2 of windowPosition) & tab & (item 1 of windowSize) & tab & (item 2 of windowSize) & tab & isMinimized & linefeed
            end repeat
        end tell
    end if
end tell
return outputText
"""
        windows = parse_window_rows(self._command_output(["osascript", "-e", script]))
        return windows or self._read_window_server_windows()

    def _read_window_server_windows(self) -> list[WechatWindow]:
        """Read only public window metadata when WeChat omits AXWindow nodes."""
        script = r'''
import Foundation
import CoreGraphics
let options: CGWindowListOption = [.optionOnScreenOnly, .excludeDesktopElements]
guard let rows = CGWindowListCopyWindowInfo(options, kCGNullWindowID) as? [[String: Any]] else {
    exit(0)
}
for row in rows {
    let owner = row[kCGWindowOwnerName as String] as? String ?? ""
    guard owner == "微信" || owner == "WeChat" else { continue }
    let title = row[kCGWindowName as String] as? String ?? ""
    let layer = row[kCGWindowLayer as String] as? Int ?? -1
    guard !title.isEmpty, layer == 0,
          let bounds = row[kCGWindowBounds as String] as? [String: Any],
          let x = bounds["X"] as? NSNumber,
          let y = bounds["Y"] as? NSNumber,
          let width = bounds["Width"] as? NSNumber,
          let height = bounds["Height"] as? NSNumber else { continue }
    print("\(title)\t\(x.intValue)\t\(y.intValue)\t\(width.intValue)\t\(height.intValue)\tfalse")
}
'''
        return parse_window_rows(self._command_output(["swift", "-e", script]))

    def _run_osascript(self, script: str) -> bool:
        try:
            result = self._run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0

    def _command_output(self, command: Sequence[str]) -> str:
        try:
            result = self._run(
                list(command),
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return result.stdout if result.returncode == 0 else ""


@dataclass(frozen=True)
class WechatPreparationResult:
    status: str
    message: str
    account_name: str
    article_open_attempted: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class WechatSessionRefresher:
    """Open a public account article through normal visible WeChat UI controls."""

    def __init__(
        self,
        probe: WechatWindowProbe | None = None,
        runner: RunCommand = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
        input_driver: MacInput | None = None,
    ) -> None:
        self.probe = probe or WechatWindowProbe(runner)
        self._run = runner
        self._sleep = sleeper
        self._input = input_driver or NativeMacInput(sleeper)

    def prepare_account(
        self,
        account_name: str,
        seed_url: str | None = None,
        *,
        session_fingerprint: Callable[[], str | None] | None = None,
        session_timeout_seconds: float = 15.0,
        session_poll_interval_seconds: float = 0.75,
    ) -> WechatPreparationResult:
        name = account_name.strip()
        if not name:
            return WechatPreparationResult("failed", "公众号名称为空", name, False)
        before = self.probe.inspect()
        if before.state != "ready":
            return WechatPreparationResult("needs_login", before.message, name, False)
        main_window = next(
            (
                window
                for window in before.windows
                if window.title in ("微信", "WeChat") and not window.minimized
            ),
            None,
        )
        if not self._close_stale_recovery_windows(before.windows):
            return WechatPreparationResult("failed", "无法关闭上一轮公众号公开浏览窗口", name, False)
        if not self.probe.activate_main_window():
            return WechatPreparationResult("failed", "无法激活微信主窗口", name, False)
        baseline_fingerprint = self._safe_session_fingerprint(session_fingerprint)
        if main_window is not None and len(before.windows) > 1:
            # Article/search child windows are normally offset to the right.
            # Prefer WeChat's own Window menu, then use the exposed left rail
            # as a fallback without closing any user window.
            activate_named = getattr(self.probe, "activate_window", None)
            activated = bool(activate_named(main_window.title)) if callable(activate_named) else False
            if not activated and not self._click_ratio(main_window, 0.08, 0.18):
                return WechatPreparationResult("failed", "无法聚焦微信主窗口", name, False)
            self._sleep(0.2)

        clipboard = self._read_clipboard()
        try:
            public_seed_url = self._public_article_seed_url(seed_url)
            search_value = public_seed_url or name
            if not self._write_clipboard(search_value.encode("utf-8")):
                return WechatPreparationResult("failed", "无法临时写入剪贴板", name, False)
            if not self._open_global_search(main_window):
                return WechatPreparationResult("failed", "无法打开微信主界面搜索", name, False)
            if public_seed_url:
                return self._open_public_seed_article(
                    name,
                    session_fingerprint=session_fingerprint,
                    baseline_fingerprint=baseline_fingerprint,
                    session_timeout_seconds=session_timeout_seconds,
                    session_poll_interval_seconds=session_poll_interval_seconds,
                )
            # The first row is commonly the local account card. The second row
            # is the public "搜一搜" route that opens the article search page.
            self._sleep(1.0)
            if not self._confirm_public_search_result():
                return WechatPreparationResult("failed", "无法进入微信公开搜索页", name, False)
            self._sleep(1.0)
            search_window = self._wait_for_window("微信 (窗口)", timeout=1.4)
            if search_window is None:
                return WechatPreparationResult("unverified", "未识别到微信公开搜索页", name, False)
            activate_named = getattr(self.probe, "activate_window", None)
            if callable(activate_named):
                activate_named(search_window.title)
            self._sleep(0.2)
            if not self._submit_exact_public_search(search_window):
                return WechatPreparationResult("failed", "无法在微信公开搜索页提交精确公众号名", name, False)
            self._sleep(1.0)
            search_window = self._window_named("微信 (窗口)") or search_window
            if not self._click_ratio(search_window, 0.465, 0.152):
                return WechatPreparationResult("failed", "无法切换微信搜一搜账号分类", name, False)
            self._sleep(1.2)
            search_window = self._window_named("微信 (窗口)") or search_window
            if not self._click_ratio(search_window, 0.48, 0.29):
                return WechatPreparationResult("failed", "无法打开搜一搜中的公众号主页", name, False)
            self._sleep(1.0)
            profile_window = self._wait_for_window("公众号", timeout=2.0)
            if profile_window is None:
                return WechatPreparationResult("unverified", "未识别到新打开的公众号主页", name, False)
            if callable(activate_named):
                activate_named(profile_window.title)
            self._sleep(0.2)
            layouts = ((0.285, 0.43), (0.31, 0.455), (0.335, 0.48))
            if session_fingerprint is None:
                layouts = layouts[:1]
            quick_layout_timeout = min(
                session_timeout_seconds / len(layouts),
                session_poll_interval_seconds,
            )
            article_open_attempted = False
            for tab_y_ratio, article_y_ratio in layouts:
                if callable(activate_named) and not activate_named(profile_window.title):
                    return WechatPreparationResult("failed", "无法重新聚焦公众号主页", name, article_open_attempted)
                self._sleep(0.2)
                profile_window = self._window_named("公众号") or profile_window
                if not self._click_ratio(profile_window, 0.39, tab_y_ratio):
                    return WechatPreparationResult("failed", "无法切换公众号主页的文章分类", name, article_open_attempted)
                self._sleep(0.8)
                profile_window = self._window_named("公众号") or profile_window
                if not self._click_ratio(profile_window, 0.50, article_y_ratio):
                    return WechatPreparationResult("failed", "无法打开公众号主页中的文章", name, article_open_attempted)
                article_open_attempted = True
                self._sleep(2.0)
                if session_fingerprint is None or self._wait_for_session_change(
                    session_fingerprint,
                    baseline_fingerprint,
                    timeout=quick_layout_timeout,
                    interval=session_poll_interval_seconds,
                ):
                    break
            else:
                remaining_timeout = max(
                    0.0,
                    session_timeout_seconds - quick_layout_timeout * len(layouts),
                )
                if remaining_timeout <= 0 or not self._wait_for_session_change(
                    session_fingerprint,
                    baseline_fingerprint,
                    timeout=remaining_timeout,
                    interval=session_poll_interval_seconds,
                ):
                    return WechatPreparationResult(
                        "unverified",
                        "已从精确公众号主页有界尝试文章入口，但尚未确认新会话；历史接口将进行一次最终验证",
                        name,
                        article_open_attempted,
                    )
            return WechatPreparationResult(
                "attempted",
                "已从精确公众号主页打开文章并确认新会话；历史接口将从最新页继续验证精确 biz",
                name,
                True,
            )
        finally:
            self._write_clipboard(clipboard)

    @staticmethod
    def _public_article_seed_url(value: str | None) -> str | None:
        if not value:
            return None
        candidate = value.strip()
        parsed = urlparse(candidate)
        if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != "mp.weixin.qq.com":
            return None
        if parsed.path != "/s" and not parsed.path.startswith("/s/"):
            return None
        return candidate

    def _open_public_seed_article(
        self,
        account_name: str,
        *,
        session_fingerprint: Callable[[], str | None] | None,
        baseline_fingerprint: str | None,
        session_timeout_seconds: float,
        session_poll_interval_seconds: float,
    ) -> WechatPreparationResult:
        # Pasting a public article URL into the fresh global search first opens
        # WeChat's public Search page. The top "访问网页" card then opens the
        # exact article. Do not search the account name when a seed URL exists.
        self._sleep(0.8)
        if not self._input.press_key(36):
            return WechatPreparationResult("failed", "无法提交公开文章链接", account_name, False)
        search_window = self._wait_for_window("微信 (窗口)", timeout=2.0)
        if search_window is None:
            return WechatPreparationResult("unverified", "未识别到链接的微信公开搜索页", account_name, False)
        activate_named = getattr(self.probe, "activate_window", None)
        if callable(activate_named):
            activate_named(search_window.title)
        self._sleep(0.2)
        search_window = self._window_named("微信 (窗口)") or search_window
        if not self._click_ratio(search_window, 0.48, 0.247):
            return WechatPreparationResult("failed", "无法打开链接的访问网页入口", account_name, False)
        self._sleep(2.0)
        if session_fingerprint is not None and not self._wait_for_session_change(
            session_fingerprint,
            baseline_fingerprint,
            timeout=session_timeout_seconds,
            interval=session_poll_interval_seconds,
        ):
            return WechatPreparationResult(
                "unverified",
                "已通过保存的公开文章链接打开正文，但尚未确认新会话；历史接口将进行一次最终验证",
                account_name,
                True,
            )
        return WechatPreparationResult(
            "attempted",
            "已通过保存的公开文章链接打开正文；历史接口将从最新页继续验证精确 biz",
            account_name,
            True,
        )

    def _open_global_search(self, main_window: WechatWindow | None) -> bool:
        # Command-F opens "搜索聊天记录" in current WeChat versions. The
        # automation must use the main window's fresh global search field and
        # must never inspect the user's historical chat messages.
        if main_window is None or not self._click_ratio(main_window, 0.23, 0.035):
            return False
        self._sleep(0.35)
        return self._input.press_key(0, NativeMacInput.COMMAND_FLAG) and self._input.press_key(
            9, NativeMacInput.COMMAND_FLAG
        )

    def _close_stale_recovery_windows(self, windows: Sequence[WechatWindow]) -> bool:
        activate_named = getattr(self.probe, "activate_window", None)
        stale_titles = ("公众号", "微信 (窗口)")
        visible_titles = {window.title for window in windows}
        for title in stale_titles:
            if title not in visible_titles:
                continue
            if not callable(activate_named) or not activate_named(title):
                return False
            self._sleep(0.15)
            if not self._input.press_key(13, NativeMacInput.COMMAND_FLAG):  # Command-W
                return False
            self._sleep(0.2)
        return True

    def _confirm_public_search_result(self) -> bool:
        if not self._input.press_key(125):
            return False
        self._sleep(0.2)
        if not self._input.press_key(125):
            return False
        self._sleep(0.2)
        return self._input.press_key(36)

    def _submit_exact_public_search(self, search_window: WechatWindow) -> bool:
        # WeChat may turn the main-window suggestion into a related query such
        # as "<name>视频号". Replace it inside the newly opened public search
        # page so account selection is always based on the exact copied name.
        if not self._click_ratio(search_window, 0.38, 0.10):
            return False
        self._sleep(0.2)
        return (
            self._input.press_key(0, NativeMacInput.COMMAND_FLAG)
            and self._input.press_key(9, NativeMacInput.COMMAND_FLAG)
            and self._input.press_key(36)
        )

    def _window_named(self, title: str) -> WechatWindow | None:
        candidates = [window for window in self.probe.inspect().windows if window.title == title and not window.minimized]
        return candidates[0] if candidates else None

    def _content_window(self) -> WechatWindow | None:
        windows = [window for window in self.probe.inspect().windows if not window.minimized]
        for title in ("公众号", "微信 (窗口)", "微信", "WeChat"):
            match = next((window for window in windows if window.title == title), None)
            if match is not None:
                return match
        return max(windows, key=lambda window: window.width * window.height, default=None)

    def _wait_for_window(
        self,
        title: str,
        *,
        timeout: float,
        interval: float = 0.2,
    ) -> WechatWindow | None:
        attempts = max(1, int(timeout / interval) + 1)
        for attempt in range(attempts):
            window = self._window_named(title)
            if window is not None:
                return window
            if attempt + 1 < attempts:
                self._sleep(interval)
        return None

    def _click_ratio(self, window: WechatWindow, x_ratio: float, y_ratio: float) -> bool:
        if window.width < 300 or window.height < 300:
            return False
        x = int(window.x + window.width * x_ratio)
        y = int(window.y + window.height * y_ratio)
        return self._input.click(x, y)

    def _wait_for_session_change(
        self,
        fingerprint: Callable[[], str | None],
        baseline: str | None,
        *,
        timeout: float,
        interval: float,
    ) -> bool:
        bounded_timeout = max(0.0, float(timeout))
        bounded_interval = max(0.05, float(interval))
        deadline = time.monotonic() + bounded_timeout
        max_attempts = max(1, int(bounded_timeout / bounded_interval) + 1)
        for attempt in range(max_attempts):
            current = self._safe_session_fingerprint(fingerprint)
            if current and current != baseline:
                return True
            if attempt + 1 >= max_attempts or time.monotonic() >= deadline:
                break
            self._sleep(min(bounded_interval, max(0.0, deadline - time.monotonic())))
        return False

    @staticmethod
    def _safe_session_fingerprint(
        fingerprint: Callable[[], str | None] | None,
    ) -> str | None:
        if fingerprint is None:
            return None
        try:
            return fingerprint()
        except Exception:
            return None

    def _read_clipboard(self) -> bytes:
        try:
            result = self._run(["pbpaste"], capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return b""
        return result.stdout if result.returncode == 0 else b""

    def _write_clipboard(self, value: bytes) -> bool:
        try:
            result = self._run(["pbcopy"], input=value, capture_output=True, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0


def parse_window_rows(output: str) -> list[WechatWindow]:
    windows: list[WechatWindow] = []
    for raw_line in output.splitlines():
        parts = raw_line.split("\t")
        if len(parts) != 6:
            continue
        try:
            windows.append(
                WechatWindow(
                    title=parts[0],
                    x=int(parts[1]),
                    y=int(parts[2]),
                    width=int(parts[3]),
                    height=int(parts[4]),
                    minimized=parts[5].strip().lower() == "true",
                )
            )
        except ValueError:
            continue
    return windows
