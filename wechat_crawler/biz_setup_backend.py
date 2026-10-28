"""Native adapter for explicit biz-only setup; never called by weekly sources.

Only reviewed installed primitives are reused. The old TARGET_RE, chat doctor,
write_success_files and snapshot CLI are deliberately not used or modified.
"""
from __future__ import annotations

import ctypes
import hashlib
import importlib
from pathlib import Path
import subprocess
import time

from . import biz_push_helper as gate
from . import biz_setup_store as store
from .biz_push_snapshot import SUPPORT_ROOT, installed_primitives
from .biz_setup_observer import observe

INIT_HASH = "5995170f252ef12dfb1428906d9c2640fcc24f18e6546bbc636935fd737cb460"


def normal_quit(pid, executable):
    """Public AppKit terminate(), not forceTerminate/kill or UI scripting.

    Apple: developer.apple.com/documentation/appkit/nsrunningapplication/terminate()
    True is only request acceptance. Caller must verify the entire copy exits.
    """
    ctypes.CDLL("/System/Library/Frameworks/AppKit.framework/AppKit")
    objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
    objc.objc_getClass.argtypes, objc.objc_getClass.restype = [ctypes.c_char_p], ctypes.c_void_p
    objc.sel_registerName.argtypes, objc.sel_registerName.restype = [ctypes.c_char_p], ctypes.c_void_p
    pointer = ctypes.cast(objc.objc_msgSend, ctypes.c_void_p).value
    get = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
    app = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int)(pointer)(
        objc.objc_getClass(b"NSRunningApplication"), objc.sel_registerName(b"runningApplicationWithProcessIdentifier:"), pid)
    if not app:
        return False
    url = get(pointer)(app, objc.sel_registerName(b"executableURL"))
    path = get(pointer)(url, objc.sel_registerName(b"path")) if url else None
    raw = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_void_p, ctypes.c_void_p)(pointer)(
        path, objc.sel_registerName(b"UTF8String")) if path else None
    if not raw or raw.decode() != str(executable):
        raise gate.GateError("private_process_identity_changed")
    return bool(ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)(pointer)(
        app, objc.sel_registerName(b"terminate")))


class NativeBackend:
    def __init__(self):
        self.init = None
        self.pid = None

    def load(self):
        if self.init is None:
            installed_primitives()
            root = SUPPORT_ROOT.resolve(strict=True)
            path = root / "live_tools/wechat_key_init.py"
            gate._no_symlink_chain(path, root)
            info = gate._regular_private(path)
            if info.st_mode & 0o022 or hashlib.sha256(path.read_bytes()).hexdigest() != INIT_HASH:
                raise gate.GateError("reviewed_initializer_changed")
            self.init = importlib.import_module("live_tools.wechat_key_init")
            if Path(self.init.__file__).resolve() != path:
                raise gate.GateError("initializer_origin_mismatch")
        return self.init

    def app(self):
        init = self.load()
        text = gate._run(["/bin/ps", "-axo", "pid=,ppid=,comm="])
        gate.official_pids(text)
        executable = next(row[2] for line in text.splitlines()
                          if len(row := line.split(None, 2)) == 3 and row[2] in gate.MAIN_EXECUTABLES)
        app = init.validate_wechat_app(Path(executable).parents[2])
        init.require_official_wechat_signature(app)
        return app, init.official_app_cdhash(app)

    def dependencies_ready(self):
        return all(self.load().dependency_status().values())

    def bind(self):
        return gate.bind_current_account()

    def recheck_app(self, scope):
        init = self.load()
        app = init.validate_wechat_app(scope.app.app_path)
        init.require_official_wechat_signature(app)
        if app != scope.app or init.official_app_cdhash(app) != scope.cdhash:
            raise gate.GateError("official_app_changed")

    def quiescent(self, scope):
        self.recheck_app(scope)
        init = self.load()
        init.assert_original_wechat_stopped(scope.app)
        init.assert_no_target_database_holders(scope.targets)

    def prepare(self, scope):
        self.quiescent(scope)
        folder = store.account_dir(scope.db_base, create=True)
        self.private = store.directory(folder / "initialization", create=True)
        init = self.load()
        init.ensure_no_owned_runtime(self.private)
        return init.prepare_runtime_copy(scope.app, self.private, scope.cdhash,
                                         quiescence_check=lambda: self.quiescent(scope))

    def capture(self, scope, runtime, duration):
        init = self.load()

        def record(pid):
            self.pid = pid
            init.atomic_write_json(self.private / init.RUNTIME_FILENAME,
                                   init.runtime_metadata(runtime, status="capturing", pid=pid))

        return observe(init, init.require_capture_dependencies(), runtime, scope.targets, duration,
                       before_spawn=lambda: self.quiescent(scope), on_pid=record)

    def family(self, runtime):
        rows = [line.split(None, 2) for line in gate._run(["/bin/ps", "-axo", "pid=,ppid=,comm="]).splitlines()]
        prefix = str(runtime.app_copy_path / "Contents") + "/"
        return [(int(r[0]), int(r[1]), r[2]) for r in rows if len(r) == 3
                and r[0].isdigit() and r[1].isdigit() and r[2].startswith(prefix)]

    def verify_account(self, scope, runtime):
        samples = []
        for index in range(2):
            family = self.family(runtime)
            if not self.pid or not any(pid == self.pid and exe == str(runtime.executable_path) for pid, _, exe in family):
                raise gate.GateError("private_process_identity_changed")
            pids = tuple(sorted(pid for pid, _, _ in family))
            opened = gate._run(["/usr/sbin/lsof", "-nP", "-a", "-p", ",".join(map(str, pids)), "-Fpfn"])
            samples.append((pids, gate.account_from_open_files(opened, pids, gate.XROOT)))
            if not index:
                time.sleep(0.25)
        if samples[0] != samples[1] or samples[0][1] != scope.db_base:
            raise gate.GateError("private_current_account_changed")

    def finish(self, scope, runtime):
        """No retries/force quit. A stuck copy blocks reopening the original."""
        family = self.family(runtime)
        if family:
            if not self.pid or not any(pid == self.pid and exe == str(runtime.executable_path) for pid, _, exe in family):
                raise gate.GateError("private_normal_quit_needs_user")
            normal_quit(self.pid, runtime.executable_path)
        deadline = time.monotonic() + 8
        while self.family(runtime):
            if time.monotonic() >= deadline:
                raise gate.GateError("private_normal_quit_needs_user")
            time.sleep(0.2)
        self.recheck_app(scope)
        self.load().assert_original_wechat_stopped(scope.app)
        result = subprocess.run(["/usr/bin/open", str(scope.app.app_path)], capture_output=True, timeout=6, check=False)
        if result.returncode:
            raise gate.GateError("original_launch_unverified")
        self.load().atomic_write_json(self.private / self.init.RUNTIME_FILENAME,
            dict(self.init.runtime_metadata(runtime, status="stopped-original-launch-requested", pid=self.pid),
                 normal_quit_required=False))
        return {"private_copy_stopped": True, "original_launch_requested": True,
                "original_login_verified": False, "runtime_cleanup_pending": True}
