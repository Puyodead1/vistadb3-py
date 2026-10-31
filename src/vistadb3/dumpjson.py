"""JSON representation of a database, shared by the CLI and the tests.

Values that JSON cannot carry directly are tagged the same way the test oracle tags them:
``{"$bin": base64}``, ``{"$dt": "YYYY-MM-DDTHH:MM:SS.fffffff"}``, ``{"$dec": "1.5"}``,
``{"$f": "1.5"}`` and ``{"$guid": "..."}``.
"""
from __future__ import annotations

import base64
import uuid
from datetime import datetime
from decimal import Decimal

from .reader import Database, Table
from .types import VType


def encode_value(v, vtype: VType | None = None):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, (bytes, bytearray)):
        return {"$bin": base64.b64encode(v).decode("ascii")}
    if isinstance(v, datetime):
        return {"$dt": v.strftime("%Y-%m-%dT%H:%M:%S.") + "%07d" % (v.microsecond * 10)}
    if isinstance(v, Decimal):
        return {"$dec": _dec_str(v)}
    if isinstance(v, float):
        return {"$f": _float_str(v, vtype)}
    if isinstance(v, uuid.UUID):
        return {"$guid": str(v)}
    raise TypeError(type(v))


def decode_value(v):
    if isinstance(v, dict):
        (k, s), = v.items()
        if k == "$bin":
            return base64.b64decode(s)
        if k == "$dt":
            head, frac = s.split(".")
            return datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(microsecond=int(frac[:6]))
        if k == "$dec":
            return Decimal(s)
        if k == "$f":
            return float(s)
        if k == "$guid":
            return uuid.UUID(s)
        raise ValueError(k)
    return v


def _dec_str(d: Decimal) -> str:
    s = format(d, "f")
    return s


def _float_str(f: float, vtype) -> str:
    if vtype == VType.REAL:
        import struct
        f = struct.unpack("<f", struct.pack("<f", f))[0]
        # shortest repr that round-trips through float32, like .NET "R"
        for p in range(1, 10):
            s = "%.*g" % (p, f)
            if struct.unpack("<f", struct.pack("<f", float(s)))[0] == f:
                return _net_float(s)
    return _net_float(repr(f))


def _net_float(s: str) -> str:
    s = s.replace("e+", "E+").replace("e-", "E-")
    if s.endswith(".0"):
        s = s[:-2]
    return s


def table_schema(t: Table) -> dict:
    return {
        "name": t.name,
        "description": t.description,
        "columns": [
            {"name": c.name, "type": c.type_name, "max_length": c.max_length, "allow_null": c.allow_null,
             "read_only": c.read_only, "code_page": c.code_page, "description": c.description}
            for c in t.columns
        ],
        "indexes": [
            {"name": i.name, "expression": i.expression, "primary": i.primary, "unique": i.unique}
            for i in t.indexes
        ],
    }


def iter_table_json(db: Database, t: Table):
    types = [c.type for c in t.columns]
    for row in db.iter_rows(t):
        yield [encode_value(v, ty) for v, ty in zip(row, types)]


def database_json(db: Database, rows: bool = True) -> dict:
    out = {"page_size": db.page_size // 1024, "lcid": db.lcid, "case_sensitive": db.case_sensitive, "tables": []}
    for t in db.tables.values():
        d = table_schema(t)
        if rows:
            d["rows"] = list(iter_table_json(db, t))
        out["tables"].append(d)
    return out
