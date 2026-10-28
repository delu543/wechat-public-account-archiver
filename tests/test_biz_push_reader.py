import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from wechat_crawler import biz_push_reader as reader
from wechat_crawler import biz_push_snapshot as snapshot
from wechat_crawler.biz_push_helper import GateError

ACCOUNT = {"biz": "target-biz", "wechat_id": "gh_target"}
SN = "0123456789abcdef0123456789abcdef"


def link(biz="target-biz", mid="123", idx="1"):
    return f"https://mp.weixin.qq.com/s?__biz={biz}&mid={mid}&idx={idx}&sn={SN}"


def database():
    db = sqlite3.connect(":memory:")
    table = "Msg_" + hashlib.md5(ACCOUNT["wechat_id"].encode()).hexdigest()
    db.execute(f'CREATE TABLE "{table}"(create_time INTEGER,message_content BLOB)')
    return db, table


def test_only_exact_target_tables_and_biz_are_read():
    db, table = database()
    db.execute("CREATE TABLE Msg_other(create_time INTEGER,message_content TEXT)")
    db.execute("INSERT INTO Msg_other VALUES(1,'PRIVATE MUST NOT BE SELECTED')")
    payload = {"items": [{"url": link(), "title": "目标", "publish_time": "2026-10-09"},
                         {"url": link("other-biz"), "title": "非目标"}]}
    db.execute(f'INSERT INTO "{table}" VALUES(?,?)', (10, json.dumps(payload)))
    statements = []
    db.set_trace_callback(statements.append)
    report = reader.read_records(db, [ACCOUNT])
    assert [r["title"] for r in report["records"]] == ["目标"]
    assert not any('FROM Msg_other' in sql or 'FROM "Msg_other"' in sql for sql in statements)
    assert report["accounts"][0]["currently_followed"] == "unknown"
    assert not report["accounts"][0]["all_publications_verified"]


def test_multicard_xml_permanent_links_and_receipt_time_not_publication():
    db, table = database()
    xml = '<msg><appmsg><mmreader><category>' + ''.join(
        f'<item><title>文章{idx}</title><url><![CDATA[{link(idx=str(idx))}&key=SECRET]]></url></item>'
        for idx in (1, 2)) + '</category></mmreader></appmsg></msg>'
    db.execute(f'INSERT INTO "{table}" VALUES(?,?)', (1790000000, xml))
    report = reader.read_records(db, [ACCOUNT])
    assert len(report["records"]) == 2
    assert all(r["publish_time"] is None for r in report["records"])
    assert "SECRET" not in json.dumps(report)
    assert all(r["received_time"] == "1790000000" for r in report["records"])


def test_zstd_payload_is_bounded():
    if importlib.util.find_spec("zstandard") is None:
        python = Path.home() / "Library/Application Support/WeChatLocalExport/tools/python/bin/python3"
        if not python.is_file():
            pytest.skip("installed helper compression runtime unavailable")
        # Only a synthetic in-memory DB; this does not bind an account or read
        # any WeChat/profile/key file. Test the actual installed dependency.
        code = '''import hashlib,json,sqlite3,zstandard
from wechat_crawler.biz_push_reader import read_records,MAX_PAYLOAD
a={"biz":"target-biz","wechat_id":"gh_target"}
t="Msg_"+hashlib.md5(a["wechat_id"].encode()).hexdigest()
c=sqlite3.connect(":memory:");c.execute('CREATE TABLE "'+t+'"(create_time INTEGER,message_content BLOB)')
u="https://mp.weixin.qq.com/s?__biz=target-biz&mid=123&idx=1&sn=0123456789abcdef0123456789abcdef"
p=zstandard.ZstdCompressor().compress(json.dumps({"url":u,"title":"fixture"}).encode())
c.execute('INSERT INTO "'+t+'" VALUES(1,?)',(p,))
oversize=zstandard.ZstdCompressor().compress(b"x"*(MAX_PAYLOAD+1))
c.execute('INSERT INTO "'+t+'" VALUES(2,?)',(oversize,))
r=read_records(c,[a]);print(json.dumps({"records":len(r["records"]),"undecoded":r["accounts"][0]["undecoded_rows"]}))'''
        result = subprocess.run([str(python), "-c", code], cwd=Path(__file__).resolve().parents[1],
                                capture_output=True, text=True, timeout=10, check=True)
        assert json.loads(result.stdout) == {"records": 1, "undecoded": 1}
        return
    import zstandard as zstd
    db, table = database()
    payload = zstd.ZstdCompressor().compress(json.dumps({"url": link(), "title": "压缩"}).encode())
    db.execute(f'INSERT INTO "{table}" VALUES(1,?)', (payload,))
    assert reader.read_records(db, [ACCOUNT])["records"][0]["title"] == "压缩"


@pytest.mark.parametrize("url", [link()+"&mid=999", link().replace("https://", "file://"),
                                link().replace("mp.weixin.qq.com", "evil.example"),
                                link("wrong"), link().replace("idx=1", "idx=0")])
def test_untrusted_or_ambiguous_identity_rejected(url):
    assert reader.public_identity(url, ACCOUNT["biz"]) is None


def test_unknown_schema_bad_xml_and_read_bound_are_explicit(monkeypatch):
    db, table = database()
    for _ in range(3):
        db.execute(f'INSERT INTO "{table}" VALUES(1,?)', ('<!DOCTYPE x><msg/>',))
    monkeypatch.setattr(reader, "MAX_ROWS", 2)
    note = reader.read_records(db, [ACCOUNT])["accounts"][0]
    assert note["undecoded_rows"] == 2 and note["limit_reached"]
    assert not note["all_publications_verified"]


def test_missing_binding_cannot_open_keys(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        pytest.fail("no credential read before exact binding")
    monkeypatch.setattr(snapshot.gate, "existing_target_binding", lambda *a, **k: "target_not_initialized")
    with pytest.raises(GateError, match="biz_setup_required"):
        snapshot.target_keys(tmp_path, b"a" * 16, SimpleNamespace(load_requested_keys=forbidden), private_root=tmp_path / "no-private-state")


def test_plaintext_is_deserialized_to_query_only_memory(tmp_path):
    path = tmp_path / "synthetic.db"
    db = sqlite3.connect(path)
    db.execute("PRAGMA page_size=4096")
    db.execute("CREATE TABLE sample(v TEXT)")
    db.execute("INSERT INTO sample VALUES('fixture')")
    db.commit()
    db.close()
    conn = snapshot.memory_connection(path, None)
    assert conn.execute("SELECT * FROM sample").fetchone() == ("fixture",)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO sample VALUES('no')")
    conn.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["synthetic.db"]


def test_child_errors_do_not_expose_paths_or_payload(monkeypatch):
    def failure():
        raise RuntimeError("PRIVATE PATH AND KEY")
    monkeypatch.setattr(reader, "current_biz_connection", failure)
    report = reader.read_current([ACCOUNT])
    assert report == {"status": "blocked", "reason": "biz_read_unavailable", "coverage_measured": False}


def test_online_snapshot_targets_only_biz_db_wal_shm(monkeypatch, tmp_path):
    base = tmp_path / "account/db_storage"
    source = base / "message/biz_message_0.db"
    calls = []
    fp = SimpleNamespace(size=4096, mtime_ns=1000000000)
    lock = SimpleNamespace(by_relative=lambda: {snapshot.gate.BIZ_REL: object()}, release=lambda: calls.append("release"))
    support = SimpleNamespace(
        _assert_regular=lambda path, label: (calls.append(str(path)) or fp),
        acquire_online_wal_locks=lambda sources: (calls.extend(str(v[0]) for v in sources.values()) or lock),
        _read_locked_online_wal_anchor=lambda lease, path: ("anchor", fp),
        apfs_clone_file_atomic=lambda src, dest, expected: calls.append(str(src)),
        materialize_copied_database_with_wal=lambda src, wal, dest, **kw: dest.write_bytes(b"x" * 4096),
    )
    monkeypatch.setattr(snapshot.gate, "XROOT", tmp_path)
    image, modified = snapshot.encrypted_image(base, tmp_path, support)
    assert modified == 1 and image.stat().st_size == 4096
    assert "release" in calls
    assert all(p == "release" or p.startswith(str(source)) for p in calls)


def test_invalid_scope_request_does_not_bind_current_account(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["biz-push", "--authorized-biz-only"])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b'{"accounts":[{"biz":"x","wechat_id":"private-chat"}]}')))
    monkeypatch.setattr(reader, "read_current", lambda a: pytest.fail("invalid scope must not probe"))
    assert reader.main() == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "invalid_biz_only_request"


def test_native_memory_sqlite_rejects_attach_and_pragma_off(tmp_path):
    from wechat_crawler.memory_sqlite import MemorySQLite
    path = tmp_path / "fixture.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE fixture(v)")
    conn.commit()
    conn.close()
    memory = MemorySQLite(path.read_bytes())
    for query in ("ATTACH 'other.db' AS other", "PRAGMA query_only=OFF", "SELECT 1;SELECT 2"):
        with pytest.raises(sqlite3.OperationalError):
            memory.execute(query)
    assert memory.execute("SELECT 1").fetchone() == (1,)
    memory.close()
    assert memory.db is None and memory.buffer is None
    assert not (tmp_path / "other.db").exists()


def test_exact_initialized_binding_loads_only_biz_alias(monkeypatch, tmp_path):
    root = tmp_path / "private"
    root.mkdir(mode=0o700)
    config = root / "local-vault.json"
    config.write_text("{}")
    base = tmp_path / "current/db_storage"
    salt = b"a" * 16
    payload = {"db_base_path": str(base), "targets": {snapshot.gate.BIZ_ALIAS: snapshot.gate.BIZ_REL},
               "salt_fingerprints": {snapshot.gate.BIZ_ALIAS: hashlib.sha256(salt).hexdigest()},
               "keys_file": str(root / "wechat-db-keys.json")}
    calls = []
    support = SimpleNamespace(_read_private_json=lambda p, label: (p, payload),
        load_requested_keys=lambda p, requested: (calls.append(requested) or {snapshot.gate.BIZ_REL: b"synthetic"}))
    monkeypatch.setattr(snapshot.gate, "existing_target_binding", lambda *a, **k: "configured_key_not_read_or_verified")
    keys = snapshot.target_keys(base, salt, support, private_root=root)
    assert set(keys) == {snapshot.gate.BIZ_REL} and calls == [[snapshot.gate.BIZ_REL]]
