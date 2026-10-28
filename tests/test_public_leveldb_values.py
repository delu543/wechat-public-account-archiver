"""Offline byte-format regressions, not live database or collection tests."""
import pytest

from wechat_crawler.public_leveldb_log import logical_records, masked_crc, varint, write_batch
from wechat_crawler.public_leveldb_snapshot import manifest_files
from wechat_crawler.public_leveldb_table import block_entries, table_records
from tests.public_leveldb_fixtures import batch, block, log_file, manifest, physical, table, vint


@pytest.mark.parametrize("payload,crc", [(bytes(32), 0x8A9136AA), (b"\xff" * 32, 0x62A8AB43),
    (bytes(range(32)), 0x46DD794E), (bytes(reversed(range(32))), 0x113FDB5C)])
def test_crc_against_leveldb_rfc3720_vectors(payload, crc):
    expected = (((crc >> 15) | (crc << 17)) + 0xA282EAD8) & 0xFFFFFFFF
    assert masked_crc(payload) == expected


def test_fragmented_log_reassembles_crc_checked_records_across_blocks():
    payloads = [b"first", b"large" * 19000, b"last"]
    assert logical_records(log_file(payloads)) == payloads


def test_six_byte_padding_and_empty_full_record_are_valid():
    payload = b"a" * (32768 - 7 - 6)
    assert logical_records(log_file([payload, b"", b"last"])) == [payload, b"", b"last"]


@pytest.mark.parametrize("data", [physical(3, b"a"), physical(4, b"a"), physical(2, b"a"),
    physical(2, b"a") + physical(1, b"b"), physical(2, b"a") + physical(2, b"b"),
    b"\0" * 7 + b"not-padding", b"\1\2", physical(5, b"a")])
def test_partial_or_invalid_logs_fail_without_fragment_scanning(data):
    with pytest.raises(ValueError):
        logical_records(data)


def test_log_crc_is_not_optional():
    data = bytearray(log_file([b"some payload"]))
    data[-1] ^= 1
    with pytest.raises(ValueError, match="checksum"):
        logical_records(data)


def test_atomic_batch_put_delete_sequences():
    data = batch(345, [(b"key", b"first"), (b"key", None), (b"another", b"last")])
    assert write_batch(data) == [(345, b"key", b"first"), (346, b"key", None), (347, b"another", b"last")]


@pytest.mark.parametrize("data", [b"small", batch(1, []) + b"junk",
    batch(1, [(b"key", b"value")])[:-1], b"\0" * 8 + b"\1\0\0\0\3",
    b"\0" * 8 + (200001).to_bytes(4, "little")])
def test_batch_count_bounds_and_tag_validation(data):
    with pytest.raises(ValueError):
        write_batch(data)


@pytest.mark.parametrize("value", [0, 127, 128, 65537, 2**63, 2**64 - 1])
def test_varint_boundaries(value):
    data = vint(value)
    assert varint(data) == (value, len(data))


@pytest.mark.parametrize("data", [b"\x80", b"\xff" * 10, b"\xff" * 9 + b"\2"])
def test_invalid_varint_is_bounded(data):
    with pytest.raises(ValueError):
        varint(data)


@pytest.mark.parametrize("compressed", [False, True])
def test_sstable_prefix_restarts_internal_keys_and_snappy(compressed):
    rows = [(30, b"key-A", b"current"), (29, b"key-A", b"old"), (31, b"key-B", None)]
    assert table_records(table(rows, compressed=compressed)) == rows


@pytest.mark.parametrize("offset", [0, -1, -9])
def test_sstable_damage_is_explicit(offset):
    data = bytearray(table([(3, b"key", b"value")]))
    data[offset] ^= 32
    with pytest.raises(ValueError):
        table_records(data)


def test_table_restart_must_point_to_actual_unshared_entry():
    data = block([(b"key-A", b"one"), (b"key-B", b"two")])
    corrupted = data[:-8] + (1).to_bytes(4, "little") + data[-4:]
    with pytest.raises(ValueError, match="restart"):
        block_entries(corrupted)


def test_manifest_discards_obsolete_tables_and_updates_log():
    initial = manifest(log=2, tables=[(3, 100), (4, 200)])
    changed = log_file([b"\6\0" + vint(3) + b"\2" + vint(5)])
    assert manifest_files(initial + changed) == ({4: 200}, 5, 0)


def test_unknown_manifest_tag_does_not_guess_live_files():
    with pytest.raises(ValueError, match="manifest_tag"):
        manifest_files(manifest(extra=b"\x64"))
