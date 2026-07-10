"""CLI command tests that don't need the network (offline, deterministic)."""

from __future__ import annotations

import zipfile

import duckdb
import pytest

from faostatdb import importer as importer_mod
from faostatdb import metadata as metadata_mod
from faostatdb import schema as schema_mod
from faostatdb.config import BuildConfig, Config, DatasetsConfig, EnrichmentConfig, parse_years
from faostatdb.cli import _year_filter_message, main, run_build

MAIN = (
    '"Area Code","Area","Item Code","Item","Value","Flag"\n'
    '"68","France","15","Wheat","37000","A"\n'
)


def test_top_level_help_uses_quarto_like_layout(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert out.startswith("Usage:   faostatdb <command> [options]\n")
    assert "Version: " in out
    assert "Description:\n\n  Build a local, source-preserving DuckDB mirror" in out
    assert "Options:\n\n  -h, --help     - Show this help." in out
    assert "Commands:\n\n  build           [options]" in out
    assert out.index("  build           [options]") < out.index("  config          <command>")
    assert out.index("  config          <command>") < out.index("  list            [options]")
    assert "Run 'faostatdb <command> --help' for command-specific options." in out


def test_config_init_writes_and_respects_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    assert main(["config", "init"]) == 0
    cfg_file = tmp_path / "faostatdb.toml"
    assert cfg_file.is_file()
    assert "[build]" in cfg_file.read_text(encoding="utf-8")

    # Second run without --force refuses; with --force it overwrites.
    assert main(["config", "init"]) == 1
    assert main(["config", "init", "--force"]) == 0


def test_year_filter_message_reflects_overwrite_mode():
    years = parse_years("2010-")

    incremental = _year_filter_message(Config(), years)
    assert "Datasets already present in faostat.duckdb accumulate" in incremental

    overwrite = _year_filter_message(
        Config(build=BuildConfig(overwrite=True)),
        years,
    )
    assert "will be overwritten before import" in overwrite
    assert "accumulate" not in overwrite


def test_successful_build_removes_default_download_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rec = metadata_mod.DatasetRecord(
        dataset_code="QCL",
        dataset_name="Crops",
        file_location="https://example.test/QCL.zip",
        file_rows=1,
    )
    snapshot = metadata_mod.MetadataSnapshot(
        url="https://example.test/datasets.json", sha256="0" * 64, datasets=[rec]
    )

    def fake_download(_url, dest, **_kwargs):
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("QCL_E_All_Data.csv", MAIN)
        return dest

    monkeypatch.setattr(metadata_mod, "fetch_and_parse", lambda: snapshot)
    monkeypatch.setattr("faostatdb.download.download_with_retry", fake_download)

    cfg = Config(
        build=BuildConfig(database="faostat.duckdb", jobs=1, compact=False),
        datasets=DatasetsConfig(mode="include", include=["QCL"]),
        enrichment=EnrichmentConfig(area_classification=False, historical_validity=False),
    )

    assert run_build(cfg, assume_yes=True, strict=True) == 0
    assert (tmp_path / "faostat.duckdb").is_file()
    assert not (tmp_path / "faostat_temp_download").exists()


def test_enrichment_failure_does_not_abort_build(tmp_path, monkeypatch):
    # Enrichment is a non-source, best-effort layer: if it blows up (e.g. a bad
    # curated-CSV edit or an unexpected area code) the build must still finalize —
    # every dataset is already imported, so the provenance row and a usable database
    # must survive. Regression guard for enrichment being unwrapped in run_build.
    monkeypatch.chdir(tmp_path)
    rec = metadata_mod.DatasetRecord(
        dataset_code="QCL",
        dataset_name="Crops",
        file_location="https://example.test/QCL.zip",
        file_rows=1,
    )
    snapshot = metadata_mod.MetadataSnapshot(
        url="https://example.test/datasets.json", sha256="0" * 64, datasets=[rec]
    )

    def fake_download(_url, dest, **_kwargs):
        with zipfile.ZipFile(dest, "w") as zf:
            zf.writestr("QCL_E_All_Data.csv", MAIN)
        return dest

    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated enrichment failure")

    monkeypatch.setattr(metadata_mod, "fetch_and_parse", lambda: snapshot)
    monkeypatch.setattr("faostatdb.download.download_with_retry", fake_download)
    monkeypatch.setattr("faostatdb.enrich.enrich_areas", boom)

    cfg = Config(
        build=BuildConfig(database="faostat.duckdb", jobs=1, compact=False),
        datasets=DatasetsConfig(mode="include", include=["QCL"]),
        enrichment=EnrichmentConfig(area_classification=True, historical_validity=True),
    )

    # Build succeeds despite the enrichment error, even under --strict.
    assert run_build(cfg, assume_yes=True, strict=True) == 0
    db = tmp_path / "faostat.duckdb"
    assert db.is_file()
    con = duckdb.connect(str(db), read_only=True)
    try:
        # Provenance row was still written (i.e. _record_build ran after the failure).
        assert con.execute("SELECT COUNT(*) FROM faostat_build").fetchone()[0] == 1
        # The imported fact table is present and queryable.
        assert con.execute("SELECT COUNT(*) FROM data_qcl").fetchone()[0] == 1
        # Enrichment genuinely did not complete.
        assert not schema_mod.table_exists(con, "area_classification")
    finally:
        con.close()


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
