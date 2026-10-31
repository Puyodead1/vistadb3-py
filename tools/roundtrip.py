"""Differential tests of the writer against the real engine (needs tools/oracle.exe + the DLL).

For each generated spec:
  1. write the database with vistadb3,
  2. read it back with vistadb3 and compare with the inserted values,
  3. dump it with the DLL (DDA and SQL) and compare with vistadb3's reading,
  4. run the DLL "verify" (Find through every index, SQL lookups),
  5. pack a copy with the DLL and compare its dump,
  6. modify a copy with the DLL (insert/update/delete via SQL), then compare the DLL's
     dump of the result with vistadb3's reading and verify again.

    python tools/roundtrip.py --oracle E:/temp/vistadb-scratch/oracle.exe --work E:/temp/vistadb-scratch/rt [names...]
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare import compare  # noqa: E402
from vistadb3.dumpjson import database_json  # noqa: E402
from vistadb3.reader import Database  # noqa: E402
from vistadb3.writer import ColumnDef, DatabaseWriter, IndexDef  # noqa: E402

ORACLE = None


def oracle(*args, check=True):
    r = subprocess.run([ORACLE, *map(str, args)], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(f"oracle {args[0]} failed:\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
    return r.stdout


# --------------------------------------------------------------------------------------
# Specs

def rnd_text(r: random.Random, n: int, alphabet: str) -> str:
    return "".join(r.choice(alphabet) for _ in range(n))


ASCII = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 -_.,'\"{}=[]"
UNI = ASCII + "éüßñçø€中文日本語ж"


def spec_types(r):
    cols = [ColumnDef("I", "Int"), ColumnDef("BI", "BigInt"), ColumnDef("SI", "SmallInt"),
            ColumnDef("TI", "TinyInt"), ColumnDef("B", "Bit"), ColumnDef("DT", "DateTime"),
            ColumnDef("SDT", "SmallDateTime"), ColumnDef("R", "Real"), ColumnDef("F", "Float"),
            ColumnDef("D", "Decimal"), ColumnDef("M", "Money"), ColumnDef("SM", "SmallMoney"),
            ColumnDef("G", "UniqueIdentifier"), ColumnDef("C", "Char", 6), ColumnDef("NC", "NChar", 6),
            ColumnDef("VC", "VarChar", 40), ColumnDef("NV", "NVarChar", 40), ColumnDef("T", "Text"),
            ColumnDef("NT", "NText"), ColumnDef("IM", "Image"), ColumnDef("VB", "VarBinary", 100)]
    rows = []
    prev_i = 0
    for k in range(400):
        def maybe(v):
            return None if r.random() < 0.15 else v
        prev_i += r.choice([1, -1, 7, 300, -70000, 2**27, -(2**30), 0])
        prev_i = max(-2**31, min(2**31 - 1, prev_i))
        rows.append([
            maybe(prev_i if k % 5 else r.randint(-2**31, 2**31 - 1)),
            maybe(r.choice([r.randint(-2**63, 2**63 - 1), r.randint(-1000, 1000), 2**62 + k])),
            maybe(r.randint(-32768, 32767)), maybe(r.randint(0, 255)), maybe(r.random() < 0.5),
            maybe(datetime(1900, 1, 1) + timedelta(seconds=r.randint(0, 4_000_000_000), milliseconds=r.randint(0, 999) // 10 * 10)),
            maybe(datetime(1900, 1, 1) + timedelta(minutes=r.randint(0, 2**31 // 60))),
            maybe(float(r.randint(-10**6, 10**6)) / 64), maybe(r.uniform(-1e12, 1e12)),
            maybe(Decimal(r.randint(-10**15, 10**15)).scaleb(-r.randint(0, 6))),
            maybe(Decimal(r.randint(-10**12, 10**12)).scaleb(-4)), maybe(Decimal(r.randint(-10**8, 10**8)).scaleb(-4)),
            maybe(uuid.UUID(int=r.getrandbits(128))),
            maybe(rnd_text(r, r.randint(0, 6), ASCII).rstrip()), maybe(rnd_text(r, r.randint(0, 6), UNI).rstrip()),
            maybe(rnd_text(r, r.randint(0, 40), ASCII)), maybe(rnd_text(r, r.randint(0, 40), UNI)),
            maybe(rnd_text(r, r.choice([0, 5, 255, 256, 257, 3000]), ASCII)),
            maybe(rnd_text(r, r.choice([0, 5, 80, 130, 2000]), UNI)),
            maybe(r.randbytes(r.choice([1, 10, 256, 257, 1024, 1025, 5000, 70000]))),
            maybe(r.randbytes(r.randint(0, 100))),
        ])
    return dict(page_size=1, case_sensitive=False, tables=[dict(name="AllTypes", columns=cols, rows=rows,
                                                               indexes=[IndexDef("IX_I", ["I"])])])


def kv_columns():
    return [ColumnDef("Key", "VarChar", 250, allow_null=False), ColumnDef("Value", "NText")]


def spec_profile_like(r, n=3000, cs=True, page_size=1):
    keys = set()
    while len(keys) < n:
        keys.add(r.choice([str(r.randint(1, 10**6)), rnd_text(r, r.randint(1, 30), ASCII.replace("'", "")).strip() or "x"]))
    if not cs:
        seen = {}
        for k in list(keys):
            seen.setdefault(k.lower().rstrip(" "), k)
        keys = set(seen.values())
    rows = [[k, '{ id = %s , name = "%s" }' % (i, rnd_text(r, r.randint(0, 400), UNI))] for i, k in enumerate(sorted(keys))]
    r.shuffle(rows)
    cache_cols = [ColumnDef("Name", "VarChar", 250, allow_null=False), ColumnDef("Data", "Image"),
                  ColumnDef("Type", "TinyInt"), ColumnDef("Size", "Int")]
    cache_rows = []
    for i in range(60):
        b = r.randbytes(r.choice([10, 300, 4000, 25000, 60000]))
        cache_rows.append(["%040X.jpg" % r.getrandbits(160), b, 0, len(b)])
    return dict(page_size=page_size, case_sensitive=cs, tables=[
        dict(name="ProductCatalog", columns=kv_columns(), rows=rows, indexes=[IndexDef("IX_Key", ["Key"], True, True)]),
        dict(name="Cache", columns=cache_cols, rows=cache_rows, indexes=[IndexDef("IX_Key", ["Name"], True, True)]),
        dict(name="Review", columns=kv_columns(), rows=[], indexes=[IndexDef("IX_Key", ["Key"], True, True)]),
        dict(name="Version", columns=[ColumnDef("Key", "VarChar", 64, allow_null=False), ColumnDef("Value", "VarChar", 64)],
             rows=[["origin_machine", '{ name = "X" }'], ["version", "{ 1 }"]], indexes=[IndexDef("IX_Key", ["Key"], True, True)]),
    ])


def spec_deep(r):
    # enough rows for three B-tree levels with 1 KiB pages
    rows = [[i * 3, "row %06d %s" % (i, rnd_text(r, r.randint(0, 30), ASCII)), r.randint(0, 50)] for i in range(40000)]
    cols = [ColumnDef("Id", "Int", allow_null=False), ColumnDef("Name", "NVarChar", 80), ColumnDef("Grp", "Int")]
    return dict(page_size=1, case_sensitive=False, tables=[dict(name="Deep", columns=cols, rows=rows, indexes=[
        IndexDef("PK_Deep", ["Id"], True, True), IndexDef("IX_Name", ["Name"]), IndexDef("IX_Grp", ["Grp", "Name"])])])


def spec_many_tables(r):
    tables = []
    for i in range(45):
        name = rnd_text(r, 1, "ABCabc") + "Table%02d_%s" % (i, rnd_text(r, 5, "xyzXYZ"))
        rows = [[f"k{j}", f"v{j}"] for j in range(r.randint(0, 5))]
        tables.append(dict(name=name, columns=kv_columns(), rows=rows, indexes=[IndexDef("IX_Key", ["Key"], True, True)]))
    return dict(page_size=1, case_sensitive=False, tables=tables)


def spec_nullable_index(r):
    cols = [ColumnDef("A", "VarChar", 20), ColumnDef("B", "Int"), ColumnDef("C", "NVarChar", 20)]
    rows = [[r.choice([None, "a", "B", "b-c", "bc", "x"]), r.choice([None, 1, 2, -5]), r.choice([None, "é", "e", "E", "ü"])]
            for _ in range(300)]
    return dict(page_size=1, case_sensitive=True, tables=[dict(name="N", columns=cols, rows=rows, indexes=[
        IndexDef("IX_A", ["A"]), IndexDef("IX_BA", ["B", "A"]), IndexDef("IX_C", ["C"])])])


def spec_desc_index(r):
    cols = [ColumnDef("A", "VarChar", 20), ColumnDef("B", "Int"), ColumnDef("C", "DateTime")]
    rows = [[rnd_text(r, r.randint(0, 8), "abcAB- "), r.choice([None, r.randint(-99, 99)]),
             datetime(2000, 1, 1) + timedelta(hours=r.randint(0, 10**5))] for _ in range(500)]
    return dict(page_size=1, case_sensitive=False, tables=[dict(name="Dsc", columns=cols, rows=rows, indexes=[
        IndexDef("IX_Adesc", ["DESC(A)"]), IndexDef("IX_B_Cdesc", ["B", "DESC(C)"]), IndexDef("IX_C", ["C"])])])


SPECS = {
    "types": spec_types,
    "profile_cs": lambda r: spec_profile_like(r, cs=True),
    "profile_ci": lambda r: spec_profile_like(r, cs=False),
    "profile_ps4": lambda r: spec_profile_like(r, n=2000, page_size=4),
    "profile_ps16": lambda r: spec_profile_like(r, n=1000, page_size=16),
    "deep": spec_deep,
    "many_tables": spec_many_tables,
    "nullable_index": spec_nullable_index,
    "desc_index": spec_desc_index,
}


def write_spec(spec, path):
    with DatabaseWriter(path, page_size=spec["page_size"], case_sensitive=spec["case_sensitive"], overwrite=True,
                        replica_id=uuid.UUID(int=1)) as w:
        for t in spec["tables"]:
            w.create_table(t["name"], t["columns"], t.get("indexes", ()))
            for row in t["rows"]:
                w.insert(t["name"], row)


# --------------------------------------------------------------------------------------
# Checks

def normalize(v, col: ColumnDef):
    import struct
    if v is None:
        return None
    t = col.type.name
    if t in ("CHAR", "NCHAR"):
        return v.rstrip(" ").ljust(col.max_length)
    if t in ("VARCHAR", "NVARCHAR"):
        v = v.rstrip(" ")
    if t in ("VARCHAR", "TEXT"):
        return v.encode("cp1252", "replace").decode("cp1252")
    if t == "REAL":
        return struct.unpack("<f", struct.pack("<f", v))[0]
    if t in ("DECIMAL", "MONEY", "SMALLMONEY"):
        return Decimal(v)
    return v


def check_python_readback(spec, path):
    bad = 0
    with Database(path) as db:
        for t in spec["tables"]:
            got = list(db.iter_rows(t["name"]))
            if len(got) != len(t["rows"]):
                print(f"   readback {t['name']}: {len(got)} rows, expected {len(t['rows'])}")
                bad += 1
                continue
            for i, (a, b) in enumerate(zip(got, t["rows"])):
                exp = [normalize(v, c) for v, c in zip(b, t["columns"])]
                act = [normalize(v, c) if v is not None else None for v, c in zip(a, t["columns"])]
                for x, y, c in zip(act, exp, t["columns"]):
                    if x != y and not (isinstance(x, float) and isinstance(y, float) and math.isclose(x, y)):
                        if bad < 5:
                            print(f"   readback {t['name']} row {i} col {c.name}: {x!r} != {y!r}")
                        bad += 1
    return bad


def dll_json_equal(path_a_json, path_b_json):
    a = json.load(open(path_a_json, encoding="utf-8"))
    b = json.load(open(path_b_json, encoding="utf-8"))
    return a["tables"] == b["tables"]


def mutate_sql(spec, r):
    out = []
    for t in spec["tables"][:3]:
        names = [c.name for c in t["columns"]]
        if [c.type.name for c in t["columns"][:2]] == ["VARCHAR", "NTEXT"]:
            for i in range(200):
                out.append(f"INSERT INTO [{t['name']}] ([{names[0]}],[{names[1]}]) VALUES ('zz_new_{i:04d}', '{'x' * r.randint(0, 600)}')")
            out.append(f"UPDATE [{t['name']}] SET [{names[1]}] = 'updated' WHERE [{names[0]}] LIKE '1%'")
            out.append(f"DELETE FROM [{t['name']}] WHERE [{names[0]}] LIKE '2%'")
        elif t["columns"][0].type.name == "INT" and len(names) >= 2:
            out.append(f"DELETE FROM [{t['name']}] WHERE [{names[0]}] % 7 = 0")
            out.append(f"INSERT INTO [{t['name']}] ([{names[0]}]) VALUES (123456789)")
    return "\nGO\n".join(out) + "\n"


def run(name, work: Path, seed=1, mutate=True):
    r = random.Random(seed)
    spec = SPECS[name](r)
    work.mkdir(parents=True, exist_ok=True)
    db = work / f"{name}.vdb3"
    write_spec(spec, db)
    fails = 0
    print(f"== {name}: {db.stat().st_size // 1024} KiB")

    n = check_python_readback(spec, db)
    print(f"   python read-back: {'ok' if not n else f'{n} mismatches'}")
    fails += bool(n)

    oracle("dump", db, f"{db}.dll.json")
    n = compare(db, f"{db}.dll.json", verbose=False)
    print(f"   DLL DDA dump == vistadb3: {'ok' if not n else f'{n} mismatches'}")
    fails += bool(n)

    oracle("dump", db, f"{db}.dllsql.json", "sql")
    ok = dll_json_equal(f"{db}.dll.json", f"{db}.dllsql.json")
    print(f"   DLL SQL dump == DDA dump: {'ok' if ok else 'DIFFERENT'}")
    fails += not ok

    v = work / f"{name}.verify.vdb3"
    shutil.copy(db, v)
    out = oracle("verify", v, 200, check=False)
    ok = "VERIFY OK" in out
    print(f"   DLL verify: {'ok' if ok else 'FAILED'}")
    if not ok:
        print("\n".join("      " + l for l in out.splitlines() if "miss" in l or "FAIL" in l or "rows" in l)[:3000])
    fails += not ok

    p = work / f"{name}.packed.vdb3"
    shutil.copy(db, p)
    oracle("pack", p)
    oracle("dump", p, f"{p}.dll.json")
    ok = dll_json_equal(f"{db}.dll.json", f"{p}.dll.json")
    print(f"   DLL pack, dump unchanged: {'ok' if ok else 'DIFFERENT'}")
    fails += not ok

    if mutate:
        m = work / f"{name}.mutated.vdb3"
        shutil.copy(db, m)
        sql = work / f"{name}.mutate.sql"
        sql.write_text(mutate_sql(spec, r), encoding="utf-8")
        oracle("sql", m, sql)
        oracle("dump", m, f"{m}.dll.json")
        n = compare(m, f"{m}.dll.json", verbose=False)
        out = oracle("verify", m, 100, check=False)
        ok = not n and "VERIFY OK" in out
        print(f"   DLL modified file, vistadb3 reads it + verify: {'ok' if ok else f'FAILED ({n} mismatches)'}")
        fails += not ok
    return fails


def main():
    global ORACLE
    ap = argparse.ArgumentParser()
    ap.add_argument("--oracle", required=True)
    ap.add_argument("--work", required=True)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("names", nargs="*")
    a = ap.parse_args()
    ORACLE = a.oracle
    total = 0
    for name in a.names or SPECS:
        total += run(name, Path(a.work), a.seed)
    print("ALL OK" if not total else f"{total} FAILURES")
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
