"""Compare the library's reading of a database with an oracle (DLL) JSON dump.

    python tools/compare.py DB ORACLE.json

Only fields both sides produce are compared.  Exit status 0 when identical.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vistadb3.dumpjson import iter_table_json, table_schema  # noqa: E402
from vistadb3.reader import Database  # noqa: E402

COL_KEYS = ("name", "type", "max_length", "allow_null", "code_page")
IX_KEYS = ("name", "expression", "primary", "unique")


def compare(db_path, oracle_path, verbose=True) -> int:
    with open(oracle_path, encoding="utf-8") as fh:
        ref = json.load(fh)
    problems = 0

    def bad(msg):
        nonlocal problems
        problems += 1
        if verbose and problems <= 30:
            print("MISMATCH", msg)

    with Database(db_path) as db:
        if db.page_size // 1024 != ref["page_size"]:
            bad(f"page size {db.page_size} vs {ref['page_size']}")
        if db.case_sensitive != ref["case_sensitive"]:
            bad(f"case sensitivity {db.case_sensitive} vs {ref['case_sensitive']}")
        names = [t.name for t in db.tables.values()]
        rnames = [t["name"] for t in ref["tables"]]
        if sorted(names) != sorted(rnames):
            bad(f"tables {names} vs {rnames}")
        for rt in ref["tables"]:
            try:
                t = db.table(rt["name"])
            except KeyError:
                continue
            s = table_schema(t)
            for a, b in zip(s["columns"], rt["columns"]):
                for k in COL_KEYS:
                    if a[k] != b[k]:
                        bad(f"{t.name}.{a['name']} {k}: {a[k]!r} vs {b[k]!r}")
            if len(s["columns"]) != len(rt["columns"]):
                bad(f"{t.name}: column count")
            ri = sorted(rt["indexes"], key=lambda i: i["name"])
            for a, b in zip(s["indexes"], ri):
                for k in IX_KEYS:
                    if a[k] != b[k]:
                        bad(f"{t.name} index {a['name']} {k}: {a[k]!r} vs {b[k]!r}")
            if len(s["indexes"]) != len(ri):
                bad(f"{t.name}: index count {len(s['indexes'])} vs {len(ri)}")
            n = 0
            for n, (a, b) in enumerate(zip(iter_table_json(db, t), rt["rows"]), 1):
                if a != b:
                    bad(f"{t.name} row {n}: {str(a)[:200]} vs {str(b)[:200]}")
            got = db.row_count(t)
            if got != len(rt["rows"]):
                bad(f"{t.name}: row count {got} vs {len(rt['rows'])}")
            if verbose:
                print(f"  {t.name}: {len(rt['rows'])} rows checked")
    if verbose:
        print("OK" if not problems else f"{problems} mismatches")
    return problems


if __name__ == "__main__":
    sys.exit(1 if compare(sys.argv[1], sys.argv[2]) else 0)
