"""Helper-only, exact-biz DB snapshot. Never call the ordinary chat doctor.

Reuse the installed, hash-pinned WAL/crypto primitives, not its broad snapshot
entry point (which reads contact.db). Plaintext exists only in child memory.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile

from . import biz_push_helper as gate
from .memory_sqlite import MemorySQLite
from .biz_push_hmac import first_page_matches, read_first_page
from .biz_push_key_binding import load_bound_biz_keys

SUPPORT_ROOT = Path.home() / "Library/Application Support/WeChatLocalExport/app/current"
SUPPORT_HASHES = {
    "wechat_safe_snapshot.py": "a4c7c5fbd07df26ba83c72c3f6a714a6ae9155f343889f4e6864945e7f684d5c",
    "platform_support.py": "b6e18461be6e1fb73d0fb2a0795129c84b1d7a2ce86d60c6a52c7fd484d99952",
}
TASK_ROOT = Path.home() / "Library/Application Support/WeChatLocalExport/tasks"
MAX_DATABASE_BYTES = 256 * 1024 * 1024


def installed_primitives():
    root = SUPPORT_ROOT.resolve(strict=True)
    for name, expected in SUPPORT_HASHES.items():
        path = root / "live_tools" / name
        gate._no_symlink_chain(path, root)
        info = gate._regular_private(path)
        if info.st_mode & 0o022 or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise gate.GateError("reviewed_helper_dependency_changed")
    sys.path.insert(0, str(root))
    module = importlib.import_module("live_tools.wechat_safe_snapshot")
    if Path(module.__file__).resolve() != root / "live_tools/wechat_safe_snapshot.py":
        raise gate.GateError("helper_dependency_origin_mismatch")
    return module


def target_keys(db_base: Path, salt: bytes, support, private_root=gate.PRIVATE_ROOT):
    scoped = load_bound_biz_keys(db_base, salt, support, root=private_root)
    if scoped is not None:
        return scoped
    # Gate BEFORE opening any credential file. No key search, alias fallback,
    # initialization, process attach, memory scan or exported key value.
    state = gate.existing_target_binding(db_base, salt, root=private_root)
    if state != "configured_key_not_read_or_verified":
        raise gate.GateError("biz_setup_required")
    configs = [private_root / "local-vault.json"]
    accounts = private_root / "accounts"
    if accounts.exists():
        configs += [p / "local-vault.json" for p in accounts.iterdir()
                    if gate.re.fullmatch(r"account-[0-9a-f]{12}", p.name)]
    matched = []
    for path in configs:
        if not path.exists():
            continue
        gate._no_symlink_chain(path, private_root)
        _, payload = support._read_private_json(path, "biz binding")
        if payload.get("db_base_path") != str(db_base):
            continue
        if (payload.get("targets", {}).get(gate.BIZ_ALIAS) != gate.BIZ_REL
                or payload.get("salt_fingerprints", {}).get(gate.BIZ_ALIAS)
                != hashlib.sha256(salt).hexdigest()):
            raise gate.GateError("biz_setup_required")
        key_path = path.parent / "wechat-db-keys.json"
        if payload.get("keys_file") != str(key_path):
            raise gate.GateError("biz_binding_invalid")
        gate._no_symlink_chain(key_path, private_root)
        matched.append(key_path)
    if len(matched) != 1:
        raise gate.GateError("biz_binding_invalid")
    try:
        return support.load_requested_keys(matched[0], [gate.BIZ_REL])
    except Exception:
        raise gate.GateError("biz_setup_required") from None


def encrypted_image(db_base: Path, destination: Path, support):
    """Coordinate only biz0's DB/WAL/SHM; never invoke broad source resolvers."""
    source = db_base / gate.BIZ_REL
    for suffix in ("", "-wal", "-shm"):
        gate._no_symlink_chain(Path(str(source) + suffix), gate.XROOT)
    before = support._assert_regular(source, "biz source")
    if before.size > MAX_DATABASE_BYTES:
        raise gate.GateError("biz_database_size_limit")
    locks = support.acquire_online_wal_locks({gate.BIZ_REL: (source, before)})
    try:
        lease = locks.by_relative()[gate.BIZ_REL]
        fingerprint = support._assert_regular(source, "biz source")
        anchor, wal_before = support._read_locked_online_wal_anchor(lease, source)
        if wal_before.size > MAX_DATABASE_BYTES:
            raise gate.GateError("biz_wal_size_limit")
        copy = destination / "encrypted.db"
        wal = destination / "encrypted.db-wal"
        support.apfs_clone_file_atomic(source, copy, fingerprint)
        support.apfs_clone_file_atomic(Path(str(source) + "-wal"), wal, wal_before)
        final_anchor, final_wal = support._read_locked_online_wal_anchor(lease, source)
        if (anchor != final_anchor or wal_before != final_wal
                or support._assert_regular(source, "biz source") != fingerprint):
            raise gate.GateError("biz_snapshot_changed")
    finally:
        locks.release()
    image = destination / "committed.db"
    support.materialize_copied_database_with_wal(
        copy, wal, image, online_anchor=anchor,
        base_cloner=support.apfs_clone_file_atomic,
    )
    if image.stat().st_size > MAX_DATABASE_BYTES:
        raise gate.GateError("biz_database_size_limit")
    return image, max(fingerprint.mtime_ns, wal_before.mtime_ns) / 1e9


def memory_connection(image: Path, support, keys=None):
    """Only a private encrypted image is read; no plaintext file is created."""
    size = image.stat().st_size
    if size > MAX_DATABASE_BYTES or size % 4096:
        raise gate.GateError("biz_database_size_limit")
    plain = bytearray()
    with image.open("rb") as stream:
        for number in range(1, size // 4096 + 1):
            page = stream.read(4096)
            if keys is not None:
                page = support._decrypt_page(page, number, keys[gate.BIZ_REL])
                if number == 1:
                    support._validate_first_plain_page(page)
            plain.extend(page)
    if plain[:16] != b"SQLite format 3\0":
        raise gate.GateError("biz_setup_required")
    # A committed standalone image has no live WAL. SQLite deserialize rejects
    # a WAL-mode header; change only our in-memory copy, never the source.
    plain[18:20] = b"\1\1"
    connection = None
    try:
        if hasattr(sqlite3.Connection, "deserialize"):
            connection = sqlite3.connect(":memory:")
            connection.deserialize(plain)
        else:
            connection = MemorySQLite(plain)
        plain[:] = b"\0" * len(plain)
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=ON")
        if list(connection.execute("PRAGMA quick_check")) != [("ok",)]:
            raise gate.GateError("biz_integrity_failed")
        return connection
    except Exception:
        plain[:] = b"\0" * len(plain)
        if connection is not None:
            connection.close()
        raise


@contextmanager
def current_biz_connection():
    db_base = gate.bind_current_account()  # OS metadata only, no process attach.
    header = gate.biz_header(db_base)
    encrypted = header != b"SQLite format 3\0"
    support = installed_primitives()
    keys = target_keys(db_base, header, support) if encrypted else None
    if encrypted:
        first_page = read_first_page(db_base)
        if first_page[:16] != header:
            raise gate.GateError("biz_first_page_changed")
        if not first_page_matches(first_page, keys[gate.BIZ_REL]):
            raise gate.GateError("biz_setup_required")
    gate._no_symlink_chain(TASK_ROOT, Path.home())
    TASK_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = TASK_ROOT.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise gate.GateError("unsafe_private_permissions")
    with tempfile.TemporaryDirectory(prefix="biz-only-", dir=TASK_ROOT) as temporary:
        image, modified = encrypted_image(db_base, Path(temporary), support)
        if gate.bind_current_account() != db_base or gate.biz_header(db_base) != header:
            raise gate.GateError("current_account_unstable")
        conn = memory_connection(image, support, keys)
        try:
            yield conn, {"source_modified_epoch": modified, "page_hmac_verified": False,
                         "key_available": True if encrypted else None,
                         "key_health": "verified_first_page_hmac" if encrypted else "plaintext_no_key_needed",
                         "first_page_hmac_verified": bool(encrypted),
                         "extra_biz_shards_present": any(
                             p.name != "biz_message_0.db" for p in (db_base / "message").glob("biz_message_*.db"))}
        finally:
            conn.close()
