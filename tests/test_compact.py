"""Compaction preserves data + views and doesn't grow the file (offline)."""

from __future__ import annotations

import zipfile

import duckdb

from faostatdb import importer as importer_mod
from faostatdb import schema as schema_mod
from faostatdb.compact import compact_database

MAIN = (
    '"Area Code","Area Code (M49)","Area","Item Code","Item","Value","Flag"\n'
    '"2","\'004","Afghanistan","15","Wheat","3200","A"\n'
    '"68","\'250","France","15","Wheat","37000","A"\n'
    '"68","\'250","France","27","Rice","100","E"\n'
)


def _build_db(path):
    archive = path.parent / "QCL.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("QCL_E_All_Data.csv", MAIN)
    con = duckdb.connect(str(path))
    schema_mod.create_metadata_tables(con)
    importer_mod.import_archive(con, archive, "QCL", path.parent / "b")
    con.execute("CHECKPOINT")
    con.close()


def test_compaction_preserves_tables_and_views(tmp_path):
    db = tmp_path / "faostat.duckdb"
    _build_db(db)

    before, after = compact_database(db)
    assert after > 0
    assert after <= before  # never larger than the pre-compaction file

    con = duckdb.connect(str(db), read_only=True)
    try:
        # Fact table survives with its rows.
        assert con.execute("SELECT COUNT(*) FROM data_qcl").fetchone()[0] == 3
        # Dimension table survives.
        assert con.execute(
            "SELECT COUNT(*) FROM dim_area WHERE dataset_code = 'QCL'"
        ).fetchone()[0] == 2
        # Labelled view survives and is still queryable.
        labels = {
            r[0]
            for r in con.execute("SELECT area_label FROM view_qcl_labelled").fetchall()
        }
        assert labels == {"Afghanistan", "France"}
    finally:
        con.close()
