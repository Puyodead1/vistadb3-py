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
    max_length: int = 0

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


def prefix_bytes(prev: bytes, chars: int, enc: str) -> int:
    """Byte length of the first ``chars`` characters (UTF-16 code units) of ``prev``."""
    if enc == "utf-16-le":
        return 2 * chars
    if enc != "utf-8":
        return chars
    units = 0
    i = 0
    while units < chars and i < len(prev):
        b = prev[i]
        n = 1 if b < 0x80 else 2 if b < 0xE0 else 3 if b < 0xF0 else 4
        units += 2 if n == 4 else 1
        i += n
    return i


def prefix_chars(a: str, b: str) -> int:
    """Number of leading UTF-16 code units shared by two strings."""
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 2 if ord(x) > 0xFFFF else 1
    return n


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
    """Decode the records of one page.

    Leaf header:     raw  [i32 key][u32 version][u8 flag][null bitmap]
                     next [delta32 key][u8 version][u8 flag][null bitmap]
    Interior header: raw  [i32 key][i64 child][u32 version][null bitmap]
                     next [delta32 key][i64 child][u8 version][null bitmap]

    The null bitmap has one bit per column of the stored row (LSB first).  Interior
    entries copy the bitmap of the first row below them and carry the key columns
    (``cols``) of that row as their data.
    """
    out: list[Record] = []
    prev: Record | None = None
    for h, d in pairs:
        child = None
        flag = 0
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
        if not interior:
            flag = h[p]
            p += 1
        bm = h[p:]
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
                pre = prefix_bytes(pv, struct.unpack_from("<H", d, pos)[0], c.encoding)
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
        s = raw.decode(c.encoding)
        if t in (VType.CHAR, VType.NCHAR) and not c.plain_text and len(s) < c.max_length:
            s = s.ljust(c.max_length)  # fixed-length strings are stored trimmed
        return s
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
        return _strip_zeros(Decimal(n).scaleb(-4))
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


SMALLDATETIME_EPOCH = datetime(1900, 1, 1)


def _strip_zeros(d: Decimal) -> Decimal:
    if d == d.to_integral_value():
        return d.quantize(Decimal(1))
    return d.normalize()


def decode_smalldatetime(raw: bytes):
    # minutes since 1900-01-01
    return SMALLDATETIME_EPOCH + timedelta(minutes=struct.unpack("<I", raw)[0])


# ---------------------------------------------------------------------------------------
# Encoding

def null_bitmap(nulls) -> bytes:
    bm = bytearray((len(nulls) + 7) // 8)
    for j, n in enumerate(nulls):
        if n:
            bm[j >> 3] |= 1 << (j & 7)
    return bytes(bm)


def leaf_header(key: int, bitmap: bytes, prev_key: int | None) -> bytes:
    if prev_key is None:
        return struct.pack("<iIB", key, 1, 0) + bitmap
    return write_delta32(key - prev_key) + b"\x01\x00" + bitmap


def interior_header(key: int, child: int, bitmap: bytes, prev_key: int | None) -> bytes:
    if prev_key is None:
        return struct.pack("<iqI", key, child, 0) + bitmap
    return write_delta32(key - prev_key) + struct.pack("<qB", child, 0) + bitmap


def frame(header: bytes, data: bytes) -> bytes:
    return struct.pack("<I", 4 + len(header)) + header + struct.pack("<I", len(data)) + data


def encode_values(raws: list, cols: list[ColumnCodec], prev: list | None) -> bytes:
    """Encode the data part of a record; ``prev`` is the previous record on the page."""
    out = bytearray()
    for j, (v, c) in enumerate(zip(raws, cols)):
        if v is None:
            continue
        pv = prev[j] if prev is not None else None
        t = c.type
        if t == VType.INT:
            out += struct.pack("<i", v) if pv is None else write_delta32(v - pv)
        elif t in (VType.BIGINT, VType.TIMESTAMP):
            out += struct.pack("<q", v) if pv is None else write_delta64(v - pv)
        elif t in FIXED_SIZE:
            out += v
        elif t in STRING_TYPES or c.plain_text:
            if pv is None:
                out += struct.pack("<H", len(v)) + v
            else:
                enc = c.encoding
                chars = prefix_chars(pv.decode(enc), v.decode(enc))
                chars = min(chars, 0xFFFF)
                suffix = v[prefix_bytes(v, chars, enc):]
                out += struct.pack("<HH", 2 + len(suffix), chars) + suffix
        elif t == VType.VARBINARY:
            out += struct.pack("<H", len(v)) + v
        elif t in BLOB_TYPES:
            payload = encode_blob(v, t)
            out += struct.pack("<H", len(payload)) + payload
        else:
            raise ValueError(f"unsupported column type {t!r}")
    return bytes(out)


def encode_blob(v, t: VType) -> bytes:
    if isinstance(v, BlobRef):
        body = struct.pack(f"<{len(v.extents)}q", *v.extents)
        body += struct.pack("<IBBHHHH", v.length, 0, 0, 0, 0, v.pages_per_extent, len(v.extents))
    else:
        body = bytes(v) + INLINE_TRAILER
    return body + b"\0" if t == VType.IMAGE else body


def blob_layout(length: int, page_size: int) -> tuple[int, int, int]:
    """(pages, pages_per_extent, extents) the engine uses for an out-of-row value."""
    pages = -(-length // page_size)
    ppe = pages // 4 + 1
    return pages, ppe, -(-pages // ppe)


def inline_limit(page_size: int) -> int:
    """Largest TEXT/NTEXT/IMAGE value (in bytes) that is kept inside the row."""
    return page_size // 4


# ---------------------------------------------------------------------------------------
# Python values to storage values

def from_python(v, c: ColumnCodec):
    """Convert a Python value to the storage form used by ``encode_values``.

    TEXT/NTEXT/IMAGE values are returned as bytes; the writer decides whether they stay
    in the row or move to extent pages.
    """
    if v is None:
        return None
    t = c.type
    if t == VType.INT:
        v = int(v)
        if not -2**31 <= v < 2**31:
            raise OverflowError(f"{v} does not fit INT")
        return v
    if t in (VType.BIGINT, VType.TIMESTAMP):
        v = int(v)
        if not -2**63 <= v < 2**63:
            raise OverflowError(f"{v} does not fit BIGINT")
        return v
    if t in STRING_TYPES or t in (VType.TEXT, VType.NTEXT) or c.plain_text:
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
        s = str(v)
        if t in STRING_TYPES and not c.plain_text:
            s = s.rstrip(" ")  # the engine never stores trailing spaces in these types
        return s.encode(c.encoding, "replace")
    if t in (VType.IMAGE, VType.VARBINARY):
        return bytes(v)
    if t == VType.TINYINT:
        return bytes([int(v)])
    if t == VType.SMALLINT:
        return struct.pack("<h", int(v))
    if t == VType.BIT:
        return b"\x01" if v else b"\x00"
    if t == VType.REAL:
        return struct.pack("<f", float(v))
    if t == VType.FLOAT:
        return struct.pack("<d", float(v))
    if t == VType.MONEY:
        return struct.pack("<q", int((Decimal(str(v)) * 10000).to_integral_value()))
    if t == VType.SMALLMONEY:
        return struct.pack("<i", int((Decimal(str(v)) * 10000).to_integral_value()))
    if t == VType.DECIMAL:
        return encode_decimal(Decimal(str(v)) if not isinstance(v, Decimal) else v)
    if t == VType.UNIQUEIDENTIFIER:
        return (v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))).bytes_le
    if t == VType.DATETIME:
        d = v - TICKS_EPOCH
        ticks = (d.days * 86400 + d.seconds) * 10_000_000 + d.microseconds * 10
        return struct.pack("<q", ticks)
    if t == VType.SMALLDATETIME:
        d = v - SMALLDATETIME_EPOCH
        return struct.pack("<I", d.days * 1440 + d.seconds // 60)
    raise ValueError(f"unsupported column type {t!r}")


def encode_decimal(d: Decimal) -> bytes:
    sign, digits, exp = d.as_tuple()
    mant = int("".join(map(str, digits)) or "0")
    scale = -exp if exp < 0 else 0
    if exp > 0:
        mant *= 10 ** exp
    if scale > 28 or mant >= 1 << 96:
        raise OverflowError(f"{d} does not fit DECIMAL")
    return mant.to_bytes(12, "little") + bytes([scale, 0x80 if sign else 0])
