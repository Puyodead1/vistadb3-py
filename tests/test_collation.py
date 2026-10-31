"""Index order of text keys must match the order the engine itself produced."""
import pytest

from conftest import FIXTURES
from vistadb3 import Database
from vistadb3.collation import sort_key


@pytest.mark.parametrize("cs", [False, True])
def test_index_order_matches_engine(cs):
    path = FIXTURES / "engine" / ("collation_cs.vdb3" if cs else "collation_ci.vdb3")
    with Database(path) as db:
        assert db.case_sensitive == cs
        t = db.table("T")
        engine_order = [k[0] for _, k in db.iter_index(t, t.indexes[0])]
    assert len(engine_order) > 300
    assert sorted(engine_order, key=lambda k: sort_key(k, cs)) == engine_order


def test_case_insensitive_equality():
    assert sort_key("Abc", False) == sort_key("aBC", False)
    assert sort_key("Abc", True) != sort_key("aBC", True)
    assert sort_key("a b ", False) == sort_key("a b", False)
    assert sort_key("ab", False) != sort_key("a-b", False)
