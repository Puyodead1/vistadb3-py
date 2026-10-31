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


def _g(f: float, digits: int) -> str:
    """.NET-style "G<digits>" formatting (ties round half away from zero)."""
    from decimal import ROUND_HALF_UP
    if f != f:
        return "NaN"
    if f in (float("inf"), float("-inf")):
        return "Infinity" if f > 0 else "-Infinity"
    if f == 0:
        return "-0" if str(f).startswith("-") else "0"
    d = Decimal(f)
    exp = d.adjusted()
    q = d.quantize(Decimal(1).scaleb(exp - digits + 1), rounding=ROUND_HALF_UP)
    if q.adjusted() != exp:  # rounding carried into a new digit
        exp = q.adjusted()
        q = q.quantize(Decimal(1).scaleb(exp - digits + 1), rounding=ROUND_HALF_UP)
    if -5 < exp < digits:
        t = format(q, "f")
        if "." in t:
            t = t.rstrip("0").rstrip(".")
        return t
    mant = format(q.scaleb(-exp), "f")
    if "." in mant:
        mant = mant.rstrip("0").rstrip(".")
    return f"{mant}E{'+' if exp >= 0 else '-'}{abs(exp):02d}"


def _float_str(f: float, vtype) -> str:
    """Format like .NET's "R" format: G15 (G7 for REAL) if it round-trips, else G17 (G9)."""
    import struct
    if vtype == VType.REAL:
        f32 = struct.unpack("<f", struct.pack("<f", f))[0]
        s = _g(f32, 7)
        if struct.unpack("<f", struct.pack("<f", float(s.replace("Infinity", "inf"))))[0] != f32:
            s = _g(f32, 9)
        return s
    s = _g(f, 15)
    if float(s.replace("Infinity", "inf")) != f:
        s = _g(f, 17)
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
