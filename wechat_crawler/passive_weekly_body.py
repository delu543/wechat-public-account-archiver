"""Conservative sequential public-body stage; never reads/replays client keys."""
from __future__ import annotations

from bs4 import BeautifulSoup
import hashlib
from urllib.parse import urlparse

from .biz_push_reader import public_identity, permanent_url
from .crawler.parser import WechatArticleParser
from .passive_weekly_plan import publication_day
from .passive_public_http import PassivePublicFetcher, public_navigation_url
from .public_article_identity import primary_page_identity

SHARED_RESTRICTIONS = {"captcha", "login_required", "rate_limited", "forbidden",
                       "forbidden status", "rate limited status", "unsupported"}


def crawl_records(storage, account, records, paused, *, fetcher=None, search_stopped=False):
    fetcher = fetcher or PassivePublicFetcher()
    parser = WechatArticleParser()
    known = {key: row for row in account["_articles"]
             if (key := public_identity(row["source_url"], account["biz"]))}
    seen, attempted, allowed, issues = set(), set(), set(), []
    result = {"discovered": 0, "already_archived": 0, "newly_crawled": 0,
              "failed": 0, "restricted": 0, "shared_restriction": False,
              "paused": False, "article_ids": allowed, "issues": issues,
              "search_examined": 0, "search_rejected_identity": 0, "search_unresolved": 0,
              "search_restriction": False}
    # Validate search results first, so an overlapping push URL cannot bypass
    # the search provenance's strict four-field body-identity check.
    ordered = [r for r in records if r.get("source") == "sogou_pc"] + [r for r in records if r.get("source") != "sogou_pc"]
    for record in ordered:
        strict = record.get("source") == "sogou_pc"
        key = public_identity(record.get("url"), account["biz"])
        if strict:
            if search_stopped or not public_navigation_url(record.get("url")):
                continue
            declared = public_identity(record.get("url"))
            if declared and declared[0] != account["biz"]:
                result["search_rejected_identity"] += 1
                continue
            result["search_examined"] += 1
        if (not strict and not key) or (key and (key in seen or key in attempted)):
            continue
        counted = not strict
        if counted:
            seen.add(key)
        hint = publication_day(record.get("publish_time"))
        if hint and not account["since"] <= hint <= account["until"]:
            continue
        if paused():
            result["paused"] = True
            break
        if counted:
            result["discovered"] += 1
        row = known.get(key)
        if (not strict and row and row["status"] == "parsed" and row["body_chars"]
                and row["id"] not in account["incomplete_article_ids"]):
            stored_day = publication_day(row["publish_time"])
            if not stored_day or not account["since"] <= stored_day <= account["until"]:
                result["discovered"] -= 1
                continue
            result["already_archived"] += 1
            allowed.add(row["id"])
            continue
        if key:
            attempted.add(key)
        response = fetcher.fetch(record["url"])
        if strict:
            final_key = public_identity(response.final_url, account["biz"])
            if final_key:
                attempted.add(final_key)
            key = key or final_key
        issue_url = permanent_url(key) if key else "search-candidate:" + hashlib.sha256(record["url"].encode()).hexdigest()[:12]
        if response.status == "restricted":
            reason = str(response.error_message or "restricted")
            shared = reason in SHARED_RESTRICTIONS
            if strict and urlparse(response.final_url).netloc != "mp.weixin.qq.com":
                result["search_unresolved"] += 1
                result["search_restriction"] = search_stopped = shared
                issues.append({"url": issue_url, "reason": "sogou_verification_required" if shared else "search_candidate_unavailable"})
                continue
            if strict and key:
                result["discovered"] += 1
                counted = True
            if counted:
                result["restricted"] += 1
            else:
                result["search_unresolved"] += 1
            issues.append({"url": issue_url, "reason": reason if shared else "article_unavailable_or_paywall"})
            result["shared_restriction"] = shared
            if shared:
                break
            continue
        if response.status != "ok":
            if strict and key:
                result["discovered"] += 1
                counted = True
            result["failed" if counted else "search_unresolved"] += 1
            issues.append({"url": issue_url, "reason": "public_fetch_failed"})
            continue
        try:
            page_key = primary_page_identity(response.text)
            if strict:
                expected = public_identity(response.final_url) or key
                if urlparse(response.final_url).netloc != "mp.weixin.qq.com" or not page_key:
                    raise ValueError("unverified_search_identity")
                if page_key[0] != account["biz"]:
                    result["search_rejected_identity"] += 1
                    if key:
                        raise ValueError("known_target_resolved_to_another_account")
                    continue
                if (key and key != page_key) or (expected and expected != page_key):
                    raise ValueError("search_identity_mismatch")
                key = page_key
                if key in seen:
                    continue
                seen.add(key)
                attempted.add(key)
                record = dict(record, url=permanent_url(key))
                result["discovered"] += 1
                counted = True
                row = known.get(key)
            elif page_key and page_key != key:
                raise ValueError("public_identity_mismatch")
            # Extract identity independently of the requested query parameters;
            # the legacy parser otherwise trusts source_url.__biz first.
            article = parser.parse(response.text, "https://mp.weixin.qq.com/s/identity-only")
            if article.account_id != account["biz"] or (article.wechat_id and article.wechat_id != account["wechat_id"]):
                raise ValueError("account_mismatch")
            body = BeautifulSoup(response.text, "html.parser")
            element = body.select_one("#js_content") or body.select_one("article")
            if not element or not element.get_text("", strip=True):
                raise ValueError("preview_or_unverified_body")
            body_day = publication_day(article.publish_time)
            if not body_day:
                raise ValueError("publication_time_missing")
            if not account["since"] <= body_day <= account["until"]:
                if strict:
                    result["discovered"] -= 1
                    continue
                raise ValueError("publication_outside_range")
            if hint and hint != body_day:
                raise ValueError("publication_hint_mismatch")
            if paused():
                result["paused"] = True
                result["failed"] += 1  # fetched but deliberately not committed
                break
            if (strict and row and row["status"] == "parsed" and row["body_chars"]
                    and row["id"] not in account["incomplete_article_ids"]):
                result["already_archived"] += 1
                allowed.add(row["id"])
                continue
            article.source_url = article.normalized_url = record["url"]
            article.account_name, article.wechat_id = account["account_name"], account["wechat_id"]
            with storage.connect() as conn:
                duplicate = conn.execute("SELECT id FROM articles WHERE account_id=? AND content_hash=? AND content_hash!=''",
                                         (account["account_id"], article.content_hash)).fetchone()
            if duplicate:
                allowed.add(duplicate[0])
                result["already_archived"] += 1
                continue
            ident = storage.upsert_article_url(record["url"], "passive_weekly", account_hint=account["account_name"])
            saved, _ = storage.save_article_with_status(article, ident, None)
            allowed.add(saved)
            result["newly_crawled"] += 1
        except Exception:
            if strict and not counted and key:
                # A failed exact search identity must not later be retried as
                # an overlapping, less-strict push record in the same run.
                seen.add(key)
                result["discovered"] += 1
                counted = True
            result["failed" if counted else "search_unresolved"] += 1
            issues.append({"url": issue_url, "reason": "body_identity_date_or_completeness_failed"})
    return result
