"""End-to-end importer tests against a synthetic FAOSTAT-like archive (offline).

These build a tiny ZIP that mimics the real FAOSTAT bulk shape — a main data CSV
with repeated dimension attributes plus a flag legend sidecar — and assert that
the importer produces the reduced fact table, dimension tables, flag dimension,
column mapping, constant-column records, and a labelled view.
"""

from __future__ import annotations

import zipfile

import duckdb
import pytest

from faostatdb import importer as importer_mod
from faostatdb import schema as schema_mod

# A miniature QCL-shaped dataset. Note the deliberate structure:
#   * "Domain Code"/"Domain" are constant across every row (as in real FAOSTAT)
#   * area/item/element carry a code + label pair (dimension extraction targets)
#   * "Year Code"/"Year" form a year dimension
#   * "Value"/"Flag" are preserved verbatim on the fact table
MAIN_CSV = """\
"Domain Code","Domain","Area Code","Area Code (M49)","Area","Item Code","Item","Element Code","Element","Year Code","Year","Unit","Value","Flag"
"QCL","Crops","2","'004","Afghanistan","15","Wheat","5510","Production","2000","2000","t","3200","A"
"QCL","Crops","2","'004","Afghanistan","15","Wheat","5510","Production","2001","2001","t","3300","E"
"QCL","Crops","2","'004","Afghanistan","15","Wheat","5312","Area harvested","2000","2000","ha","1000","A"
"QCL","Crops","2","'004","Afghanistan","27","Rice","5510","Production","2000","2000","t","5200","A"
"QCL","Crops","68","'250","France","15","Wheat","5510","Production","2000","2000","t","37000","A"
"QCL","Crops","68","'250","France","15","Wheat","5510","Production","2001","2001","t","36500","E"
"QCL","Crops","68","'250","France","15","Wheat","5312","Area harvested","2000","2000","ha","5000","A"
"QCL","Crops","68","'250","France","27","Rice","5510","Production","2000","2000","t","100","E"
"""

FLAG_CSV = """\
"Flag","Description"
"A","Official figure"
"E","Estimated value"
"""


def _make_archive(tmp_path, main=MAIN_CSV, flag=FLAG_CSV):
    """Write a synthetic FAOSTAT-style archive and return its path."""
    archive = tmp_path / "QCL.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("QCL_E_All_Data.csv", main)
        if flag is not None:
            zf.writestr("QCL_E_Flags.csv", flag)
    return archive


@pytest.fixture()
def con():
    c = duckdb.connect()  # in-memory
    schema_mod.create_metadata_tables(c)
    try:
        yield c
    finally:
        c.close()


def _cols(con, table):
    return [d[0] for d in con.execute(f'SELECT * FROM "{table}" LIMIT 0').description]


def _coltypes(con, table):
    return {r[0]: r[1] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}


def test_import_builds_reduced_fact_table(con, tmp_path):
    archive = _make_archive(tmp_path)
    build_dir = tmp_path / "build"
    result = importer_mod.import_archive(con, archive, "QCL", build_dir)

    assert result.table_name == "data_qcl"
    assert result.row_count == 8

    cols = _cols(con, "data_qcl")
    # Codes and measurements are kept (they vary across rows)...
    assert "value" in cols
    assert "flag_code" in cols
    assert "area_code" in cols
    assert "item_code" in cols
    assert "element_code" in cols
    # ...labels are moved out to dimensions...
    assert "area_label" not in cols
    assert "item_label" not in cols
    # ...and the constant Domain columns are dropped entirely.
    assert "domain_code" not in cols
    assert "domain" not in cols


def test_dimension_tables_hold_labels(con, tmp_path):
    importer_mod.import_archive(con, _make_archive(tmp_path), "QCL", tmp_path / "b")

    assert schema_mod.table_exists(con, "dim_area")
    assert "area_label" in _cols(con, "dim_area")
    # Afghanistan + France, keyed by dataset_code.
    labels = {
        r[0]
        for r in con.execute(
            "SELECT area_label FROM dim_area WHERE dataset_code = 'QCL'"
        ).fetchall()
    }
    assert labels == {"Afghanistan", "France"}
    # M49 alternate code: the leading text-marker apostrophe is stripped, but the
    # leading zeros survive as text (still VARCHAR, not coerced to a number).
    m49 = {
        r[0]
        for r in con.execute(
            "SELECT area_code_m49 FROM dim_area WHERE dataset_code = 'QCL'"
        ).fetchall()
    }
    assert m49 == {"004", "250"}


def test_flag_dimension_from_sidecar(con, tmp_path):
    result = importer_mod.import_archive(con, _make_archive(tmp_path), "QCL", tmp_path / "b")
    assert result.flag_rows == 2
    descr = dict(
        con.execute(
            "SELECT flag_code, flag_description FROM dim_flag WHERE dataset_code = 'QCL'"
        ).fetchall()
    )
    assert descr == {"A": "Official figure", "E": "Estimated value"}


def test_constant_columns_recorded(con, tmp_path):
    importer_mod.import_archive(con, _make_archive(tmp_path), "QCL", tmp_path / "b")
    constants = dict(
        con.execute(
            "SELECT column_name, value FROM faostat_constant_column "
            "WHERE dataset_code = 'QCL'"
        ).fetchall()
    )
    # Domain Code was constant "QCL" across all rows -> dropped + recorded.
    # (The "Domain" label itself moved into dim_domain during dimension extraction.)
    assert constants.get("domain_code") == "QCL"


def test_column_mapping_recorded(con, tmp_path):
    importer_mod.import_archive(con, _make_archive(tmp_path), "QCL", tmp_path / "b")
    mapping = dict(
        con.execute(
            "SELECT original_column_name, normalized_column_name "
            "FROM faostat_column_mapping WHERE dataset_code = 'QCL'"
        ).fetchall()
    )
    assert mapping["Area Code (M49)"] == "area_code_m49"
    assert mapping["Item"] == "item_label"
    assert mapping["Value"] == "value"
    assert mapping["Flag"] == "flag_code"


def test_labelled_view_joins_labels(con, tmp_path):
    result = importer_mod.import_archive(con, _make_archive(tmp_path), "QCL", tmp_path / "b")
    assert result.labelled_view == "view_qcl_labelled"

    rows = con.execute(
        "SELECT area_label, item_label, element_label, value, flag_code, flag_description "
        "FROM view_qcl_labelled "
        "WHERE area_label = 'France' AND item_label = 'Wheat' "
        "AND element_label = 'Production' "
        "ORDER BY value"
    ).fetchall()
    # France/Wheat/Production has two years; labels + flag description join back in.
    assert len(rows) == 2
    assert rows[0][0] == "France"
    assert rows[0][1] == "Wheat"
    assert rows[0][2] == "Production"
    assert rows[0][5] in ("Official figure", "Estimated value")


def test_import_without_flag_sidecar(con, tmp_path):
    # Archives without a flag legend must still import cleanly (0 flag rows).
    archive = _make_archive(tmp_path, flag=None)
    result = importer_mod.import_archive(con, archive, "QCL", tmp_path / "b")
    assert result.flag_rows == 0
    assert result.row_count == 8


def test_row_count_verified_against_source_csv(con, tmp_path):
    # The fast line-count path proves losslessness for a normal (no multi-line
    # field) file: every physical data line becomes exactly one imported row.
    result = importer_mod.import_archive(con, _make_archive(tmp_path), "QCL", tmp_path / "b")
    assert result.row_count == 8
    assert result.source_row_count == 8
    assert result.count_method == "line-count"
    assert result.lossless


def test_row_count_verification_handles_embedded_newline(con, tmp_path):
    # A quoted field containing a newline is ONE record, but spans two physical
    # lines — so the fast line count over-counts and verification must fall back to
    # the exact CSV parse, which agrees with DuckDB (still lossless).
    main = MAIN_CSV.replace('"Wheat"', '"Winter\nwheat"', 1)
    result = importer_mod.import_archive(
        con, _make_archive(tmp_path, main=main), "QCL", tmp_path / "b"
    )
    assert result.row_count == 8
    assert result.source_row_count == 8
    assert result.count_method == "csv-parse"
    assert result.lossless


def test_source_row_counters_agree_on_multiline_file(tmp_path):
    # Unit-level: the physical counter over-counts a multi-line record while the
    # exact CSV counter does not — the two disagree exactly when they should.
    csv_path = tmp_path / "m.csv"
    csv_path.write_text(
        '"a","b"\n"1","plain"\n"2","has\nnewline"\n"3","plain"\n', encoding="utf-8"
    )
    assert importer_mod.count_physical_data_rows(csv_path) == 4  # over-counts by 1
    assert importer_mod.count_csv_records(csv_path, "utf-8") == 3  # exact
    assert importer_mod.count_source_rows(csv_path, "utf-8", imported=3) == (3, "csv-parse")
    assert importer_mod.count_source_rows(csv_path, "utf-8", imported=4) == (4, "line-count")


def test_keep_raw_tables_preserves_untouched_copy(con, tmp_path):
    importer_mod.import_csv(
        con,
        _extract_main(con, tmp_path),
        "QCL",
        keep_raw=True,
    )
    # raw_<code> keeps every original (normalized) column, including labels.
    raw_cols = _cols(con, "raw_qcl")
    assert "area_label" in raw_cols
    assert "domain" in raw_cols


def test_numeric_columns_get_real_types(con, tmp_path):
    # Columns that are numeric on every row are given real numeric types, not text.
    importer_mod.import_archive(con, _make_archive(tmp_path), "QCL", tmp_path / "b")
    types = _coltypes(con, "data_qcl")
    assert types["value"] in ("BIGINT", "DOUBLE")
    assert types["area_code"] == "BIGINT"
    assert types["item_code"] == "BIGINT"
    # The fact table keeps the bare ``year`` (not ``year_code``); numeric years type BIGINT.
    assert types["year"] == "BIGINT"
    assert "year_code" not in types


def test_mixed_value_column_stays_text_and_preserves_cells(con, tmp_path):
    # A single non-numeric cell (a FAOSTAT censored threshold like "<0.1") makes the
    # whole column text so the value is preserved verbatim rather than coerced/dropped.
    main = MAIN_CSV.replace('"3200"', '"<0.1"', 1)
    importer_mod.import_archive(con, _make_archive(tmp_path, main=main), "QCL", tmp_path / "b")
    assert _coltypes(con, "data_qcl")["value"] == "VARCHAR"
    values = {r[0] for r in con.execute('SELECT value FROM data_qcl').fetchall()}
    assert "<0.1" in values


def test_boolean_like_token_not_coerced_to_boolean(con, tmp_path):
    # DuckDB's default inference reads the unit "t" (tonnes) as boolean true; we
    # restrict candidates to numeric-or-text so it stays the literal string "t".
    # Here every Unit == "t", so it is lifted into faostat_constant_column: its
    # recorded value must be "t", not "True".
    main = MAIN_CSV.replace('"ha"', '"t"')
    importer_mod.import_archive(con, _make_archive(tmp_path, main=main), "QCL", tmp_path / "b")
    constants = dict(
        con.execute(
            "SELECT column_name, value FROM faostat_constant_column "
            "WHERE dataset_code = 'QCL'"
        ).fetchall()
    )
    assert constants.get("unit") == "t"


def test_shared_dimension_survives_heterogeneous_code_types(con, tmp_path):
    # dim_item is shared across datasets, but one dataset's item_code is all-numeric
    # (fact column typed BIGINT) while another's is alphanumeric (typed VARCHAR).
    # The shared, text-typed dimension must accept both — importing the numeric one
    # first, which would otherwise fix dim_item.item_code to BIGINT and reject 'F1001'.
    (tmp_path / "a").mkdir()
    (tmp_path / "b2").mkdir()
    numeric_arch = _make_archive(tmp_path / "a", main=MAIN_CSV)
    alpha_main = MAIN_CSV.replace('"15"', '"F1001"').replace('"27"', '"210400TSUB"')
    alpha_arch = _make_archive(tmp_path / "b2", main=alpha_main)

    importer_mod.import_archive(con, numeric_arch, "QCL", tmp_path / "bn")
    importer_mod.import_archive(con, alpha_arch, "FOP", tmp_path / "bf")

    # Shared dimension keeps codes as text and holds both datasets' codes.
    assert _coltypes(con, "dim_item")["item_code"] == "VARCHAR"
    codes = {r[0] for r in con.execute("SELECT item_code FROM dim_item").fetchall()}
    assert {"15", "27", "F1001", "210400TSUB"} <= codes
    # Fact tables keep their own inferred types.
    assert _coltypes(con, "data_qcl")["item_code"] == "BIGINT"
    assert _coltypes(con, "data_fop")["item_code"] == "VARCHAR"
    # The labelled view resolves labels across the type boundary (cast join).
    assert con.execute(
        "SELECT item_label FROM view_fop_labelled WHERE item_code = '210400TSUB'"
    ).fetchone() == ("Rice",)


def test_fact_keeps_year_and_year_code_moves_to_dim(con, tmp_path):
    # The fact table exposes the human-readable `year` (not `year_code`); the
    # source `year_code` is preserved losslessly in dim_year and re-surfaced by
    # the labelled view. This is the one dimension keyed on the bare stem.
    importer_mod.import_archive(con, _make_archive(tmp_path), "QCL", tmp_path / "b")

    fact_cols = _cols(con, "data_qcl")
    assert "year" in fact_cols
    assert "year_code" not in fact_cols

    # dim_year is keyed on `year` and still carries the source `year_code`.
    dim_cols = _cols(con, "dim_year")
    assert {"year", "year_code"} <= set(dim_cols)
    pairs = set(con.execute("SELECT year, year_code FROM dim_year").fetchall())
    assert {("2000", "2000"), ("2001", "2001")} <= pairs

    # The labelled view carries both the fact `year` and the joined-back `year_code`.
    view_cols = _cols(con, "view_qcl_labelled")
    assert {"year", "year_code"} <= set(view_cols)
    row = con.execute(
        "SELECT year, year_code FROM view_qcl_labelled "
        "WHERE area_label = 'France' AND item_label = 'Wheat' "
        "AND element_label = 'Production' AND year = 2001"
    ).fetchone()
    assert row == (2001, "2001")


def test_leading_apostrophe_stripped_but_leading_zeros_kept(con, tmp_path):
    # FAOSTAT prefixes the M49 code with a text-marker apostrophe ('004). It is a
    # spreadsheet artifact, not part of the code, so it is stripped — while the
    # value stays VARCHAR so the leading zero is not lost to a numeric cast.
    importer_mod.import_archive(con, _make_archive(tmp_path), "QCL", tmp_path / "b")

    assert _coltypes(con, "dim_area")["area_code_m49"] == "VARCHAR"
    m49 = {
        r[0]
        for r in con.execute(
            "SELECT area_code_m49 FROM dim_area WHERE dataset_code = 'QCL'"
        ).fetchall()
    }
    assert m49 == {"004", "250"}  # apostrophe gone, leading zero preserved
    # No value anywhere in the labelled view still carries the marker.
    assert con.execute(
        "SELECT COUNT(*) FROM view_qcl_labelled WHERE area_code_m49 LIKE '''%'"
    ).fetchone() == (0,)


def test_strip_leading_apostrophe_only_touches_uniform_columns():
    # Unit-level: a column is stripped only when EVERY non-null value carries the
    # apostrophe (a column-wide text marker). A column where only some rows start
    # with a quote is left untouched, so genuine content is never altered.
    c = duckdb.connect()
    try:
        c.execute(
            "CREATE TABLE t AS SELECT * FROM (VALUES "
            "('''004', '''all', 'some'), "
            "('''250', '''all', '''one'), "
            "(NULL,     '''all', 'plain')) AS v(marker, uniform, mixed)"
        )
        stripped = importer_mod.strip_leading_apostrophe(c, "t")
        assert set(stripped) == {"marker", "uniform"}  # 'mixed' is not uniform

        rows = c.execute("SELECT marker, uniform, mixed FROM t ORDER BY uniform, marker").fetchall()
        # marker/uniform lose the leading quote (NULL is left as NULL); mixed is verbatim.
        assert rows == [
            ("004", "all", "some"),
            ("250", "all", "'one"),
            (None, "all", "plain"),
        ]
    finally:
        c.close()


def _extract_main(con, tmp_path):
    archive = _make_archive(tmp_path)
    return importer_mod.extract_main_csv(archive, tmp_path / "b")
