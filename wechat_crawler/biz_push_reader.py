"""Isolated biz0 reader: target push metadata only, redacted child protocol."""
from __future__ import annotations

from datetime import datetime
import hashlib
import html
import io
import json
import re
import sqlite3
import sys
from urllib.parse import parse_qsl, urlencode, urlparse
from xml.etree import ElementTree

from .biz_push_helper import GateError
from .biz_push_snapshot import current_biz_connection

MAX_PAYLOAD = 4 * 1024 * 1024
MAX_ROWS = 20000


def public_identity(url, target_biz=None):
    parsed = urlparse(html.unescape(str(url or "")))
    if parsed.scheme not in ("http", "https") or parsed.netloc != "mp.weixin.qq.com" or parsed.path != "/s":
        return None
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if len({key for key, _ in pairs}) != len(pairs):
        return None
    params = dict(pairs)
    required = ("__biz", "mid", "idx", "sn")
    if not all(params.get(key) for key in required):
        return None
    if target_biz is not None and params["__biz"] != target_biz:
        return None
    if not params["mid"].isdigit() or not params["idx"].isdigit() or int(params["idx"]) < 1:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", params["sn"]):
        return None
    return tuple(params[key] for key in required)


def permanent_url(identity):
    return "https://mp.weixin.qq.com/s?" + urlencode(dict(zip(("__biz", "mid", "idx", "sn"), identity)))


def _text(value):
    if isinstance(value, bytes):
        if len(value) > MAX_PAYLOAD:
            raise ValueError("payload_limit")
        if value.startswith(b"\x28\xb5\x2f\xfd"):
            import zstandard
            if zstandard.get_frame_parameters(value).window_size > MAX_PAYLOAD:
                raise ValueError("compression_window_limit")
            with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(value)) as stream:
                value = stream.read(MAX_PAYLOAD + 1)
        if len(value) > MAX_PAYLOAD:
            raise ValueError("payload_limit")
        value = value.decode("utf-8", errors="strict")
    if not isinstance(value, str) or len(value.encode()) > MAX_PAYLOAD:
        raise ValueError("payload_limit")
    return value


def _items(value):
    text = _text(value).strip()
    if text.startswith("<"):
        if "<!DOCTYPE" in text.upper() or "<!ENTITY" in text.upper():
            raise ValueError("xml_entity_not_allowed")
        root = ElementTree.fromstring(text)
        # Actual public pushes use mmreader/category/item. Do not mine arbitrary
        # text/links from other service messages in the target table.
        items = root.findall(".//mmreader/category/item")
        if not items:
            appmsg = root.find(".//appmsg")
            if appmsg is not None and appmsg.find("url") is not None:
                items = [appmsg]
        for item in items[:100]:
            yield {child.tag: "".join(child.itertext()) for child in item}
        return
    if not text.startswith(("{", "[")):
        return
    pending, count = [(json.loads(text), 0)], 0
    while pending:
        obj, depth = pending.pop()
        count += 1
        if depth > 12 or count > 10000:
            raise ValueError("json_structure_limit")
        if isinstance(obj, dict):
            if obj.get("url") or obj.get("content_url"):
                yield obj
            pending.extend((v, depth + 1) for v in obj.values() if isinstance(v, (dict, list)))
        elif isinstance(obj, list):
            pending.extend((v, depth + 1) for v in obj)


def read_records(connection: sqlite3.Connection, accounts):
    connection.execute("PRAGMA trusted_schema=OFF")
    connection.execute("PRAGMA query_only=ON")
    records, coverage = {}, []
    for account in accounts:
        biz, username = account["biz"], account["wechat_id"]
        table = "Msg_" + hashlib.md5(username.encode()).hexdigest()
        present = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        note = {"biz": biz, "table_present": bool(present), "rows_examined": 0,
                "undecoded_rows": 0, "limit_reached": False, "schema_supported": False,
                "currently_followed": "unknown", "all_publications_verified": False}
        coverage.append(note)
        if not present or "VIRTUAL TABLE" in str(present[0]).upper():
            continue
        columns = {r[1].lower(): r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')}
        content = next((columns[c] for c in ("message_content", "content", "card_wraplist_buffer") if c in columns), None)
        timestamp = next((columns[c] for c in ("create_time", "message_create_time") if c in columns), None)
        if not content or not timestamp:
            continue
        # Column names originate from a bounded known signature; quote defensively.
        content, timestamp = content.replace('"', '""'), timestamp.replace('"', '""')
        note["schema_supported"] = True
        cursor = connection.execute(f'SELECT "{timestamp}", "{content}" FROM "{table}" ORDER BY "{timestamp}" DESC LIMIT ?', (MAX_ROWS + 1,))
        for received, payload in cursor:
            note["rows_examined"] += 1
            if note["rows_examined"] > MAX_ROWS:
                note["limit_reached"] = True
                break
            try:
                for item in _items(payload):
                    identity = public_identity(item.get("url") or item.get("content_url"), biz)
                    if not identity:
                        continue
                    published = item.get("pub_time") or item.get("publish_time") or item.get("create_time")
                    if published is not None:
                        published = str(published)
                        if not published.isdigit():
                            datetime.fromisoformat(published)
                    records[identity] = {"url": permanent_url(identity), "biz": biz,
                        "title": str(item.get("title") or "")[:500],
                        "publish_time": published, "received_time": str(received), "source": "biz_push"}
            except Exception:
                # Never return raw message payloads or parser exception text.
                note["undecoded_rows"] += 1
    return {"status": "read", "records": list(records.values()), "accounts": coverage,
            "coverage_measured": True, "scope": "biz_message_0_only",
            "chat_database_reads": 0, "persistent_plaintext_snapshots": 0,
            "network_requests": 0, "wechat_input_events": 0, "signed_companion": False}


def read_current(accounts):
    try:
        with current_biz_connection() as (connection, source):
            result = read_records(connection, accounts)
            result.update(source)
            return result
    except GateError as exc:
        return {"status": "blocked", "reason": str(exc), "coverage_measured": False}
    except Exception:
        return {"status": "blocked", "reason": "biz_read_unavailable", "coverage_measured": False}


def main():
    try:
        if sys.argv[1:] != ["--authorized-biz-only"]:
            raise ValueError()
        request = json.loads(sys.stdin.buffer.read(65537))
        accounts = request["accounts"]
        if not isinstance(accounts, list) or not 1 <= len(accounts) <= 32:
            raise ValueError()
        for account in accounts:
            if (not isinstance(account, dict) or set(account) != {"biz", "wechat_id"}
                    or not re.fullmatch(r"[A-Za-z0-9_+=/-]{4,128}", account["biz"])
                    or not re.fullmatch(r"gh_[A-Za-z0-9_]{1,80}", account["wechat_id"])):
                raise ValueError()
        if len({a["biz"] for a in accounts}) != len(accounts):
            raise ValueError()
    except Exception:
        print(json.dumps({"status": "blocked", "reason": "invalid_biz_only_request"}))
        return 2
    result = read_current(accounts)
    encoded = json.dumps(result, ensure_ascii=False)
    if len(encoded.encode()) > 16 * 1024 * 1024:
        result = {"status": "blocked", "reason": "biz_metadata_output_limit", "coverage_measured": False}
        encoded = json.dumps(result)
    print(encoded)
    return 0 if result["status"] == "read" else 2


if __name__ == "__main__":
    raise SystemExit(main())
