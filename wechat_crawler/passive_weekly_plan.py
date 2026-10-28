"""Read-only account boundaries. Publication day is Beijing time, never receipt time."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from .biz_push_reader import public_identity

BEIJING = ZoneInfo("Asia/Shanghai")


def publication_day(value):
    try:
        text = str(value or "")
        stamp = (datetime.fromtimestamp(int(text), BEIJING) if text.isdigit()
                 else datetime.fromisoformat(text.replace("Z", "+00:00")))
        return (stamp.astimezone(BEIJING) if stamp.tzinfo else stamp).date().isoformat()
    except (ValueError, TypeError, OverflowError, OSError):
        return None


@contextmanager
def readonly_database(path):
    conn = sqlite3.connect(Path(path).expanduser().resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA trusted_schema=OFF")
    try:
        yield conn
    finally:
        conn.close()


def make_plan(db_path, settings, until, *, biz=None):
    end = date.fromisoformat(until)
    with readonly_database(db_path) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        control = conn.execute("SELECT paused,updated_at FROM automation_control WHERE id=1").fetchone()
        subscriptions = conn.execute(
            "SELECT s.*,a.account_name,a.biz,a.wechat_id FROM subscriptions s "
            "JOIN accounts a ON a.id=s.account_id WHERE s.enabled=1 ORDER BY a.id").fetchall()
        accounts = []
        for row in subscriptions:
            if biz and row["biz"] != biz:
                continue
            account = dict(row)
            articles = [dict(r) for r in conn.execute(
                "SELECT ar.id,ar.source_url,ar.normalized_url,ar.title,ar.publish_time,ar.content_hash,"
                "ar.status,length(ar.content_text) AS body_chars,eba.batch_id "
                "FROM articles ar LEFT JOIN export_batch_articles eba ON eba.article_id=ar.id "
                "WHERE ar.account_id=? ORDER BY ar.id", (row["account_id"],))]
            days = [day for a in articles if a["status"] == "parsed"
                    and (day := publication_day(a["publish_time"]))]
            latest = max(days) if days else None
            node = None
            if "passive_weekly_checkpoints" in tables:
                node = conn.execute("SELECT verified_through FROM passive_weekly_checkpoints WHERE biz=?", (row["biz"],)).fetchone()
            verified = str(node[0]) if node else None
            observed = None
            if "passive_weekly_observed_progress" in tables:
                progress = conn.execute("SELECT latest_exported_day,last_checked_until FROM passive_weekly_observed_progress WHERE biz=?",
                                        (row["biz"],)).fetchone()
                if progress:
                    observed = {"latest_exported_day": progress[0], "last_checked_until": progress[1],
                                "scope": "observed_sources_only", "full_interval_verified": False}
            # An export watermark never suppresses unresolved historical gaps,
            # incomplete articles or pending discovery intervals.
            anchor = verified or latest
            if anchor:
                start = date.fromisoformat(anchor) - timedelta(days=int(row["lookback_days"]))
            else:
                start = date.fromisoformat(settings["initial_since"])
            gap = settings.get("recovery_gaps", {}).get(row["biz"])
            if gap and (not verified or verified < gap["until"]):
                start = min(start, date.fromisoformat(gap["since"]))
            incomplete = set(settings.get("incomplete_article_ids", []))
            incomplete &= {a["id"] for a in articles}
            if verified:
                incomplete = set()  # Successful validation resolved the imported recovery issues.
            for article in articles:
                if article["id"] in incomplete and (day := publication_day(article["publish_time"])):
                    start = min(start, date.fromisoformat(day))
            pending = None
            if "passive_weekly_pending" in tables:
                pending = conn.execute("SELECT since_date,until_date FROM passive_weekly_pending WHERE biz=? AND state='open'", (row["biz"],)).fetchone()
                if pending:
                    start = min(start, date.fromisoformat(pending[0]))
            account.update(since=min(start, end).isoformat(), until=until,
                latest_archived_beijing=latest, verified_through=verified, observed_export_progress=observed,
                recovery_gap=gap if gap and (not verified or verified < gap["until"]) else None,
                pending_range=dict(pending) if pending else None,
                incomplete_article_ids=sorted(incomplete), _articles=articles)
            account["_sample_keys"] = {key for a in articles
                if (day := publication_day(a["publish_time"]))
                and settings.get("sample_since", "2026-09-23") <= day <= settings.get("sample_until", "2026-10-08")
                and (key := public_identity(a["source_url"], row["biz"]))}
            account["_sample_unkeyed"] = sum(1 for a in articles
                if (day := publication_day(a["publish_time"]))
                and settings.get("sample_since", "2026-09-23") <= day <= settings.get("sample_until", "2026-10-08")
                and not public_identity(a["source_url"], row["biz"]))
            accounts.append(account)
        if biz and not accounts:
            raise ValueError("exact enabled subscription not found")
    return {"until": until, "paused": bool(control["paused"]) if control else True,
            "pause_updated_at": control["updated_at"] if control else None, "accounts": accounts}


def public_plan(plan):
    return {"until": plan["until"], "automation_paused": plan["paused"],
            "pause_updated_at": plan["pause_updated_at"], "accounts": [{
                key: account.get(key) for key in ("account_name", "biz", "since", "until",
                    "latest_archived_beijing", "last_success_at", "verified_through", "recovery_gap",
                    "incomplete_article_ids", "pending_range", "observed_export_progress")
            } for account in plan["accounts"]]}


def coverage_for(account, source, records, *, now=None, freshness_hours=36):
    if source.get("status") != "read" or not source.get("coverage_measured"):
        # No rows were available. Zero matches/missing would falsely turn an
        # unread source into measured absence. Preserve unknown per account.
        return {"coverage_measured": False, "known_sample_total": len(account["_sample_keys"]),
                "known_sample_matched": None, "known_sample_missing": None,
                "known_sample_unkeyed": account.get("_sample_unkeyed", 0),
                "source_fresh": None, "source_age_hours": None,
                "currently_followed": "unknown", "all_publications_verified": None,
                "publication_interval_proved": None, "complete": False}
    observed = {key for record in records if (key := public_identity(record["url"], account["biz"]))}
    sample = account["_sample_keys"]
    note = next((n for n in source.get("accounts", []) if n.get("biz") == account["biz"]), {})
    epoch = source.get("source_modified_epoch")
    clock = now or datetime.now().astimezone()
    age = (clock.timestamp() - epoch) if isinstance(epoch, (int, float)) else None
    fresh = age is not None and -300 <= age <= freshness_hours * 3600
    missing = len(sample - observed)
    proof = note.get("publication_proof") or {}
    interval_proved = (proof.get("source") in ("native_history_index", "official_export")
                       and isinstance(proof.get("since"), str) and isinstance(proof.get("until"), str)
                       and proof["since"] <= account["since"] and proof["until"] >= account["until"])
    # Sample coverage cannot manufacture full-publication or follow proof.
    complete = bool(source.get("status") == "read" and fresh and not missing and interval_proved
                    and not account.get("_sample_unkeyed")
                    and note.get("schema_supported") and not note.get("undecoded_rows")
                    and not note.get("limit_reached") and not source.get("extra_biz_shards_present")
                    and note.get("all_publications_verified") is True)
    return {"coverage_measured": True, "known_sample_total": len(sample), "known_sample_matched": len(sample & observed),
            "known_sample_missing": missing, "source_fresh": fresh,
            "known_sample_unkeyed": account.get("_sample_unkeyed", 0),
            "publication_interval_proved": bool(interval_proved),
            "source_age_hours": round(age / 3600, 1) if age is not None else None,
            "currently_followed": note.get("currently_followed", "unknown"),
            "all_publications_verified": note.get("all_publications_verified", False),
            "complete": complete}
