"""Creating VistaDB 3.5 database files.

The writer builds a complete database in one pass: tables are declared, rows are
inserted, and ``close()`` lays out the B-trees, indexes, catalog and header.  Large
TEXT/NTEXT/IMAGE values are written to extent pages as they are inserted, so memory use
stays proportional to the in-row data.

File layout produced (page numbers):

    0           database header (with the fixed schema of the catalog table)
    1           catalog B-tree root
    2           catalog column map
    3 ...       out-of-row values, in insertion order
    then, per table: table header, column map, row B-tree, per index: header + B-tree
    then        remaining catalog B-tree pages
"""
from __future__ import annotations

import functools
import os
import struct
import uuid
import warnings
from dataclasses import dataclass, field
from datetime import datetime

from . import collation
from .reader import (CATALOG_CODECS, IX_PRIMARY, IX_UNIQUE, NODE_LEAF, NODE_ROOT, OBJ_COLUMN, OBJ_INDEX,
                     OBJ_TABLE, PAGE_DATABASE, PAGE_INDEX, PAGE_NODE, PAGE_TABLE)
from .rows import (BlobRef, ColumnCodec, blob_layout, encode_values, frame, from_python, inline_limit,
                   interior_header, leaf_header, null_bitmap)
from .types import BLOB_TYPES, BY_PUBLIC_NAME, FIXED_SIZE, STRING_TYPES, UNICODE_TYPES, VType

NODE_HEADER = 0x30
IX_CASE_SENSITIVE = 0x40
DB_FLAG_CASE_SENSITIVE = 0x40

SIGNATURE = ("VistaDB 3.5 (C) 2007-2009 VistaDB Software, Inc. All rights reserved. "
             "Provider: 3.5.84 Runtime: 4.0")

# Fixed schema of the catalog table as stored in the database header:
# (name, type, max length, code page, nullable)
CATALOG_SCHEMA = [
    ("typeId", VType.INT, 0, 0, False),
    ("foreignReference", VType.BIGINT, 0, 0, False),
    ("name", VType.NCHAR, 256, 1200, False),
    ("objectId", VType.BIGINT, 0, 0, True),
    ("reference", VType.BIGINT, 0, 0, True),
    ("options", VType.BIGINT, 0, 0, True),
    ("status", VType.BIT, 0, 0, True),
    ("scriptValue", VType.NCHAR, 256, 1200, True),
    ("description", VType.NCHAR, 256, 1200, True),
    ("extraBinary", VType.VARBINARY, 0xFFFF, 0, True),
    ("extraBlob", VType.IMAGE, 0, 0, True),
]
CATALOG_KEY_COLUMNS = 3  # the catalog is ordered by (typeId, foreignReference, name)

DEFAULT_CODE_PAGE = {VType.CHAR: 1252, VType.VARCHAR: 1252, VType.TEXT: 1252,
                     VType.NCHAR: 65001, VType.NVARCHAR: 65001, VType.NTEXT: 65001}


@dataclass
class ColumnDef:
    name: str
    type: VType | str
    max_length: int = 0
    allow_null: bool = True
    code_page: int | None = None

    def __post_init__(self):
        if isinstance(self.type, str):
            self.type = BY_PUBLIC_NAME[self.type.lower()]
        if self.code_page is None:
            self.code_page = DEFAULT_CODE_PAGE.get(self.type, 0)
        if self.type == VType.VARBINARY:
            self.max_length = 0xFFFF  # the engine ignores declared VARBINARY lengths
        if self.type in STRING_TYPES and not self.max_length:
            raise ValueError(f"column {self.name}: {self.type.name} needs a max_length")

    @property
    def codec(self) -> ColumnCodec:
        return ColumnCodec(VType(self.type), self.code_page, max_length=self.max_length)

    @property
    def options(self) -> int:
        stored_len = self.max_length if self.type in STRING_TYPES or self.type == VType.VARBINARY else 0
        flags = (1 if self.allow_null else 0) | (2 if self.type == VType.TIMESTAMP else 0)
        return (int(self.type) << 56) | (stored_len << 24) | (self.code_page << 8) | flags


@dataclass
class IndexDef:
    """An index; a column written as ``"DESC(name)"`` is sorted in descending order."""
    name: str
    columns: list[str]
    unique: bool = False
    primary: bool = False

    @property
    def keys(self) -> list[tuple[str, bool]]:
        out = []
        for c in self.columns:
            c = c.strip()
            if c.upper().startswith("DESC(") and c.endswith(")"):
                out.append((c[5:-1].strip(), True))
            else:
                out.append((c, False))
        return out

    @property
    def expression(self) -> str:
        return ";".join(f"DESC({n})" if d else n for n, d in self.keys)


@dataclass
class _Table:
    name: str
    columns: list[ColumnDef]
    indexes: list[IndexDef] = field(default_factory=list)
    rows: list = field(default_factory=list)   # (rowid, raws)
    header_offset: int = 0
    catalog_rowid: int = 0

    @property
    def codecs(self):
        return [c.codec for c in self.columns]


class DatabaseWriter:
    """Create a new VistaDB 3.5 database file.

    >>> with DatabaseWriter("profile.data", case_sensitive=True) as w:
    ...     w.create_table("Market", [ColumnDef("Key", "VarChar", 250, allow_null=False),
    ...                               ColumnDef("Value", "NText")],
    ...                    primary_key="Key")
    ...     w.insert("Market", ["1", '{ market_id = 1 }'])
    """

    def __init__(self, path, page_size: int = 1, lcid: int = 1033, case_sensitive: bool = False,
                 overwrite: bool = False, replica_id: uuid.UUID | None = None):
        if page_size not in (1, 2, 4, 8, 16):
            raise ValueError("page_size is in KiB: 1, 2, 4, 8 or 16")
        self.path = os.fspath(path)
        if os.path.exists(self.path) and not overwrite:
            raise FileExistsError(self.path)
        self.page_units = page_size
        self.ps = page_size * 1024
        self.lcid = lcid
        self.case_sensitive = case_sensitive
        self.replica_id = replica_id or uuid.uuid4()
        self._f = open(self.path, "w+b")
        self._pages = 0
        self._tables: dict[str, _Table] = {}
        self._catalog_rowid = 0
        self._closed = False
        self._warned_collation = False
        for _ in range(3):  # header, catalog root, catalog column map
            self._alloc(1)

    # --- context management ----------------------------------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, *exc):
        if exc_type is None:
            self.close()
        else:
            self._f.close()
            self._closed = True

    # --- schema ----------------------------------------------------------------------
    def create_table(self, name: str, columns, indexes=(), primary_key: str | list[str] | None = None,
                     primary_key_name: str | None = None):
        """Declare a table.  ``columns`` are ColumnDef objects or (name, type[, max_length[,
        allow_null]]) tuples.  ``primary_key`` adds a unique primary index (named
        ``primary_key_name``, default ``PK_<table>``)."""
        if self._key(name) in {self._key(t) for t in self._tables}:
            raise ValueError(f"table {name} already exists")
        cols = [c if isinstance(c, ColumnDef) else ColumnDef(*c) for c in columns]
        if len({self._key(c.name) for c in cols}) != len(cols):
            raise ValueError(f"table {name}: duplicate column names")
        ixs = [i if isinstance(i, IndexDef) else IndexDef(*i) for i in indexes]
        if primary_key is not None:
            keys = [primary_key] if isinstance(primary_key, str) else list(primary_key)
            ixs.append(IndexDef(primary_key_name or f"PK_{name}", keys, unique=True, primary=True))
        t = _Table(name, cols)
        for ix in ixs:
            for cname, _ in ix.keys:
                self._col_index(t, cname)
            t.indexes.append(ix)
        self._tables[name] = t
        return t

    def _key(self, s: str) -> str:
        return s if self.case_sensitive else s.lower()

    def _col_index(self, t: _Table, name: str) -> int:
        for i, c in enumerate(t.columns):
            if c.name.lower() == name.lower():
                return i
        raise KeyError(f"table {t.name} has no column {name}")

    def _table(self, name: str) -> _Table:
        for t in self._tables.values():
            if t.name.lower() == name.lower():
                return t
        raise KeyError(name)

    # --- rows ------------------------------------------------------------------------
    def insert(self, table: str, row):
        """Insert one row given as a sequence (in column order) or a dict by column name."""
        t = self._table(table)
        if isinstance(row, dict):
            lowered = {k.lower(): v for k, v in row.items()}
            unknown = set(lowered) - {c.name.lower() for c in t.columns}
            if unknown:
                raise KeyError(f"table {t.name} has no column(s) {sorted(unknown)}")
            values = [lowered.get(c.name.lower()) for c in t.columns]
        else:
            values = list(row)
            if len(values) != len(t.columns):
                raise ValueError(f"table {t.name} has {len(t.columns)} columns, got {len(values)} values")
        raws = []
        for v, c in zip(values, t.columns):
            if v is None:
                if not c.allow_null:
                    raise ValueError(f"{t.name}.{c.name} does not allow NULL")
                raws.append(None)
                continue
            raw = from_python(v, c.codec)
            if c.type in STRING_TYPES:
                n = len(raw.decode(c.codec.encoding))
                if n > c.max_length:
                    raise ValueError(f"{t.name}.{c.name}: value of {n} characters exceeds {c.max_length}")
            if c.type in BLOB_TYPES and len(raw) > inline_limit(self.ps):
                raw = self._write_blob(raw)
            raws.append(raw)
        t.rows.append((len(t.rows) + 1, raws))

    def insert_many(self, table: str, rows):
        for r in rows:
            self.insert(table, r)

    # --- pages -----------------------------------------------------------------------
    def _alloc(self, n: int) -> int:
        off = self._pages * self.ps
        self._pages += n
        return off

    def _write(self, offset: int, data: bytes):
        self._f.seek(offset)
        self._f.write(data)

    def _write_blob(self, data: bytes) -> BlobRef:
        pages, ppe, n = blob_layout(len(data), self.ps)
        start = self._alloc(pages)
        padded = data + b"\0" * (pages * self.ps - len(data))
        self._write(start, padded)
        return BlobRef(len(data), ppe, [start + i * ppe * self.ps for i in range(n)])

    # --- B-trees ---------------------------------------------------------------------
    def _build_tree(self, items, cols, key_cols: int, root_offset: int | None = None) -> int:
        """Bulk-load a B-tree.

        ``items``: (key, nulls, raws) in tree order; ``cols``: codecs of ``raws``;
        ``key_cols``: how many leading columns interior entries carry.
        Returns the byte offset of the root page.
        """
        limit = min(self.ps - NODE_HEADER, self.ps * 15 // 16)
        level = []  # entries of the level being built: (key, child, bitmap, raws)
        leaf = True
        cur = [(k, None, null_bitmap(n), r) for k, n, r in items]
        icols = cols[:key_cols]
        while True:
            pages = []  # each: list of entries
            sizes = 0
            page = []
            prev = None
            for e in cur:
                rec = self._encode_entry(e, cols if leaf else icols, prev, leaf, placeholder=True)
                if page and sizes + len(rec) > limit:
                    pages.append(page)
                    page, sizes, prev = [], 0, None
                    rec = self._encode_entry(e, cols if leaf else icols, None, leaf, placeholder=True)
                if len(rec) > limit:
                    raise ValueError("row too large for a page; use a larger page size")
                page.append(e)
                sizes += len(rec)
                prev = e
            pages.append(page)  # may be empty (empty table)
            if len(pages) == 1:
                off = root_offset if root_offset is not None else self._alloc(1)
                self._write_node(off, pages[0], cols if leaf else icols, leaf, NODE_ROOT | (NODE_LEAF if leaf else 0),
                                 -1, -1)
                return off
            offs = [self._alloc(1) for _ in pages]
            nxt_level = []
            for i, (pg, off) in enumerate(zip(pages, offs)):
                prv = offs[i - 1] if i else -1
                nxt = offs[i + 1] if i + 1 < len(offs) else -1
                self._write_node(off, pg, cols if leaf else icols, leaf, NODE_LEAF if leaf else 0, prv, nxt)
                k, _, bm, raws = pg[0]
                nxt_level.append((k, off, bm, raws[:key_cols]))
            cur = nxt_level
            leaf = False

    @staticmethod
    def _encode_entry(e, cols, prev, leaf: bool, placeholder: bool = False) -> bytes:
        key, child, bm, raws = e
        pk = prev[0] if prev is not None else None
        praws = prev[3] if prev is not None else None
        if leaf:
            h = leaf_header(key, bm, pk)
        else:
            h = interior_header(key, child, bm, pk)
        return frame(h, encode_values(raws[:len(cols)], cols, praws[:len(cols)] if praws is not None else None))

    def _write_node(self, off: int, entries, cols, leaf: bool, flags: int, prv: int, nxt: int):
        body = bytearray()
        prev = None
        for e in entries:
            body += self._encode_entry(e, cols, prev, leaf)
            prev = e
        pg = bytearray(self.ps)
        struct.pack_into("<IIIIHHHI", pg, 0, 0x0E, PAGE_NODE, 1, 0x1E8000, 0, flags, 0, len(entries))
        struct.pack_into("<qqHHH", pg, 0x1A, prv, nxt, len(body), len(body), len(entries))
        pg[self.ps - len(body):] = body
        self._write(off, bytes(pg))

    # --- sorting ---------------------------------------------------------------------
    def _value_key(self, raw, c: ColumnCodec):
        if raw is None:
            return (0,)
        t = c.type
        if t in STRING_TYPES or t in (VType.TEXT, VType.NTEXT) or c.plain_text:
            text = raw.decode(c.encoding)
            if not self._warned_collation and not collation.is_exact(text):
                self._warned_collation = True
                warnings.warn("index key with non-ASCII text: its position is approximated and may differ "
                              "from the engine's order (lookups of such keys could fail)", stacklevel=4)
            return (1, collation.sort_key(text, self.case_sensitive))
        if t in (VType.INT, VType.BIGINT, VType.TIMESTAMP):
            return (1, raw)
        if t in FIXED_SIZE and t not in (VType.UNIQUEIDENTIFIER,):
            from .rows import to_python
            return (1, to_python(raw, c))
        if t == VType.UNIQUEIDENTIFIER:
            # .NET Guid.CompareTo order: Data1, Data2, Data3, then bytes 8..15
            a, b, cc = struct.unpack_from("<IHH", raw)
            return (1, (a, b, cc, raw[8:]))
        if isinstance(raw, BlobRef):
            raise ValueError("out-of-row values cannot be index keys")
        return (1, bytes(raw))

    # --- finishing -------------------------------------------------------------------
    def close(self):
        if self._closed:
            return
        catalog = []  # (sort key, rowid, raws)
        for t in self._tables.values():
            self._finish_table(t, catalog)
        self._finish_catalog(catalog)
        self._f.close()
        self._closed = True

    def _catalog_row(self, catalog, type_id, fref, name, object_id, options=0, status=True,
                     script=None, extra_binary=None):
        self._catalog_rowid += 1
        enc = "utf-16-le"
        raws = [type_id, fref, name.encode(enc), object_id, -1, options, b"\x01" if status else b"\x00",
                script.encode(enc) if script is not None else None, None, extra_binary, None]
        key = (type_id, fref, collation.sort_key(name, self.case_sensitive))
        catalog.append((key, self._catalog_rowid, raws))

    def _finish_table(self, t: _Table, catalog):
        t.header_offset = self._alloc(1)
        aux = self._alloc(1)
        codecs = t.codecs
        root = self._build_tree([(rid, [r is None for r in raws], raws) for rid, raws in t.rows], codecs, 0)

        self._catalog_row(catalog, OBJ_TABLE, -1, t.name, t.header_offset)
        for i, c in enumerate(t.columns):
            self._catalog_row(catalog, OBJ_COLUMN, t.header_offset, c.name, i, c.options)

        for ix in t.indexes:
            hdr = self._alloc(1)
            idx = [self._col_index(t, n) for n, _ in ix.keys]
            desc = [d for _, d in ix.keys]
            kcodecs = [codecs[i] for i in idx]
            items = []
            for rid, raws in t.rows:
                kr = [raws[i] for i in idx]
                for i in idx:
                    if isinstance(raws[i], BlobRef):
                        raise ValueError(f"index {ix.name}: out-of-row values cannot be index keys")
                sk = tuple(self._value_key(v, c) for v, c in zip(kr, kcodecs))
                items.append((sk, rid, kr))
            if any(desc):
                items.sort(key=functools.cmp_to_key(lambda a, b: _cmp_keys(a, b, desc)))
            else:
                items.sort(key=lambda x: (x[0], x[1]))
            if ix.unique:
                for a, b in zip(items, items[1:]):
                    if a[0] == b[0]:
                        raise ValueError(f"duplicate key in unique index {ix.name} of {t.name}: "
                                         f"rows {a[1]} and {b[1]}")
            ixroot = self._build_tree([(rid, [v is None for v in kr], kr) for _, rid, kr in items],
                                      kcodecs, len(kcodecs))
            flags = (IX_UNIQUE if ix.unique else 0) | (IX_PRIMARY if ix.primary else 0) | \
                    (IX_CASE_SENSITIVE if self.case_sensitive else 0)
            h = bytearray(self.ps)
            struct.pack_into("<IIBIHII", h, 0, 0x0F, PAGE_INDEX, 1, 1, 0, 0x30, flags)
            struct.pack_into("<IHI", h, 0x17, 0, self.page_units, self.lcid)
            struct.pack_into("<q", h, 0x3A, ixroot)
            self._write(hdr, bytes(h))
            self._catalog_row(catalog, OBJ_INDEX, t.header_offset, ix.name, hdr, flags, status=False,
                              script=ix.expression,
                              extra_binary=bytes(b for i, d in zip(idx, desc) for b in (i, int(d))))

        # column map page
        self._write(aux, _column_map(len(t.columns)))
        # table header
        last = t.rows[-1][0] if t.rows else 0
        h = bytearray(self.ps)
        struct.pack_into("<IIqI", h, 0, 0x16, PAGE_TABLE, aux, 1)
        struct.pack_into("<HIII", h, 0x14, 0, 0x38, 1, len(t.rows))
        struct.pack_into("<HII", h, 0x22, self.page_units, self.lcid, last)
        h[0x35] = len(t.indexes)
        struct.pack_into("<qBIB", h, 0x41, root, 0, self.ps, 1)
        self._write(t.header_offset, bytes(h))

    def _finish_catalog(self, catalog):
        catalog.sort(key=lambda x: x[0])
        items = [(rid, [v is None for v in raws], raws) for _, rid, raws in catalog]
        root = self._build_tree(items, CATALOG_CODECS, CATALOG_KEY_COLUMNS, root_offset=self.ps)
        self._write(2 * self.ps, _column_map(len(CATALOG_SCHEMA), self.ps))
        self._write(0, self._database_header(len(catalog), self._catalog_rowid, root))
        # make sure the file covers every allocated page
        self._f.seek(0, os.SEEK_END)
        if self._f.tell() < self._pages * self.ps:
            self._f.truncate(self._pages * self.ps)

    def _database_header(self, rows: int, last_rowid: int, root: int) -> bytes:
        h = bytearray(self.ps)
        flags = 4 | (DB_FLAG_CASE_SENSITIVE if self.case_sensitive else 0)
        struct.pack_into("<IIqI", h, 0, 0x24, PAGE_DATABASE, 2 * self.ps, 1)
        h[0x14:0x24] = bytes.fromhex("00000040800001020408102040800001")
        struct.pack_into("<IIIHII", h, 0x24, 0x22C, flags, rows, self.page_units, self.lcid, last_rowid)
        struct.pack_into("<qBIB", h, 0x4F, root, 0, self.ps, 1)
        struct.pack_into("<I", h, 0x60, 9)
        struct.pack_into("<H", h, 0x72, len(CATALOG_SCHEMA))
        sig = SIGNATURE.encode("ascii")
        struct.pack_into("<H", h, 0x7C, len(sig))
        h[0x7E:0x7E + len(sig)] = sig
        h[0xE1:0xF1] = self.replica_id.bytes_le
        pos = 0x101
        for name, vtype, max_len, cp, nullable in CATALOG_SCHEMA:
            key = 1 if not nullable else 0
            struct.pack_into("<BHHBBBBH", h, pos, int(vtype) if vtype != VType.NCHAR else 2, max_len, cp,
                             0, 0, 1 if nullable else 0, 0, key)
            pos += 11
            nm = name.encode("utf-16-le")
            struct.pack_into("<H", h, pos, len(nm))
            h[pos + 2:pos + 2 + len(nm)] = nm
            pos += 2 + len(nm)
        return bytes(h)


def _cmp_keys(a, b, desc) -> int:
    for x, y, d in zip(a[0], b[0], desc):
        if x != y:
            r = -1 if x < y else 1
            return -r if d else r
    return (a[1] > b[1]) - (a[1] < b[1])


def _column_map(ncols: int, page_size: int | None = None) -> bytes:
    """Page listing the live columns of a table (one bit per column)."""
    mask = bytearray((ncols + 7) // 8)
    for j in range(ncols):
        mask[j >> 3] |= 1 << (j & 7)
    body = struct.pack("<IIBBxxx", 13 + len(mask), 0, 1, 1) + bytes(mask)
    return body + b"\0" * ((page_size or 1024) - len(body)) if page_size else body


def create_database(path, **kw) -> DatabaseWriter:
    return DatabaseWriter(path, **kw)
