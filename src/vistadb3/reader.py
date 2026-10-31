"""Reading VistaDB 3.5 database files."""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from typing import Iterator

from .rows import BlobRef, ColumnCodec, Record, decode_records, split_records, to_python
from .types import BLOB_TYPES, PUBLIC_NAME, VType

# Page types (u32 at offset 4 of a page).
PAGE_FREE_MAP = 0
PAGE_NODE = 2
PAGE_TABLE = 3
PAGE_DATABASE = 5
PAGE_INDEX = 6

# Node page flags (u16 at offset 0x12).
NODE_ROOT = 1
NODE_LEAF = 2

# Catalog (system table) object kinds, column "typeId".
OBJ_TABLE = 1
OBJ_INDEX = 2
OBJ_COLUMN = 3
OBJ_DEFAULT = 5
OBJ_IDENTITY = 6

CATALOG_COLUMNS = [
    ("typeId", ColumnCodec(VType.INT)),
    ("foreignReference", ColumnCodec(VType.BIGINT)),
    ("name", ColumnCodec(VType.NCHAR, 1200, plain_text=True)),
    ("objectId", ColumnCodec(VType.BIGINT)),
    ("reference", ColumnCodec(VType.BIGINT)),
    ("options", ColumnCodec(VType.BIGINT)),
    ("status", ColumnCodec(VType.BIT)),
    ("scriptValue", ColumnCodec(VType.NCHAR, 1200, plain_text=True)),
    ("description", ColumnCodec(VType.NCHAR, 1200, plain_text=True)),
    ("extraBinary", ColumnCodec(VType.VARBINARY)),
    ("extraBlob", ColumnCodec(VType.IMAGE)),
]
CATALOG_CODECS = [c for _, c in CATALOG_COLUMNS]

# Index option bits (catalog "options" of an index entry).
IX_UNIQUE = 4
IX_PRIMARY = 8

# Default maximum lengths reported for the large-object types.
BLOB_MAX_LENGTH = {VType.TEXT: 134213632, VType.NTEXT: 67106816, VType.IMAGE: 134213632}


@dataclass
class Column:
    name: str
    type: VType
    max_length: int
    allow_null: bool
    code_page: int
    ordinal: int
    options: int
    read_only: bool = False
    description: str | None = None

    @property
    def codec(self) -> ColumnCodec:
        return ColumnCodec(self.type, self.code_page, max_length=self.max_length)

    @property
    def type_name(self) -> str:
        return PUBLIC_NAME[self.type]


@dataclass
class Index:
    name: str
    expression: str
    options: int
    header_offset: int

    @property
    def primary(self) -> bool:
        return bool(self.options & IX_PRIMARY)

    @property
    def unique(self) -> bool:
        return bool(self.options & IX_UNIQUE)


@dataclass
class Table:
    name: str
    header_offset: int
    columns: list[Column] = field(default_factory=list)
    indexes: list[Index] = field(default_factory=list)
    defaults: dict[str, str] = field(default_factory=dict)
    identities: dict[str, tuple[str, str]] = field(default_factory=dict)
    description: str | None = None


def decode_column_options(options: int):
    """Split a column's catalog "options" value into its parts."""
    options &= 0xFFFFFFFFFFFFFFFF
    vtype = VType((options >> 56) & 0xFF)
    max_length = (options >> 24) & 0xFFFFFFFF
    code_page = (options >> 8) & 0xFFFF
    flags = options & 0xFF
    return vtype, max_length, code_page, flags


class Database:
    """A VistaDB 3.5 database file opened for reading."""

    def __init__(self, path: str | os.PathLike):
        self.path = os.fspath(path)
        self._f = open(self.path, "rb")
        head = self._f.read(0x400)
        if struct.unpack_from("<I", head, 4)[0] != PAGE_DATABASE or b"VistaDB" not in head:
            raise ValueError(f"{self.path}: not a VistaDB 3.5 database")
        self.page_size = struct.unpack_from("<H", head, 0x30)[0] * 1024
        self.lcid = struct.unpack_from("<I", head, 0x32)[0]
        self.flags = struct.unpack_from("<I", head, 0x28)[0]
        self.catalog_root = struct.unpack_from("<q", head, 0x4F)[0]
        self.signature = head[0x7E:0x7E + struct.unpack_from("<H", head, 0x7C)[0]].decode("latin-1")
        self._tables: dict[str, Table] | None = None

    # --- context management ----------------------------------------------------------
    def close(self):
        self._f.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def case_sensitive(self) -> bool:
        return bool(self.flags & 0x40)

    # --- pages -----------------------------------------------------------------------
    def read_at(self, offset: int, n: int) -> bytes:
        self._f.seek(offset)
        b = self._f.read(n)
        if len(b) != n:
            raise ValueError(f"short read at {offset}")
        return b

    def page(self, offset: int) -> bytes:
        return self.read_at(offset, self.page_size)

    def leaf_pages(self, root: int) -> Iterator[bytes]:
        """Yield the leaf pages of the B-tree rooted at ``root`` in key order."""
        pg = self.page(root)
        while not struct.unpack_from("<H", pg, 0x12)[0] & NODE_LEAF:
            h, _ = _first_record(pg, struct.unpack_from("<H", pg, 0x2A)[0])
            pg = self.page(struct.unpack_from("<q", h, 4)[0])
        while True:
            yield pg
            nxt = struct.unpack_from("<q", pg, 0x22)[0]
            if nxt < 0:
                return
            pg = self.page(nxt)

    def records(self, root: int, cols: list[ColumnCodec]) -> Iterator[Record]:
        for pg in self.leaf_pages(root):
            used, count = struct.unpack_from("<HxxH", pg, 0x2A)
            yield from decode_records(split_records(pg, count, used), cols)

    def read_blob(self, ref: BlobRef) -> bytes:
        out = bytearray()
        chunk = ref.pages_per_extent * self.page_size
        for off in ref.extents:
            n = min(chunk, ref.length - len(out))
            out += self.read_at(off, n)
        if len(out) != ref.length:
            raise ValueError("blob extents shorter than blob length")
        return bytes(out)

    # --- catalog ---------------------------------------------------------------------
    def catalog(self) -> list[dict]:
        rows = []
        for r in self.records(self.catalog_root, CATALOG_CODECS):
            row = {"_rowid": r.key}
            for (name, codec), raw in zip(CATALOG_COLUMNS, r.raw):
                if isinstance(raw, BlobRef):
                    raw = self.read_blob(raw)
                row[name] = to_python(raw, codec)
            rows.append(row)
        return rows

    @property
    def tables(self) -> dict[str, Table]:
        if self._tables is None:
            self._tables = self._load_schema()
        return self._tables

    def _load_schema(self) -> dict[str, Table]:
        cat = self.catalog()
        by_off: dict[int, Table] = {}
        for r in cat:
            if r["typeId"] == OBJ_TABLE:
                by_off[r["objectId"]] = Table(r["name"], r["objectId"], description=r["description"])
        for r in cat:
            t = by_off.get(r["foreignReference"])
            if t is None:
                continue
            if r["typeId"] == OBJ_COLUMN:
                vtype, max_length, code_page, flags = decode_column_options(r["options"])
                if vtype in BLOB_TYPES:
                    max_length = BLOB_MAX_LENGTH[vtype]
                elif vtype not in (VType.CHAR, VType.NCHAR, VType.VARCHAR, VType.NVARCHAR, VType.VARBINARY):
                    from .types import FIXED_SIZE
                    max_length = FIXED_SIZE.get(vtype, max_length)
                t.columns.append(Column(r["name"], vtype, max_length, bool(flags & 1), code_page or 0,
                                        r["objectId"], r["options"], bool(flags & 2), r["description"]))
            elif r["typeId"] == OBJ_INDEX:
                t.indexes.append(Index(r["name"], r["scriptValue"] or "", r["options"], r["objectId"]))
            elif r["typeId"] == OBJ_DEFAULT:
                t.defaults[r["name"]] = r["scriptValue"]
            elif r["typeId"] == OBJ_IDENTITY:
                t.identities[r["name"]] = (r["scriptValue"], r["description"])
        for t in by_off.values():
            t.columns.sort(key=lambda c: c.ordinal)
            t.indexes.sort(key=lambda i: i.name)
        return {t.name: t for t in sorted(by_off.values(), key=lambda t: t.name)}

    def table(self, name: str) -> Table:
        for t in self.tables.values():
            if t.name.lower() == name.lower():
                return t
        raise KeyError(name)

    # --- rows ------------------------------------------------------------------------
    def table_root(self, t: Table) -> int:
        return struct.unpack_from("<q", self.page(t.header_offset), 0x41)[0]

    def row_count(self, t: Table | str) -> int:
        if isinstance(t, str):
            t = self.table(t)
        return struct.unpack_from("<I", self.page(t.header_offset), 0x1E)[0]

    def iter_rows(self, t: Table | str, with_rowid: bool = False) -> Iterator[tuple]:
        """Yield the rows of a table in storage (row id) order as tuples of Python values."""
        if isinstance(t, str):
            t = self.table(t)
        codecs = [c.codec for c in t.columns]
        for r in self.records(self.table_root(t), codecs):
            vals = []
            for raw, c in zip(r.raw, codecs):
                if isinstance(raw, BlobRef):
                    raw = self.read_blob(raw)
                vals.append(to_python(raw, c))
            yield (r.key, *vals) if with_rowid else tuple(vals)


def _first_record(pg: bytes, used: int):
    pos = len(pg) - used
    hl = struct.unpack_from("<I", pg, pos)[0]
    return pg[pos + 4:pos + hl], None


def open_database(path) -> Database:
    return Database(path)
