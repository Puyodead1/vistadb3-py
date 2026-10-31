"""Command line interface: ``python -m vistadb3 {info,dump,build,copy} ...``"""
from __future__ import annotations

import argparse
import json
import os
import sys

from .dumpjson import decode_value, iter_table_json, table_schema
from .reader import Database
from .writer import ColumnDef, DatabaseWriter, IndexDef


def cmd_info(a):
    with Database(a.database) as db:
        if a.json:
            out = {"page_size": db.page_size // 1024, "lcid": db.lcid, "case_sensitive": db.case_sensitive,
                   "tables": [dict(table_schema(t), row_count=db.row_count(t)) for t in db.tables.values()]}
            json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
            print()
            return
        print(f"{a.database}: page size {db.page_size // 1024} KiB, LCID {db.lcid}, "
              f"case {'sensitive' if db.case_sensitive else 'insensitive'}")
        for t in db.tables.values():
            print(f"\n{t.name} ({db.row_count(t)} rows)")
            for c in t.columns:
                size = f"({c.max_length})" if c.type_name in ("Char", "NChar", "VarChar", "NVarChar") else ""
                print(f"  {c.name:<24} {c.type_name}{size}{'' if c.allow_null else ' NOT NULL'}")
            for i in t.indexes:
                kind = "PRIMARY KEY" if i.primary else "UNIQUE INDEX" if i.unique else "INDEX"
                print(f"  {kind} {i.name} ({i.expression})")


def cmd_dump(a):
    with Database(a.database) as db:
        tables = [db.table(n) for n in a.table] if a.table else list(db.tables.values())
        head = {"page_size": db.page_size // 1024, "lcid": db.lcid, "case_sensitive": db.case_sensitive}
        if a.jsonl:
            os.makedirs(a.output, exist_ok=True)
            with open(os.path.join(a.output, "schema.json"), "w", encoding="utf-8") as fh:
                json.dump(dict(head, tables=[table_schema(t) for t in tables]), fh, indent=2, ensure_ascii=False)
            for t in tables:
                with open(os.path.join(a.output, t.name + ".jsonl"), "w", encoding="utf-8", newline="\n") as fh:
                    for row in iter_table_json(db, t):
                        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            return
        out = open(a.output, "w", encoding="utf-8", newline="\n") if a.output and a.output != "-" else sys.stdout
        try:
            # written incrementally so large databases do not need to fit in memory as JSON
            out.write(json.dumps(head, ensure_ascii=False)[:-1] + ', "tables": [')
            for n, t in enumerate(tables):
                s = json.dumps(table_schema(t), ensure_ascii=False)[:-1]
                out.write(("," if n else "") + "\n" + s + ', "rows": [')
                for m, row in enumerate(iter_table_json(db, t)):
                    out.write(("," if m else "") + "\n" + json.dumps(row, ensure_ascii=False))
                out.write("]}")
            out.write("]}\n")
        finally:
            if out is not sys.stdout:
                out.close()


def load_spec(path: str):
    """Load a build spec: a dump JSON file, or a directory with schema.json + <table>.jsonl."""
    if os.path.isdir(path):
        with open(os.path.join(path, "schema.json"), encoding="utf-8") as fh:
            spec = json.load(fh)
        for t in spec["tables"]:
            if "rows" in t:
                continue
            f = os.path.join(path, t["name"] + ".jsonl")
            t["rows_file"] = f if os.path.exists(f) else None
        return spec
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def iter_spec_rows(t):
    if "rows" in t:
        yield from t["rows"]
    elif t.get("rows_file"):
        with open(t["rows_file"], encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    yield json.loads(line)


def build_from_spec(spec, output, overwrite=False, page_size=None):
    with DatabaseWriter(output, page_size=page_size or spec.get("page_size", 1), lcid=spec.get("lcid", 1033),
                        case_sensitive=spec.get("case_sensitive", False), overwrite=overwrite) as w:
        for t in spec["tables"]:
            cols = [ColumnDef(c["name"], c["type"], c.get("max_length", 0), c.get("allow_null", True),
                              c.get("code_page")) for c in t["columns"]]
            ixs = []
            for i in t.get("indexes", []):
                names = i.get("columns") or [x.strip() for x in i["expression"].split(";")]
                ixs.append(IndexDef(i["name"], names, unique=i.get("unique", False) or i.get("primary", False),
                                    primary=i.get("primary", False)))
            w.create_table(t["name"], cols, ixs)
            for row in iter_spec_rows(t):
                if isinstance(row, dict):
                    w.insert(t["name"], {k: decode_value(v) for k, v in row.items()})
                else:
                    w.insert(t["name"], [decode_value(v) for v in row])


def copy_database(src, dst, overwrite=False, page_size=None):
    """Rebuild ``src`` into a new file ``dst`` (schema, indexes and rows)."""
    with Database(src) as db:
        with DatabaseWriter(dst, page_size=page_size or db.page_size // 1024, lcid=db.lcid,
                            case_sensitive=db.case_sensitive, overwrite=overwrite) as w:
            for t in db.tables.values():
                cols = [ColumnDef(c.name, c.type, c.max_length if c.type_name in
                                  ("Char", "NChar", "VarChar", "NVarChar", "VarBinary") else 0,
                                  c.allow_null, c.code_page) for c in t.columns]
                ixs = [IndexDef(i.name, [x.strip() for x in i.expression.split(";")], i.unique or i.primary,
                                i.primary) for i in t.indexes]
                w.create_table(t.name, cols, ixs)
                for row in db.iter_rows(t):
                    w.insert(t.name, row)


def cmd_build(a):
    build_from_spec(load_spec(a.spec), a.output, overwrite=a.force, page_size=a.page_size)


def cmd_copy(a):
    copy_database(a.source, a.output, overwrite=a.force, page_size=a.page_size)


def main(argv=None):
    p = argparse.ArgumentParser(prog="vistadb3", description="Read and write VistaDB 3.5 database files.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("info", help="show tables, columns and indexes")
    s.add_argument("database")
    s.add_argument("--json", action="store_true", help="print the schema as JSON")
    s.set_defaults(func=cmd_info)

    s = sub.add_parser("dump", help="dump tables to JSON (one file) or JSONL (a directory)")
    s.add_argument("database")
    s.add_argument("-o", "--output", default="-", help="output file, or directory with --jsonl")
    s.add_argument("--jsonl", action="store_true", help="write schema.json + one <table>.jsonl per table")
    s.add_argument("-t", "--table", action="append", help="only this table (repeatable)")
    s.set_defaults(func=cmd_dump)

    s = sub.add_parser("build", help="create a database from a dump (JSON file or JSONL directory)")
    s.add_argument("spec")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("-f", "--force", action="store_true", help="overwrite the output file")
    s.add_argument("--page-size", type=int, help="page size in KiB (default: from the spec, else 1)")
    s.set_defaults(func=cmd_build)

    s = sub.add_parser("copy", help="rebuild a database into a new file")
    s.add_argument("source")
    s.add_argument("-o", "--output", required=True)
    s.add_argument("-f", "--force", action="store_true")
    s.add_argument("--page-size", type=int)
    s.set_defaults(func=cmd_copy)

    a = p.parse_args(argv)
    a.func(a)


if __name__ == "__main__":
    main()
