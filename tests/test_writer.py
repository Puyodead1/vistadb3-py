"""Files written by vistadb3 and checked by the real engine (see tools/make_fixtures.py)."""
import struct
import uuid
from datetime import datetime
from decimal import Decimal

import pytest

import specs
from conftest import FIXTURES, engine_view, library_view
from vistadb3 import Database
from vistadb3.writer import ColumnDef, DatabaseWriter, IndexDef

WRITTEN = FIXTURES / "written"


@pytest.mark.parametrize("name", sorted(specs.SPECS))
def test_writer_output_is_unchanged(name, tmp_path):
    """The writer must keep producing the files the engine accepted."""
    out = tmp_path / f"{name}.vdb3"
    specs.write(specs.build(name), out)
    assert out.read_bytes() == (WRITTEN / f"{name}.vdb3").read_bytes()


@pytest.mark.parametrize("name", sorted(specs.SPECS))
def test_engine_read_back_what_was_written(name):
    """The engine's dump of the written file equals vistadb3's reading of it ..."""
    db = WRITTEN / f"{name}.vdb3"
    assert library_view(db) == engine_view(WRITTEN / f"{name}.json")
    # ... and the engine found every row through every index.
    assert "VERIFY OK" in (WRITTEN / f"{name}.verify.txt").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(specs.SPECS))
def test_written_values(name):
    """... and vistadb3's reading equals the values that were inserted."""
    spec = specs.build(name)
    with Database(WRITTEN / f"{name}.vdb3") as db:
        for t in spec["tables"]:
            got = list(db.iter_rows(t["name"]))
            assert len(got) == len(t["rows"])
            for g, row in zip(got, t["rows"]):
                for v, w, c in zip(g, row, t["columns"]):
                    assert v == _expected(w, c), (t["name"], c.name)


def _expected(v, c):
    if v is None:
        return None
    t = c.type.name
    if t in ("CHAR", "NCHAR"):
        return v.rstrip(" ").ljust(c.max_length)
    if t in ("VARCHAR", "NVARCHAR"):
        v = v.rstrip(" ")
    if t in ("VARCHAR", "TEXT"):
        return v.encode("cp1252", "replace").decode("cp1252")
    if t == "REAL":
        return struct.unpack("<f", struct.pack("<f", v))[0]
    if t in ("DECIMAL", "MONEY", "SMALLMONEY"):
        return Decimal(v)
    return v


def test_empty_database(tmp_path):
    p = tmp_path / "e.vdb3"
    DatabaseWriter(p, replica_id=uuid.UUID(int=0)).close()
    with Database(p) as db:
        assert db.page_size == 1024 and db.tables == {} and not db.case_sensitive
        assert db.signature.startswith("VistaDB 3.5")


def test_unique_violation(tmp_path):
    w = DatabaseWriter(tmp_path / "u.vdb3")
    w.create_table("T", [ColumnDef("Key", "VarChar", 10, False)], primary_key="Key")
    w.insert("T", ["abc"])
    w.insert("T", ["ABC"])  # equal in a case-insensitive database
    with pytest.raises(ValueError, match="duplicate key"):
        w.close()


def test_not_null_and_length(tmp_path):
    w = DatabaseWriter(tmp_path / "n.vdb3")
    w.create_table("T", [ColumnDef("A", "VarChar", 3, False), ColumnDef("B", "Int")])
    with pytest.raises(ValueError, match="NULL"):
        w.insert("T", [None, 1])
    with pytest.raises(ValueError, match="exceeds"):
        w.insert("T", ["abcd", 1])
    with pytest.raises(KeyError):
        w.insert("T", {"A": "x", "Nope": 1})
    w.insert("T", {"a": "x"})
    w.close()
    with Database(tmp_path / "n.vdb3") as db:
        assert list(db.iter_rows("T")) == [("x", None)]


def test_dict_rows_and_types(tmp_path):
    p = tmp_path / "d.vdb3"
    with DatabaseWriter(p, case_sensitive=True) as w:
        w.create_table("T", [("K", "VarChar", 20, False), ("When", "DateTime"), ("Amount", "Money")],
                       [IndexDef("IX_When", ["DESC(When)"])], primary_key="K", primary_key_name="IX_Key")
        w.insert("T", {"K": "b", "When": datetime(2024, 5, 1, 12, 30), "Amount": "12.5"})
        w.insert("T", {"K": "B", "When": datetime(2023, 1, 1), "Amount": 3})
    with Database(p) as db:
        t = db.table("t")
        assert [i.name for i in t.indexes] == ["IX_Key", "IX_When"]
        assert list(db.iter_rows(t)) == [("b", datetime(2024, 5, 1, 12, 30), Decimal("12.5")),
                                         ("B", datetime(2023, 1, 1), Decimal(3))]
        assert [k for k, _ in db.iter_index(t, "IX_Key")] == [1, 2]  # lowercase sorts first
        assert [k for k, _ in db.iter_index(t, "IX_When")] == [1, 2]  # descending
