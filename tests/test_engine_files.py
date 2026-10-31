"""vistadb3 must read databases written by the real engine exactly as the engine does."""
import pytest

from conftest import FIXTURES, engine_view, library_view
from vistadb3 import Database
from vistadb3.rows import BlobRef

ENGINE = sorted(p.stem for p in (FIXTURES / "engine").glob("*.vdb3"))


@pytest.mark.parametrize("name", ENGINE)
def test_reads_like_engine(name):
    db = FIXTURES / "engine" / f"{name}.vdb3"
    assert library_view(db) == engine_view(db.with_suffix(".json"))


def test_deleted_rows_are_gone():
    with Database(FIXTURES / "engine" / "deleted.vdb3") as db:
        keys = [r[0] for r in db.iter_rows("T")]
        assert not any(k.startswith("1") for k in keys)
        assert "new" in keys
        assert db.row_count("T") == len(keys)


def test_blob_storage_modes():
    """Values up to page_size/4 bytes stay in the row; larger ones use extent pages."""
    with Database(FIXTURES / "engine" / "blobs.vdb3") as db:
        t = db.table("T")
        for r in db.records(db.table_root(t), [c.codec for c in t.columns]):
            n = r.raw[0]
            assert isinstance(r.raw[1], BlobRef) == (n > 256)
