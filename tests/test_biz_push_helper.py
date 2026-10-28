import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from wechat_crawler import biz_push_helper as helper


MAIN = "/Applications/微信.app/Contents/MacOS/WeChat"
RENDERER = "/Applications/微信.app/Contents/Frameworks/WeChatAppEx.app/Contents/MacOS/WeChatAppEx"


def test_process_family_excludes_foreign_and_annotation_lookalikes():
    text = f"20 1 {MAIN}\n30 20 {RENDERER}\n31 999 {RENDERER}\n40 1 /tmp/WeChat\n41 1 /tmp/crashpad --annotation={MAIN}"
    assert helper.official_pids(text) == (20, 30)
    with pytest.raises(helper.GateError):
        helper.official_pids(text + f"\n50 1 {MAIN}")


def test_numeric_file_evidence_only_and_no_historical_guess(tmp_path):
    root = tmp_path / "xwechat_files"
    current = root / "current" / "db_storage"
    historical = root / "old" / "db_storage"
    opened = f"p20\nf4r\nn{current}/message/biz_message_0.db\nfmem\nn{historical}/message/message_0.db\np99\nf5r\nn{historical}/contact/contact.db"
    assert helper.account_from_open_files(opened, (20,), root) == current
    with pytest.raises(helper.GateError):
        helper.account_from_open_files(opened + f"\np20\nf6r\nn{historical}/message/message_0.db", (20,), root)


def test_parent_pid_is_not_assumed_to_be_numerically_smallest(tmp_path, monkeypatch):
    root = tmp_path / "xwechat_files"
    base = root / "current" / "db_storage"
    monkeypatch.setattr(helper, "XROOT", root)
    commands = []

    def runner(argv):
        commands.append(argv)
        if argv[0] == "/bin/ps":
            return f"900 1 {MAIN}\n10 900 {RENDERER}"
        if argv[0] == "/usr/bin/codesign":
            assert argv[-1] == "/Applications/微信.app"
            return ""
        return f"p900\nf4r\nn{base}/message/biz_message_0.db"

    assert helper.bind_current_account(runner, lambda _: None) == base
    assert len(commands) == 6


def test_unstable_current_account_stops_before_database_open(tmp_path, monkeypatch):
    root = tmp_path / "xwechat_files"
    monkeypatch.setattr(helper, "XROOT", root)
    sample = []

    def runner(argv):
        if argv[0] == "/bin/ps":
            return f"20 1 {MAIN}"
        if argv[0] == "/usr/bin/codesign":
            return ""
        sample.append(1)
        return f"p20\nf4r\nn{root}/account{len(sample)}/db_storage/message/biz_message_0.db"

    with pytest.raises(helper.GateError, match="unstable"):
        helper.bind_current_account(runner, lambda _: None)


def prepare_database(tmp_path, monkeypatch, *, plain=False):
    root = tmp_path / "xwechat_files"
    base = root / "current" / "db_storage"
    path = base / helper.BIZ_REL
    path.parent.mkdir(parents=True)
    salt = b"SQLite format 3\0" if plain else b"synthetic-salt!!"
    path.write_bytes(salt + bytes(4096 - len(salt)))
    monkeypatch.setattr(helper, "XROOT", root)
    return base, path, salt


def test_header_reads_only_fixed_biz_database_readonly(tmp_path, monkeypatch):
    base, path, salt = prepare_database(tmp_path, monkeypatch)
    opened = []
    real_open = os.open

    def track(selected, flags, *args):
        opened.append((Path(selected), flags & os.O_ACCMODE))
        return real_open(selected, flags, *args)

    monkeypatch.setattr(helper.os, "open", track)
    assert helper.biz_header(base) == salt
    assert opened == [(path, os.O_RDONLY)]


def test_symlink_target_rejected(tmp_path, monkeypatch):
    base, path, _ = prepare_database(tmp_path, monkeypatch)
    outside = tmp_path / "outside.db"
    path.rename(outside)
    path.symlink_to(outside)
    with pytest.raises(helper.GateError, match="symlink"):
        helper.biz_header(base)


def prepare_config(tmp_path, base, salt, *, initialized):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    config = private / "local-vault.json"
    targets = {helper.BIZ_ALIAS: helper.BIZ_REL} if initialized else {"contact": "contact/contact.db"}
    config.write_text(json.dumps({"db_base_path": str(base), "targets": targets,
        "salt_fingerprints": {helper.BIZ_ALIAS: hashlib.sha256(salt).hexdigest()}}))
    config.chmod(0o600)
    key = private / "wechat-db-keys.json"
    key.write_text('"SYNTHETIC_NEVER_READ_OR_PRINT"')
    key.chmod(0o600)
    return private, config, key


@pytest.mark.parametrize("initialized", [False, True])
def test_configuration_check_never_reads_keys_or_chat_databases(tmp_path, monkeypatch, initialized):
    base, _, salt = prepare_database(tmp_path, monkeypatch)
    private, config, key = prepare_config(tmp_path, base, salt, initialized=initialized)
    paths = []
    real_open = os.open

    def track(path, flags, *args):
        paths.append(Path(path))
        assert Path(path) != key
        return real_open(path, flags, *args)

    monkeypatch.setattr(helper.os, "open", track)
    expected = "configured_key_not_read_or_verified" if initialized else "target_not_initialized"
    assert helper.existing_target_binding(base, salt, private) == expected
    assert paths == [config]


def test_config_access_time_change_is_not_false_instability(tmp_path, monkeypatch):
    _, path, _ = prepare_database(tmp_path, monkeypatch)
    info = path.stat()
    fields = ("st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
    first = SimpleNamespace(**{field: getattr(info, field) for field in fields}, st_atime_ns=1)
    second = SimpleNamespace(**{field: getattr(info, field) for field in fields}, st_atime_ns=999)
    assert helper._metadata_identity(first) == helper._metadata_identity(second)
    second.st_mtime_ns += 1
    assert helper._metadata_identity(first) != helper._metadata_identity(second)


def test_missing_authorization_flag_does_not_run_probe(monkeypatch, capsys):
    monkeypatch.setattr(helper, "preflight", lambda: pytest.fail("unauthorized data access"))
    assert helper.main(["preflight"]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "explicit_biz_only_scope_required"


def test_unknown_failure_is_redacted(monkeypatch, capsys):
    def fail():
        raise OSError("/private/account/SECRET_DO_NOT_LOG")
    monkeypatch.setattr(helper, "bind_current_account", fail)
    assert helper.main(["preflight", "--authorized-biz-only"]) == 2
    output = capsys.readouterr().out
    assert "SECRET" not in output and "/private" not in output
    assert json.loads(output)["coverage_measured"] is False


def test_plain_header_never_checks_credential_configuration(tmp_path, monkeypatch):
    base, _, _ = prepare_database(tmp_path, monkeypatch, plain=True)
    monkeypatch.setattr(helper, "bind_current_account", lambda: base)
    monkeypatch.setattr(helper, "existing_target_binding", lambda *args: pytest.fail("unexpected private config read"))
    report = helper.preflight()
    assert report["existing_target_binding"] == "not_needed"
    assert report["key_reads"] == report["chat_database_reads"] == 0
    assert report["coverage_measured"] is False
