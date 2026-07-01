"""CLI command tests that don't need the network (offline, deterministic)."""

from __future__ import annotations

import zipfile

import duckdb

from faostatdb import importer as importer_mod
from faostatdb import schema as schema_mod
from faostatdb.cli import main

MAIN = (
    '"Area Code","Area","Item Code","Item","Value","Flag"\n'
    '"68","France","15","Wheat","37000","A"\n'
)


def test_config_init_writes_and_respects_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAOSTATDB_DATABASE", raising=False)

    assert main(["config", "init"]) == 0
    cfg_file = tmp_path / "faostatdb.toml"
    assert cfg_file.is_file()
    assert "[build]" in cfg_file.read_text(encoding="utf-8")

    # Second run without --force refuses; with --force it overwrites.
    assert main(["config", "init"]) == 1
    assert main(["config", "init", "--force"]) == 0


def _built_db(tmp_path):
    """Create a minimal but real FAOSTATdb database file and return its path."""
    archive = tmp_path / "QCL.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("QCL_E_All_Data.csv", MAIN)
    db = tmp_path / "faostat.duckdb"
    con = duckdb.connect(str(db))
    schema_mod.create_metadata_tables(con)
    importer_mod.import_archive(con, archive, "QCL", tmp_path / "b")
    con.execute(
        "INSERT INTO faostat_dataset (dataset_code, dataset_name, import_status) "
        "VALUES ('QCL', 'Crops', 'imported')"
    )
    con.execute(
        "INSERT INTO faostat_build (build_id, faostatdb_version, duckdb_version) "
        "VALUES ('abc', '0.2.0', ?)",
        [duckdb.__version__],
    )
    con.close()
    return db


def test_info_and_validate_and_sql(tmp_path, capsys):
    db = _built_db(tmp_path)

    assert main(["info", str(db)]) == 0
    out = capsys.readouterr().out
    assert "FAOSTATdb database" in out
    assert "Datasets:" in out

    assert main(["validate", str(db)]) == 0
    assert "validate: OK" in capsys.readouterr().out

    assert main(["sql", "SELECT COUNT(*) AS n FROM data_qcl", "--database", str(db)]) == 0
    assert "1" in capsys.readouterr().out


def test_info_missing_database(tmp_path, capsys):
    assert main(["info", str(tmp_path / "nope.duckdb")]) == 1
