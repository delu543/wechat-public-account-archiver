"""Isolated, resumable discovered Word batches; no product database writes."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import re

from .cache_capture_validation import digest
from .cache_delivery_receipts import file_sha256, read_delivery, read_json
from .discovered_word import DiscoveredWordExporter, verify_discovered_word
from .passive_weekly_plan import publication_day


def safe_path(path):
    path = Path(path).expanduser().absolute()
    if path.resolve() != path:
        raise ValueError("symlinked_output_forbidden")
    return path


def write_once(path, value):
    path = safe_path(path)
    with path.open("x", encoding="utf-8") as handle:
        path.chmod(0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)


@contextmanager
def output_lock(root):
    root = safe_path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / ".cache-update.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def batches(root):
    root = safe_path(root)
    if not root.exists():
        return []
    result = sorted(root.glob("batch-*/plan.json"))
    if len(result) > 520:
        raise ValueError("batch_budget_exceeded")
    for path in result:
        safe_path(path)
    return result


def pending_batches(root):
    return [path.parent for path in batches(root) if not (path.parent / "verification.json").exists()]


def checked_plan(path):
    plan = read_json(path, limit=128 * 1024 * 1024)
    if (plan.get("fingerprint") != digest({k:v for k,v in plan.items() if k != "fingerprint"})
        or plan.get("mode") != "discovered_increment" or plan.get("full_interval_verified") is not False
        or plan.get("product_checkpoint_advanced") is not False or not plan.get("accepted_unknown_coverage")):
        raise ValueError("batch_plan_changed")
    return plan


def export_batch(plan, root, *, before_write=lambda: None):
    """Called under output_lock. An earlier interrupted batch always wins."""
    pending = pending_batches(root)
    if len(pending) > 1:
        raise ValueError("multiple_pending_batches_require_review")
    resumed = bool(pending)
    if pending:
        folder = pending[0]
        stored = checked_plan(folder / "plan.json")
        if not {a["biz"] for a in stored["accounts"]} <= {a["biz"] for a in plan["accounts"]}:
            return {"status": "pending_batch_outside_selected_scope", "folder": str(folder)}
        plan = stored
    else:
        if not any(a["articles"] for a in plan["accounts"]):
            return {"status": "no_eligible_retained_articles", "documents": 0, "zero_increment_claimed": False}
        folder = safe_path(root) / f"batch-{plan['until']}-{plan['fingerprint']}"
        before_write()
        folder.mkdir(mode=0o700, exist_ok=True)
        if (folder / "plan.json").exists():
            if checked_plan(folder / "plan.json") != plan:
                raise ValueError("batch_identity_conflict")
        else:
            write_once(folder / "plan.json", plan)
    manifest_path = folder / "delivery-manifest.json"
    if manifest_path.exists():
        read_delivery(manifest_path, require_review=False)
        return {"status": "awaiting_render_review", "folder": str(folder), "resumed": resumed}
    artifacts, exporter = [], DiscoveredWordExporter()
    for account in plan["accounts"]:
        articles = account["articles"]
        if not articles:
            continue
        name = re.sub(r"[^\w .-]", "_", account["account_name"])[:60].strip(" .") or "公众号"
        suffix = digest(account["biz"])[:8]
        word = folder / f"{name}_{account['since']}至{account['until']}_已发现增量_{suffix}.docx"
        before_write()
        # On interrupted output, only adopt a byte-valid, text-matching Word.
        # Never replace even a damaged old file: retain it for investigation.
        qa = verify_discovered_word(word, account) if word.exists() else exporter.author(account, word)
        before_write()
        days = [publication_day(a["publish_time"]) for a in articles]
        artifacts.append({"biz": account["biz"], "account_name": account["account_name"],
            "file": word.name, "since": account["since"], "until": account["until"], "count": len(articles),
            "actual_publication_since": min(days), "actual_publication_until": max(days),
            "sha256": file_sha256(word), "structural_qa": qa, "gaps": account["gaps"],
            "duplicate_bodies": account["duplicate_bodies"],
            "members": [{k:a[k] for k in ("url", "title", "publish_time", "content_hash")} for a in articles]})
    manifest = {"schema": 1, "mode": "discovered_increment", "payload_fingerprint": plan["fingerprint"],
        "full_interval_verified": False, "product_checkpoint_advanced": False, "artifacts": artifacts,
        "excluded_biz": plan["excluded_biz"], "product_writes": 0,
        "accounts_without_document": [{k:a.get(k) for k in ("biz","account_name","gaps","retained_count")}
                                     for a in plan["accounts"] if not a["articles"]]}
    before_write()
    write_once(manifest_path, manifest)
    # The machine-readable summary retains unknown/empty accounts as well.
    read_delivery(manifest_path, require_review=False)
    return {"status": "awaiting_render_review", "folder": str(folder), "resumed": resumed,
            "documents": len(artifacts), "articles": sum(a["count"] for a in artifacts)}


def confirm_delivery(folder, *, render_reviewed=False):
    if not render_reviewed:
        raise ValueError("render_review_required")
    folder = safe_path(folder)
    plan, manifest = checked_plan(folder / "plan.json"), read_json(folder / "delivery-manifest.json")
    if manifest.get("payload_fingerprint") != plan["fingerprint"]:
        raise ValueError("manifest_plan_mismatch")
    accounts = {a["biz"]:a for a in plan["accounts"] if a["articles"]}
    delivered = read_delivery(folder / "delivery-manifest.json", require_review=False)
    if set(delivered) != set(accounts):
        raise ValueError("manifest_plan_membership_mismatch")
    for artifact in manifest["artifacts"]:
        account = accounts[artifact["biz"]]
        if delivered[artifact["biz"]]["urls"] != {a["url"]:a["content_hash"] for a in account["articles"]}:
            raise ValueError("manifest_plan_membership_mismatch")
        verify_discovered_word(folder / artifact["file"], account)
    review_path = folder / "verification.json"
    if not review_path.exists():
        write_once(review_path, {"discovered_delivery_ready": True, "render_reviewed": True,
            "review_attested_by": "operator", "verified_at": datetime.now().astimezone().isoformat(),
            "output_manifest_sha256": file_sha256(folder / "delivery-manifest.json"),
            "documents": len(accounts), "articles": sum(len(a["articles"]) for a in accounts.values()),
            "full_interval_verified": False, "product_checkpoint_advanced": False})
    read_delivery(folder / "delivery-manifest.json")
    return {"status": "discovered_delivery_registered", "folder": str(folder), "product_checkpoint_advanced": False}
