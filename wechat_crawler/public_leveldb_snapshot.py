"""Select live public-cache files from CURRENT/MANIFEST, never obsolete SSTs.

Read callback must enforce allowed roots, regular files, stable stat snapshots
and aggregate IO/time budgets. This module never opens a LevelDB engine.
Manifest tags follow google/leveldb db/version_edit.cc (format-only reference).
"""
from __future__ import annotations

import re
from pathlib import Path

from .public_indexeddb_inline import inline_values
from .public_leveldb_log import MAX_ENTRIES, logical_records, prefixed, varint, write_batch
from .public_leveldb_table import table_records


def manifest_files(data):
    files, log, previous = {}, None, 0
    for edit in logical_records(data):
        pos, removed, added = 0, [], []
        while pos < len(edit):
            tag, pos = varint(edit, pos, bits=32)
            if tag == 1:
                _, pos = prefixed(edit, pos)
            elif tag in (2, 3, 4, 9):
                value, pos = varint(edit, pos)
                if tag == 2:
                    log = value
                elif tag == 9:
                    previous = value
            elif tag in (5, 6, 7):
                level, pos = varint(edit, pos, bits=32)
                if level >= 7:
                    raise ValueError("unsupported_manifest_level")
                if tag == 5:
                    _, pos = prefixed(edit, pos)
                    continue
                number, pos = varint(edit, pos)
                if tag == 6:
                    removed.append((level, number))
                else:
                    size, pos = varint(edit, pos)
                    smallest, pos = prefixed(edit, pos)
                    largest, pos = prefixed(edit, pos)
                    if len(smallest) < 8 or len(largest) < 8 or not number:
                        raise ValueError("invalid_manifest_internal_key")
                    added.append(((level, number), size))
            else:
                raise ValueError("unsupported_manifest_tag")
        for location in removed:
            files.pop(location, None)
        for location, size in added:
            files[location] = size
    if log is None or log <= 0 or len(files) > 64:
        raise ValueError("invalid_manifest_live_files")
    tables = {}
    for (_, number), size in files.items():
        if number in tables:
            raise ValueError("ambiguous_manifest_table")
        tables[number] = size
    return tables, log, previous


def snapshot_values(folder, read_file, *, names=None):
    folder = Path(folder)
    if folder.name != "weixin_xworker_0.indexeddb.leveldb" or folder.is_symlink():
        raise ValueError("not_public_indexeddb_namespace")
    current = read_file(folder / "CURRENT")
    if not re.fullmatch(rb"MANIFEST-[0-9]{6,20}\n", current):
        raise ValueError("invalid_public_current")
    manifest_path = folder / current[:-1].decode("ascii")
    manifest = read_file(manifest_path)
    tables, minimum_log, previous_log = manifest_files(manifest)
    if names is None:
        names = []
        for entry in folder.iterdir():
            if len(names) >= 512:
                raise ValueError("public_cache_directory_budget")
            names.append(entry.name)
    logs = sorted(name for name in names if re.fullmatch(r"[0-9]{6,20}\.log", name)
                  and (int(name[:-4]) >= minimum_log or int(name[:-4]) == previous_log))
    if not logs:
        raise ValueError("live_public_log_missing")
    records, size = [], 0

    def retain(values):
        nonlocal size
        for row in values:
            size += len(row[1]) + len(row[2] or b"")
            if len(records) >= MAX_ENTRIES or size > 40 * 1024 * 1024:
                raise ValueError("public_record_budget")
            records.append(row)

    for number, expected_size in sorted(tables.items()):
        name = f"{number:06d}.ldb"
        if name not in names:
            name = f"{number:06d}.sst"
        data = read_file(folder / name)
        if len(data) != expected_size:
            raise ValueError("manifest_table_size_mismatch")
        retain(table_records(data))
    for name in logs:
        for batch in logical_records(read_file(folder / name)):
            retain(write_batch(batch))
    # Rotation/compaction while reading cannot yield an accepted mixed window.
    if current != read_file(folder / "CURRENT") or manifest != read_file(manifest_path):
        raise ValueError("public_manifest_changed_during_read")
    result = inline_values(records)
    result.update(live_tables=len(tables), live_logs=len(logs), physical_entries=len(records))
    return result
