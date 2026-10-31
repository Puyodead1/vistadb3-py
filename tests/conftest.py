import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
sys.path.insert(0, str(HERE))

FIXTURES = HERE / "fixtures"

COL_KEYS = ("name", "type", "max_length", "allow_null", "code_page")
IX_KEYS = ("name", "expression", "primary", "unique")


def engine_view(path):
    """The parts of an engine (oracle) dump that vistadb3 reproduces."""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    return {
        "page_size": d["page_size"],
        "case_sensitive": d["case_sensitive"],
        "tables": [{
            "name": t["name"],
            "columns": [{k: c[k] for k in COL_KEYS} for c in t["columns"]],
            "indexes": sorted(({k: i[k] for k in IX_KEYS} for i in t["indexes"]), key=lambda i: i["name"]),
            "rows": t["rows"],
        } for t in sorted(d["tables"], key=lambda t: t["name"])],
    }


def library_view(path):
    from vistadb3 import Database
    from vistadb3.dumpjson import iter_table_json, table_schema
    with Database(path) as db:
        tables = []
        for t in sorted(db.tables.values(), key=lambda t: t.name):
            s = table_schema(t)
            tables.append({
                "name": t.name,
                "columns": [{k: c[k] for k in COL_KEYS} for c in s["columns"]],
                "indexes": [{k: i[k] for k in IX_KEYS} for i in s["indexes"]],
                "rows": list(iter_table_json(db, t)),
            })
        return {"page_size": db.page_size // 1024, "case_sensitive": db.case_sensitive, "tables": tables}
