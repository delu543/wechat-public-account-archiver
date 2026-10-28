"""Current Chromium IndexedDB object values, not journal byte-pattern matches.

Format references: Chromium IndexedDB coding_scheme.md and Google dfindexeddb
chromium/record.py KeyPrefix/ObjectStoreDataValue. No upstream code copied.
"""
from __future__ import annotations

from .chromium_indexeddb_values import MAX_VALUE_BYTES, unwrap_indexeddb_value
from .public_leveldb_log import MAX_ENTRIES, varint


def is_object_key(key):
    if not key:
        return False
    lead = key[0]
    lengths = (((lead >> 5) & 7) + 1, ((lead >> 2) & 7) + 1, (lead & 3) + 1)
    if len(key) <= 1 + sum(lengths):
        return False
    fields, pos = [], 1
    for width in lengths:
        fields.append(int.from_bytes(key[pos:pos + width], "little"))
        pos += width
    return fields[0] > 0 and fields[1] > 0 and fields[2] == 1


def inline_values(records):
    latest = {}
    for sequence, key, value in records:
        if not is_object_key(key):
            continue
        old = latest.get(key)
        if old is not None and sequence == old[0] and value != old[1]:
            raise ValueError("conflicting_leveldb_sequence")
        if old is None or sequence > old[0]:
            latest[key] = (sequence, value)
        if len(latest) > MAX_ENTRIES:
            raise ValueError("too_many_indexeddb_keys")
    values, external, deleted, total = [], 0, 0, 0
    for _, value in latest.values():
        if value is None:
            deleted += 1
            continue
        _, pos = varint(value)
        payload = value[pos:]
        if payload.startswith(b"\xff\x11\x01"):
            # Blob references are not inline V8 values. The blob reader remains
            # separate; never scan this key's older, already-replaced versions.
            size, offset = varint(payload, 3)
            _, offset = varint(payload, offset)
            if offset != len(payload) or size > MAX_VALUE_BYTES:
                raise ValueError("invalid_external_blob_reference")
            external += 1
            continue
        if not payload.startswith(b"\xff"):
            raise ValueError("unsupported_inline_value_envelope")
        logical = unwrap_indexeddb_value(payload)
        total += len(logical)
        if total > 40 * 1024 * 1024:
            raise ValueError("inline_decompression_budget")
        values.append(logical)
    return {"values": values, "current_object_keys": len(latest),
            "external_blob_references": external, "deleted_object_keys": deleted,
            "logical_bytes": total}
