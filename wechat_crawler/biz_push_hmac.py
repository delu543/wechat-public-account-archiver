"""Helper-internal SQLCipher4 first-page authentication; no key/file output.

Fixed profile from Zetetic's documented/default SQLCipher4 design: 4096-byte
pages, SHA512 MAC, 80-byte reserve, 2-round masked-salt HMAC KDF, LE page number.
This authenticates page 1 only, not every page or the completeness of articles.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import struct

from . import biz_push_helper as gate

PROFILE = "sqlcipher4-sha512-4096-le-v1"


def first_page_matches(page: bytes, data_key: bytes) -> bool:
    if len(page) != 4096 or len(data_key) != 32 or page[:16] == b"SQLite format 3\0":
        return False
    hmac_salt = bytes(value ^ 0x3A for value in page[:16])
    auth_key = hashlib.pbkdf2_hmac("sha512", data_key, hmac_salt, 2, dklen=32)
    authenticated = page[16:4032] + struct.pack("<I", 1)
    calculated = hmac.new(auth_key, authenticated, hashlib.sha512).digest()
    return hmac.compare_digest(calculated, page[4032:4096])


def read_first_page(db_base):
    """Read only fixed biz0 [0,4096), with no symlink or torn-read fallback."""
    path = db_base / gate.BIZ_REL
    gate._no_symlink_chain(path, gate.XROOT)
    before = gate._regular_private(path)
    if before.st_size < 4096 or before.st_size % 4096:
        raise gate.GateError("unsupported_biz_database_size")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    try:
        if gate._metadata_identity(os.fstat(descriptor)) != gate._metadata_identity(before):
            raise gate.GateError("biz_first_page_changed")
        page = os.pread(descriptor, 4096, 0)
        if (len(page) != 4096 or page != os.pread(descriptor, 4096, 0)
                or gate._metadata_identity(os.fstat(descriptor)) != gate._metadata_identity(before)
                or gate._metadata_identity(path.lstat()) != gate._metadata_identity(before)):
            raise gate.GateError("biz_first_page_changed")
        return page
    finally:
        os.close(descriptor)
