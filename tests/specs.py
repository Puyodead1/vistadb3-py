"""Deterministic database specs used for the writer fixtures.

``tools/make_fixtures.py`` writes each spec with vistadb3, has the real engine dump and
verify the file, and commits file + dump; the tests rebuild the spec and expect the same
bytes, and expect vistadb3 to read the file exactly as the engine did.
"""
from __future__ import annotations

import random
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from vistadb3.writer import ColumnDef, DatabaseWriter, IndexDef

REPLICA_ID = uuid.UUID("0badc0de-0000-4000-8000-000000000001")
ASCII = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_.,{}=[]'\""
UNI = ASCII + "éüßñçø€中文日本語ж"


def _text(r, n, alphabet=ASCII):
    return "".join(r.choice(alphabet) for _ in range(n))


def kv():
    return [ColumnDef("Key", "VarChar", 250, allow_null=False), ColumnDef("Value", "NText")]


def spec_profile(r):
    """Shaped like a kiosk profile.data: Key/Value tables plus a Cache table of images."""
    keys = sorted({str(r.randint(1000, 99999)) for _ in range(400)})
    r.shuffle(keys)
    catalog = [[k, '{ product_id = %s , long_name = "%s" , starring = { "%s" } }'
                % (k, _text(r, r.randint(3, 40), UNI), _text(r, r.randint(0, 300)))] for k in keys]
    cache = []
    for _ in range(12):
        b = r.randbytes(r.choice([40, 256, 257, 900, 3000, 9000]))
        cache.append(["%040X.jpg" % r.getrandbits(160), b, 0, len(b)])
    return dict(page_size=1, case_sensitive=True, tables=[
        dict(name="Cache", columns=[ColumnDef("Name", "VarChar", 250, allow_null=False), ColumnDef("Data", "Image"),
                                    ColumnDef("Type", "TinyInt"), ColumnDef("Size", "Int")],
             rows=cache, indexes=[IndexDef("IX_Key", ["Name"], True, True)]),
        dict(name="Market", columns=kv(), rows=[[str(i), '{ market_id = %d , market_name = "M%d" }' % (i, i)] for i in range(1, 40)],
             indexes=[IndexDef("IX_Key", ["Key"], True, True)]),
        dict(name="ProductCatalog", columns=kv(), rows=catalog, indexes=[IndexDef("IX_Key", ["Key"], True, True)]),
        dict(name="Review", columns=kv(), rows=[], indexes=[IndexDef("IX_Key", ["Key"], True, True)]),
        dict(name="Version", columns=[ColumnDef("Key", "VarChar", 64, allow_null=False), ColumnDef("Value", "VarChar", 64)],
             rows=[["origin_machine", '{ name = "BUILD" }'], ["version", "{ 1 }"]],
             indexes=[IndexDef("IX_Key", ["Key"], True, True)]),
    ])


def spec_types(r):
    cols = [ColumnDef("I", "Int"), ColumnDef("BI", "BigInt"), ColumnDef("SI", "SmallInt"), ColumnDef("TI", "TinyInt"),
            ColumnDef("B", "Bit"), ColumnDef("DT", "DateTime"), ColumnDef("SDT", "SmallDateTime"), ColumnDef("R", "Real"),
            ColumnDef("F", "Float"), ColumnDef("D", "Decimal"), ColumnDef("M", "Money"), ColumnDef("SM", "SmallMoney"),
            ColumnDef("G", "UniqueIdentifier"), ColumnDef("C", "Char", 6), ColumnDef("NC", "NChar", 6),
            ColumnDef("VC", "VarChar", 40), ColumnDef("NV", "NVarChar", 40), ColumnDef("T", "Text"),
            ColumnDef("NT", "NText"), ColumnDef("IM", "Image"), ColumnDef("VB", "VarBinary")]
    rows = []
    for k in range(120):
        def m(v):
            return None if r.random() < 0.15 else v
        rows.append([
            m(r.choice([k, -k, r.randint(-2**31, 2**31 - 1), 2**30 + k])),
            m(r.choice([r.randint(-2**63, 2**63 - 1), r.randint(-1000, 1000)])),
            m(r.randint(-32768, 32767)), m(r.randint(0, 255)), m(r.random() < 0.5),
            m(datetime(1900, 1, 1) + timedelta(seconds=r.randint(0, 4_000_000_000), milliseconds=r.randint(0, 99) * 10)),
            m(datetime(1900, 1, 1) + timedelta(minutes=r.randint(0, 2**31 // 60))),
            m(r.randint(-10**6, 10**6) / 64), m(r.uniform(-1e12, 1e12)),
            m(Decimal(r.randint(-10**15, 10**15)).scaleb(-r.randint(0, 6))),
            m(Decimal(r.randint(-10**12, 10**12)).scaleb(-4)), m(Decimal(r.randint(-10**8, 10**8)).scaleb(-4)),
            m(uuid.UUID(int=r.getrandbits(128))),
            m(_text(r, r.randint(0, 6)).rstrip()), m(_text(r, r.randint(0, 6), UNI).rstrip()),
            m(_text(r, r.randint(0, 40)).rstrip()), m(_text(r, r.randint(0, 40), UNI).rstrip()),
            m(_text(r, r.choice([0, 5, 256, 257, 1500]))), m(_text(r, r.choice([0, 5, 80, 300]), UNI)),
            m(r.randbytes(r.choice([1, 10, 256, 257, 2100]))), m(r.randbytes(r.randint(0, 60))),
        ])
    return dict(page_size=1, case_sensitive=False, tables=[dict(name="AllTypes", columns=cols, rows=rows,
                                                               indexes=[IndexDef("IX_I", ["I"])])])


def spec_indexes(r):
    cols = [ColumnDef("Id", "Int", allow_null=False), ColumnDef("A", "VarChar", 20), ColumnDef("B", "Int"),
            ColumnDef("C", "NVarChar", 20), ColumnDef("D", "DateTime")]
    rows = [[i, r.choice([None, "a", "B", "b-c", "bc", "x", "co-op", "coop", "Co op"]), r.choice([None, 1, 2, -5]),
             r.choice([None, "é", "e", "E", "ü", "z"]), datetime(2001, 1, 1) + timedelta(hours=r.randint(0, 9999))]
            for i in range(700)]
    return dict(page_size=1, case_sensitive=True, tables=[dict(name="Idx", columns=cols, rows=rows, indexes=[
        IndexDef("PK_Idx", ["Id"], True, True), IndexDef("IX_A", ["A"]), IndexDef("IX_BA", ["B", "A"]),
        IndexDef("IX_C", ["C"]), IndexDef("IX_Ddesc", ["DESC(D)"])])])


def spec_many_tables(r):
    tables = []
    for i in range(40):
        name = r.choice("ABCabc") + "Table%02d_%s" % (i, _text(r, 4, "xyzXYZ"))
        rows = [[f"k{j}", f"v{j}"] for j in range(r.randint(0, 4))]
        tables.append(dict(name=name, columns=kv(), rows=rows, indexes=[IndexDef("IX_Key", ["Key"], True, True)]))
    return dict(page_size=1, case_sensitive=False, tables=tables)


def spec_pagesize4(r):
    rows = [[str(i), _text(r, r.choice([0, 50, 900, 1024, 1025, 1500]), UNI)] for i in range(120)]
    return dict(page_size=4, case_sensitive=False, tables=[
        dict(name="Big", columns=kv(), rows=rows, indexes=[IndexDef("IX_Key", ["Key"], True, True)])])


SPECS = {
    "profile": spec_profile,
    "types": spec_types,
    "indexes": spec_indexes,
    "many_tables": spec_many_tables,
    "pagesize4": spec_pagesize4,
}


def build(name: str):
    return SPECS[name](random.Random(name))


def write(spec, path):
    with DatabaseWriter(path, page_size=spec["page_size"], case_sensitive=spec["case_sensitive"], overwrite=True,
                        replica_id=REPLICA_ID) as w:
        for t in spec["tables"]:
            w.create_table(t["name"], t["columns"], t.get("indexes", ()))
            for row in t["rows"]:
                w.insert(t["name"], row)
