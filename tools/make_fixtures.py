"""Regenerate tests/fixtures with the real VistaDB engine (Windows + .NET Framework + the DLL).

    python tools/make_fixtures.py --oracle path/to/oracle.exe

tests/fixtures/engine/   databases created by the engine through SQL, with the engine's
                         JSON dump of each (expected reader output)
tests/fixtures/written/  databases written by vistadb3 from tests/specs.py, with the
                         engine's dump and the output of the engine's verify run
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import specs  # noqa: E402

FIX = ROOT / "tests" / "fixtures"
ORACLE = None


def oracle(*args):
    r = subprocess.run([ORACLE, *map(str, args)], capture_output=True, text=True)
    if r.returncode:
        raise RuntimeError(f"oracle {args} failed:\n{r.stdout}\n{r.stderr}")
    return r.stdout


def kv_rows(n, start=0, prefix=""):
    return "".join(f"INSERT INTO T ([Key],[Value]) VALUES ('{prefix}{i}', '{{ id = {i} , name = \"item {i}\" }}')\nGO\n"
                   for i in range(start, start + n))


KV = "CREATE TABLE T ([Key] VARCHAR(250) NOT NULL PRIMARY KEY, [Value] NTEXT)\nGO\n"

ENGINE_SAMPLES = {
    # name: (CREATE DATABASE options, SQL)
    "empty": ("PAGE SIZE 1, LCID 1033, CASE SENSITIVE FALSE", ""),
    "kv_ci": ("PAGE SIZE 1, LCID 1033, CASE SENSITIVE FALSE", KV + kv_rows(300)),
    "kv_cs": ("PAGE SIZE 1, LCID 1033, CASE SENSITIVE TRUE", KV + kv_rows(120, prefix="k") + kv_rows(120, prefix="K")),
    "kv_ps4": ("PAGE SIZE 4, LCID 1033, CASE SENSITIVE FALSE", KV + kv_rows(400)),
    "deleted": ("PAGE SIZE 1, LCID 1033, CASE SENSITIVE FALSE",
                KV + kv_rows(200) + "DELETE FROM T WHERE [Key] LIKE '1%'\nGO\n"
                "UPDATE T SET [Value] = 'changed' WHERE [Key] LIKE '5%'\nGO\n"
                "INSERT INTO T VALUES ('new', 'row after deletes')\nGO\n"),
    "types": ("PAGE SIZE 1, LCID 1033, CASE SENSITIVE FALSE",
              "CREATE TABLE T (I INT, BI BIGINT, SI SMALLINT, TI TINYINT, B BIT, DT DATETIME, SDT SMALLDATETIME, "
              "R REAL, F FLOAT, D DECIMAL(18,4), M MONEY, SM SMALLMONEY, G UNIQUEIDENTIFIER, C CHAR(5), NC NCHAR(5), "
              "VC VARCHAR(20), NV NVARCHAR(20), T TEXT, NT NTEXT, IM IMAGE, VB VARBINARY(20), TS TIMESTAMP)\nGO\n"
              "INSERT INTO T (I,BI,SI,TI,B,DT,SDT,R,F,D,M,SM,G,C,NC,VC,NV,T,NT,IM,VB) VALUES (1,2,3,4,1,'2020-01-02 03:04:05.123',"
              "'2020-01-02 03:04:00',1.5,2.25,-12.3456,1.2345,-2.5,'00112233-4455-6677-8899-aabbccddeeff','ab','cd','vc',"
              "'nv','t','nt',0x0102,0x0304)\nGO\n"
              "INSERT INTO T (I,BI,SI,TI,B,DT,SDT,R,F,D,M,SM,G,C,NC,VC,NV,T,NT,IM,VB) VALUES (-1,-2,-3,255,0,'1753-01-01',"
              "'1900-01-01',-1.5,-2.25,12345678901234.5678,-92233720368.5808,214748.3647,"
              "'ffffffff-4455-6677-8899-aabbccddeeff','','','','','','',0x,0x)\nGO\n"
              "INSERT INTO T (I) VALUES (NULL)\nGO\n"
              "INSERT INTO T (I,BI,SI,TI,B,DT,SDT,R,F,D,M,SM,G,C,NC,VC,NV,T,NT,IM,VB) VALUES (2147483647,9223372036854775807,"
              "32767,0,1,'9999-12-31 23:59:59.997','2079-06-06 23:59:00',3.4e38,1.7e308,0.0001,0,0,"
              "'00000000-0000-0000-0000-000000000000','abcde','abcde','x','y','long','x',0xff,0xff)\nGO\n"
              "INSERT INTO T (I,BI,VC,NV,T,NT) VALUES (-2147483648,-9223372036854775807,'é€',"
              "'中文é','é€','中文 \U0001F600')\nGO\n"
              "INSERT INTO T (I,BI,VC,NV) VALUES (-2147483647,5,'é€x','中文éx')\nGO\n"),
    "blobs": ("PAGE SIZE 1, LCID 1033, CASE SENSITIVE FALSE",
              "CREATE TABLE T (Id INT, NT NTEXT, IM IMAGE)\nGO\n" + "".join(
                  f"INSERT INTO T VALUES ({n}, '{('abcdefghij' * (n // 10 + 1))[:n]}', 0x{bytes((i * 7) % 256 for i in range(n)).hex()})\nGO\n"
                  for n in (0, 1, 255, 256, 257, 1024, 1025, 3000, 4096, 9000))),
    "indexes": ("PAGE SIZE 1, LCID 1033, CASE SENSITIVE FALSE",
                "CREATE TABLE T (Z INT NOT NULL, A VARCHAR(10), M INT, D DATETIME)\nGO\n"
                "CREATE UNIQUE INDEX PK_T ON T (Z)\nGO\nCREATE INDEX IX_AM ON T (A, M)\nGO\n"
                "CREATE INDEX IX_D ON T (D DESC)\nGO\n" + "".join(
                    f"INSERT INTO T VALUES ({i}, '{['a', 'B', 'b-c', 'bc', None][i % 5] or ''}', {i % 7}, '2001-01-{1 + i % 28:02d}')\nGO\n"
                    for i in range(150))),
    "many_tables": ("PAGE SIZE 1, LCID 1033, CASE SENSITIVE FALSE",
                    "".join(f"CREATE TABLE Tab{i:02d} ([Key] VARCHAR(250) NOT NULL PRIMARY KEY, [Value] NTEXT)\nGO\n"
                            f"INSERT INTO Tab{i:02d} VALUES ('k', 'v{i}')\nGO\n" for i in range(30))),
}


def collation_samples():
    """Keys whose index order (as built by the engine) the collation tests check."""
    import random
    r = random.Random(7)
    alpha = "".join(chr(c) for c in range(32, 127) if chr(c) != "'") + "''--"
    keys = set()
    while len(keys) < 500:
        keys.add("".join(r.choice(alpha) for _ in range(r.randint(1, 6))).rstrip(" ") or "x")
    return sorted(keys)


def make_engine(work: Path):
    out = FIX / "engine"
    out.mkdir(parents=True, exist_ok=True)
    samples = dict(ENGINE_SAMPLES)
    keys = collation_samples()
    for cs in (False, True):
        seen, rows = set(), []
        for k in keys:
            kk = k if cs else k.lower()
            if kk not in seen:
                seen.add(kk)
                rows.append(k)
        sql = "CREATE TABLE T ([Key] VARCHAR(50) NOT NULL PRIMARY KEY)\nGO\n" + "".join(
            "INSERT INTO T VALUES ('%s')\nGO\n" % k.replace("'", "''") for k in rows)
        samples["collation_cs" if cs else "collation_ci"] = (
            f"PAGE SIZE 1, LCID 1033, CASE SENSITIVE {'TRUE' if cs else 'FALSE'}", sql)
    for name, (opts, sql) in samples.items():
        db = out / f"{name}.vdb3"
        if db.exists():
            db.unlink()
        c = work / f"{name}.create.sql"
        c.write_text(f"CREATE DATABASE '{db}', {opts};\n", encoding="utf-8")
        oracle("sql", "-", c)
        if sql:
            s = work / f"{name}.sql"
            s.write_text(sql, encoding="utf-8")
            oracle("sql", db, s)
        oracle("dump", db, out / f"{name}.json")
        print(f"engine/{name}: {db.stat().st_size} bytes")


def make_written(work: Path):
    out = FIX / "written"
    out.mkdir(parents=True, exist_ok=True)
    for name in specs.SPECS:
        db = out / f"{name}.vdb3"
        specs.write(specs.build(name), db)
        oracle("dump", db, out / f"{name}.json")
        v = work / f"{name}.verify.vdb3"
        shutil.copy(db, v)
        log = oracle("verify", v, 100)
        (out / f"{name}.verify.txt").write_text(log, encoding="utf-8", newline="\n")
        p = work / f"{name}.packed.vdb3"
        shutil.copy(db, p)
        oracle("pack", p)
        oracle("dump", p, work / f"{name}.packed.json")
        if (work / f"{name}.packed.json").read_bytes() != (out / f"{name}.json").read_bytes():
            raise RuntimeError(f"{name}: engine dump changed after pack")
        print(f"written/{name}: {db.stat().st_size} bytes, verify OK, pack OK")


def main():
    global ORACLE
    ap = argparse.ArgumentParser()
    ap.add_argument("--oracle", required=True)
    ap.add_argument("--work", default=os.environ.get("TEMP", "."))
    a = ap.parse_args()
    ORACLE = a.oracle
    work = Path(a.work) / "vistadb3-fixtures"
    work.mkdir(parents=True, exist_ok=True)
    make_engine(work)
    make_written(work)


if __name__ == "__main__":
    main()
