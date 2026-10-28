"""Offline nine-account shared-read, pause and isolated stage regressions."""
import json

import pytest

from cache_capture_fixtures import record, scope, url
from wechat_crawler import cache_capture_all as cli
from wechat_crawler.cache_capture_many import collect_accounts
from wechat_crawler.cache_capture_store import CaptureStore
from wechat_crawler.cache_capture_validation import scope_value

CONTROL = {"paused": True, "updated_at": "existing-pause"}


def fixtures(tmp_path, count=9):
    scopes, paths, records = [], {}, []
    for number in range(count):
        biz, gh = f"fixture_biz_{number}", f"gh_fixture_{number}"
        value = scope_value(dict(scope(), biz=biz, wechat_id=gh, account_name=f"合成号{number}",
            newest_url=url(27, biz), old_urls=[url(8, biz)]))
        scopes.append(value)
        paths[biz] = tmp_path / f"stage-{number}.sqlite"
        records.append(dict(record(27), biz=biz, wechat_id=gh, url=url(27, biz), account_name=value["account_name"]))
    return scopes, paths, records


def snapshot(records):
    return {"status": "read", "records": records, "read_errors": 0, "unsupported_values": 0,
            "metadata_conflicts": 0, "scan_within_bounds": True}


def test_nine_accounts_use_one_shared_read_and_separate_stages(tmp_path):
    scopes, paths, records = fixtures(tmp_path)
    calls = []
    def read(values):
        calls.append(values)
        return snapshot(records)
    result = collect_accounts(scopes, paths, read, lambda: CONTROL,
        max_polls=1, allow_local_stage_while_paused=True, clock=lambda: 0)
    assert len(calls) == result["shared_source_reads"] == 1
    assert result["windows_committed_this_session"] == len(result["accounts"]) == 9
    for value in scopes:
        store = CaptureStore(paths[value["biz"]], value)
        assert len(store.records()) == 1 and store.records()[0]["biz"] == value["biz"]
        store.close()
    assert not result["master_pause_changed"] and not result["product_checkpoint_advanced"]


def test_empty_accounts_are_unknown_and_have_no_empty_stages(tmp_path):
    scopes, paths, records = fixtures(tmp_path)
    result = collect_accounts(scopes, paths, lambda _: snapshot(records[:1]), lambda: CONTROL,
        max_polls=1, allow_local_stage_while_paused=True, clock=lambda: 0)
    assert sum(path.exists() for path in paths.values()) == 1
    waiting = result["accounts"][1:]
    assert all(a["status"] == "waiting_target_cache" and a["coverage"] is None for a in waiting)
    assert all(a["new_article_count"] is None and not a["zero_increment_claimed"] for a in waiting)


def test_paused_default_stops_before_source_or_stages(tmp_path):
    scopes, paths, _ = fixtures(tmp_path)
    def forbidden(_):
        pytest.fail("read must not run")
    result = collect_accounts(scopes, paths, forbidden, lambda: CONTROL)
    assert result["status"] == "paused" and not any(p.exists() for p in paths.values())


def test_control_change_during_shared_read_stops_before_all_commits(tmp_path):
    scopes, paths, records = fixtures(tmp_path)
    state = dict(CONTROL)
    def read(_):
        state["updated_at"] = "new-pause"
        return snapshot(records)
    result = collect_accounts(scopes, paths, read, lambda: state,
        max_polls=1, allow_local_stage_while_paused=True, clock=lambda: 0)
    assert result["status"] == "control_changed" and not any(p.exists() for p in paths.values())
    assert result["source_read_attempts"] == 1


def test_pause_at_commit_rolls_back_affected_account(tmp_path, monkeypatch):
    scopes, paths, records = fixtures(tmp_path, 2)
    state, original = dict(CONTROL), CaptureStore.ingest
    def altered(self, value, **kwargs):
        if self.scope["biz"] == scopes[1]["biz"]:
            state["updated_at"] = "pause-before-commit"
        return original(self, value, **kwargs)
    monkeypatch.setattr(CaptureStore, "ingest", altered)
    result = collect_accounts(scopes, paths, lambda _: snapshot(records), lambda: state,
        max_polls=1, allow_local_stage_while_paused=True, clock=lambda: 0)
    assert result["status"] == "control_changed" and result["windows_committed_this_session"] == 1
    assert result["accounts"][1]["coverage"]["records_captured"] == 0


def test_interruption_reports_read_attempt_and_resumes_previous_accounts(tmp_path):
    scopes, paths, records = fixtures(tmp_path, 2)
    calls = []
    def read(_):
        if calls:
            raise KeyboardInterrupt()
        calls.append(1)
        return snapshot(records[:1])
    first = collect_accounts(scopes, paths, read, lambda: CONTROL, max_polls=3, interval=0,
        allow_local_stage_while_paused=True, clock=lambda: 0, sleep=lambda _: None)
    assert first["status"] == "interrupted_resumable" and first["source_read_attempts"] == 2
    second = collect_accounts(scopes, paths, lambda _: snapshot(records[1:]), lambda: CONTROL,
        max_polls=1, allow_local_stage_while_paused=True, clock=lambda: 0)
    assert all(a["coverage"]["records_captured"] == 1 for a in second["accounts"])


def test_foreign_account_in_source_cannot_create_any_stage(tmp_path):
    scopes, paths, records = fixtures(tmp_path, 2)
    bad = dict(records[0], biz="foreign_biz")
    with pytest.raises(ValueError, match="foreign"):
        collect_accounts(scopes, paths, lambda _: snapshot([bad]), lambda: CONTROL,
            max_polls=1, allow_local_stage_while_paused=True, clock=lambda: 0)
    assert not any(path.exists() for path in paths.values())


def test_duplicate_stage_paths_are_rejected_before_source(tmp_path):
    scopes, paths, _ = fixtures(tmp_path, 2)
    paths[scopes[1]["biz"]] = paths[scopes[0]["biz"]]
    with pytest.raises(ValueError, match="stage_paths"):
        collect_accounts(scopes, paths, lambda _: pytest.fail("no read"), lambda: CONTROL,
            max_polls=1, allow_local_stage_while_paused=True, clock=lambda: 0)


def test_unavailable_source_does_not_persist_zero_increment(tmp_path):
    scopes, paths, _ = fixtures(tmp_path, 2)
    result = collect_accounts(scopes, paths, lambda _: {"status":"blocked","records":[]}, lambda: CONTROL,
        max_polls=1, allow_local_stage_while_paused=True, clock=lambda: 0)
    assert all(a["coverage"] is None for a in result["accounts"])
    assert not any(path.exists() for path in paths.values())


def test_all_plan_does_not_read_public_cache_or_write_stage(tmp_path, monkeypatch, capsys):
    scopes, paths, _ = fixtures(tmp_path, 2)
    monkeypatch.setattr(cli,"load_config",lambda _: {"database":{"path":"unused"}})
    monkeypatch.setattr(cli,"prepare_scopes",lambda *a,**k:(scopes,paths))
    monkeypatch.setattr(cli,"read_control",lambda _:CONTROL)
    monkeypatch.setattr(cli,"read_shared",lambda _:pytest.fail("no source read"))
    assert cli.main(["--stage-root",str(tmp_path),"--plan"]) == 0
    assert json.loads(capsys.readouterr().out)["stage_writes"] == 0
    assert not any(path.exists() for path in paths.values())


def test_all_cli_can_retain_old_batch_readonly_in_new_interval(tmp_path,monkeypatch,capsys):
    import hashlib
    scopes,paths,records = fixtures(tmp_path,1)
    source = tmp_path / "prior.sqlite"
    old = CaptureStore(source,scopes[0])
    old.ingest(snapshot(records))
    old.close()
    before = hashlib.sha256(source.read_bytes()).hexdigest()
    scopes = [dict(scopes[0],until="2026-10-28",newest_url=None)]
    monkeypatch.setattr(cli,"load_config",lambda _: {"database":{"path":"unused"}})
    monkeypatch.setattr(cli,"prepare_scopes",lambda *a,**k:(scopes,paths))
    monkeypatch.setattr(cli,"read_control",lambda _:CONTROL)
    monkeypatch.setattr(cli,"read_shared",lambda _:snapshot([]))
    result = cli.main(["--stage-root",str(tmp_path),"--collect","--seconds","1",
        "--retain-stage",str(source),"--allow-local-stage-while-paused"])
    output = capsys.readouterr().out
    assert result == 0 and '"source_modified": false' in output
    store = CaptureStore(next(iter(paths.values())),scopes[0])
    assert len(store.records()) == 1 and len(store.windows()) == 1
    store.close()
    assert hashlib.sha256(source.read_bytes()).hexdigest() == before


def test_all_cli_paused_default_does_not_even_open_retain_source(tmp_path,monkeypatch,capsys):
    scopes,paths,_ = fixtures(tmp_path,1)
    monkeypatch.setattr(cli,"load_config",lambda _: {"database":{"path":"unused"}})
    monkeypatch.setattr(cli,"prepare_scopes",lambda *a,**k:(scopes,paths))
    monkeypatch.setattr(cli,"read_control",lambda _:CONTROL)
    monkeypatch.setattr(cli,"load_scope",lambda _:pytest.fail("must not open retained data"))
    monkeypatch.setattr(cli,"read_shared",lambda _:pytest.fail("must not read cache"))
    assert cli.main(["--stage-root",str(tmp_path),"--collect","--retain-stage","unused"]) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "paused"
    assert not any(path.exists() for path in paths.values())
