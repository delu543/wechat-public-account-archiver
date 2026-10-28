"""No real Frida, AppKit calls, process operation or WeChat launch."""
from types import SimpleNamespace

import pytest

from wechat_crawler.biz_push_helper import GateError
from wechat_crawler.biz_setup_observer import observe
from wechat_crawler import biz_setup_backend as native
from wechat_crawler import biz_setup_observer as observer


def observer_case(failure=None):
    events, callbacks = [], {}
    targets = {"biz_message_0": object(), "biz_message_1": object()}
    def action(name):
        events.append(name)
        if failure == name:
            raise RuntimeError("private detail not for output")
    script = SimpleNamespace(on=lambda name, fn: callbacks.update({name: fn}),
        load=lambda: action("load"), unload=lambda: action("unload"))
    session = SimpleNamespace(on=lambda *args: None,
        create_script=lambda source: script, detach=lambda: action("detach"))
    def resume(pid):
        action("resume")
        callbacks["message"]({"type": "send", "payload": {"type": "pbkdf2"}}, None)
    device = SimpleNamespace(spawn=lambda path: 123, attach=lambda pid: session, resume=resume,
                             kill=lambda *args: pytest.fail("must never kill"))
    def match(row, exact, matched):
        assert set(exact) == set(targets)
        matched.update({name: "0" * 64 for name in exact})
    init = SimpleNamespace(build_frida_script=lambda exact: "exact-salt-only", match_pbkdf_row=match)
    return events, targets, init, SimpleNamespace(get_local_device=lambda: device)


def test_observer_detaches_immediately_when_all_exact_targets_arrive():
    events, targets, init, frida = observer_case()
    matched = observe(init, frida, SimpleNamespace(executable_path="synthetic-private-executable"), targets, 5,
                      before_spawn=lambda: events.append("preflight"), on_pid=lambda p: events.append("pid"))
    assert set(matched) == set(targets)
    assert events == ["preflight", "pid", "load", "resume", "unload", "detach"]


@pytest.mark.parametrize("failure", ["load", "resume", "unload", "detach"])
def test_observer_failure_attempts_both_cleanup_actions_and_never_kills(failure):
    events, targets, init, frida = observer_case(failure)
    with pytest.raises(GateError, match="biz_observation_or_detach_unverified"):
        observe(init, frida, SimpleNamespace(executable_path="synthetic-private-executable"), targets, 5,
                before_spawn=lambda: None, on_pid=lambda p: None)
    assert "unload" in events and "detach" in events


def lifecycle(tmp_path, monkeypatch, *, exits):
    backend = native.NativeBackend()
    backend.pid, backend.private = 123, tmp_path
    events = []
    runtime = SimpleNamespace(executable_path=tmp_path / "Owned.app/Contents/MacOS/WeChat",
                              app_copy_path=tmp_path / "Owned.app")
    scope = SimpleNamespace(app=SimpleNamespace(app_path=tmp_path / "Official.app"))
    family = [(123, 1, str(runtime.executable_path))]
    samples = iter([family, []])
    backend.family = (lambda r: next(samples, [])) if exits else lambda r: family
    backend.recheck_app = lambda s: events.append("verify-original")
    backend.init = SimpleNamespace(RUNTIME_FILENAME="runtime.json",
        assert_original_wechat_stopped=lambda app: events.append("original-stopped"),
        atomic_write_json=lambda *a: None, runtime_metadata=lambda *a, **k: {})
    monkeypatch.setattr(native, "normal_quit", lambda *a: events.append("normal-quit"))
    monkeypatch.setattr(native.subprocess, "run", lambda *a, **k: events.append("launch-original") or SimpleNamespace(returncode=0))
    clock = iter([0, 9])
    monkeypatch.setattr(native.time, "monotonic", lambda: next(clock))
    return backend, scope, runtime, events


def test_normal_quit_request_is_not_exit_proof_and_original_never_opens_early(tmp_path, monkeypatch):
    backend, scope, runtime, events = lifecycle(tmp_path, monkeypatch, exits=False)
    with pytest.raises(GateError, match="private_normal_quit_needs_user"):
        backend.finish(scope, runtime)
    assert events == ["normal-quit"]


def test_private_exit_then_verified_original_launch_request_not_login_claim(tmp_path, monkeypatch):
    backend, scope, runtime, events = lifecycle(tmp_path, monkeypatch, exits=True)
    report = backend.finish(scope, runtime)
    assert events == ["normal-quit", "verify-original", "original-stopped", "launch-original"]
    assert report["private_copy_stopped"] and report["original_launch_requested"]
    assert report["original_login_verified"] is False


def test_timeout_with_partial_set_detaches_and_never_publishes(monkeypatch):
    events, targets, init, frida = observer_case()
    event = observer.threading.Event
    class ImmediateTimeout(event):
        def wait(self, timeout=None):
            return False
    monkeypatch.setattr(observer.threading, "Event", ImmediateTimeout)
    init.match_pbkdf_row = lambda row, exact, matched: matched.update({"biz_message_0": "0" * 64})
    with pytest.raises(GateError, match="incomplete_biz_key_set"):
        observe(init, frida, SimpleNamespace(executable_path="synthetic-private-executable"), targets, 5,
                before_spawn=lambda: None, on_pid=lambda p: None)
    assert events[-2:] == ["unload", "detach"]


def test_initializer_hash_change_blocks_before_module_import(tmp_path, monkeypatch):
    path = tmp_path / "live_tools/wechat_key_init.py"
    path.parent.mkdir()
    path.write_text("raise RuntimeError('must not execute')")
    monkeypatch.setattr(native, "SUPPORT_ROOT", tmp_path)
    monkeypatch.setattr(native, "installed_primitives", lambda: None)
    monkeypatch.setattr(native.importlib, "import_module", lambda *a: pytest.fail("changed module must not load"))
    with pytest.raises(GateError, match="reviewed_initializer_changed"):
        native.NativeBackend().load()
