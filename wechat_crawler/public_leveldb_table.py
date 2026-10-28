"""CRC-checked LevelDB SST blocks; format-only, no database engine required.

References: google/leveldb doc/table_format.md, table/format.cc and
table/block.cc. Independently implemented; no upstream code copied.
"""
from __future__ import annotations

from .chromium_indexeddb_values import SNAPPY_PREFIX, unwrap_indexeddb_value
from .public_leveldb_log import MAX_BYTES, MAX_ENTRIES, masked_crc, varint

MAGIC = 0xDB4775248B80FB57


def block_entries(data):
    if len(data) < 8 or len(data) > MAX_BYTES:
        raise ValueError("invalid_table_block_size")
    count = int.from_bytes(data[-4:], "little")
    if not count or count > (len(data) - 4) // 4:
        raise ValueError("invalid_restart_count")
    end = len(data) - 4 * (count + 1)
    restarts = [int.from_bytes(data[i:i + 4], "little") for i in range(end, len(data) - 4, 4)]
    if restarts[0] != 0 or restarts != sorted(set(restarts)) or any(p > end for p in restarts):
        raise ValueError("invalid_restart_offsets")
    result, offsets, previous, pos = [], {}, b"", 0
    while pos < end:
        offset = pos
        shared, pos = varint(data, pos, bits=32)
        unshared, pos = varint(data, pos, bits=32)
        value_length, pos = varint(data, pos, bits=32)
        if shared > len(previous) or unshared + value_length > end - pos:
            raise ValueError("invalid_prefix_compressed_entry")
        key = previous[:shared] + data[pos:pos + unshared]
        pos += unshared
        value = data[pos:pos + value_length]
        pos += value_length
        result.append((bytes(key), bytes(value)))
        offsets[offset] = shared
        previous = key
        if len(result) > MAX_ENTRIES:
            raise ValueError("too_many_table_entries")
    if pos != end or any(offsets.get(p) != 0 for p in restarts if end):
        raise ValueError("restart_not_at_unshared_entry")
    return result


def handle(data, pos=0):
    offset, pos = varint(data, pos)
    length, pos = varint(data, pos)
    return (offset, length), pos


def table_records(data):
    if len(data) < 48 or len(data) > MAX_BYTES or int.from_bytes(data[-8:], "little") != MAGIC:
        raise ValueError("invalid_sstable_footer")
    footer, footer_start = data[-48:-8], len(data) - 48
    meta, pos = handle(footer)
    index, pos = handle(footer, pos)
    if any(footer[pos:]):
        raise ValueError("invalid_sstable_footer_padding")
    total, occupied = 0, []

    def block(location):
        nonlocal total
        offset, length = location
        end = offset + length + 5
        if length > MAX_BYTES or end > footer_start:
            raise ValueError("invalid_sstable_block_bounds")
        if any(offset < b and a < end for a, b in occupied):
            raise ValueError("overlapping_sstable_blocks")
        occupied.append((offset, end))
        payload, kind = data[offset:offset + length], data[offset + length]
        checksum = int.from_bytes(data[offset + length + 1:end], "little")
        if masked_crc(payload + bytes([kind])) != checksum:
            raise ValueError("table_checksum_mismatch")
        if kind == 0:
            value = payload
        elif kind == 1:
            value = unwrap_indexeddb_value(SNAPPY_PREFIX + payload)
        else:
            raise ValueError("unsupported_table_compression")
        total += len(value)
        if total > 40 * 1024 * 1024:
            raise ValueError("table_decompression_budget")
        return block_entries(value)

    block(meta)
    index_entries = block(index)
    result = []
    for _, encoded in index_entries:
        location, consumed = handle(encoded)
        if consumed != len(encoded):
            raise ValueError("invalid_data_block_handle")
        for key, value in block(location):
            if len(key) < 8:
                raise ValueError("invalid_internal_key")
            suffix = int.from_bytes(key[-8:], "little")
            kind, sequence = suffix & 255, suffix >> 8
            if kind not in (0, 1) or (kind == 0 and value):
                raise ValueError("invalid_internal_value_type")
            result.append((sequence, key[:-8], value if kind else None))
            if len(result) > MAX_ENTRIES:
                raise ValueError("too_many_table_records")
    return result
