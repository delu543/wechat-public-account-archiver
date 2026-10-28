"""Read verified local Word membership without advancing product checkpoints."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
import re

from .biz_push_reader import permanent_url, public_identity
from .passive_weekly_plan import publication_day


def read_json(path, limit=8 * 1024 * 1024):
    path = Path(path).expanduser().absolute()
    if path.resolve() != path or not path.is_file() or path.stat().st_size > limit:
        raise ValueError("unsafe_or_missing_receipt")
    return json.loads(path.read_text(encoding="utf-8"))


def file_sha256(path):
    path = Path(path)
    if path.resolve() != path or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
        raise ValueError("unsafe_or_missing_word")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_delivery(path, *, require_review=True):
    path = Path(path).expanduser().absolute()
    receipt = read_json(path)
    if (receipt.get("mode") != "discovered_increment" or receipt.get("full_interval_verified") is not False
        or receipt.get("product_checkpoint_advanced") is not False):
        raise ValueError("invalid_discovered_receipt")
    if require_review:
        review = read_json(path.parent / "verification.json")
        if (review.get("discovered_delivery_ready") is not True
            or review.get("output_manifest_sha256") != file_sha256(path)):
            raise ValueError("delivery_review_missing_or_changed")
    artifacts = receipt.get("artifacts")
    if not isinstance(artifacts, list) or not 1 <= len(artifacts) <= 32:
        raise ValueError("invalid_receipt_accounts")
    accounts, total = {}, 0
    for artifact in artifacts:
        if date.fromisoformat(artifact["since"]) > date.fromisoformat(artifact["until"]):
            raise ValueError("invalid_receipt_interval")
        biz = artifact["biz"]
        if biz in accounts:
            raise ValueError("duplicate_receipt_account")
        word = Path(artifact["file"])
        if not word.is_absolute():
            word = path.parent / word
        if word.parent != path.parent or word.suffix != ".docx" or file_sha256(word) != artifact["sha256"]:
            raise ValueError("registered_word_changed_or_missing")
        members = artifact["members"]
        total += len(members)
        if not members or len(members) != artifact["count"] or total > 20000:
            raise ValueError("receipt_membership_count")
        values, hashes, days = {}, set(), []
        for member in members:
            key, body_hash = public_identity(member.get("url"), biz), member.get("content_hash")
            day = publication_day(member.get("publish_time"))
            if (not key or not isinstance(body_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", body_hash)
                or not day or not artifact["since"] <= day <= artifact["until"]):
                raise ValueError("invalid_receipt_member")
            url = permanent_url(key)
            if url != member["url"] or url in values or body_hash in hashes:
                raise ValueError("duplicate_or_nonsanitized_receipt_member")
            values[url], hashes = body_hash, hashes | {body_hash}
            days.append(day)
        accounts[biz] = {"urls": values, "hashes": hashes, "latest_exported_day": max(days)}
    return accounts


def delivered_members(paths):
    paths = sorted({str(Path(p).expanduser().absolute()) for p in paths})
    if len(paths) > 520:
        raise ValueError("delivery_receipt_budget")
    result = {}
    for path in paths:
        for biz, account in read_delivery(path).items():
            target = result.setdefault(biz, {"urls": {}, "hashes": set(), "latest_exported_day": None})
            for url, body_hash in account["urls"].items():
                if url in target["urls"] and target["urls"][url] != body_hash:
                    raise ValueError("conflicting_delivery_membership")
            target["urls"].update(account["urls"])
            target["hashes"].update(account["hashes"])
            target["latest_exported_day"] = max(target["latest_exported_day"] or "", account["latest_exported_day"])
    return result
