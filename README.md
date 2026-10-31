# vistadb3

A pure-Python library that reads and writes **VistaDB 3.5** database files (`.vdb3`; the
format of `VistaDB.NET20.dll` 3.5.1.84, header "VistaDB 3.5 (C) 2007-2009 ... Provider:
3.5.8x"). It needs no .NET runtime and no VistaDB DLL.

It was written so that a Redbox kiosk backend can read the kiosk catalog (`profile.data`,
`cache.data`, `Local.data`) and build new `profile.data` files without shipping the
proprietary DLL. Nothing in it is Redbox-specific.

* **Read** any table: schema, indexes, rows (all 21 column types), large values stored
  outside the row, rows in index order.
* **Write** new databases: tables, primary keys and other indexes (unique, composite,
  descending), all column types. The real engine opens the files read-write, finds every
  row through every index, packs them, and modifies them.
* **CLI**: `info`, `dump` (JSON or JSONL), `build` (JSON/JSONL to a database), `copy`.

Python 3.9+, standard library only.

## Install

```
pip install .
```

## Command line

```
vistadb3 info profile.data                  # tables, columns, indexes, row counts
vistadb3 info profile.data --json
vistadb3 dump profile.data -o profile.json  # one JSON document
vistadb3 dump profile.data --jsonl -o dir/  # dir/schema.json + dir/<Table>.jsonl
vistadb3 dump profile.data -t ProductCatalog -o catalog.json
vistadb3 build dir/ -o new_profile.data     # from a JSON file or a JSONL directory
vistadb3 copy profile.data -o rebuilt.data  # read + write in one step
```

(`python -m vistadb3 ...` works without installing.)

In the JSON, each row is a list of values in column order. Values JSON cannot hold are
tagged: `{"$bin": base64}` (IMAGE, VARBINARY), `{"$dt": "2020-01-02T03:04:05.1230000"}`,
`{"$dec": "12.3456"}` (DECIMAL, MONEY), `{"$f": "1.5"}` (REAL, FLOAT) and
`{"$guid": "..."}`. The schema part looks like this, and is what `build` reads:

```json
{"page_size": 1, "lcid": 1033, "case_sensitive": true, "tables": [
  {"name": "Market",
   "columns": [{"name": "Key", "type": "VarChar", "max_length": 250, "allow_null": false},
               {"name": "Value", "type": "NText"}],
   "indexes": [{"name": "IX_Key", "expression": "Key", "primary": true, "unique": true}],
   "rows": [["1", "{ market_id = 1 , market_name = \"Denver, CO\" }"]]}]}
```

In a JSONL directory, `schema.json` holds the same without `rows`, and each
`<Table>.jsonl` holds one row (a JSON list, or an object keyed by column name) per line.

## Python API

```python
from vistadb3 import Database

with Database("profile.data") as db:
    print(db.page_size, db.lcid, db.case_sensitive)
    for t in db.tables.values():
        print(t.name, [(c.name, c.type_name, c.max_length) for c in t.columns],
              [(i.name, i.expression, i.primary) for i in t.indexes], db.row_count(t))
    for key, value in db.iter_rows("ProductCatalog"):     # storage (row id) order
        ...
    for name, data, type_, size in db.iter_rows("Cache"):  # IMAGE values are bytes
        ...
    for rowid, (key,) in db.iter_index("Market", "IX_Key"):  # index order
        ...
```

Values come back as `int`, `str`, `bytes`, `bool`, `float`, `decimal.Decimal`,
`datetime.datetime` or `uuid.UUID` (or `None`).

```python
from vistadb3.writer import DatabaseWriter, ColumnDef, IndexDef

with DatabaseWriter("profile.data", page_size=1, lcid=1033, case_sensitive=True) as w:
    kv = [ColumnDef("Key", "VarChar", 250, allow_null=False), ColumnDef("Value", "NText")]
    w.create_table("ProductCatalog", kv, primary_key="Key", primary_key_name="IX_Key")
    w.create_table("Cache", [ColumnDef("Name", "VarChar", 250, allow_null=False),
                             ColumnDef("Data", "Image"), ColumnDef("Type", "TinyInt"),
                             ColumnDef("Size", "Int")],
                   [IndexDef("IX_Key", ["Name"], unique=True, primary=True)])
    w.insert("ProductCatalog", ["1000", '{ product_id = 1000 , long_name = "..." }'])
    w.insert("Cache", {"Name": "5FAA...jpg", "Data": jpeg_bytes, "Type": 0, "Size": len(jpeg_bytes)})
# the file is complete when the writer is closed
```

* `page_size` is in KiB (1, 2, 4, 8 or 16); Redbox files use 1.
* Rows are kept in memory until `close()` (only in-row data: values larger than a
  quarter page are written to the file as they are inserted). Rebuilding the 330 MB
  kiosk `profile.data` (123k rows, 10.7k images) takes about 12 s.
* `close()` raises `ValueError` on a duplicate key in a unique index. `insert` checks
  NOT NULL and string lengths.
* Like the engine, the writer drops trailing spaces from CHAR/NCHAR/VARCHAR/NVARCHAR
  values (the engine strips them on every insert), and stores VARBINARY with length 65535.
* `vistadb3.cli.copy_database(src, dst)` and `build_from_spec(spec, dst)` are the CLI's
  building blocks.

## How correctness was established

The format was mapped from file bytes only. The DLL was used as a black box (through its
public API, from the small C# program in `oracle/`) to create databases that differ in one
thing at a time, and as an oracle to check results. No VistaDB code was decompiled or
copied.

* **Reading**: for the kiosk's real `profile.data` (330 MB), `cache.data` and `Local.data`
  the library's dump equals the DLL's dump: all tables, schemas, indexes and rows,
  including every image.
* **Writing**: `profile.data` rebuilt from the library's own reading of the real file is
  dumped by the DLL identically to the original; the DLL finds all 122,931 rows through
  `IX_Key` with `Find()` (what the kiosk's `DataCacheService` does), point lookups with
  `SET OPTIMIZATION ON; SELECT ... WHERE [Key] = ...` succeed, and the kiosk's
  `VistaDBDataAdapter` path returns the same rows.
* `tools/roundtrip.py` runs nine generated databases (all types with NULLs and boundary
  values, Unicode, 40,000-row three-level trees, page sizes 1/4/16, case-sensitive and
  insensitive, composite, nullable and descending indexes, 45 tables) through: write ->
  read back -> DLL dump and SQL dump -> DLL index verification -> DLL pack -> DLL
  insert/update/delete on the file -> library reads the DLL-modified file.
* The pytest suite (`tests/`) needs neither the DLL nor Redbox data: `tests/fixtures/engine`
  holds small databases the engine built plus the engine's dumps, and
  `tests/fixtures/written` holds files this library wrote plus the engine's dump and
  verification log for each. `tools/make_fixtures.py` regenerates both (needs Windows, .NET
  Framework 4 and the DLL; build the oracle with
  `csc /out:oracle.exe /r:VistaDB.NET20.dll oracle/Oracle.cs`).

```
pip install pytest
pytest
```

## Not supported

* Changing an existing file: the writer creates new databases only. (Reading files the
  engine has modified, including deleted and updated rows, is supported.)
* Encrypted (password-protected) databases.
* Full-text indexes, foreign keys, CHECK constraints, column defaults and identities,
  CLR procedures and triggers, table/column descriptions. The reader lists defaults and
  identities if present; the writer does not create them.
* Tables whose schema was altered after creation (dropped/renamed columns).
* Exact index order for text outside ASCII. Text keys are ordered like the engine's
  Windows word sort (LCID 1033) for ASCII; other characters are approximated (accents
  stripped, then code point), and the writer warns when such a key is indexed. Lookups of
  those keys through the engine could then miss. Redbox keys are numbers and file names.
* DATETIME is read with microsecond precision (the file holds 100 ns ticks; values written
  by SQL have millisecond precision, so nothing is lost for them).
* A row must fit in one page (values over a quarter page are stored outside the row, so
  this only matters for very wide rows).

## File format (as mapped)

Everything is little-endian. Offsets below are byte offsets in a page. "Constant" means
the value never varied in any sample; its meaning is unknown.

### Pages

The file is an array of pages of `page_size` KiB (`PAGE SIZE n` in `CREATE DATABASE`).
Pointers to pages are 64-bit byte offsets; -1 means none. The u32 at offset 4 is the page
type for structured pages: 5 database header, 3 table header, 6 index header, 2 B-tree
node, 0 column map. Pages holding out-of-row values have no header.

### Database header (page 0)

| Offset | Type | Value |
|---|---|---|
| 0x00 | u32 | 0x24 |
| 0x04 | u32 | 5 |
| 0x08 | i64 | catalog column map page |
| 0x10 | u32 | change counter (1 in packed files) |
| 0x14 | 16 bytes | constant `00 00 00 40 80 00 01 02 04 08 10 20 40 80 00 01` |
| 0x24 | u32 | constant 0x22C |
| 0x28 | u32 | flags: 0x04 always, 0x40 case-sensitive |
| 0x2C | u32 | catalog row count |
| 0x30 | u16 | page size in KiB |
| 0x32 | u32 | LCID |
| 0x36 | u32 | last catalog row id |
| 0x4F | i64 | catalog B-tree root |
| 0x58 | u32 | page size in bytes |
| 0x5C | u8 | constant 1 |
| 0x60 | u32 | constant 9 |
| 0x72 | u16 | number of catalog columns (11) |
| 0x7C | u16 + ASCII | signature, e.g. `VistaDB 3.5 (C) 2007-2009 ... Provider: 3.5.84 Runtime: 4.0` |
| 0xE1 | 16 bytes | replica GUID (random per database) |
| 0x101 | ... | schema of the catalog table: per column `[u8 type][u16 max length][u16 code page][u8 0][u8 0][u8 nullable][u8 0][u16 key column]`, then `[u16 byte length][UTF-16 name]` |

### Catalog

The catalog is a B-tree (rooted at 0x4F) of rows with these columns, ordered by
(`typeId`, `foreignReference`, `name`) with the database collation:

| Column | Type | Table row | Column row | Index row |
|---|---|---|---|---|
| typeId | Int | 1 | 3 | 2 |
| foreignReference | BigInt | -1 | table header offset | table header offset |
| name | NChar (UTF-16) | table name | column name | index name |
| objectId | BigInt | table header offset | ordinal | index header offset |
| reference | BigInt | -1 | -1 | -1 |
| options | BigInt | 0 | see below | 4 unique, 8 primary, 0x40 case-sensitive |
| status | Bit | 1 | 1 | 0 |
| scriptValue | NChar (UTF-16) | | | key expression: `Key`, `A;M`, `DESC(D)` |
| description | NChar (UTF-16) | | | |
| extraBinary | VarBinary | | | per key column `[u8 ordinal][u8 descending]` |
| extraBlob | Image | | | |

Column `options` = `type << 56 | max_length << 24 | code_page << 8 | flags` (flags: 1
nullable, 2 read-only/timestamp). Type codes are the `VistaDBType` values: Char 1, NChar 2,
VarChar 3, NVarChar 4, Text 5, NText 6, TinyInt 8, SmallInt 9, Int 10, BigInt 11, Real 12,
Float 13, Decimal 14, Money 15, SmallMoney 16, Bit 17, DateTime 19, Image 20,
VarBinary 21, UniqueIdentifier 22, SmallDateTime 23, Timestamp 24. Code pages: 1252 for
Char/VarChar/Text, 65001 (UTF-8) for the N types. Column defaults (typeId 5) and
identities (typeId 6) keep their expression in `scriptValue`.

### Table header

| Offset | Type | Value |
|---|---|---|
| 0x00 | u32 | 0x16 |
| 0x04 | u32 | 3 |
| 0x08 | i64 | column map page |
| 0x10 | u32 | change counter |
| 0x16 | u32 | constant 0x38 |
| 0x1A | u32 | constant 1 |
| 0x1E | u32 | row count |
| 0x22 | u16 | page size in KiB |
| 0x24 | u32 | LCID |
| 0x28 | u32 | last row id |
| 0x35 | u8 | number of indexes |
| 0x41 | i64 | row B-tree root |
| 0x4A | u32 | page size in bytes |
| 0x4E | u8 | 1 (2 after the engine packs the file) |

The **column map** page is `[u32 13 + n][u32 0][u8 1][u8 1][3 bytes 0][bitmap]` with one
set bit per column.

### Index header

`[u32 0x0F][u32 6][u8 1][u32 change counter][u16 0][u32 0x30][u32 flags as in the
catalog][u32 0][u16 page size KiB][u32 LCID]`, and the index B-tree root as i64 at 0x3A.

### B-tree nodes (type 2)

| Offset | Type | Value |
|---|---|---|
| 0x00 | u32 | 0x0E |
| 0x04 | u32 | 2 |
| 0x08 | u32 | constant 1 |
| 0x0C | u32 | constant 0x1E8000 |
| 0x12 | u16 | flags: 1 root, 2 leaf |
| 0x16 | u32 | entry count |
| 0x1A | i64 | previous node on the same level |
| 0x22 | i64 | next node on the same level |
| 0x2A | u16 | bytes used by entries (twice, at 0x2A and 0x2C) |
| 0x2E | u16 | entry count |

Entries fill the end of the page, in key order, each as
`[u32 4 + header length][header][u32 data length][data]`. vistadb3 fills nodes to at most
15/16 of a page; nodes the engine builds stay below that as well.

* **Row tree** (per table): key = row id. Leaf header `[i32 row id][u32 1][u8 0][null
  bitmap]`, data = the row's column values.
* **Index tree**: entries sorted by key columns, then row id. Leaf header as above with
  the row id of the indexed row and a null bitmap over the key columns; data = key values.
* **Interior entries** point at the child holding keys >= theirs: header
  `[i32 first row id below][i64 child][u32 0][null bitmap of that row]`; data = the key
  columns of that row (none for row trees, the key columns for indexes, the first three
  columns for the catalog).

The null bitmap has one bit per column (LSB first, `ceil(n/8)` bytes); NULL columns take
no space in the data.

**Delta compression.** The first entry on a page is stored as above. Every later entry
is stored relative to the entry before it on the same page:

* the header's row id becomes a 32-bit delta varint, and the u32 after it a single byte;
* Int values: 32-bit delta varint. First byte: bits 0-2 = number of extra bytes n
  (0-3), bit 3 = negative; magnitude = `(first >> 4) | extra << 4`; delta = magnitude, or
  `~magnitude` when negative. n = 4 means the delta follows as a raw int32.
* BigInt and Timestamp values: zigzag-encoded 64-bit delta, first byte bits 0-3 = extra
  bytes (0-7), value = `(first >> 4) | extra << 4`; 8 means a raw int64 follows.
* Char/NChar/VarChar/NVarChar values (and the catalog's text columns):
  `[u16 2 + suffix bytes][u16 shared prefix in UTF-16 code units][suffix bytes]`.
* other types are stored as in a first entry.

A value is only stored as a delta when the same column of the previous entry is not NULL.

### Column values (first entry)

| Type | Stored as |
|---|---|
| Int, BigInt, SmallInt, TinyInt | int32 / int64 / int16 / uint8 |
| Bit | one byte, 0 or 1 |
| Real, Float | IEEE float32 / float64 |
| Money, SmallMoney | int64 / int32 of value x 10,000 |
| Decimal | 96-bit magnitude, u8 scale, u8 sign (0x80 negative) |
| DateTime | int64 .NET ticks (100 ns since 0001-01-01) |
| SmallDateTime | uint32 minutes since 1900-01-01 |
| UniqueIdentifier | 16 bytes, .NET `Guid.ToByteArray()` order |
| Char, NChar, VarChar, NVarChar | `[u16 byte length][bytes]`, code page 1252 or UTF-8; Char/NChar are stored without padding |
| VarBinary | `[u16 length][bytes]` |
| Text, NText, Image | `[u16 length][payload]`, see below |

**Large values.** A Text/NText/Image payload is either the value itself followed by the
14-byte trailer `00 00 00 00 00 01 00 00 00 00 01 00 00 00`, used when the value is at
most page size / 4 bytes, or a list of extents: `[i64 offset] x n` followed by
`[u32 length][u8 0][u8 0][u16 0][u16 0][u16 pages per extent][u16 n]`. Image payloads
have one more zero byte at the end. Extents are runs of whole pages holding the raw bytes,
with `pages = ceil(length / page size)`, `pages per extent = pages // 4 + 1` and `n =
ceil(pages / pages per extent)`; the engine allocates them back to back.

### Collation

Text keys are compared like the engine's culture comparison for LCID 1033 (Windows word
sort): first by character weights (``space ! " # $ % & ( ) * , . / : ; ? @ [ \ ] ^ _ ` { | } ~ + < = >``,
then digits, then letters, case ignored), hyphen and apostrophe ignored at that level; then
lowercase before uppercase in case-sensitive databases; then hyphens/apostrophes (apostrophe
first, later positions first). Trailing spaces are not significant. See
`src/vistadb3/collation.py`.

## Layout of the code

| File | Contents |
|---|---|
| `src/vistadb3/types.py` | type codes |
| `src/vistadb3/codec.py` | delta varints |
| `src/vistadb3/rows.py` | entry and value encoding/decoding |
| `src/vistadb3/reader.py` | `Database` |
| `src/vistadb3/writer.py` | `DatabaseWriter` |
| `src/vistadb3/collation.py` | index order of text |
| `src/vistadb3/dumpjson.py`, `cli.py` | JSON form and command line |
| `oracle/Oracle.cs` | test oracle around the DLL (public API only) |
| `tools/` | comparison, differential round trips, fixture generation |
