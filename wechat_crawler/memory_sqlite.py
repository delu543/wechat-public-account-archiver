"""Small read-only DB-API subset over SQLite's public C deserialize interface.

Python 3.9 lacks Connection.deserialize; macOS SQLite already supplies it.
No CPython layout introspection, plaintext files, extension loading or installs.
See https://sqlite.org/c3ref/deserialize.html and c_deserialize_freeonclose.html.
"""
from __future__ import annotations

import ctypes as C
import ctypes.util
import re
import sqlite3
import sys


class MemorySQLite:
    def __init__(self, data):
        library = "/usr/lib/libsqlite3.dylib" if sys.platform == "darwin" else ctypes.util.find_library("sqlite3")
        self.lib = C.CDLL(library)
        signatures = {
            "sqlite3_open": ([C.c_char_p, C.POINTER(C.c_void_p)], C.c_int),
            "sqlite3_close": ([C.c_void_p], C.c_int),
            "sqlite3_deserialize": ([C.c_void_p, C.c_char_p, C.c_void_p, C.c_int64, C.c_int64, C.c_uint], C.c_int),
            "sqlite3_prepare_v2": ([C.c_void_p, C.c_char_p, C.c_int, C.POINTER(C.c_void_p), C.POINTER(C.c_char_p)], C.c_int),
            "sqlite3_bind_text": ([C.c_void_p, C.c_int, C.c_char_p, C.c_int, C.c_void_p], C.c_int),
            "sqlite3_bind_int64": ([C.c_void_p, C.c_int, C.c_int64], C.c_int),
            "sqlite3_step": ([C.c_void_p], C.c_int),
            "sqlite3_finalize": ([C.c_void_p], C.c_int),
            "sqlite3_column_count": ([C.c_void_p], C.c_int),
            "sqlite3_column_type": ([C.c_void_p, C.c_int], C.c_int),
            "sqlite3_column_int64": ([C.c_void_p, C.c_int], C.c_int64),
            "sqlite3_column_double": ([C.c_void_p, C.c_int], C.c_double),
            "sqlite3_column_blob": ([C.c_void_p, C.c_int], C.c_void_p),
            "sqlite3_column_bytes": ([C.c_void_p, C.c_int], C.c_int),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.lib, name)
            function.argtypes, function.restype = args, result
        self.db, self.cursors = C.c_void_p(), set()
        # SQLite advises 20 guard bytes for an untrusted serialized image.
        self.buffer = C.create_string_buffer(bytes(data), len(data) + 20)
        if self.lib.sqlite3_open(b":memory:", C.byref(self.db)):
            self.close()
            raise sqlite3.OperationalError("memory SQLite open failed")
        if self.lib.sqlite3_deserialize(self.db, b"main", self.buffer, len(data), len(data), 4):
            self.close()
            raise sqlite3.OperationalError("memory SQLite deserialize failed")

    def execute(self, sql, parameters=()):
        # This restricted reader never accepts user SQL. Keep even accidental
        # ATTACH/DDL/write/pragma-off unreachable, including a second statement.
        allowed_pragma = re.fullmatch(r'PRAGMA (?:quick_check|temp_store=MEMORY|trusted_schema=OFF|query_only=ON|table_info\("Msg_[0-9a-f]{32}"\))', sql)
        if not self.db or ";" in sql or (not sql.startswith("SELECT ") and not allowed_pragma):
            raise sqlite3.OperationalError("read-only memory SQL required")
        statement, tail = C.c_void_p(), C.c_char_p()
        code = self.lib.sqlite3_prepare_v2(self.db, sql.encode(), -1, C.byref(statement), C.byref(tail))
        if code or (tail.value and tail.value.strip()):
            if statement:
                self.lib.sqlite3_finalize(statement)
            raise sqlite3.OperationalError("memory SQL preparation failed")
        cursor = _Cursor(self, statement)
        self.cursors.add(cursor)
        try:
            for index, value in enumerate(parameters, 1):
                if isinstance(value, int):
                    code = self.lib.sqlite3_bind_int64(statement, index, value)
                elif isinstance(value, str):
                    raw = value.encode()
                    code = self.lib.sqlite3_bind_text(statement, index, raw, len(raw), C.c_void_p(-1))
                else:
                    raise sqlite3.OperationalError("unsupported memory SQL parameter")
                if code:
                    raise sqlite3.OperationalError("memory SQL bind failed")
            if not self.lib.sqlite3_column_count(statement):
                cursor.fetchone()
            return cursor
        except Exception:
            cursor.close()
            raise

    def close(self):
        for cursor in list(getattr(self, "cursors", [])):
            cursor.close()
        if getattr(self, "db", None):
            self.lib.sqlite3_close(self.db)
            self.db = None
        if getattr(self, "buffer", None) is not None:
            C.memset(self.buffer, 0, len(self.buffer))
            self.buffer = None


class _Cursor:
    def __init__(self, connection, statement):
        self.connection, self.statement = connection, statement

    def fetchone(self):
        if not self.statement:
            return None
        lib = self.connection.lib
        code = lib.sqlite3_step(self.statement)
        if code == 101:  # SQLITE_DONE
            self.close()
            return None
        if code != 100:  # SQLITE_ROW
            self.close()
            raise sqlite3.OperationalError("memory SQL step failed")
        values = []
        for index in range(lib.sqlite3_column_count(self.statement)):
            kind = lib.sqlite3_column_type(self.statement, index)
            if kind == 1:
                value = lib.sqlite3_column_int64(self.statement, index)
            elif kind == 2:
                value = lib.sqlite3_column_double(self.statement, index)
            elif kind == 5:
                value = None
            else:
                length = lib.sqlite3_column_bytes(self.statement, index)
                if length > 4 * 1024 * 1024:
                    self.close()
                    raise sqlite3.OperationalError("memory field size limit")
                value = C.string_at(lib.sqlite3_column_blob(self.statement, index), length)
                if kind == 3:
                    value = value.decode("utf-8", errors="strict")
            values.append(value)
        return tuple(values)

    def fetchall(self):
        return list(self)

    def __iter__(self):
        try:
            while (row := self.fetchone()) is not None:
                yield row
        finally:
            self.close()

    def close(self):
        if self.statement:
            self.connection.lib.sqlite3_finalize(self.statement)
            self.statement = None
            self.connection.cursors.discard(self)
