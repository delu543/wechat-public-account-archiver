"""Bounded, keyless decoding of Chromium's Snappy-wrapped IndexedDB values.

Format references (no third-party source copied or runtime installed):
Chromium b62e33f47930b144cb1eb047a4921949a38df024,
third_party/blink/renderer/modules/indexeddb/idb_value_wrapping.cc;
google/snappy format_description.txt (raw block, not framed Snappy).
Only an exact whole-value ff1102 prefix is supported, not arbitrary fragments.
"""
from __future__ import annotations

SNAPPY_PREFIX = b"\xff\x11\x02"
MAX_VALUE_BYTES = 20 * 1024 * 1024
MAX_DECODED_VALUE_BYTES = 40 * 1024 * 1024


def unwrap_indexeddb_value(data: bytes, *, max_output=MAX_VALUE_BYTES) -> bytes:
    if len(data) > MAX_VALUE_BYTES:
        raise ValueError("indexeddb_value_too_large")
    if not data.startswith(SNAPPY_PREFIX):
        return data
    # Retain the conservative default and compressed-input limit. A public
    # multi-account batch may opt into a larger, still hard-bounded output.
    if type(max_output) is not int or not 0 <= max_output <= MAX_DECODED_VALUE_BYTES:
        raise ValueError("invalid_output_limit")
    raw, pos, expected = memoryview(data)[3:], 0, 0

    def take(size):
        nonlocal pos
        if size < 0 or size > len(raw) - pos:
            raise ValueError("truncated_snappy_block")
        result = raw[pos:pos + size]
        pos += size
        return result

    for shift in range(0, 35, 7):
        byte = take(1)[0]
        if shift == 28 and byte > 15:
            raise ValueError("invalid_snappy_length")
        expected |= (byte & 127) << shift
        if not byte & 128:
            break
    else:
        raise ValueError("invalid_snappy_length")
    if expected > max_output:
        raise ValueError("indexeddb_value_too_large")
    out = bytearray()
    while pos < len(raw) and len(out) < expected:
        tag = take(1)[0]
        mode, length = tag & 3, tag >> 2
        if mode == 0:
            if length < 60:
                length += 1
            else:
                length = int.from_bytes(take(length - 59), "little") + 1
            if length > expected - len(out):
                raise ValueError("snappy_output_overflow")
            out.extend(take(length))
            continue
        if mode == 1:
            length = 4 + ((tag >> 2) & 7)
            offset = ((tag & 224) << 3) | take(1)[0]
        else:
            length += 1
            offset = int.from_bytes(take(2 if mode == 2 else 4), "little")
        if not offset or offset > len(out) or length > expected - len(out):
            raise ValueError("invalid_snappy_copy")
        # Backreferences can overlap. Repeat only the short required prefix.
        begin = len(out) - offset
        seed = bytes(out[begin:begin + min(offset, length)])
        out.extend((seed * ((length + len(seed) - 1) // len(seed)))[:length])
    if len(out) != expected or pos != len(raw):
        raise ValueError("snappy_length_or_trailing_data")
    return bytes(out)
