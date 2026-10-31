"""Low-level encodings used inside VistaDB 3.5 rows.

Rows on a B-tree page are stored one after another.  The first row of a page is stored
"raw"; every following row is stored relative to the row before it on the same page:

* INT columns hold a signed 32-bit delta in a nibble varint (see ``read_delta32``),
* BIGINT / TIMESTAMP columns hold a zigzag 64-bit delta in a nibble varint
  (see ``read_delta64``),
* CHAR/NCHAR/VARCHAR/NVARCHAR columns hold ``[u16 prefix][suffix]`` where ``prefix`` is
  the number of leading bytes shared with the previous row's value,
* every other type is stored exactly as in a raw row.

A column is only stored relative to the previous row when that row had a non-NULL value
in the same column; otherwise it is stored raw.
"""
from __future__ import annotations

import struct

MASK32 = 0xFFFFFFFF
MASK64 = 0xFFFFFFFFFFFFFFFF


def _s32(v: int) -> int:
    v &= MASK32
    return v - (1 << 32) if v & 0x80000000 else v


def _s64(v: int) -> int:
    v &= MASK64
    return v - (1 << 64) if v & (1 << 63) else v


# --- 32-bit delta varint -------------------------------------------------------------
# First byte: low 3 bits = number of extra bytes n (0..3), bit 3 = negative.
# Magnitude m = (first >> 4) | (extra bytes, little endian) << 4; delta = ~m if negative.
# n == 4 means the delta follows as a raw little-endian int32 (first byte is 0x04).

def read_delta32(buf, pos: int) -> tuple[int, int]:
    b0 = buf[pos]
    n = b0 & 7
    if n == 4:
        return struct.unpack_from("<i", buf, pos + 1)[0], pos + 5
    if n > 4:
        raise ValueError(f"bad int32 delta header 0x{b0:02x}")
    m = (b0 >> 4) | (int.from_bytes(buf[pos + 1:pos + 1 + n], "little") << 4)
    return (~m if b0 & 8 else m), pos + 1 + n


def write_delta32(delta: int) -> bytes:
    delta = _s32(delta)
    neg = delta < 0
    m = ~delta if neg else delta
    for n in range(4):
        if m < (16 << (8 * n)):
            v = (m << 4) | n | (8 if neg else 0)
            return v.to_bytes(n + 1, "little")
    return b"\x04" + struct.pack("<i", delta)


# --- 64-bit delta varint -------------------------------------------------------------
# First byte: low 4 bits = number of extra bytes n (0..7); value is zigzag encoded.
# n == 8 means the delta follows as a raw little-endian int64.

def read_delta64(buf, pos: int) -> tuple[int, int]:
    b0 = buf[pos]
    n = b0 & 15
    if n == 8:
        return struct.unpack_from("<q", buf, pos + 1)[0], pos + 9
    if n > 8:
        raise ValueError(f"bad int64 delta header 0x{b0:02x}")
    z = (b0 >> 4) | (int.from_bytes(buf[pos + 1:pos + 1 + n], "little") << 4)
    return (z >> 1) ^ -(z & 1), pos + 1 + n


def write_delta64(delta: int) -> bytes:
    delta = _s64(delta)
    z = ((delta << 1) ^ (delta >> 63)) & MASK64
    for n in range(8):
        if z < (16 << (8 * n)):
            return ((z << 4) | n).to_bytes(n + 1, "little")
    return b"\x08" + struct.pack("<q", delta)


def add32(a: int, d: int) -> int:
    return _s32(a + d)


def add64(a: int, d: int) -> int:
    return _s64(a + d)


def common_prefix(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b), 0xFFFF)
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i
