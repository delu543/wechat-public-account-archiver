"""Explicit setup only. Reuse the reviewed hook/matcher, not broad run_capture.

Unlike the legacy capture wrapper, never call device.kill on a failed spawn.
Always attempt unload + detach; neither a timeout nor partial keys is success.
Tests inject a fake Frida device; importing this module does not load Frida.
"""
from __future__ import annotations

import threading

from .biz_push_helper import GateError


def observe(init, frida, runtime, targets, duration, *, before_spawn, on_pid):
    if not 5 <= duration <= 180:
        raise GateError("invalid_biz_capture_duration")
    matched, failures = {}, []
    finished, lock = threading.Event(), threading.Lock()
    session = script = None

    def message(row, _data):
        if row.get("type") == "error":
            failures.append("hook")
            finished.set()
            return
        payload = row.get("payload", {})
        if row.get("type") != "send" or not isinstance(payload, dict):
            return
        if payload.get("type") == "hook-error":
            failures.append("hook")
            finished.set()
        elif payload.get("type") == "pbkdf2":
            with lock:
                init.match_pbkdf_row(payload, targets, matched)
                if set(matched) == set(targets):
                    finished.set()

    def detached(*_args):
        if not finished.is_set():
            failures.append("detached")
            finished.set()

    try:
        device = frida.get_local_device()
        before_spawn()
        pid = int(device.spawn(str(runtime.executable_path)))
        if pid <= 0:
            raise GateError("private_spawn_unverified")
        on_pid(pid)
        session = device.attach(pid)
        session.on("detached", detached)
        script = session.create_script(init.build_frida_script(targets))
        script.on("message", message)
        script.load()
        device.resume(pid)
        finished.wait(duration)
    except Exception:
        failures.append("capture")
    finally:
        for action in (getattr(script, "unload", None), getattr(session, "detach", None)):
            if action:
                try:
                    action()
                except Exception:
                    failures.append("detach")
    if failures:
        raise GateError("biz_observation_or_detach_unverified")
    if set(matched) != set(targets):
        raise GateError("incomplete_biz_key_set")
    return matched
