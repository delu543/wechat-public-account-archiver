"""Exact biz-only inventory/consent descriptors, never a generic chat scan."""
from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path

from . import biz_push_helper as gate


@dataclass(frozen=True)
class BizTarget:
    name: str
    relative_path: str
    path: Path = field(repr=False)
    size: int
    salt: bytes = field(repr=False)
    mtime_ns: int = field(default=0, repr=False)


def relative_path(alias):
    if not isinstance(alias, str) or not gate.re.fullmatch(r"biz_message_(0|[1-9][0-9]*)", alias):
        raise gate.GateError("invalid_biz_target")
    return "message/" + alias + ".db"


def inventory(db_base):
    # Never choose a historical account by mtime/name. Caller must first bind
    # one official current session; this validator only restricts its path.
    try:
        parts = Path(db_base).relative_to(gate.XROOT).parts
    except ValueError:
        raise gate.GateError("invalid_current_account_path") from None
    if len(parts) != 2 or parts[-1] != "db_storage" or parts[0] in (".", ".."):
        raise gate.GateError("invalid_current_account_path")
    folder = Path(db_base) / "message"
    gate._no_symlink_chain(folder, gate.XROOT)
    aliases = []
    for index, item in enumerate(folder.iterdir()):
        if index >= 512:
            raise gate.GateError("biz_inventory_limit")
        if gate.re.fullmatch(r"biz_message_(0|[1-9][0-9]*)\.db", item.name):
            aliases.append(item.stem)
    if gate.BIZ_ALIAS not in aliases or len(aliases) > 32:
        raise gate.GateError("biz_inventory_unavailable")
    targets = {}
    for alias in sorted(aliases):
        path = Path(db_base) / relative_path(alias)
        gate._no_symlink_chain(path, gate.XROOT)
        before = gate._regular_private(path)
        if not 4096 <= before.st_size <= 256 * 1024 * 1024 or before.st_size % 4096:
            raise gate.GateError("unsupported_biz_database_size")
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            if gate._metadata_identity(os.fstat(descriptor)) != gate._metadata_identity(before):
                raise gate.GateError("biz_database_changed")
            salt = os.pread(descriptor, 16, 0)
            after = path.lstat()
            if (salt != os.pread(descriptor, 16, 0)
                    or (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size)):
                raise gate.GateError("biz_database_changed")
        finally:
            os.close(descriptor)
        if len(salt) != 16 or salt == b"SQLite format 3\0":
            raise gate.GateError("encrypted_biz_target_required")
        targets[alias] = BizTarget(alias, relative_path(alias), path, before.st_size, salt, before.st_mtime_ns)
    if len({target.salt for target in targets.values()}) != len(targets):
        raise gate.GateError("biz_salt_not_unique")
    return targets


def scope_digest(db_base, targets, app, cdhash):
    """Match after normal quit: bind file identity and salt, not times or size.

    The required normal quit checkpoints the WAL and can grow the same file in
    place, so size and times are not consent scope. A changed salt, file
    identity, target union or official app invalidates consent.
    """
    if not gate.re.fullmatch(r"[0-9a-f]{40,64}", cdhash or ""):
        raise gate.GateError("official_signature_unverified")
    descriptors = []
    for name, target in sorted(targets.items()):
        info = target.path.lstat()
        if target.relative_path != relative_path(name) or info.st_size != target.size:
            raise gate.GateError("biz_scope_changed")
        descriptors.append((name, info.st_dev, info.st_ino,
                            hashlib.sha256(target.salt).hexdigest()))
    executable = app.executable_path.lstat()
    payload = [str(db_base), descriptors, str(app.app_path), app.bundle_id, app.version, cdhash,
               executable.st_dev, executable.st_ino, executable.st_size]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def pages_for(targets):
    pages = {}
    for name, target in targets.items():
        gate._no_symlink_chain(target.path, gate.XROOT)
        before = gate._regular_private(target.path)
        descriptor = os.open(target.path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            page = os.pread(descriptor, 4096, 0)
            after = target.path.lstat()
            if (len(page) != 4096 or page[:16] != target.salt
                    or (before.st_dev, before.st_ino) != (os.fstat(descriptor).st_dev, os.fstat(descriptor).st_ino)
                    or (before.st_dev, before.st_ino, before.st_size) != (after.st_dev, after.st_ino, after.st_size)
                    or page != os.pread(descriptor, 4096, 0)):
                raise gate.GateError("biz_database_changed")
            pages[name] = page
        finally:
            os.close(descriptor)
    return pages
