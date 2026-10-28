"""One-command workflow, nine synthetic accounts; no native helper or network."""
import json
from pathlib import Path

from docx import Document
import pytest

from test_passive_weekly import Source, stored, url
from wechat_crawler import main as cli
from wechat_crawler import passive_weekly as weekly
from wechat_crawler.automation_store import AutomationStore
from wechat_crawler.biz_push_reader import public_identity
from wechat_crawler.crawler.models import FetchResult
from wechat_crawler.crawler.storage import Storage
from wechat_crawler.passive_weekly_plan import make_plan
from wechat_crawler.passive_weekly_store import PassiveWeeklyStore
from wechat_crawler.weekly_source_router import WeeklySourceRouter, public_source_health


def fixture(tmp_path, monkeypatch, *, count=9, paused=False, complete=False):
    storage = Storage(tmp_path / "archive.sqlite")
    automation = AutomationStore(storage)
    automation.init_db()
    for index in range(count):
        stored(storage, biz=f"biz-fixture-{index}", name=f"公众号{index}")
    automation.adopt_existing_accounts()
    automation.set_automation_paused(paused)
    settings = {"delivered_baseline_max_article_id": count,
        "recovery_gaps": {"biz-fixture-0": {"since": "2026-09-23", "until": "2026-09-28"}}}
    config = {"database": {"path": str(storage.db_path)}, "output": {"path": str(tmp_path / "out")},
              "passive_weekly": settings}
    monkeypatch.setattr(cli, "load_config", lambda *a: config)
    source = Source(complete=complete)
    monkeypatch.setattr(WeeklySourceRouter, "read", lambda self, accounts: source.read(accounts))
    return storage, automation, settings, source


class Bodies:
    def __init__(self, *, mismatch=None, failed=False, restricted=False):
        self.calls, self.mismatch, self.failed, self.restricted = [], mismatch, failed, restricted

    def fetch(self, link):
        self.calls.append(link)
        key = public_identity(link)
        if self.failed:
            return FetchResult(link, link, None, "", "failed", "synthetic failure")
        if self.restricted:
            return FetchResult(link, link, 200, "", "restricted", "captcha")
        canonical = link
        if self.mismatch:
            old = {"mid": "mid=2", "idx": "idx=1", "sn": "sn=" + key[3]}[self.mismatch]
            new = {"mid": "mid=999", "idx": "idx=2", "sn": "sn=" + "x" * 32}[self.mismatch]
            canonical = link.replace(old, new)
        text = f'''<h1 id="activity-name">新文章{key[0]}</h1><a id="js_name">公众号</a>
          <link rel="canonical" href="{canonical}"><em id="publish_time">2026-10-10 12:00:00</em>
          <div id="js_content"><p>独立完整正文 {key[0]} {key[1]}</p></div>
          <script>var biz="{key[0]}"; var user_name="gh_target";</script>'''
        return FetchResult(link, link, 200, text, "ok")


def wire(monkeypatch, *, bodies=None, search=None):
    original = weekly.PassiveWeeklyRunner.__init__
    def init(self, *args, **kwargs):
        kwargs.update(fetcher=bodies or Bodies(), searcher=search or (lambda a: {"status": "read", "candidates": []}))
        original(self, *args, **kwargs)
    monkeypatch.setattr(weekly.PassiveWeeklyRunner, "__init__", init)


def test_default_auto_only_uses_A_and_missing_binding_has_one_notice():
    calls = []
    def child(module, request, **kwargs):
        calls.append(module)
        return {"status": "blocked", "reason": "biz_setup_required"}
    result = WeeklySourceRouter({}, child=child).read([{"biz": "fixture", "wechat_id": "gh_fixture"}] * 9)
    health = public_source_health({"source_reason": result["reason"]}, "auto")
    assert calls == ["wechat_crawler.biz_push_reader"]
    assert health["effective_source"] == "biz" and len(health["required_actions"]) == 1
    assert "需要一次性初始化" in health["required_actions"][0]


def test_cli_unbound_never_searches_writes_tables_or_falls_back(tmp_path, monkeypatch, capsys):
    storage, _, _, source = fixture(tmp_path, monkeypatch)
    source.blocked = True
    wire(monkeypatch, search=lambda a: pytest.fail("no query before binding"))
    assert cli.main(["weekly-run", "--until", "2026-10-27"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert len(result["required_actions"]) == 1 and result["effective_source"] == "biz"
    assert not (tmp_path / "out").exists()
    with storage.connect() as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'passive_weekly_%'").fetchall()


def test_paused_and_check_commands_have_no_search_or_body_side_effects(tmp_path, monkeypatch, capsys):
    storage, _, _, source = fixture(tmp_path, monkeypatch, paused=True)
    bodies = Bodies()
    wire(monkeypatch, bodies=bodies, search=lambda a: pytest.fail("check/pause must not query"))
    assert cli.main(["weekly-run", "--until", "2026-10-27"]) == 2
    assert source.calls == 0 and not bodies.calls
    capsys.readouterr()
    assert cli.main(["weekly-run", "--check", "--until", "2026-10-27"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert source.calls == 1 and result["product_writes"] == 0 and not bodies.calls
    with storage.connect() as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'passive_weekly_%'").fetchall()


def test_nine_account_default_exports_observed_words_only_and_repeats_without_duplicates(tmp_path, monkeypatch, capsys):
    storage, automation, settings, _ = fixture(tmp_path, monkeypatch)
    before = [s["last_success_at"] for s in automation.list_subscriptions()]
    calls, bodies = [], Bodies()
    def search(account):
        calls.append(account["biz"])
        return {"status": "read", "candidates": [{"url": url(account["biz"], 2)}]}
    wire(monkeypatch, bodies=bodies, search=search)
    assert cli.main(["weekly-run", "--until", "2026-10-27"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(calls) == len(set(calls)) == 9 and len(bodies.calls) == 9
    assert result["status"] == "observed_completed" and result["discovery_scope"] == "observed_sources_only"
    paths = [Path(a["word_path"]) for a in result["accounts"]]
    assert len(paths) == 9 and all("已发现增量" in p.name and "2026-10-27" in p.name for p in paths)
    assert all("已发现增量" in "\n".join(p.text for p in Document(path).paragraphs) for path in paths)
    assert "已发现增量" in Path(result["summary_path"]).name
    assert all(not a["full_interval_verified"] for a in result["accounts"])
    with storage.connect() as db:
        assert db.execute("SELECT count(*) FROM passive_weekly_observed_progress").fetchone()[0] == 9
        assert db.execute("SELECT count(*) FROM passive_weekly_checkpoints").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM passive_weekly_pending WHERE state='open'").fetchone()[0] == 9
    assert before == [s["last_success_at"] for s in automation.list_subscriptions()]
    plan = make_plan(storage.db_path, settings, "2026-10-27")
    first = plan["accounts"][0]
    assert first["recovery_gap"] and first["observed_export_progress"]["latest_exported_day"] == "2026-10-10"
    assert first["since"] <= "2026-09-23" and first["verified_through"] is None
    saved = {p: p.read_bytes() for p in paths}
    assert cli.main(["weekly-run", "--until", "2026-10-27"]) == 0
    repeat = json.loads(capsys.readouterr().out)
    assert all(a["exported_count"] == 0 for a in repeat["accounts"])
    assert all(p.read_bytes() == value for p, value in saved.items())


@pytest.mark.parametrize("mismatch", ["mid", "idx", "sn"])
def test_search_candidate_must_match_all_body_fields_and_cannot_retry_as_push(tmp_path, monkeypatch, capsys, mismatch):
    storage, _, _, _ = fixture(tmp_path, monkeypatch, count=1)
    bodies = Bodies(mismatch=mismatch)
    wire(monkeypatch, bodies=bodies, search=lambda a: {"status": "read", "candidates": [{"url": url(a["biz"], 2)}]})
    assert cli.main(["weekly-run", "--until", "2026-10-27"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["accounts"][0]["failed"] == 1 and len(bodies.calls) == 1
    with storage.connect() as db:
        assert db.execute("SELECT count(*) FROM articles").fetchone()[0] == 1
    assert not list((tmp_path / "out").rglob("*.docx"))


def test_same_candidate_transient_failure_is_not_retried_as_overlapping_push(tmp_path, monkeypatch, capsys):
    fixture(tmp_path, monkeypatch, count=1)
    bodies = Bodies(failed=True)
    wire(monkeypatch, bodies=bodies, search=lambda a: {"status": "read", "candidates": [{"url": url(a["biz"], 2)}]})
    cli.main(["weekly-run", "--until", "2026-10-27"])
    result = json.loads(capsys.readouterr().out)
    assert len(bodies.calls) == 1 and result["accounts"][0]["failed"] == 1


def test_sogou_verification_stops_search_but_independent_push_exports_continue(tmp_path, monkeypatch, capsys):
    fixture(tmp_path, monkeypatch)
    searches, bodies = [], Bodies()
    def search(a):
        searches.append(a["biz"])
        return {"status": "restricted", "restriction_provider": "sogou", "candidates": []}
    wire(monkeypatch, bodies=bodies, search=search)
    assert cli.main(["weekly-run", "--until", "2026-10-27"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(searches) == 1 and len(bodies.calls) == 9
    assert len(result["required_actions"]) == 1 and all(a["word_path"] for a in result["accounts"])


def test_wechat_body_verification_stops_all_related_body_requests(tmp_path, monkeypatch, capsys):
    fixture(tmp_path, monkeypatch)
    bodies = Bodies(restricted=True)
    wire(monkeypatch, bodies=bodies)
    assert cli.main(["weekly-run", "--until", "2026-10-27"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert len(bodies.calls) == 1 and all(a["status"] == "blocked_shared_restriction" for a in result["accounts"])
    assert not list((tmp_path / "out").rglob("*.docx"))


def test_explicit_complete_mode_retains_old_full_interval_gate(tmp_path, monkeypatch, capsys):
    fixture(tmp_path, monkeypatch, count=1)
    bodies = Bodies()
    wire(monkeypatch, bodies=bodies)
    assert cli.main(["weekly-run", "--require-complete", "--until", "2026-10-27"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert result["discovery_scope"] == "all_publications" and not bodies.calls
    assert result["accounts"][0]["status"] == "coverage_incomplete"


def test_empty_observed_check_does_not_invent_an_export_date(tmp_path, monkeypatch, capsys):
    storage, _, _, source = fixture(tmp_path, monkeypatch, count=1)
    original = source.read
    def old_only(accounts):
        value = original(accounts)
        value["records"] = [r for r in value["records"] if public_identity(r["url"])[1] == "1"]
        return value
    source.read = old_only
    wire(monkeypatch)
    assert cli.main(["weekly-run", "--until", "2026-10-27"]) == 0
    result = json.loads(capsys.readouterr().out)
    row = result["accounts"][0]
    assert row["status"] == "observed_checked" and row["observed_export_progress"]["latest_exported_day"] is None
    assert not list((tmp_path / "out").rglob("*.docx"))
    with storage.connect() as db:
        assert db.execute("SELECT count(*) FROM passive_weekly_checkpoints").fetchone()[0] == 0


def test_crash_after_word_membership_before_observed_progress_resumes_without_reexport(tmp_path, monkeypatch, capsys):
    storage, _, _, _ = fixture(tmp_path, monkeypatch, count=1)
    wire(monkeypatch)
    original = PassiveWeeklyStore.record_observed_export
    monkeypatch.setattr(PassiveWeeklyStore, "record_observed_export", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("interrupted")))
    assert cli.main(["weekly-run", "--until", "2026-10-27"]) == 2
    first = json.loads(capsys.readouterr().out)
    path = Path(first["accounts"][0]["word_path"])
    before = path.read_bytes()
    monkeypatch.setattr(PassiveWeeklyStore, "record_observed_export", original)
    assert cli.main(["weekly-run", "--until", "2026-10-27"]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["accounts"][0]["word_path"] == str(path) and second["accounts"][0]["exported_count"] == 0
    assert before == path.read_bytes()
    with storage.connect() as db:
        assert db.execute("SELECT latest_exported_day FROM passive_weekly_observed_progress").fetchone()[0] == "2026-10-10"
        assert db.execute("SELECT count(*) FROM passive_weekly_checkpoints").fetchone()[0] == 0


def test_opaque_pc_links_are_scoped_per_account_and_never_written_to_report(tmp_path, monkeypatch, capsys):
    fixture(tmp_path, monkeypatch, count=2)
    class RedirectBodies(Bodies):
        def __init__(self):
            super().__init__()
            self.redirects = []
        def fetch(self, link):
            if link.startswith("https://weixin.sogou.com/link"):
                self.redirects.append(link)
                index = link.rsplit("OPAQUE_", 1)[1]
                final = url("biz-fixture-" + index, 2)
                value = super().fetch(final)
                return FetchResult(link, final, 200, value.text, "ok")
            return super().fetch(link)
    bodies = RedirectBodies()
    def search(a):
        index = a["biz"].rsplit("-", 1)[1]
        return {"status": "read", "candidates": [{"url": "https://weixin.sogou.com/link?url=OPAQUE_" + index}]}
    wire(monkeypatch, bodies=bodies, search=search)
    assert cli.main(["weekly-run", "--until", "2026-10-27"]) == 0
    output = capsys.readouterr().out
    report = json.loads(output)
    assert len(bodies.redirects) == len(bodies.calls) == 2
    assert "OPAQUE_" not in output and "OPAQUE_" not in Path(report["report_path"]).read_text()


def test_sogou_link_challenge_stops_later_link_resolution_not_independent_pushes(tmp_path, monkeypatch, capsys):
    fixture(tmp_path, monkeypatch, count=2)
    class LinkChallenge(Bodies):
        def __init__(self):
            super().__init__()
            self.links = []
        def fetch(self, link):
            if link.startswith("https://weixin.sogou.com/link"):
                self.links.append(link)
                return FetchResult(link, link, 200, "", "restricted", "captcha")
            return super().fetch(link)
    bodies = LinkChallenge()
    wire(monkeypatch, bodies=bodies, search=lambda a: {"status": "read", "candidates": [{"url": "https://weixin.sogou.com/link?url=" + a["biz"]}]})
    assert cli.main(["weekly-run", "--until", "2026-10-27"]) == 2
    result = json.loads(capsys.readouterr().out)
    assert len(bodies.links) == 1 and len(bodies.calls) == 2
    assert all(row["word_path"] for row in result["accounts"]) and len(result["required_actions"]) == 1
