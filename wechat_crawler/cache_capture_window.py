"""One bounded, keyless public blob/inline snapshot shared by exact accounts."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .biz_push_reader import permanent_url, public_identity
from .chromium_indexeddb_values import MAX_DECODED_VALUE_BYTES, MAX_VALUE_BYTES, SNAPPY_PREFIX, unwrap_indexeddb_value
from .crawler.dedup import normalized_text_hash
from .crawler.discoverers.content_flow_cache_discoverer import WechatContentFlowCacheDiscoverer
from .crawler.discoverers.wechat_cache_discoverer import WechatCacheDiscoverer
from .manual_cache_source import publication
from .passive_weekly_sources import APP_DATA
from .public_leveldb_snapshot import snapshot_values


def public_text(text):
    """Do not retain client transport parameters embedded in a cached URL."""
    def clean(match):
        value = match[0]
        key = public_identity(value)
        if key:
            return permanent_url(key)
        parsed = urlsplit(value)
        sensitive = {"key", "uin", "pass_ticket", "exportkey", "appmsg_token", "wxtoken", "cookie", "sessionid"}
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        safe = [(k, v) for k, v in pairs if k.lower() not in sensitive and "session" not in k.lower()]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(safe), "")) if safe != pairs else value
    return re.sub(r'https?://mp\.weixin\.qq\.com/[^\s<>"\x27]+', clean, text)


def records_from_batch(batch, account):
    """A wrapper is one identity context; never join fields across records."""
    flow = WechatContentFlowCacheDiscoverer("unused", account["biz"], account["wechat_id"])
    for hint, wrapper in batch.items():
        if not isinstance(wrapper, dict) or not isinstance(wrapper.get("Content"), str):
            continue
        try:
            item = json.loads(wrapper["Content"])
        except (ValueError, TypeError):
            continue
        if not isinstance(item, dict) or item.get("user_name") != account["wechat_id"]:
            continue
        raw_urls = [v for v in (item.get("link"), wrapper.get("Url")) if isinstance(v, str)]
        identities = {k for v in raw_urls if (k := public_identity(v))}
        if len(identities) != 1:
            continue
        key = next(iter(identities))
        if key[0] != account["biz"]:
            continue
        # A present but invalid primary link cannot borrow a wrapper's sn.
        if item.get("link") and not public_identity(item["link"], account["biz"]):
            continue
        article = flow._article_from_payload(str(hint), wrapper)
        if not article or public_identity(article.source_url) != key:
            continue
        page = item.get("text_page_info")
        has_body_field = any(isinstance(value, str) and value.strip() for value in (
            item.get("content_noencode"), item.get("content"),
            page.get("content_noencode") if isinstance(page, dict) else None,
            page.get("content") if isinstance(page, dict) else None))
        body = public_text(article.content_text or "")
        stamp = publication(item.get("create_time"))
        title = item.get("title")
        title_kind = "original_title"
        if not isinstance(title, str) or not title.strip() or len(title.strip()) > 200 or "\n" in title:
            title, title_kind = flow._short_title(body), "body_first_line_no_separate_title"
        restricted = (item.get("is_pay") in (True, 1, "1") or item.get("is_truncated")
                      or item.get("is_content_complete") is False)
        ready = bool(has_body_field and body and stamp and not restricted)
        yield {"url": permanent_url(key), "biz": key[0], "wechat_id": account["wechat_id"],
               "account_name": account["account_name"], "title": public_text(title.strip()),
               "title_kind": title_kind, "publish_time": stamp, "content_text": body,
               "content_html": flow._text_to_html(body), "content_hash": normalized_text_hash(body),
               "body_ready": ready, "body_evidence": "restricted_or_truncated" if restricted else (
                   "structured_cache_content_field" if has_body_field else "title_only"),
               "source": "passive_public_cache", "all_publications_verified": False}


def read_window(account, *, root=None, paths=None, clock=time.monotonic):
    return read_windows([account], root=root, paths=paths, clock=clock)


def read_windows(accounts, *, root=None, paths=None, clock=time.monotonic):
    if not isinstance(accounts, (list, tuple)) or not 1 <= len(accounts) <= 32:
        raise ValueError("invalid_public_cache_accounts")
    if (any(not isinstance(a, dict) or not isinstance(a.get("biz"), str)
            or not re.fullmatch(r"[A-Za-z0-9_+=/-]{4,128}", a["biz"])
            or not isinstance(a.get("wechat_id"), str)
            or not re.fullmatch(r"gh_[A-Za-z0-9_]{1,80}", a["wechat_id"])
            or not isinstance(a.get("account_name"), str) for a in accounts)
        or len({a["biz"] for a in accounts}) != len(accounts)):
        raise ValueError("invalid_public_cache_accounts")
    account = accounts[0]
    root = APP_DATA if root is None else Path(root)
    reader = WechatCacheDiscoverer(root, account["biz"], account["wechat_id"])
    flow = WechatContentFlowCacheDiscoverer(root, account["biz"], account["wechat_id"])
    start, files, decoded, errors, unsupported, decoded_bytes = clock(), 0, 0, 0, 0, 0
    input_bytes, inline_count, inline_deleted, external, inline_non_article = 0, 0, 0, 0, 0
    records, variants, error_codes, complete = {}, {}, {}, True

    def read_file(path):
        nonlocal files, input_bytes, complete
        if files >= 64 or clock() - start > 20:
            complete = False
            raise ValueError("snapshot_budget")
        path = Path(path)
        path.relative_to(root)
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError("symlink")
        before = path.stat()
        if not path.is_file() or before.st_size > MAX_VALUE_BYTES:
            raise ValueError("not_bounded_file")
        if input_bytes + before.st_size > 40 * 1024 * 1024:
            complete = False
            raise ValueError("snapshot_budget")
        data = reader._read_bytes(path)
        files += 1
        after = path.stat()
        if data is None or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("unstable_read")
        input_bytes += len(data)
        return data

    def accept(data, *, inline=False):
        nonlocal decoded_bytes, decoded, unsupported, complete, inline_non_article
        # Physical files remain <=20 MiB. Native shared batches can legitimately
        # expand past that; give the decoder only this snapshot's unused budget.
        try:
            logical = (unwrap_indexeddb_value(data, max_output=MAX_DECODED_VALUE_BYTES - decoded_bytes)
                       if data.startswith(SNAPPY_PREFIX) else data)
        except ValueError as exc:
            if str(exc) == "indexeddb_value_too_large":
                complete = False
            raise
        if decoded_bytes + len(logical) > MAX_DECODED_VALUE_BYTES:
            complete = False
            raise ValueError("logical_snapshot_budget")
        decoded_bytes += len(logical)
        batches = list(flow._decode_batches(logical))
        if not batches:
            if inline:
                inline_non_article += 1
            else:
                unsupported += 1
        decoded += len(batches)
        for batch in batches:
            for target in accounts:
                for record in records_from_batch(batch, target):
                    if len(records) >= 20000:
                        complete = False
                        raise ValueError("article_count_budget")
                    key = public_identity(record["url"])
                    variants.setdefault(key, set()).add((record["content_hash"], record["publish_time"], record["title"]))
                    records.setdefault(key, record)

    def failed(exc):
        nonlocal errors
        errors += 1
        # Error messages never expose public-cache contents or local paths.
        code = str(exc) if isinstance(exc, ValueError) and re.fullmatch(r"[a-z_]+", str(exc)) else "public_cache_io_error"
        error_codes[code] = error_codes.get(code, 0) + 1

    profiles = reader._profile_roots() if paths is None else []
    for profile in profiles:
        folder = profile / "IndexedDB/weixin_xworker_0.indexeddb.leveldb"
        if not folder.is_dir():
            continue
        try:
            names = reader._protected_directory_names(folder) if reader._is_protected_wechat_path(folder) else None
            snapshot = snapshot_values(folder, read_file, names=names)
            inline_count += len(snapshot["values"])
            inline_deleted += snapshot["deleted_object_keys"]
            external += snapshot["external_blob_references"]
            for value in snapshot["values"]:
                accept(value, inline=True)
        except (OSError, ValueError) as exc:
            failed(exc)
        if not complete:
            break

    def blobs():
        for profile in profiles:
            folder = profile / "IndexedDB/weixin_xworker_0.indexeddb.blob"
            if folder.is_dir() and not folder.is_symlink():
                yield from (p for p in folder.rglob("*") if p.is_file())

    try:
        for path in blobs() if paths is None else paths:
            if not complete:
                break
            try:
                accept(read_file(path))
            except (OSError, ValueError) as exc:
                failed(exc)
    except OSError as exc:
        failed(exc)
    conflicts = sum(len(values) > 1 for values in variants.values())
    ordered = sorted(records.values(), key=lambda r: (r["publish_time"] or "", r["url"]), reverse=True)
    fingerprint = hashlib.sha256(json.dumps(ordered, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return {"status": "read" if decoded else "waiting_cache", "records": ordered, "fingerprint": fingerprint,
            "files_read": files, "decoded_batches": decoded, "read_errors": errors,
            "read_error_codes": error_codes, "inline_current_values": inline_count,
            "inline_deleted_keys": inline_deleted, "inline_external_blob_references": external,
            "inline_non_article_values": inline_non_article,
            "unsupported_values": unsupported, "metadata_conflicts": conflicts, "scan_within_bounds": complete,
            "window_is_ordered_publication_index": False, "all_publications_verified": False,
            "network_requests": 0, "wechat_input_events": 0, "private_key_reads": 0}


if __name__ == "__main__":
    try:
        from .cache_capture_validation import scope_value
        if sys.argv[1:] not in (["--public-window-only"], ["--public-windows-only"]):
            raise ValueError()
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        request = json.loads(raw)
        if sys.argv[1:] == ["--public-window-only"]:
            result = read_window(scope_value(request["scope"]))
        else:
            scopes = request["scopes"]
            if not isinstance(scopes, list) or not 1 <= len(scopes) <= 32:
                raise ValueError()
            result = read_windows([scope_value(scope) for scope in scopes])
        if result["status"] != "read":
            result.update(status="blocked", reason="public_cache_not_available")
    except Exception:
        result = {"status": "blocked", "reason": "bounded_public_window_unavailable", "records": []}
    print(json.dumps(result, ensure_ascii=False))
