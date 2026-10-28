"""Helper-private biz credentials. No legacy key/profile modification or fallback.

One atomic file holds the whole approved set. A new biz0 salt gets a new file;
old files are retained, never searched for a working key. Public reports must
not serialize these payloads, paths, salt fingerprints or key values.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from . import biz_push_helper as gate
from .biz_push_hmac import PROFILE, first_page_matches

ROOT = Path.home() / "Library/Application Support/WeChatPublicAccountArchive/biz-helper"


def directory(path, *, create=False):
    path = Path(path)
    if not path.is_absolute():
        raise gate.GateError("unsafe_biz_private_root")
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise gate.GateError("symlink_not_allowed")
    if not path.exists():
        if not create:
            return None
        path.mkdir(mode=0o700, parents=True, exist_ok=False)
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise gate.GateError("unsafe_private_permissions")
    return path


def account_dir(db_base, root=None, *, create=False):
    root = ROOT if root is None else Path(root)
    if directory(root, create=create) is None:
        return None
    name = hashlib.sha256(str(db_base).encode()).hexdigest()
    return directory(root / name, create=create)


def credential_path(folder, salt):
    if len(salt) != 16 or salt == b"SQLite format 3\0":
        raise gate.GateError("invalid_biz_salt")
    return folder / ("biz0-" + hashlib.sha256(salt).hexdigest() + ".json")


def read_private(path):
    before = gate._regular_private(path, private=True)
    if before.st_size > gate.MAX_CONFIG_BYTES:
        raise gate.GateError("biz_credential_too_large")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        if gate._metadata_identity(os.fstat(descriptor)) != gate._metadata_identity(before):
            raise gate.GateError("biz_credential_changed")
        raw = os.read(descriptor, gate.MAX_CONFIG_BYTES + 1)
        if gate._metadata_identity(path.lstat()) != gate._metadata_identity(before):
            raise gate.GateError("biz_credential_changed")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError()
        return payload
    except (ValueError, UnicodeError):
        raise gate.GateError("biz_credential_invalid") from None
    finally:
        os.close(descriptor)


def load_current(db_base, salt, *, root=None):
    folder = account_dir(db_base, root)
    if folder is None:
        return None
    selected = credential_path(folder, salt)
    if not selected.exists():
        # Names only: do not read any previous credential after a salt change.
        entries = list(folder.iterdir())
        if len(entries) > 64:
            raise gate.GateError("biz_credential_inventory_limit")
        if any(gate.re.fullmatch(r"biz0-[0-9a-f]{64}\.json", p.name) for p in entries):
            raise gate.GateError("biz_salt_changed")
        return None
    if selected.is_symlink():
        raise gate.GateError("symlink_not_allowed")
    payload = read_private(selected)
    if (payload.get("schema_version") != 1 or payload.get("db_base_path") != str(db_base)
            or payload.get("hmac_profile") != PROFILE
            or payload.get("salt_fingerprints", {}).get(gate.BIZ_ALIAS) != hashlib.sha256(salt).hexdigest()
            or payload.get("targets", {}).get(gate.BIZ_ALIAS) != gate.BIZ_REL):
        raise gate.GateError("biz_credential_invalid")
    try:
        key = bytes.fromhex(payload["keys"][gate.BIZ_ALIAS])
        if len(key) != 32:
            raise ValueError()
        return {gate.BIZ_REL: key}
    except (KeyError, TypeError, ValueError):
        raise gate.GateError("biz_credential_invalid") from None


def publish(db_base, targets, matched_keys, pages, *, root=None):
    """Validate all exact targets before any write; atomic create, not replace."""
    if set(targets) != set(matched_keys) or set(targets) != set(pages) or gate.BIZ_ALIAS not in targets:
        raise gate.GateError("incomplete_biz_key_set")
    from .biz_setup_targets import relative_path
    encoded = {}
    for name, target in targets.items():
        if target.relative_path != relative_path(name):
            raise gate.GateError("invalid_biz_target")
        try:
            value = matched_keys[name]
            key = value if isinstance(value, bytes) else bytes.fromhex(value)
            if pages[name][:16] != target.salt or not first_page_matches(pages[name], key):
                raise ValueError()
            encoded[name] = key.hex()
        except (ValueError, TypeError):
            raise gate.GateError("biz_capture_hmac_unverified") from None
    payload = {"schema_version": 1, "db_base_path": str(db_base), "hmac_profile": PROFILE,
        "targets": {n: t.relative_path for n, t in targets.items()}, "keys": encoded,
        "salt_fingerprints": {n: hashlib.sha256(t.salt).hexdigest() for n, t in targets.items()}}
    folder = account_dir(db_base, root, create=True)
    path = credential_path(folder, targets[gate.BIZ_ALIAS].salt)
    if path.exists() or path.is_symlink():
        raise gate.GateError("existing_biz_credential_requires_review")
    descriptor, temporary = tempfile.mkstemp(prefix=".biz-publish-", dir=folder)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(payload, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return True
