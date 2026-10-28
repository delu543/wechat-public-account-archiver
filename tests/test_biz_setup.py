"""Offline only: synthetic pages, fake current account/app and native backend."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from wechat_crawler import biz_push_helper as gate
from wechat_crawler import biz_setup as setup
from wechat_crawler import biz_setup_store as store
from wechat_crawler.biz_setup_targets import inventory, relative_path

KEY = bytes(range(32))


def page(salt=b"synthetic-salt01", key=KEY):
    assert len(salt) == 16
    value = bytearray(salt + bytes(4080))
    auth = hashlib.pbkdf2_hmac("sha512", key, bytes(v ^ 0x3a for v in salt), 2, 32)
    value[4032:] = hmac.digest(auth, bytes(value[16:4032]) + struct.pack("<I", 1), "sha512")
    return bytes(value)


class Backend:
    def __init__(self, base, app):
        self.base, self.application, self.events = base, app, []
        self.stopped, self.match, self.restore, self.same_account = True, True, True, True

    def bind(self):
        return self.base

    def app(self):
        return self.application, "a" * 40

    def dependencies_ready(self):
        return True

    def quiescent(self, scope):
        self.events.append("quiescent")
        if not self.stopped:
            raise gate.GateError("official_still_running")

    def prepare(self, scope):
        self.events.append("prepare")
        return object()

    def capture(self, scope, runtime, duration):
        self.events.extend(["capture", "unload", "detach"])
        return {name: (KEY if self.match else bytes(32)).hex() for name in scope.targets}

    def verify_account(self, scope, runtime):
        self.events.append("account")
        if not self.same_account:
            raise gate.GateError("private_current_account_changed")

    def finish(self, scope, runtime):
        self.events.append("normal_quit")
        if not self.restore:
            raise gate.GateError("copy_still_running")
        self.events.append("original_launch")
        return {"private_copy_stopped": True, "original_launch_requested": True, "original_login_verified": False}


@pytest.fixture
def case(tmp_path, monkeypatch):
    xroot = tmp_path / "xwechat_files"
    base = xroot / "account/db_storage"
    target = base / gate.BIZ_REL
    target.parent.mkdir(parents=True)
    target.write_bytes(page())
    app = tmp_path / "Official.app"
    app.mkdir()
    executable = app / "WeChat"
    executable.write_bytes(b"synthetic app")
    monkeypatch.setattr(gate, "XROOT", xroot)
    monkeypatch.setattr(store, "ROOT", tmp_path / "native-private")
    backend = Backend(base, SimpleNamespace(app_path=app, executable_path=executable,
                                           bundle_id="com.tencent.xinWeChat", version="4.1.15"))
    return base, target, backend


@pytest.mark.parametrize("alias", ["contact", "message_0", "media_0", "biz_message_00", "biz_message_*", "../biz_message_0"])
def test_only_exact_biz_aliases(alias):
    with pytest.raises(gate.GateError):
        relative_path(alias)


def test_dry_scan_is_readonly_redacted_and_only_biz_headers(case, monkeypatch):
    base, target, backend = case
    (target.parent / "message_0.db").write_bytes(b"PRIVATE CHAT NOT TO BE READ")
    before = target.read_bytes()
    opened = []
    original = os.open
    def open_read(path, flags, *args, **kwargs):
        opened.append(Path(path))
        return original(path, flags, *args, **kwargs)
    monkeypatch.setattr(os, "open", open_read)
    scope = setup.scan(backend)
    assert set(scope.targets) == {"biz_message_0"} and opened == [target]
    public = json.dumps(setup.public_scan(scope))
    assert str(base) not in public and scope.digest not in public and KEY.hex() not in public
    assert str(base) not in repr(scope) and "synthetic-salt01" not in repr(scope)
    assert not store.ROOT.exists() and before == target.read_bytes()


def test_no_consent_has_no_native_actions_or_files(case):
    _, _, backend = case
    scope = setup.scan(backend)
    report = setup.initialize(scope, backend, explicit_skill=True)
    assert report["reason"] == "explicit_setup_skill_and_fresh_exact_consent_required"
    assert backend.events == [] and not store.ROOT.exists()


def test_quit_and_scope_change_gate_before_prepare(case):
    _, target, backend = case
    scope = setup.scan(backend)
    backend.stopped = False
    assert setup.initialize(scope, backend, explicit_skill=True, exact_consent=True)["reason"] == "official_still_running"
    assert not scope.consumed
    backend.stopped = True
    target.write_bytes(page(b"synthetic-salt02"))
    report = setup.initialize(scope, backend, explicit_skill=True, exact_consent=True)
    assert report["reason"] == "biz_scope_changed_rescan_and_new_consent_required"
    assert "prepare" not in backend.events and not store.ROOT.exists()


def test_normal_quit_checkpoint_growth_keeps_consent_and_refreshes_targets(case):
    # Real 4.1.15 run: quitting WeChat checkpointed the WAL and grew the same
    # biz_message_0.db in place; consent must survive the quit it requires.
    _, target, backend = case
    scope = setup.scan(backend)
    with target.open("ab") as handle:
        handle.write(bytes(4096))
    report = setup.initialize(scope, backend, explicit_skill=True, exact_consent=True)
    assert report["status"] == "initialized"
    assert scope.targets[gate.BIZ_ALIAS].size == target.stat().st_size


def test_private_copy_growth_during_capture_still_publishes(case):
    _, target, backend = case
    scope = setup.scan(backend)
    capture = backend.capture
    def growing_capture(*args):
        with target.open("ab") as handle:
            handle.write(bytes(4096))
        return capture(*args)
    backend.capture = growing_capture
    report = setup.initialize(scope, backend, explicit_skill=True, exact_consent=True)
    assert report["status"] == "initialized" and report["credentials_written"]


def test_success_all_targets_atomic_private_store_and_no_replay(case):
    base, target, backend = case
    second = target.parent / "biz_message_1.db"
    second.write_bytes(page(b"synthetic-salt02"))
    scope = setup.scan(backend)
    report = setup.initialize(scope, backend, explicit_skill=True, exact_consent=True)
    assert report["status"] == "initialized" and report["credentials_written"]
    assert report["observation_detached"] and not report["original_login_verified"]
    assert backend.events.index("detach") < backend.events.index("normal_quit") < backend.events.index("original_launch")
    files = list(store.ROOT.rglob("biz0-*.json"))
    assert len(files) == 1 and files[0].stat().st_mode & 0o777 == 0o600
    assert files[0].parent.stat().st_mode & 0o777 == 0o700
    assert set(store.read_private(files[0])["keys"]) == {"biz_message_0", "biz_message_1"}
    assert store.load_current(base, target.read_bytes()[:16]) == {gate.BIZ_REL: KEY}
    assert str(base) not in json.dumps(report) and KEY.hex() not in json.dumps(report)
    events = backend.events[:]
    assert setup.initialize(scope, backend, explicit_skill=True, exact_consent=True)["reason"] == "setup_scope_expired_or_consumed"
    assert backend.events == events


@pytest.mark.parametrize("failure", ["match", "same_account", "restore"])
def test_failed_validation_still_quits_and_restores_without_claiming_success(case, failure):
    _, _, backend = case
    setattr(backend, failure, False)
    report = setup.initialize(setup.scan(backend), backend, explicit_skill=True, exact_consent=True)
    assert report["status"] == "blocked" and "normal_quit" in backend.events
    if failure == "restore":
        assert "original_launch" not in backend.events and report["restore_reason"]
    else:
        assert not report["credentials_written"] and "original_launch" in backend.events
        assert not store.ROOT.exists()


def test_salt_change_checks_filenames_only_and_does_not_read_old_keys(case, monkeypatch):
    base, target, backend = case
    setup.initialize(setup.scan(backend), backend, explicit_skill=True, exact_consent=True)
    monkeypatch.setattr(store, "read_private", lambda *args: pytest.fail("old secret must not be opened"))
    with pytest.raises(gate.GateError, match="biz_salt_changed"):
        store.load_current(base, b"synthetic-salt02")
    assert store.load_current(base.parent.parent / "other/db_storage", target.read_bytes()[:16]) is None


def test_symlink_target_rejected_without_open(case):
    base, target, _ = case
    (target.parent / "biz_message_1.db").symlink_to(target)
    with pytest.raises(gate.GateError, match="symlink"):
        inventory(base)


def test_cli_without_explicit_skill_or_tty_never_loads_native(monkeypatch, capsys):
    monkeypatch.setattr(setup, "scan", lambda *a: pytest.fail("no native scan"))
    assert setup.main(["initialize"]) == 2
    monkeypatch.setattr(setup.sys.stdin, "isatty", lambda: False)
    assert setup.main(["initialize", "--explicit-setup-skill"]) == 2
    assert "human_interactive_consent_required" in capsys.readouterr().out


@pytest.mark.parametrize("change", ["salt", "inode", "new_shard"])
def test_scope_changes_require_new_scan_before_any_copy(case, change):
    _, target, backend = case
    scope = setup.scan(backend)
    data = target.read_bytes()
    if change == "salt":
        target.write_bytes(page(b"synthetic-salt02"))
    elif change == "inode":
        target.rename(target.with_suffix(".retained"))
        target.write_bytes(data)
    else:
        (target.parent / "biz_message_1.db").write_bytes(page(b"synthetic-salt02"))
    result = setup.initialize(scope, backend, explicit_skill=True, exact_consent=True)
    assert result["reason"] == "biz_scope_changed_rescan_and_new_consent_required"
    assert "prepare" not in backend.events and not store.ROOT.exists()


def test_mtime_only_change_does_not_turn_normal_quit_into_endless_rescan(case):
    _, target, backend = case
    scope = setup.scan(backend)
    info = target.stat()
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000000))
    assert setup.initialize(scope, backend, explicit_skill=True, exact_consent=True)["status"] == "initialized"


def test_partial_key_set_never_publishes_and_expired_scope_has_no_actions(case):
    _, target, backend = case
    (target.parent / "biz_message_1.db").write_bytes(page(b"synthetic-salt02"))
    scope = setup.scan(backend)
    scope.created_at -= 1801
    assert setup.initialize(scope, backend, explicit_skill=True, exact_consent=True)["reason"] == "setup_scope_expired_or_consumed"
    assert backend.events == []
    scope = setup.scan(backend)
    backend.capture = lambda *a: {gate.BIZ_ALIAS: KEY}
    report = setup.initialize(scope, backend, explicit_skill=True, exact_consent=True)
    assert report["reason"] == "incomplete_biz_key_set" and not store.ROOT.exists()
    assert backend.events[-2:] == ["normal_quit", "original_launch"]
