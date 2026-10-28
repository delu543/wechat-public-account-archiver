"""Restricted passive biz-push preflight; no UI, network, keys or chat DB reads.

Standalone child-process boundary. The existing ordinary chat doctor is never
used: it opens contact/message databases. This gate reads only the fixed biz
DB header and bounded credential-free target-binding metadata. It cannot
initialize keys, decrypt, retain plaintext, crawl, or export.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import time


XROOT = Path.home() / "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"
PRIVATE_ROOT = Path.home() / "Library/Application Support/WeChatVoiceMP4/private"
BIZ_REL = "message/biz_message_0.db"
BIZ_ALIAS = "biz_message_0"
MAX_CONFIG_BYTES = 65536
MAIN_EXECUTABLES = ("/Applications/微信.app/Contents/MacOS/WeChat",
                    "/Applications/WeChat.app/Contents/MacOS/WeChat")
# Same official-app requirement as the installed Local Export helper; no code
# is injected into WeChat. codesign only verifies its on-disk application.
OFFICIAL_REQUIREMENT = (
    '(anchor apple generic and certificate leaf[field.1.2.840.113635.100.6.1.9] exists or '
    'anchor apple generic and certificate 1[field.1.2.840.113635.100.6.2.6] exists and '
    'certificate leaf[field.1.2.840.113635.100.6.1.13] exists and '
    'certificate leaf[subject.OU] = "5A4RE8SF68") and identifier "com.tencent.xinWeChat"'
)


class GateError(RuntimeError):
    """Only a fixed safe code may cross the helper boundary."""


def _run(argv):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=6, check=False)
    if result.returncode or len(result.stdout.encode()) > 8 * 1024 * 1024:
        raise GateError("metadata_unavailable")
    return result.stdout


def official_pids(ps_text: str) -> tuple[int, ...]:
    rows = []
    for line in ps_text.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            rows.append((int(parts[0]), int(parts[1]), parts[2]))
    mains = [(pid, command) for pid, _, command in rows if command in MAIN_EXECUTABLES]
    if len(mains) != 1:
        raise GateError("official_process_not_unique")
    main_pid, executable = mains[0]
    contents = str(Path(executable).parents[1]) + "/"
    family = {main_pid}
    for _ in range(len(rows)):
        previous = len(family)
        family.update(pid for pid, parent, command in rows
                      if parent in family and command.startswith(contents))
        if len(family) == previous:
            break
    if len(family) > 128:
        raise GateError("official_process_not_unique")
    return tuple(sorted(family))


def account_from_open_files(output: str, pids: tuple[int, ...], root: Path) -> Path:
    accounts = set()
    pid, fd = None, ""
    prefix = str(root) + "/"
    for line in output.splitlines():
        if not line:
            continue
        tag, value = line[0], line[1:]
        if tag == "p":
            pid, fd = int(value) if value.isdigit() else None, ""
        elif tag == "f":
            fd = value
        elif tag == "n" and pid in pids and re.fullmatch(r"\d+[A-Za-z]*", fd):
            if not value.startswith(prefix) or "\x00" in value:
                continue
            parts = Path(value[len(prefix):]).parts
            if len(parts) >= 4 and parts[1] == "db_storage" and not any(p in (".", "..") for p in parts):
                accounts.add(root / parts[0] / "db_storage")
    if len(accounts) != 1:
        raise GateError("current_account_not_unique")
    return next(iter(accounts))


def bind_current_account(runner=_run, sleeper=time.sleep) -> Path:
    samples = []
    for _ in range(2):
        ps_text = runner(["/bin/ps", "-axo", "pid=,ppid=,comm="])
        pids = official_pids(ps_text)
        main = next(parts[2] for line in ps_text.splitlines()
                    if len(parts := line.split(None, 2)) == 3 and parts[2] in MAIN_EXECUTABLES)
        app = Path(main).parents[2]
        try:
            runner(["/usr/bin/codesign", "--verify", "--strict", "-R=" + OFFICIAL_REQUIREMENT, str(app)])
        except GateError:
            raise GateError("official_signature_unverified") from None
        opened = runner(["/usr/sbin/lsof", "-nP", "-a", "-p", ",".join(map(str, pids)), "-Fpfn"])
        samples.append((pids, account_from_open_files(opened, pids, XROOT)))
        if len(samples) == 1:
            sleeper(0.25)
    if samples[0] != samples[1]:
        raise GateError("current_account_unstable")
    return samples[0][1]


def _regular_private(path: Path, *, private=False):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
        raise GateError("unsafe_local_file")
    if private and info.st_mode & 0o077:
        raise GateError("unsafe_private_permissions")
    return info


def _metadata_identity(info):
    # Reads can update atime. Do not confuse that with a content/identity
    # change; keep inode, ownership, permissions, size, mtime and ctime gates.
    return tuple(getattr(info, field) for field in (
        "st_dev", "st_ino", "st_uid", "st_gid", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns"))


def _no_symlink_chain(path: Path, root: Path):
    path.relative_to(root)
    for item in (root, *reversed(path.relative_to(root).parents), path):
        selected = item if item.is_absolute() else root / item
        if selected.is_symlink():
            raise GateError("symlink_not_allowed")


def biz_header(db_base: Path) -> bytes:
    path = db_base / BIZ_REL
    _no_symlink_chain(path, XROOT)
    before = _regular_private(path)
    if before.st_size < 4096 or before.st_size % 4096:
        raise GateError("unsupported_biz_database_size")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise GateError("biz_database_changed")
        prefix = os.pread(descriptor, 16, 0)
        after = path.lstat()
        if prefix != os.pread(descriptor, 16, 0) or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            raise GateError("biz_database_changed")
        return prefix
    finally:
        os.close(descriptor)


def existing_target_binding(db_base: Path, salt: bytes, root=PRIVATE_ROOT) -> str:
    """Read only non-secret configs; never open a key file or another DB."""
    if not root.exists():
        return "not_initialized"
    _no_symlink_chain(root, root)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise GateError("unsafe_private_permissions")
    paths = [root / "local-vault.json"]
    accounts = root / "accounts"
    if accounts.exists():
        _no_symlink_chain(accounts, root)
        entries = list(accounts.iterdir())
        if len(entries) > 64:
            raise GateError("binding_inventory_limit")
        paths.extend(p / "local-vault.json" for p in entries
                     if re.fullmatch(r"account-[0-9a-f]{12}", p.name))
    matched = []
    for path in paths:
        if not path.exists():
            continue
        _no_symlink_chain(path, root)
        before = _regular_private(path, private=True)
        if before.st_size > MAX_CONFIG_BYTES:
            raise GateError("binding_metadata_too_large")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if _metadata_identity(os.fstat(descriptor)) != _metadata_identity(before):
                raise GateError("binding_metadata_changed")
            payload = json.loads(os.read(descriptor, MAX_CONFIG_BYTES + 1))
            if (_metadata_identity(os.fstat(descriptor)) != _metadata_identity(before)
                    or _metadata_identity(path.lstat()) != _metadata_identity(before)):
                raise GateError("binding_metadata_changed")
        finally:
            os.close(descriptor)
        if isinstance(payload, dict) and payload.get("db_base_path") == str(db_base):
            matched.append(payload)
    if len(matched) > 1:
        raise GateError("target_binding_ambiguous")
    if not matched:
        return "not_initialized"
    payload = matched[0]
    if payload.get("targets", {}).get(BIZ_ALIAS) != BIZ_REL:
        return "target_not_initialized"
    if payload.get("salt_fingerprints", {}).get(BIZ_ALIAS) != hashlib.sha256(salt).hexdigest():
        return "target_binding_mismatch"
    return "configured_key_not_read_or_verified"


def preflight() -> dict:
    report = {"backend": "development-biz-only-preflight", "signed_companion": False,
              "network_requests": 0, "wechat_input_events": 0, "chat_database_reads": 0,
              "key_reads": 0, "persistent_plaintext_snapshots": 0, "coverage_measured": False,
              "biz_header_read": False, "current_account": "not_evaluated"}
    try:
        db_base = bind_current_account()
        report["current_account"] = "unique_two_samples"
        header = biz_header(db_base)
        report["biz_header_read"] = True
        report["database_format"] = "sqlite" if header == b"SQLite format 3\0" else "encrypted"
        report["existing_target_binding"] = ("not_needed" if report["database_format"] == "sqlite"
                                              else existing_target_binding(db_base, header))
        report["status"] = "preflight_only_coverage_pending"
        if report["existing_target_binding"] not in ("not_needed", "configured_key_not_read_or_verified"):
            report["status"] = "blocked_existing_biz_initialization_missing"
    except GateError as error:
        report.update(status="blocked", reason=str(error))
    except Exception:
        report.update(status="blocked", reason="local_preflight_unavailable")
    return report


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise GateError("explicit_biz_only_scope_required")
    parser = Parser(add_help=False)
    parser.add_argument("command", choices=["preflight"])
    parser.add_argument("--authorized-biz-only", action="store_true", required=True)
    try:
        parser.parse_args(argv)
        report = preflight()
    except GateError:
        report = {"status": "blocked", "reason": "explicit_biz_only_scope_required"}
    print(json.dumps(report, ensure_ascii=False))
    return 2 if report["status"].startswith("blocked") else 0


if __name__ == "__main__":
    raise SystemExit(main())
