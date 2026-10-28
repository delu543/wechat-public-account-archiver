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


class NativeAppleScript:
    """Execute a small AppleScript inside the already-authorized process."""

    def __init__(self) -> None:
        self._foundation = None
        self._objc = None
        self._send0 = None
        self._send1 = None
        self._send_cstring = None
        self._send_pointer = None

    def run(self, source_text: str) -> bool:
        if not source_text or not self._load():
            return False
        pool = self._send0(self._class("NSAutoreleasePool"), self._selector("new"))
        script = None
        try:
            source = self._send_cstring(
                self._class("NSString"),
                self._selector("stringWithUTF8String:"),
                source_text.encode("utf-8"),
            )
            allocated = self._send0(self._class("NSAppleScript"), self._selector("alloc"))
            script = self._send1(allocated, self._selector("initWithSource:"), source)
            if not script:
                return False
            error = ctypes.c_void_p()
            self._send_pointer(
                script,
                self._selector("executeAndReturnError:"),
                ctypes.byref(error),
            )
            return not bool(error.value)
        except (OSError, ValueError):
            return False
        finally:
            if script:
                self._send0(script, self._selector("release"))
            if pool:
                self._send0(pool, self._selector("drain"))

    def _load(self) -> bool:
        if sys.platform != "darwin":
            return False
        if self._objc is not None:
            return True
        try:
            self._foundation = ctypes.CDLL(
                "/System/Library/Frameworks/Foundation.framework/Foundation"
            )
            objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        except OSError:
            return False
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        self._objc = objc
        self._send0 = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(("objc_msgSend", objc))
        self._send1 = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        )(("objc_msgSend", objc))
        self._send_cstring = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_char_p,
        )(("objc_msgSend", objc))
        self._send_pointer = ctypes.CFUNCTYPE(
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        )(("objc_msgSend", objc))
        return True

    def _class(self, name: str) -> int:
        return self._objc.objc_getClass(name.encode("utf-8"))

    def _selector(self, name: str) -> int:
        return self._objc.sel_registerName(name.encode("utf-8"))


class InProcessSystemEventsInput:
    """Use macOS System Events without spawning a separately-permissioned tool."""

    def __init__(
        self,
        sleeper: Callable[[float], None] = time.sleep,
        script_runner: Callable[[str], bool] | None = None,
    ) -> None:
        self._sleep = sleeper
        self._run_script = script_runner or NativeAppleScript().run

    def click(self, x: int, y: int) -> bool:
        script = f'tell application "System Events" to click at {{{int(x)}, {int(y)}}}'
        if not self._run_script(script):
            return False
        self._sleep(0.15)
        return True

    def press_key(self, key_code: int, flags: int = 0) -> bool:
        modifier = " using {command down}" if flags & NativeMacInput.COMMAND_FLAG else ""
        script = f'tell application "System Events" to key code {int(key_code)}{modifier}'
        if not self._run_script(script):
            return False
        self._sleep(0.1)
        return True


class WechatForegroundInput:
    """Use native mouse events for web content and System Events for keys."""

    def __init__(
        self,
        sleeper: Callable[[float], None] = time.sleep,
        mouse: MacInput | None = None,
        keyboard: MacInput | None = None,
    ) -> None:
        self._mouse = mouse or NativeMacInput(sleeper)
        self._keyboard = keyboard or InProcessSystemEventsInput(sleeper)

    def click(self, x: int, y: int) -> bool:
        return self._mouse.click(x, y)

    def press_key(self, key_code: int, flags: int = 0) -> bool:
        return self._keyboard.press_key(key_code, flags)


class NativeAccessibilityWindows:
    """Read and raise WeChat windows inside the authorized Python process."""

    UTF8 = 0x08000100
    AX_VALUE_POINT = 1
    AX_VALUE_SIZE = 2

    class Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]

    class Size(ctypes.Structure):
        _fields_ = [("width", ctypes.c_double), ("height", ctypes.c_double)]

    def __init__(self) -> None:
        self._ax = None
        self._cf = None
        self._true = None

    def windows(self, pid: int) -> list[WechatWindow]:
        if pid <= 0 or not self._load():
            return []
        application = self._ax.AXUIElementCreateApplication(pid)
        if not application:
            return []
        window_array = self._copy_attribute(application, "AXWindows")
        try:
            if not window_array:
                return []
            rows: list[WechatWindow] = []
            count = int(self._cf.CFArrayGetCount(window_array))
            for index in range(count):
                element = self._cf.CFArrayGetValueAtIndex(window_array, index)
                row = self._window(element)
                if row is not None:
                    rows.append(row)
            return rows
        finally:
            if window_array:
                self._cf.CFRelease(window_array)
            self._cf.CFRelease(application)

    def search_field_focused(self, pid: int) -> bool:
        """Inspect only focused control metadata, never a chat's text/value."""
        if pid <= 0 or not self._load():
            return False
        application = self._ax.AXUIElementCreateApplication(pid)
        if not application:
            return False
        focused = self._copy_attribute(application, "AXFocusedUIElement")
        try:
            if not focused:
                return False
            role = self._attribute_text(focused, "AXRole")
            if role == "AXSearchField":
                return True
            if role != "AXTextField":
                return False
            label = " ".join(
                self._attribute_text(focused, attribute)
                for attribute in ("AXDescription", "AXPlaceholderValue", "AXSubrole")
            ).lower()
            return "搜索" in label or "search" in label
        finally:
            if focused:
                self._cf.CFRelease(focused)
            self._cf.CFRelease(application)

    def activate_window(self, pid: int, title: str) -> bool:
        if pid <= 0 or not title or not self._load():
            return False
        application = self._ax.AXUIElementCreateApplication(pid)
        if not application:
            return False
        window_array = self._copy_attribute(application, "AXWindows")
        action = self._create_string("AXRaise")
        try:
            if not window_array or not action:
                return False
            count = int(self._cf.CFArrayGetCount(window_array))
            for index in range(count):
                element = self._cf.CFArrayGetValueAtIndex(window_array, index)
                if self._attribute_text(element, "AXTitle") != title:
                    continue
                self._focus_window(application, element)
                return self._ax.AXUIElementPerformAction(element, action) == 0
            return False
        finally:
            if action:
                self._cf.CFRelease(action)
            if window_array:
                self._cf.CFRelease(window_array)
            self._cf.CFRelease(application)

    def _focus_window(self, application: int, window: int) -> None:
        if not self._true:
            return
        assignments = (
            (application, "AXFrontmost", self._true),
            (application, "AXFocusedWindow", window),
            (window, "AXMain", self._true),
        )
        for element, attribute, value in assignments:
            key = self._create_string(attribute)
            if not key:
                continue
            try:
                self._ax.AXUIElementSetAttributeValue(element, key, value)
            finally:
                self._cf.CFRelease(key)

    def _window(self, element: int) -> WechatWindow | None:
        title = self._attribute_text(element, "AXTitle")
        position_value = self._copy_attribute(element, "AXPosition")
        size_value = self._copy_attribute(element, "AXSize")
        minimized_value = self._copy_attribute(element, "AXMinimized")
        try:
            if not title or not position_value or not size_value:
                return None
            point = self.Point()
            size = self.Size()
            if not self._ax.AXValueGetValue(
                position_value,
                self.AX_VALUE_POINT,
                ctypes.byref(point),
            ):
                return None
            if not self._ax.AXValueGetValue(
                size_value,
                self.AX_VALUE_SIZE,
                ctypes.byref(size),
            ):
                return None
            minimized = bool(self._cf.CFBooleanGetValue(minimized_value)) if minimized_value else False
            return WechatWindow(
                title=title,
                x=int(point.x),
                y=int(point.y),
                width=int(size.width),
                height=int(size.height),
                minimized=minimized,
            )
        finally:
            for value in (position_value, size_value, minimized_value):
                if value:
                    self._cf.CFRelease(value)

    def _attribute_text(self, element: int, attribute: str) -> str:
        value = self._copy_attribute(element, attribute)
        try:
            if not value:
                return ""
            length = int(self._cf.CFStringGetLength(value))
            capacity = int(self._cf.CFStringGetMaximumSizeForEncoding(length, self.UTF8)) + 1
            buffer = ctypes.create_string_buffer(max(1, capacity))
            if not self._cf.CFStringGetCString(value, buffer, capacity, self.UTF8):
                return ""
            return buffer.value.decode("utf-8", errors="replace")
        finally:
            if value:
                self._cf.CFRelease(value)

    def _copy_attribute(self, element: int, attribute: str) -> int | None:
        key = self._create_string(attribute)
        if not key:
            return None
        value = ctypes.c_void_p()
        try:
            error = self._ax.AXUIElementCopyAttributeValue(element, key, ctypes.byref(value))
            return value.value if error == 0 else None
        finally:
            self._cf.CFRelease(key)

    def _create_string(self, value: str) -> int | None:
        return self._cf.CFStringCreateWithCString(
            None,
            value.encode("utf-8"),
            self.UTF8,
        )

    def _load(self) -> bool:
        if sys.platform != "darwin":
            return False
        if self._ax is not None and self._cf is not None:
            return True
        try:
            ax = ctypes.CDLL(
                "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
            )
            cf = ctypes.CDLL(
                "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
            )
        except OSError:
            return False
        ax.AXUIElementCreateApplication.argtypes = [ctypes.c_int32]
        ax.AXUIElementCreateApplication.restype = ctypes.c_void_p
        ax.AXUIElementCopyAttributeValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        ax.AXUIElementCopyAttributeValue.restype = ctypes.c_int32
        ax.AXUIElementPerformAction.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        ax.AXUIElementPerformAction.restype = ctypes.c_int32
        ax.AXUIElementSetAttributeValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        ax.AXUIElementSetAttributeValue.restype = ctypes.c_int32
        ax.AXValueGetValue.argtypes = [ctypes.c_void_p, ctypes.c_int32, ctypes.c_void_p]
        ax.AXValueGetValue.restype = ctypes.c_bool
        cf.CFStringCreateWithCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
        ]
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringGetLength.argtypes = [ctypes.c_void_p]
        cf.CFStringGetLength.restype = ctypes.c_long
        cf.CFStringGetMaximumSizeForEncoding.argtypes = [ctypes.c_long, ctypes.c_uint32]
        cf.CFStringGetMaximumSizeForEncoding.restype = ctypes.c_long
        cf.CFStringGetCString.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_long,
            ctypes.c_uint32,
        ]
        cf.CFStringGetCString.restype = ctypes.c_bool
        cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
        cf.CFArrayGetCount.restype = ctypes.c_long
        cf.CFArrayGetValueAtIndex.argtypes = [ctypes.c_void_p, ctypes.c_long]
        cf.CFArrayGetValueAtIndex.restype = ctypes.c_void_p
        cf.CFBooleanGetValue.argtypes = [ctypes.c_void_p]
        cf.CFBooleanGetValue.restype = ctypes.c_bool
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        self._ax = ax
        self._cf = cf
        self._true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value
        return True


class WechatWindowProbe:
    """Inspect WeChat's outer AX window and separate content renderer safely."""

    def __init__(
        self,
        runner: RunCommand = subprocess.run,
        native_windows: NativeAccessibilityWindows | None = None,
    ) -> None:
        self._run = runner
        self._native_windows = (
            native_windows
            if native_windows is not None
            else NativeAccessibilityWindows() if runner is subprocess.run else None
        )

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
        wechat_pid = self._wechat_pid(process_output)
        app_running = wechat_pid is not None
        renderer_running = bool(self._renderer_pids(process_output, wechat_pid))
        screen_locked = self._screen_locked()
        windows = tuple(self._read_windows(wechat_pid)) if wechat_pid is not None else ()
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
            message = "微信进程存在，但没有可操作窗口；任务将保留并稍后重试"
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

    def search_field_focused(self) -> bool:
        native = self._native_windows
        check = getattr(native, "search_field_focused", None)
        if not callable(check):
            return False
        output = self._command_output(["ps", "-axo", "pid=,ppid=,command="])
        main_pid = self._wechat_pid(output)
        if main_pid is None:
            return False
        pids = [main_pid, *self._renderer_pids(output, main_pid)]
        return any(check(pid) for pid in pids)

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
        process_output = self._command_output(["ps", "-axo", "pid=,ppid=,command="])
        wechat_pid = self._wechat_pid(process_output)
        if (
            wechat_pid is not None
            and self._native_windows is not None
            and self._native_windows.activate_window(wechat_pid, title)
        ):
            return True
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

    def _read_windows(self, wechat_pid: int) -> list[WechatWindow]:
        if self._native_windows is not None:
            windows = self._native_windows.windows(wechat_pid)
            if windows:
                return windows
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

    @staticmethod
    def _process_rows(process_output: str) -> list[tuple[int, int, str]]:
        rows = []
        for line in process_output.splitlines():
            fields = line.strip().split(None, 2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                continue
            # Match argv[0], not the whole command with flags/annotations.
            # In particular crashpad's app_path annotation is not WeChat.
            rows.append((int(fields[0]), int(fields[1]), fields[2].split(None, 1)[0]))
        return rows

    @classmethod
    def _wechat_pid(cls, process_output: str) -> int | None:
        for pid, _parent, executable in cls._process_rows(process_output):
            if executable.endswith("/微信.app/Contents/MacOS/WeChat"):
                return pid
        return None

    @classmethod
    def _renderer_pids(cls, process_output: str, main_pid: int | None) -> list[int]:
        rows = cls._process_rows(process_output)
        main_executable = next(
            (executable for pid, _parent, executable in rows if pid == main_pid
             and executable.endswith("/微信.app/Contents/MacOS/WeChat")), None,
        )
        if main_executable is None:
            return []
        expected = main_executable.rsplit("/", 1)[0] + "/WeChatAppEx.app/Contents/MacOS/WeChatAppEx"
        return [pid for pid, parent, executable in rows
                if parent == main_pid and executable == expected]

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
    session_verified: bool = False

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


class _RecoveryInterrupted(RuntimeError):
    def __init__(self, runtime: WechatRuntimeStatus) -> None:
        super().__init__(runtime.message)
        self.status = runtime.state if runtime.state in ("needs_login", "screen_locked") else "unverified"


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
        self._input = input_driver or WechatForegroundInput(sleeper)
        self._article_open_attempted = False

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
        self._article_open_attempted = False
        if not name:
            return WechatPreparationResult("failed", "公众号名称为空", name, False)
        before = self.probe.inspect()
        if before.state in ("no_visible_window", "starting"):
            self.probe.activate_main_window()
            self._sleep(1.0)
            before = self.probe.inspect()
        if before.state != "ready":
            status = before.state if before.state in ("needs_login", "screen_locked") else "unverified"
            return WechatPreparationResult(status, before.message, name, False)
        public_seed_url = self._public_article_seed_url(seed_url)
        baseline_fingerprint = (
            self._safe_session_fingerprint(session_fingerprint)
            if public_seed_url
            else None
        )
        search_window = next(
            (
                window
                for window in before.windows
                if window.title == "微信 (窗口)" and not window.minimized
            ),
            None,
        )
        main_window = next(
            (
                window
                for window in before.windows
                if window.title in ("微信", "WeChat") and not window.minimized
            ),
            None,
        )
        clipboard = self._read_clipboard()
        try:
            # Cache reads and clipboard access can outlive the original window
            # snapshot. Never post an event using that stale login state.
            self._ready_status()
            search_value = public_seed_url or name
            if not self._write_clipboard(search_value.encode("utf-8")):
                return WechatPreparationResult("failed", "无法临时写入剪贴板", name, False)
            if public_seed_url and search_window is not None:
                # A public-search window is the reliable recovery surface when
                # WeChat protects the main window. Reuse its search tab before
                # touching the main window so the seed cannot land in a chat.
                result = self._open_public_seed_from_search_window(name, search_window)
                if result is not None:
                    return self._wait_for_public_seed_cache(
                        result,
                        session_fingerprint,
                        baseline_fingerprint,
                        timeout=session_timeout_seconds,
                        interval=session_poll_interval_seconds,
                    )
            if not self.probe.activate_main_window():
                return WechatPreparationResult("failed", "无法激活微信主窗口", name, False)
            if main_window is not None:
                # ``open -b`` ensures that WeChat is running but does not
                # guarantee that its only window is frontmost. Always raise
                # and focus the main window before clicking the global search
                # field, otherwise paste can land in the previously focused
                # chat composer.
                activate_named = getattr(self.probe, "activate_window", None)
                activated = bool(activate_named(main_window.title)) if callable(activate_named) else False
                needs_click_fallback = (
                    callable(activate_named) and not activated
                ) or (
                    not callable(activate_named) and len(before.windows) > 1
                )
                if needs_click_fallback and not self._click_ratio(main_window, 0.08, 0.18):
                    return WechatPreparationResult("failed", "无法聚焦微信主窗口", name, False)
                if callable(activate_named) or needs_click_fallback:
                    self._sleep(0.2)
            if not self._open_global_search(main_window):
                return WechatPreparationResult(
                    "unverified", "无法确认或操作微信搜索焦点（可能受窗口保护影响）；未继续粘贴或抓取", name, False,
                )
            if public_seed_url:
                result = self._open_public_seed_article(name)
                if not result.article_open_attempted:
                    hidden_search_window = self._activate_public_search_window()
                    if hidden_search_window is not None:
                        fallback = self._open_public_seed_from_search_window(
                            name,
                            hidden_search_window,
                        )
                        if fallback is not None:
                            result = fallback
                return self._wait_for_public_seed_cache(
                    result,
                    session_fingerprint,
                    baseline_fingerprint,
                    timeout=session_timeout_seconds,
                    interval=session_poll_interval_seconds,
                )
            baseline_fingerprint = self._safe_session_fingerprint(session_fingerprint)
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
            session_verified = False
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
                self._article_open_attempted = True
                self._sleep(2.0)
                self._ready_status()
                if session_fingerprint is None:
                    break
                if self._wait_for_session_change(
                    session_fingerprint,
                    baseline_fingerprint,
                    timeout=quick_layout_timeout,
                    interval=session_poll_interval_seconds,
                ):
                    session_verified = True
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
                        "已从精确公众号主页有界尝试文章入口，但尚未确认新会话；已停止历史接口重试",
                        name,
                        article_open_attempted,
                    )
                session_verified = True
            return WechatPreparationResult(
                "attempted",
                (
                    "已从精确公众号主页确认新会话；历史接口将从最新页继续验证精确 biz"
                    if session_verified else "已尝试精确公众号主页文章入口；未验证新会话"
                ),
                name,
                True,
                session_verified,
            )
        except _RecoveryInterrupted as exc:
            return WechatPreparationResult(
                exc.status, f"微信恢复已中止：{exc}", name, self._article_open_attempted,
            )
        finally:
            self._write_clipboard(clipboard)

    def _wait_for_public_seed_cache(
        self,
        result: WechatPreparationResult,
        fingerprint: Callable[[], str | None] | None,
        baseline: str | None,
        *,
        timeout: float,
        interval: float,
    ) -> WechatPreparationResult:
        self._ready_status()
        if not result.article_open_attempted or fingerprint is None:
            return result
        # No extra UI or page inspection: only wait until the exact-account
        # local cache has finished writing before the immediate API retry.
        verified = self._wait_for_session_change(
            fingerprint,
            baseline,
            timeout=timeout,
            interval=interval,
        )
        return WechatPreparationResult(
            "attempted" if verified else "unverified",
            (
                "已确认保存链接对应公众号的新会话；历史接口将从最新页验证精确 biz"
                if verified else "已尝试保存的公开链接，但尚未确认新会话；已停止历史接口重试"
            ),
            result.account_name, result.article_open_attempted, verified,
        )

    def _open_public_seed_from_search_window(
        self,
        account_name: str,
        search_window: WechatWindow,
    ) -> WechatPreparationResult | None:
        """Reuse the visible public-search page when WeChat protects its main UI."""
        activate_named = getattr(self.probe, "activate_window", None)
        if not callable(activate_named) or not activate_named(search_window.title):
            return None
        self._sleep(0.2)
        search_window = self._window_named("微信 (窗口)") or search_window
        if not self._select_public_search_tab(search_window):
            return None
        self._sleep(0.2)
        search_window = self._window_named("微信 (窗口)") or search_window
        if not self._submit_exact_public_search(search_window):
            return None
        self._sleep(1.2)
        search_window = self._window_named("微信 (窗口)") or search_window
        return self._open_public_web_card(account_name, search_window)

    def _activate_public_search_window(self) -> WechatWindow | None:
        """Raise a restorable public-search window hidden behind the main UI."""
        search_window = self._window_named("微信 (窗口)")
        if search_window is not None:
            return search_window
        activate_named = getattr(self.probe, "activate_window", None)
        if not callable(activate_named) or not activate_named("微信 (窗口)"):
            return None
        self._sleep(0.2)
        return self._wait_for_window("微信 (窗口)", timeout=1.0)

    def _select_public_search_tab(self, window: WechatWindow) -> bool:
        """Select the first retained Search tab before replacing its query."""
        window = self._live_window(window)
        if window is None or window.width < 300 or window.height < 300:
            return False
        x = int(window.x + min(max(window.width * 0.35, 180), 250))
        y = int(window.y + 18)
        return self._input.click(x, y)

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
    ) -> WechatPreparationResult:
        # Pasting a public article URL into the fresh global search first opens
        # WeChat's public Search page. The top "访问网页" card then opens the
        # exact article. Do not search the account name when a seed URL exists.
        self._sleep(0.35)
        if not self._search_field_focused():
            return WechatPreparationResult("unverified", "搜索焦点已丢失；未提交链接", account_name, False)
        if not self._press_key(36):
            return WechatPreparationResult("failed", "无法提交公开文章链接", account_name, False)
        search_window = self._wait_for_window("微信 (窗口)", timeout=2.0)
        if search_window is None:
            return WechatPreparationResult("unverified", "未识别到链接的微信公开搜索页", account_name, False)
        activate_named = getattr(self.probe, "activate_window", None)
        if callable(activate_named):
            activate_named(search_window.title)
        self._sleep(1.0)
        return self._open_public_web_card(account_name, search_window)

    def _open_public_web_card(
        self,
        account_name: str,
        search_window: WechatWindow,
    ) -> WechatPreparationResult:
        if not self._click_public_web_result(search_window):
            return WechatPreparationResult("failed", "无法打开链接的访问网页入口", account_name, False)
        self._article_open_attempted = True
        self._sleep(2.0)
        self._ready_status()
        return WechatPreparationResult(
            "attempted",
            "已尝试保存链接的访问网页入口；尚未验证正文或新会话",
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
        for key_code in (0, 9):
            if self._live_window(main_window) is None or not self._search_field_focused():
                return False
            if not self._press_key(key_code, NativeMacInput.COMMAND_FLAG):
                return False
        return True

    def _search_field_focused(self) -> bool:
        self._ready_status()
        check = getattr(self.probe, "search_field_focused", None)
        return bool(callable(check) and check())

    def _confirm_public_search_result(self) -> bool:
        if not self._search_field_focused():
            return False
        if not self._press_key(125):
            return False
        self._sleep(0.2)
        if not self._press_key(125):
            return False
        self._sleep(0.2)
        return self._press_key(36)

    def _submit_exact_public_search(self, search_window: WechatWindow) -> bool:
        # WeChat may turn the main-window suggestion into a related query such
        # as "<name>视频号". Replace it inside the newly opened public search
        # page so account selection is always based on the exact copied name.
        if not self._click_ratio(search_window, 0.38, 0.10):
            return False
        self._sleep(0.2)
        for key_code in (0, 9):
            if self._live_window(search_window) is None or not self._press_key(
                key_code, NativeMacInput.COMMAND_FLAG,
            ):
                return False
        self._sleep(0.15)
        return self._click_public_search_button(search_window)

    def _click_public_search_button(self, window: WechatWindow) -> bool:
        window = self._live_window(window)
        if window is None or window.width < 300 or window.height < 300:
            return False
        x = int(window.x + min(window.width * 0.80, 922))
        y = int(window.y + window.height * 0.10)
        return self._input.click(x, y)

    def _window_named(self, title: str) -> WechatWindow | None:
        candidates = [window for window in self._ready_status().windows if window.title == title and not window.minimized]
        return candidates[0] if candidates else None

    def _ready_status(self) -> WechatRuntimeStatus:
        runtime = self.probe.inspect()
        if runtime.state != "ready" or runtime.screen_locked:
            raise _RecoveryInterrupted(runtime)
        return runtime

    def _live_window(self, window: WechatWindow) -> WechatWindow | None:
        return self._window_named(window.title)

    def _press_key(self, key_code: int, flags: int = 0) -> bool:
        self._ready_status()
        return self._input.press_key(key_code, flags)

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
        window = self._live_window(window)
        if window is None or window.width < 300 or window.height < 300:
            return False
        x = int(window.x + window.width * x_ratio)
        y = int(window.y + window.height * y_ratio)
        return self._input.click(x, y)

    def _click_public_web_result(self, window: WechatWindow) -> bool:
        """Click the top public web card in compact and wide WeChat layouts."""
        window = self._live_window(window)
        if window is None or window.width < 300 or window.height < 300:
            return False
        # WeChat caps the result column width on wide windows instead of
        # stretching it. Bound the target inside that column while retaining
        # the compact-window position used by older layouts.
        x = int(window.x + min(window.width * 0.24, 355))
        # The card starts below the search field and category strip. The old
        # target stopped at y=147 and landed on the category strip instead of
        # the visible “访问网页” row.
        y = int(window.y + min(window.height * 0.245, 210))
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
            self._ready_status()
            current = self._safe_session_fingerprint(fingerprint)
            self._ready_status()
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
