"""Synthetic credentials only. Never probes a real account or private profile."""
import hashlib
import hmac
import json
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest

from wechat_crawler import biz_push_helper as gate
from wechat_crawler import biz_push_key_binding as binding
from wechat_crawler import biz_push_snapshot as snapshot
from wechat_crawler import biz_setup_store as native_store
from wechat_crawler.biz_push_hmac import first_page_matches

KEY = bytes(range(32))
OTHER = bytes(range(32, 64))


def page_for(key=KEY):
    page = bytearray((i * 17 + 3) % 256 for i in range(4096))
    auth_key = hashlib.pbkdf2_hmac("sha512", key, bytes(v ^ 0x3A for v in page[:16]), 2, 32)
    page[4032:] = hmac.digest(auth_key, bytes(page[16:4032]) + struct.pack("<I", 1), "sha512")
    return bytes(page)


def fixture(tmp_path, monkeypatch, *, first_key=OTHER, second_key=KEY):
    monkeypatch.setattr(native_store, "ROOT", tmp_path / "native-biz-private")
    data = tmp_path / "data"
    base = data / "account/db_storage"
    target = base / gate.BIZ_REL
    target.parent.mkdir(parents=True)
    target.write_bytes(page_for())
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    config, keys = private / "local-vault.json", private / "wechat-db-keys.json"
    config.write_text(json.dumps({"db_base_path": str(base), "keys_file": str(keys),
        "targets": {"message_0": "message/message_0.db", "contact": "contact/contact.db", "media_0": "message/media_0.db"},
        "salt_fingerprints": {"message_0": "1" * 64, "contact": "2" * 64, "media_0": "3" * 64}}))
    keys.write_text(json.dumps({"message_0": first_key.hex(), "contact": second_key.hex(), "media_0": KEY.hex()}))
    config.chmod(0o600)
    keys.chmod(0o600)
    calls = []
    def load(path, requested):
        calls.extend(requested)
        payload = json.loads(path.read_text())
        return {relative: bytes.fromhex(payload[Path(relative).stem.replace("contact", "contact")]) for relative in requested}
    support = SimpleNamespace(_read_private_json=lambda path, label: (path, json.loads(path.read_text())),
                              load_requested_keys=load)
    monkeypatch.setattr(gate, "XROOT", data)
    return base, private, support, config, keys, calls


@pytest.mark.parametrize("offset", [0, 19, 1000, 4016, 4032, 4095])
def test_fixed_hmac_profile_rejects_salt_ciphertext_iv_or_tag_tampering(offset):
    page = page_for()
    assert first_page_matches(page, KEY)
    changed = bytearray(page)
    changed[offset] ^= 1
    assert not first_page_matches(bytes(changed), KEY)
    assert not first_page_matches(page, OTHER)


def test_hmac_rejects_wrong_lengths_and_plaintext():
    assert not first_page_matches(page_for()[:-1], KEY)
    assert not first_page_matches(page_for(), KEY[:-1])
    assert not first_page_matches(b"SQLite format 3\0" + page_for()[16:], KEY)


def test_no_explicit_scope_does_not_probe_or_load_keys():
    def forbidden():
        pytest.fail("no account metadata or credential reads before authorization")
    assert binding.probe_existing(binder=forbidden)["reason"] == "explicit_existing_key_reuse_scope_required"


def test_match_writes_only_credential_free_sidecar_and_leaves_legacy_bytes(tmp_path, monkeypatch):
    base, root, support, config, keys, calls = fixture(tmp_path, monkeypatch)
    before = config.read_bytes(), keys.read_bytes()
    report = binding.probe_existing(authorized=True, bind_on_match=True, root=root, support=support, binder=lambda: base)
    assert report["status"] == "hmac_verified" and report["binding_written"]
    assert report["candidates_tested"] == 2
    assert calls == ["message/message_0.db", "contact/contact.db"]
    assert before == (config.read_bytes(), keys.read_bytes())
    path = root / binding.BINDING_NAME
    proof = json.loads(path.read_text())
    assert path.stat().st_mode & 0o777 == 0o600
    assert proof["key_alias"] == "contact" and proof["database_relative"] == gate.BIZ_REL
    assert KEY.hex() not in path.read_text() and "key" not in proof
    assert str(base) not in json.dumps(report) and KEY.hex() not in json.dumps(report)
    loaded = binding.load_bound_biz_keys(base, page_for()[:16], support, root=root)
    assert set(loaded) == {gate.BIZ_REL}


def test_no_match_does_not_try_unapproved_alias_or_write_any_binding(tmp_path, monkeypatch):
    base, root, support, config, keys, calls = fixture(tmp_path, monkeypatch, second_key=OTHER)
    before = config.read_bytes(), keys.read_bytes()
    report = binding.probe_existing(authorized=True, bind_on_match=True, root=root, support=support, binder=lambda: base)
    assert report["status"] == "no_hmac_match" and report["candidates_tested"] == 2
    assert not (root / binding.BINDING_NAME).exists()
    assert calls == ["message/message_0.db", "contact/contact.db"]
    assert before == (config.read_bytes(), keys.read_bytes())


def test_legacy_bound_config_without_salt_metadata_still_requires_actual_biz_hmac(tmp_path, monkeypatch):
    base, root, support, config, _, calls = fixture(tmp_path, monkeypatch)
    payload = json.loads(config.read_text())
    payload.pop("salt_fingerprints")
    config.write_text(json.dumps(payload))
    report = binding.probe_existing(authorized=True, bind_on_match=True, root=root, support=support, binder=lambda: base)
    assert report["candidates_declared"] == 2 and report["candidates_tested"] == 2
    assert report["status"] == "hmac_verified" and report["first_page_hmac_verified"]
    assert calls == ["message/message_0.db", "contact/contact.db"]


def test_zero_candidates_is_unmeasured_not_a_hmac_mismatch(tmp_path, monkeypatch):
    base, root, support, _, _, _ = fixture(tmp_path, monkeypatch)
    def failure(*args):
        raise RuntimeError("private value must not escape")
    support.load_requested_keys = failure
    report = binding.probe_existing(authorized=True, bind_on_match=True, root=root, support=support, binder=lambda: base)
    assert report["status"] == "not_verified" and report["key_reusable"] is None
    assert report["candidates_tested"] == 0 and report["candidate_load_failures"] == 2
    assert not (root / binding.BINDING_NAME).exists()


def test_first_match_stops_and_read_only_mode_does_not_write(tmp_path, monkeypatch):
    base, root, support, _, _, calls = fixture(tmp_path, monkeypatch, first_key=KEY)
    report = binding.probe_existing(authorized=True, root=root, support=support, binder=lambda: base)
    assert report["status"] == "hmac_verified" and not report["binding_written"]
    assert calls == ["message/message_0.db"] and not (root / binding.BINDING_NAME).exists()


def test_account_switch_after_match_prevents_binding(tmp_path, monkeypatch):
    base, root, support, _, _, _ = fixture(tmp_path, monkeypatch)
    values = iter([base, tmp_path / "other/db_storage"])
    report = binding.probe_existing(authorized=True, bind_on_match=True, root=root, support=support, binder=lambda: next(values))
    assert report["status"] == "blocked" and not (root / binding.BINDING_NAME).exists()


def test_expired_binding_does_not_try_another_alias_or_create_snapshot(tmp_path, monkeypatch):
    base, root, support, _, keys, calls = fixture(tmp_path, monkeypatch, first_key=KEY)
    assert binding.probe_existing(authorized=True, bind_on_match=True, root=root, support=support, binder=lambda: base)["status"] == "hmac_verified"
    payload = json.loads(keys.read_text())
    payload["message_0"] = OTHER.hex()
    keys.write_text(json.dumps(payload))
    calls.clear()
    original = snapshot.target_keys
    monkeypatch.setattr(snapshot, "target_keys", lambda b, s, p: original(b, s, p, private_root=root))
    monkeypatch.setattr(gate, "bind_current_account", lambda: base)
    monkeypatch.setattr(snapshot, "installed_primitives", lambda: support)
    monkeypatch.setattr(snapshot, "encrypted_image", lambda *a: pytest.fail("HMAC must gate snapshot"))
    with pytest.raises(gate.GateError, match="biz_setup_required"):
        with snapshot.current_biz_connection():
            pytest.fail("bad key must not yield a DB")
    assert calls == ["message/message_0.db"]


def test_salt_change_rejects_reference_before_key_read(tmp_path, monkeypatch):
    base, root, support, _, _, calls = fixture(tmp_path, monkeypatch)
    binding.probe_existing(authorized=True, bind_on_match=True, root=root, support=support, binder=lambda: base)
    calls.clear()
    with pytest.raises(gate.GateError, match="biz_salt_changed"):
        binding.load_bound_biz_keys(base, b"different-salt!!", support, root=root)
    assert not calls


def test_symlink_or_foreign_sidecar_is_not_overwritten(tmp_path, monkeypatch):
    base, root, support, _, _, _ = fixture(tmp_path, monkeypatch)
    foreign = tmp_path / "keep.json"
    foreign.write_text("keep")
    (root / binding.BINDING_NAME).symlink_to(foreign)
    report = binding.probe_existing(authorized=True, bind_on_match=True, root=root, support=support, binder=lambda: base)
    assert report["status"] == "blocked" and foreign.read_text() == "keep"


def test_probe_exceptions_are_redacted():
    def failure():
        raise RuntimeError("PRIVATE ACCOUNT KEY MUST NOT ESCAPE")
    report = binding.probe_existing(authorized=True, binder=failure)
    assert report["reason"] == "local_key_probe_unavailable"
    assert "PRIVATE" not in json.dumps(report)


def test_weekly_health_exposes_first_page_state_without_secret_values(tmp_path):
    from test_passive_weekly import Source, prepare, runner
    storage, _, settings = prepare(tmp_path)
    source, original = Source(), Source().read
    def read(accounts):
        result = original(accounts)
        result.update(key_available=True, key_health="verified_first_page_hmac", first_page_hmac_verified=True)
        return result
    source.read = read
    report = runner(tmp_path, storage, settings, sources=source).inspect("2026-10-26", source_check=True)
    assert report["key_available"] is True and report["first_page_hmac_verified"] is True


def test_native_biz_credentials_route_without_opening_or_changing_legacy_keys(tmp_path, monkeypatch):
    from wechat_crawler.biz_setup_targets import inventory
    base, root, support, config, keys, calls = fixture(tmp_path, monkeypatch)
    old = config.read_bytes(), keys.read_bytes()
    targets = inventory(base)
    native_store.publish(base, targets, {gate.BIZ_ALIAS: KEY}, {gate.BIZ_ALIAS: page_for()})
    loaded = binding.load_bound_biz_keys(base, page_for()[:16], support, root=root)
    assert loaded == {gate.BIZ_REL: KEY} and not calls
    assert old == (config.read_bytes(), keys.read_bytes())
    with pytest.raises(gate.GateError, match="biz_salt_changed"):
        binding.load_bound_biz_keys(base, b"different-salt!!", support, root=root)
    assert not calls


def test_invalid_native_file_never_falls_back_to_other_aliases(tmp_path, monkeypatch):
    from wechat_crawler.biz_setup_targets import inventory
    base, root, support, _, _, calls = fixture(tmp_path, monkeypatch)
    binding.probe_existing(authorized=True, bind_on_match=True, root=root, support=support, binder=lambda: base)
    targets = inventory(base)
    native_store.publish(base, targets, {gate.BIZ_ALIAS: KEY}, {gate.BIZ_ALIAS: page_for()})
    selected = next(native_store.ROOT.rglob("biz0-*.json"))
    selected.write_text("{}")
    calls.clear()
    with pytest.raises(gate.GateError, match="biz_credential_invalid"):
        binding.load_bound_biz_keys(base, page_for()[:16], support, root=root)
    assert not calls
