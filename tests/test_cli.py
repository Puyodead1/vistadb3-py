import json

from conftest import FIXTURES, library_view
from vistadb3.cli import main


def test_dump_build_roundtrip(tmp_path):
    src = FIXTURES / "engine" / "types.vdb3"
    dump = tmp_path / "d.json"
    main(["dump", str(src), "-o", str(dump)])
    data = json.loads(dump.read_text(encoding="utf-8"))
    assert data["tables"][0]["name"] == "T"
    out = tmp_path / "rebuilt.vdb3"
    main(["build", str(dump), "-o", str(out)])
    assert library_view(src)["tables"] == library_view(out)["tables"]


def test_jsonl_roundtrip(tmp_path):
    src = FIXTURES / "engine" / "kv_cs.vdb3"
    d = tmp_path / "dump"
    main(["dump", str(src), "--jsonl", "-o", str(d)])
    assert (d / "schema.json").exists() and (d / "T.jsonl").exists()
    out = tmp_path / "rebuilt.vdb3"
    main(["build", str(d), "-o", str(out)])
    assert library_view(src) == library_view(out)


def test_copy_and_info(tmp_path, capsys):
    src = FIXTURES / "engine" / "blobs.vdb3"
    out = tmp_path / "c.vdb3"
    main(["copy", str(src), "-o", str(out)])
    assert library_view(src)["tables"] == library_view(out)["tables"]
    main(["info", str(out)])
    assert "T (10 rows)" in capsys.readouterr().out
