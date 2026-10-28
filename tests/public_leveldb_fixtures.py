"""Synthetic LevelDB wire formats only; never use actual WeChat files."""
from wechat_crawler.public_leveldb_log import BLOCK_SIZE, masked_crc
from wechat_crawler.public_leveldb_table import MAGIC


def vint(value):
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    return bytes(result) + bytes([value])


def lp(value):
    return vint(len(value)) + value


def physical(kind, payload):
    return masked_crc(bytes([kind]) + payload).to_bytes(4, "little") + len(payload).to_bytes(2, "little") + bytes([kind]) + payload


def log_file(payloads):
    result = bytearray()
    for payload in payloads:
        first = True
        while first or payload:
            remaining = BLOCK_SIZE - len(result) % BLOCK_SIZE
            if remaining < 7:
                result.extend(b"\0" * remaining)
                remaining = BLOCK_SIZE
            length = min(len(payload), remaining - 7)
            last = length == len(payload)
            kind = (1 if last else 2) if first else (4 if last else 3)
            result.extend(physical(kind, payload[:length]))
            payload, first = payload[length:], False
    return bytes(result)


def batch(sequence, rows):
    data = sequence.to_bytes(8, "little") + len(rows).to_bytes(4, "little")
    for key, value in rows:
        data += bytes([int(value is not None)]) + lp(key)
        if value is not None:
            data += lp(value)
    return data


def raw_snappy(value):
    return vint(len(value)) + b"".join(b"\0" + bytes([b]) for b in value)


def block(rows):
    payload, previous = b"", b""
    for key, value in rows:
        shared = 0
        if previous:
            while shared < min(len(key), len(previous)) and key[shared] == previous[shared]:
                shared += 1
        payload += vint(shared) + vint(len(key) - shared) + vint(len(value)) + key[shared:] + value
        previous = key
    return payload + (0).to_bytes(4, "little") + (1).to_bytes(4, "little")


def table(rows, *, compressed=False):
    output = bytearray()

    def add(payload):
        offset = len(output)
        kind = int(compressed)
        payload = raw_snappy(payload) if compressed else payload
        output.extend(payload + bytes([kind]) + masked_crc(payload + bytes([kind])).to_bytes(4, "little"))
        return vint(offset) + vint(len(payload))

    encoded = [(key + ((seq << 8) | int(value is not None)).to_bytes(8, "little"), value or b"")
               for seq, key, value in rows]
    data_handle = add(block(encoded))
    meta_handle = add(block([]))
    index_handle = add(block([(b"index-separator", data_handle)]))
    footer = (meta_handle + index_handle).ljust(40, b"\0") + MAGIC.to_bytes(8, "little")
    return bytes(output) + footer


def object_key(number=1, *, index=1):
    return b"\0\x01\x01" + bytes([index]) + b"\x01" + vint(1) + number.to_bytes(2, "big")


def manifest(*, log=2, tables=(), extra=b""):
    edit = b"\x02" + vint(log) + b"\x09\0"
    for number, size in tables:
        edit += b"\x07\0" + vint(number) + vint(size) + lp(b"smallest-key") + lp(b"largest-key")
    return log_file([edit + extra])


def snapshot_mapping(folder, rows, *, table_rows=(), extra_names=()):
    data = table(table_rows) if table_rows else None
    mapping = {"CURRENT": b"MANIFEST-000001\n", "000002.log": log_file([batch(100, rows)]),
        "MANIFEST-000001": manifest(tables=[(3, len(data))] if data else [])}
    if data:
        mapping["000003.ldb"] = data
    mapping.update(extra_names)
    return {folder / name: content for name, content in mapping.items()}
