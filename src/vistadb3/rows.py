"""Encoding and decoding of row records stored on B-tree pages."""
from __future__ import annotations

import struct
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .codec import add32, add64, common_prefix, read_delta32, read_delta64, write_delta32, write_delta64
from .types import BLOB_TYPES, FIXED_SIZE, STRING_TYPES, UNICODE_TYPES, VType

# Trailer that follows the content of an inline TEXT/NTEXT/IMAGE value.  IMAGE values
# carry one extra zero byte after it.
INLINE_TRAILER = bytes.fromhex("0000000000010000000001000000")
BLOB_TRAILER_LEN = 14


@dataclass
class ColumnCodec:
    """What the row codec needs to know about a column."""
    type: VType
    code_page: int = 0
    # System (catalog) text columns are stored as plain prefix-compressed strings.
    plain_text: bool = False

    @property
    def encoding(self) -> str:
        return codepage_encoding(self.code_page, self.type)


def codepage_encoding(code_page: int, vtype: VType) -> str:
    if code_page == 65001:
        return "utf-8"
    if code_page == 1200:
        return "utf-16-le"
    if code_page in (0, None):
        return "utf-8" if vtype in UNICODE_TYPES else "cp1252"
    return f"cp{code_page}"


def char_width(enc: str) -> int:
    return 2 if enc == "utf-16-le" else 1


@dataclass
class BlobRef:
    """A TEXT/NTEXT/IMAGE value whose bytes live in extent pages outside the row."""
    length: int
    pages_per_extent: int
    extents: list[int]  # byte offsets in the file


@dataclass
class Record:
    key: int            # row id (data/index leaves) or first row id below (interior nodes)
    version: int        # second header field; 1 on leaf rows, 0 on interior entries
    flag: int
    nulls: list[bool]
    raw: list           # per column: decoded storage value (bytes for strings/blobs, ints, ...)
    child: int | None = None  # interior nodes: byte offset of the child page


# ---------------------------------------------------------------------------------------
# Raw page record framing: [u32 header_len][header...][u32 data_len][data...]

def split_records(page: bytes, count: int, used: int) -> list[tuple[bytes, bytes]]:
    pos = len(page) - used
    out = []
    for _ in range(count):
        hl = struct.unpack_from("<I", page, pos)[0]
        h = page[pos + 4:pos + hl]
        pos += hl
        dl = struct.unpack_from("<I", page, pos)[0]
        d = page[pos + 4:pos + 4 + dl]
        pos += 4 + dl
        out.append((h, d))
    if pos != len(page):
        raise ValueError("record framing does not fill page")
    return out


# ---------------------------------------------------------------------------------------
# Decoding

def decode_records(pairs, cols: list[ColumnCodec], interior: bool = False) -> list[Record]:
    out: list[Record] = []
    prev: Record | None = None
    nb = (len(cols) + 7) // 8
    for h, d in pairs:
        child = None
        if prev is None:
            key = struct.unpack_from("<i", h, 0)[0]
            p = 4
            if interior:
                child = struct.unpack_from("<q", h, p)[0]
                p += 8
            version = struct.unpack_from("<I", h, p)[0]
            p += 4
        else:
            dk, p = read_delta32(h, 0)
            key = add32(prev.key, dk)
            if interior:
                child = struct.unpack_from("<q", h, p)[0]
                p += 8
            version = h[p]
            p += 1
        flag = h[p]
        p += 1
        bm = h[p:p + nb] if not interior or len(h) > p else b"\0" * nb
        nulls = [bool(bm[j >> 3] >> (j & 7) & 1) if (j >> 3) < len(bm) else False for j in range(len(cols))]
        raw = _decode_values(d, cols, nulls, prev)
        rec = Record(key, version, flag, nulls, raw, child)
        out.append(rec)
        prev = rec
    return out


def _decode_values(d: bytes, cols: list[ColumnCodec], nulls, prev: Record | None) -> list:
    pos = 0
    vals = []
    for j, c in enumerate(cols):
        if nulls[j]:
            vals.append(None)
            continue
        pv = prev.raw[j] if prev is not None else None
        t = c.type
        if t == VType.INT:
            if pv is None:
                v = struct.unpack_from("<i", d, pos)[0]
                pos += 4
            else:
                dd, pos = read_delta32(d, pos)
                v = add32(pv, dd)
        elif t in (VType.BIGINT, VType.TIMESTAMP):
            if pv is None:
                v = struct.unpack_from("<q", d, pos)[0]
                pos += 8
            else:
                dd, pos = read_delta64(d, pos)
                v = add64(pv, dd)
        elif t in FIXED_SIZE:
            n = FIXED_SIZE[t]
            v = bytes(d[pos:pos + n])
            pos += n
        elif t in STRING_TYPES or c.plain_text:
            ln = struct.unpack_from("<H", d, pos)[0]
            pos += 2
            if pv is None:
                v = bytes(d[pos:pos + ln])
            else:
                pre = struct.unpack_from("<H", d, pos)[0] * char_width(c.encoding)
                v = pv[:pre] + bytes(d[pos + 2:pos + ln])
            pos += ln
        elif t == VType.VARBINARY:
            ln = struct.unpack_from("<H", d, pos)[0]
            v = bytes(d[pos + 2:pos + 2 + ln])
            pos += 2 + ln
        elif t in BLOB_TYPES:
            ln = struct.unpack_from("<H", d, pos)[0]
            v = _decode_blob(bytes(d[pos + 2:pos + 2 + ln]), t)
            pos += 2 + ln
        else:
            raise ValueError(f"unsupported column type {t!r}")
        vals.append(v)
    if pos != len(d):
        raise ValueError(f"row data length mismatch ({pos} != {len(d)})")
    return vals


def _decode_blob(payload: bytes, t: VType):
    if t == VType.IMAGE:
        payload = payload[:-1]
    body, tr = payload[:-BLOB_TRAILER_LEN], payload[-BLOB_TRAILER_LEN:]
    ext_len, _, inline, _, _, ppe, n = struct.unpack("<IBBHHHH", tr)
    if inline:
        return body
    extents = list(struct.unpack_from(f"<{n}q", body, 0))
    if len(body) != 8 * n:
        raise ValueError("bad blob descriptor")
    return BlobRef(ext_len, ppe, extents)


# ---------------------------------------------------------------------------------------
# Converting storage values to Python values

TICKS_EPOCH = datetime(1, 1, 1)


def to_python(raw, c: ColumnCodec):
    if raw is None:
        return None
    t = c.type
    if t in (VType.INT, VType.BIGINT, VType.TIMESTAMP):
        return raw
    if t in STRING_TYPES or t in (VType.TEXT, VType.NTEXT) or c.plain_text:
        return raw.decode(c.encoding)
    if t in (VType.IMAGE, VType.VARBINARY):
        return raw
    if t == VType.TINYINT:
        return raw[0]
    if t == VType.SMALLINT:
        return struct.unpack("<h", raw)[0]
    if t == VType.BIT:
        return raw[0] != 0
    if t == VType.REAL:
        return struct.unpack("<f", raw)[0]
    if t == VType.FLOAT:
        return struct.unpack("<d", raw)[0]
    if t in (VType.MONEY, VType.SMALLMONEY):
        n = struct.unpack("<q" if t == VType.MONEY else "<i", raw)[0]
        return Decimal(n).scaleb(-4)
    if t == VType.DECIMAL:
        mant = int.from_bytes(raw[:12], "little")
        scale, flags = raw[12], raw[13]
        v = Decimal(mant).scaleb(-scale)
        return -v if flags & 0x80 else v
    if t == VType.UNIQUEIDENTIFIER:
        return uuid.UUID(bytes_le=raw)
    if t == VType.DATETIME:
        ticks = struct.unpack("<q", raw)[0] & 0x3FFFFFFFFFFFFFFF
        return TICKS_EPOCH + timedelta(microseconds=ticks // 10)
    if t == VType.SMALLDATETIME:
        return decode_smalldatetime(raw)
    raise ValueError(f"unsupported column type {t!r}")


def decode_smalldatetime(raw: bytes):
    v = struct.unpack("<I", raw)[0]
    days, minutes = v >> 16, v & 0xFFFF
    return datetime(1900, 1, 1) + timedelta(days=days, minutes=minutes)
