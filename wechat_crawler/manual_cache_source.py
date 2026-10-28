"""Keyless B: passive public homepage cache, including body-less list cards.

No refresh/UI/network/keys/private DB. Supported JSON/V8 structures are an
explicit parser contract, not proof that the current client wrote that schema.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
import html
import json
from pathlib import Path
import sys
import time

from .biz_push_reader import public_identity, permanent_url
from .chromium_indexeddb_values import SNAPPY_PREFIX
from .crawler.discoverers.content_flow_cache_discoverer import WechatContentFlowCacheDiscoverer
from .crawler.discoverers.wechat_cache_discoverer import WechatCacheDiscoverer
from .passive_weekly_sources import APP_DATA

BEIJING = timezone(timedelta(hours=8))


def publication(value):
    try:
        if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
            result = datetime.fromtimestamp(int(value), BEIJING)
        elif isinstance(value, str):
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            result = result.replace(tzinfo=BEIJING) if result.tzinfo is None else result.astimezone(BEIJING)
        else:
            return None
        return result.isoformat(timespec="seconds") if 2000 <= result.year <= 2100 else None
    except (ValueError, OverflowError, OSError):
        return None


def cards(payload, accounts):
    """Only identities in one record/wrapper; never join unrelated cache bytes."""
    allowed = {a["biz"]: a.get("wechat_id") for a in accounts}
    pending, visited = [(payload, None, None, 0)], 0
    while pending and visited < 10000:
        item, hint, inherited_time, depth = pending.pop()
        visited += 1
        if depth > 10:
            continue
        if isinstance(item, list):
            pending.extend((v, None, inherited_time, depth + 1) for v in item[:5000])
            continue
        if not isinstance(item, dict):
            continue
        content = item.get("Content")
        if isinstance(content, str):
            try:
                inner = json.loads(content)
            except (ValueError, TypeError):
                inner = None
            if isinstance(inner, dict):
                pending.append((inner, item.get("Url") or hint, inherited_time, depth + 1))
                continue
        common = item.get("comm_msg_info")
        stamp = common.get("datetime") if isinstance(common, dict) else inherited_time
        urls = [item[k] for k in ("link", "url", "content_url", "Url") if isinstance(item.get(k), str)]
        if not urls and hint:
            urls = [hint]
        identities = {key for u in urls if (key := public_identity(html.unescape(u)))}
        if len(identities) == 1:
            key = next(iter(identities))
            if key[0] in allowed and (not item.get("user_name") or item["user_name"] == allowed[key[0]]):
                raw_time = next((item[k] for k in ("publish_time", "pub_time", "create_time", "ori_create_time", "datetime")
                                 if item.get(k) is not None), stamp)
                title = item.get("title") or item.get("Title") or ""
                yield {"url": permanent_url(key), "biz": key[0], "title": title.strip()[:512] if isinstance(title, str) else "",
                       "publish_time": publication(raw_time), "source": "manual_homepage_cache"}
        for name, value in item.items():
            if name in ("app_msg_ext_info", "multi_app_msg_item_list", "appmsg_list", "app_msg_list", "list", "data"):
                if isinstance(value, str) and name == "data":
                    try:
                        value = json.loads(value)
                    except ValueError:
                        continue
                pending.append((value, None, stamp, depth + 1))
            elif isinstance(value, dict):
                hint_url = name if name.startswith("https:") else "https://" + name
                valid_hint = public_identity(hint_url)
                # Native batch map keys may omit sn. Visit their Content/Url
                # wrapper, but never borrow an incomplete outer identity.
                # The inner record must independently pass exact-biz checks.
                if valid_hint or isinstance(value.get("Content"), str):
                    pending.append((value, hint_url if valid_hint else None, stamp, depth + 1))


def passive_files(reader):
    seen = set()
    for path in reader._iter_files():
        if path not in seen:
            seen.add(path)
            yield path
    # Existing URL reader covers LevelDB but not xworker blob payloads. Stay
    # within current public renderer roots, never the app_data fallback tree.
    for root in reader._profile_roots():
        folder = root / "IndexedDB/weixin_xworker_0.indexeddb.blob"
        if folder.is_dir() and not folder.is_symlink():
            for path in folder.rglob("*"):
                if path not in seen and path.is_file():
                    seen.add(path)
                    yield path


def read_manual_cache(accounts, *, root=None, paths=None, clock=time.monotonic):
    if not accounts or len(accounts) > 32 or len({a["biz"] for a in accounts}) != len(accounts):
        return {"status": "blocked", "reason": "invalid_cache_scope", "coverage_measured": False}
    root = APP_DATA if root is None else Path(root)
    reader = WechatCacheDiscoverer(root, accounts[0]["biz"])
    flow = WechatContentFlowCacheDiscoverer(root, accounts[0]["biz"])
    records, conflicts, files, batches, errors, complete, modified = {}, set(), 0, 0, 0, True, None
    start = clock()
    for path in passive_files(reader) if paths is None else paths:
        if files >= 128 or clock() - start > 20:
            complete = False
            break
        path = Path(path)
        try:
            path.relative_to(root)
            if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
                errors += 1
                continue
            before = path.stat()
            data = reader._read_bytes(path)
            files += 1
            after = path.stat()
            if data is None or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
                errors += 1
                continue
            modified = max(modified or 0, after.st_mtime)
            payloads = list(flow._decode_batches(data)) if (
                b"__batch_get_appmsg_data" in data or data.startswith(SNAPPY_PREFIX)) else []
            try:
                payloads.append(json.loads(data))
            except (ValueError, UnicodeError):
                pass
            batches += len(payloads)
            for payload in payloads:
                for record in cards(payload, accounts):
                    key = public_identity(record["url"])
                    old = records.get(key)
                    if old and any(old[field] and record[field] and old[field] != record[field] for field in ("title", "publish_time")):
                        conflicts.add(key)
                    records[key] = {field: record[field] or (old or {}).get(field) for field in record}
        except (OSError, ValueError):
            errors += 1
    for key in conflicts:
        records[key].update(title="", publish_time=None)
    return {"status": "read" if files else "blocked", "reason": None if files else "manual_homepage_refresh_required",
        "records": list(records.values()), "coverage_measured": bool(files), "source_mode": "cache",
        "key_available": None, "key_health": "not_used", "files_read": files, "decoded_batches": batches,
        "metadata_conflicts": len(conflicts), "cache_read_errors": errors, "scan_within_bounds": complete,
        "source_modified_epoch": modified, "freshness_evidence": "public_cache_file_mtime_only_not_publication_proof",
        "accounts": [{"biz": a["biz"], "schema_supported": bool(batches), "undecoded_rows": errors + len(conflicts),
                      "limit_reached": not complete, "all_publications_verified": False, "currently_followed": "unknown"}
                     for a in accounts], "all_publications_verified": False, "network_requests": 0, "wechat_input_events": 0}


if __name__ == "__main__":
    try:
        if sys.argv[1:] != ["--manual-cache-only"]:
            raise ValueError()
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError()
        result = read_manual_cache(json.loads(raw)["accounts"])
    except Exception:
        result = {"status": "blocked", "reason": "bounded_manual_cache_unavailable", "coverage_measured": False}
    print(json.dumps(result, ensure_ascii=False))
