"""Explicit two-alias HMAC probe; isolated credential-free biz binding sidecar.

Never writes legacy local-vault/keys, captures keys, opens another DB, or runs
setup. Secrets stay in this helper process. Ordinary weekly reads never probe
other aliases: they may follow only an already persisted exact reference.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile

from . import biz_push_helper as gate
from .biz_push_hmac import PROFILE, first_page_matches, read_first_page

BINDING_NAME = "biz-push-binding.json"
SOURCE_ALIASES = {"message_0": "message/message_0.db", "contact": "contact/contact.db"}


def config_paths(root):
    if not root.exists():
        return []
    gate._no_symlink_chain(root, root)
    info = root.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise gate.GateError("unsafe_private_permissions")
    paths = [root / "local-vault.json"]
    accounts = root / "accounts"
    if accounts.exists():
        gate._no_symlink_chain(accounts, root)
        entries = list(accounts.iterdir())
        if len(entries) > 64:
            raise gate.GateError("binding_inventory_limit")
        paths += [p / "local-vault.json" for p in entries if gate.re.fullmatch(r"account-[0-9a-f]{12}", p.name)]
    return paths


def private_json(path, root, support):
    gate._no_symlink_chain(path, root)
    if gate._regular_private(path, private=True).st_size > gate.MAX_CONFIG_BYTES:
        raise gate.GateError("binding_metadata_too_large")
    return support._read_private_json(path, "biz binding")[1]


def account_config(db_base, root, support, paths=None):
    matches = []
    for path in config_paths(root) if paths is None else paths:
        if path.exists():
            payload = private_json(path, root, support)
            if payload.get("db_base_path") == str(db_base):
                matches.append((path, payload))
    if len(matches) != 1:
        raise gate.GateError("existing_account_binding_not_unique")
    path, payload = matches[0]
    info = path.parent.stat()
    if info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise gate.GateError("unsafe_private_permissions")
    key_file = path.parent / "wechat-db-keys.json"
    if payload.get("keys_file") != str(key_file):
        raise gate.GateError("existing_key_reference_invalid")
    gate._no_symlink_chain(key_file, root)
    gate._regular_private(key_file, private=True)
    return path, payload, key_file


def bound_aliases(config):
    # Older initializer profiles legitimately lack source salt fingerprints.
    # Exact current-account config + declared alias establishes permitted key
    # provenance; biz0's actual HMAC, not unrelated DB metadata, proves reuse.
    return [alias for alias, relative in SOURCE_ALIASES.items()
            if config.get("targets", {}).get(alias) == relative]


def load_bound_biz_keys(db_base, salt, support, *, root=None, native_root=None):
    # Independent native biz setup owns its own credentials; old chat key/config
    # bytes are untouched. A changed salt stops before reading any old key.
    from .biz_setup_store import load_current
    native = load_current(db_base, salt, root=native_root)
    if native is not None:
        return native
    root = gate.PRIVATE_ROOT if root is None else root
    paths = config_paths(root)
    if not any((p.parent / BINDING_NAME).exists() for p in paths):
        return None
    path, config, key_file = account_config(db_base, root, support, paths)
    sidecar = path.parent / BINDING_NAME
    if not sidecar.exists():
        return None
    proof = private_json(sidecar, root, support)
    alias = proof.get("key_alias")
    if (proof.get("schema_version") != 1 or proof.get("db_base_path") != str(db_base)
            or proof.get("database_relative") != gate.BIZ_REL or proof.get("hmac_profile") != PROFILE
            or proof.get("keys_file") != str(key_file) or alias not in bound_aliases(config)):
        raise gate.GateError("biz_setup_required")
    if proof.get("salt_fingerprint") != hashlib.sha256(salt).hexdigest():
        raise gate.GateError("biz_salt_changed")
    try:
        values = support.load_requested_keys(key_file, [SOURCE_ALIASES[alias]])
        return {gate.BIZ_REL: values[SOURCE_ALIASES[alias]]}
    except Exception:
        raise gate.GateError("biz_setup_required") from None


def publish_binding(path, payload, root, support):
    gate._no_symlink_chain(path, root)
    if path.exists():
        old = private_json(path, root, support)
        if all(old.get(k) == v for k, v in payload.items() if k != "created_at"):
            return False
        raise gate.GateError("existing_biz_binding_requires_review")
    descriptor, temporary = tempfile.mkstemp(prefix=".biz-binding-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(payload, sort_keys=True).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path, follow_symlinks=False)  # atomic create, never overwrite
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return True
    finally:
        Path(temporary).unlink(missing_ok=True)


def probe_existing(*, authorized=False, bind_on_match=False, root=None, support=None, binder=None):
    report = {"backend": "development-biz-key-probe", "signed_companion": False,
              "candidates_tested": 0, "hmac_profile": PROFILE, "binding_written": False,
              "candidate_load_failures": 0,
              "chat_database_reads": 0, "article_rows_read": 0, "new_key_capture": False,
              "legacy_config_or_key_written": False, "wechat_input_events": 0}
    if not authorized:
        return dict(report, status="blocked", reason="explicit_existing_key_reuse_scope_required")
    try:
        root = gate.PRIVATE_ROOT if root is None else root
        binder = gate.bind_current_account if binder is None else binder
        db_base = binder()
        page = read_first_page(db_base)
        if page[:16] == b"SQLite format 3\0":
            raise gate.GateError("plaintext_biz_does_not_need_key_reuse")
        if support is None:
            from .biz_push_snapshot import installed_primitives
            support = installed_primitives()
        config_path, config, key_file = account_config(db_base, root, support)
        before = [gate._metadata_identity(p.lstat()) for p in (config_path, key_file)]
        matched = None
        aliases = bound_aliases(config)
        report["candidates_declared"] = len(aliases)
        for alias in aliases:
            try:
                values = support.load_requested_keys(key_file, [SOURCE_ALIASES[alias]])
            except Exception:
                report["candidate_load_failures"] += 1
                continue
            report["candidates_tested"] += 1
            if first_page_matches(page, values[SOURCE_ALIASES[alias]]):
                matched = alias
                break
        if before != [gate._metadata_identity(p.lstat()) for p in (config_path, key_file)]:
            raise gate.GateError("existing_binding_changed_during_probe")
        if not matched:
            if not report["candidates_tested"]:
                return dict(report, status="not_verified", key_reusable=None,
                            reason="existing_candidate_load_blocked" if aliases else "no_declared_allowed_alias")
            return dict(report, status="no_hmac_match", key_reusable=False,
                        reason="no_match_for_fixed_profile_not_proof_of_missing_keys")
        if binder() != db_base or read_first_page(db_base) != page:
            raise gate.GateError("current_source_changed_during_probe")
        if bind_on_match:
            payload = {"schema_version": 1, "db_base_path": str(db_base),
                "database_relative": gate.BIZ_REL, "salt_fingerprint": hashlib.sha256(page[:16]).hexdigest(),
                "keys_file": str(key_file), "key_alias": matched, "hmac_profile": PROFILE,
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
            report["binding_written"] = publish_binding(config_path.parent / BINDING_NAME, payload, root, support)
        return dict(report, status="hmac_verified", key_reusable=True, first_page_hmac_verified=True)
    except gate.GateError as exc:
        return dict(report, status="blocked", reason=str(exc))
    except Exception:
        return dict(report, status="blocked", reason="local_key_probe_unavailable")


def main(argv=None):
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise gate.GateError("invalid_key_probe_request")
    parser = Parser(add_help=False)
    parser.add_argument("command", choices=["probe"])
    parser.add_argument("--authorized-existing-key-reuse", action="store_true", required=True)
    parser.add_argument("--bind-on-match", action="store_true")
    try:
        args = parser.parse_args(argv)
        result = probe_existing(authorized=args.authorized_existing_key_reuse, bind_on_match=args.bind_on_match)
    except Exception:
        result = {"status": "blocked", "reason": "invalid_key_probe_request"}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "hmac_verified" else 2


if __name__ == "__main__":
    raise SystemExit(main())
