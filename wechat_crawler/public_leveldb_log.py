"""Read bounded LevelDB records without opening a database or changing its WAL.

Independent implementation of google/leveldb doc/log_format.md and
db/write_batch.cc (BSD-3-Clause specification); no upstream code is vendored.
Every physical CRC and complete logical record is validated before use.
"""
from __future__ import annotations

MAX_BYTES = 20 * 1024 * 1024
MAX_ENTRIES = 200000
BLOCK_SIZE = 32768


def _crc_table():
    result = []
    for number in range(256):
        for _ in range(8):
            number = (number >> 1) ^ (0x82F63B78 if number & 1 else 0)
        result.append(number)
    return tuple(result)


_CRC_TABLE = _crc_table()


def masked_crc(data):
    crc = 0xFFFFFFFF
    for byte in data:
        crc = _CRC_TABLE[(crc ^ byte) & 255] ^ (crc >> 8)
    crc ^= 0xFFFFFFFF
    return (((crc >> 15) | (crc << 17)) + 0xA282EAD8) & 0xFFFFFFFF


def varint(data, pos=0, *, bits=64):
    value = 0
    for shift in range(0, bits, 7):
        if pos >= len(data):
            raise ValueError("truncated_varint")
        byte = data[pos]
        pos += 1
        value |= (byte & 127) << shift
        if value >= 1 << bits:
            raise ValueError("oversize_varint")
        if not byte & 128:
            return value, pos
    raise ValueError("oversize_varint")


def prefixed(data, pos):
    length, pos = varint(data, pos, bits=32)
    if length > MAX_BYTES or length > len(data) - pos:
        raise ValueError("invalid_prefixed_length")
    return bytes(data[pos:pos + length]), pos + length


def logical_records(data):
    if len(data) > MAX_BYTES:
        raise ValueError("leveldb_file_too_large")
    records, fragments, fragmented_size, pos = [], None, 0, 0
    while pos < len(data):
        block_end = min(((pos // BLOCK_SIZE) + 1) * BLOCK_SIZE, len(data))
        remaining = block_end - pos
        if remaining < 7:
            if any(data[pos:block_end]):
                raise ValueError("truncated_log_header")
            pos = block_end
            continue
        checksum = int.from_bytes(data[pos:pos + 4], "little")
        length = int.from_bytes(data[pos + 4:pos + 6], "little")
        kind = data[pos + 6]
        if checksum == length == kind == 0:
            if any(data[pos:block_end]):
                raise ValueError("invalid_log_padding")
            pos = block_end
            continue
        if kind not in (1, 2, 3, 4) or length > remaining - 7:
            raise ValueError("invalid_log_fragment")
        payload = bytes(data[pos + 7:pos + 7 + length])
        if masked_crc(bytes([kind]) + payload) != checksum:
            raise ValueError("log_checksum_mismatch")
        pos += 7 + length
        if kind == 1:
            if fragments is not None:
                raise ValueError("unfinished_log_record")
            records.append(payload)
        elif kind == 2:
            if fragments is not None:
                raise ValueError("nested_log_fragment")
            fragments, fragmented_size = [payload], length
        else:
            if fragments is None:
                raise ValueError("orphan_log_fragment")
            fragmented_size += length
            if fragmented_size > MAX_BYTES:
                raise ValueError("logical_record_too_large")
            fragments.append(payload)
            if kind == 4:
                records.append(b"".join(fragments))
                fragments = None
        if len(records) > MAX_ENTRIES:
            raise ValueError("too_many_log_records")
    if fragments is not None:
        raise ValueError("truncated_log_record")
    return records


def write_batch(data):
    if len(data) < 12 or len(data) > MAX_BYTES:
        raise ValueError("invalid_write_batch")
    sequence = int.from_bytes(data[:8], "little")
    count = int.from_bytes(data[8:12], "little")
    if count > MAX_ENTRIES or sequence + count >= 1 << 56:
        raise ValueError("invalid_batch_count_or_sequence")
    result, pos = [], 12
    for offset in range(count):
        if pos >= len(data) or data[pos] not in (0, 1):
            raise ValueError("invalid_write_batch_tag")
        kind, pos = data[pos], pos + 1
        key, pos = prefixed(data, pos)
        value = None
        if kind == 1:
            value, pos = prefixed(data, pos)
        result.append((sequence + offset, key, value))
    if pos != len(data):
        raise ValueError("write_batch_count_or_trailing_data")
    return result
