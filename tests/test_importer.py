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
from faostatdb.config import parse_years

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

# A miniature dataset whose label headers do NOT share their code's stem — the
# shape of the real trade (FT/RFM/TM) and producer-price (PE) datasets:
#   * "Reporter Countries" / "Partner Countries" are the labels for
#     "Reporter Country Code" / "Partner Country Code" (stems reporter_country /
#     partner_country) — plurals that miss the "<stem>_" prefix.
#   * "Currency" is the label for "ISO Currency Code" (stem iso_currency).
# Without the pinned overrides these labels ride along on every fact row; with
# them they must be lifted into dim_reporter_country / dim_partner_country /
# dim_iso_currency. Every dimension varies so nothing is dropped as constant.
TRADE_CSV = """\
"Reporter Country Code","Reporter Countries","Partner Country Code","Partner Countries","Item Code","Item","Element Code","Element","ISO Currency Code","Currency","Year Code","Year","Value","Flag"
"4","Afghanistan","231","United States of America","15","Wheat","5610","Import","USD","US Dollar","2000","2000","100","A"
"68","France","276","Germany","15","Wheat","5910","Export","EUR","Euro","2000","2000","200","A"
"4","Afghanistan","276","Germany","27","Rice","5610","Import","USD","US Dollar","2001","2001","300","E"
"68","France","231","United States of America","27","Rice","5910","Export","EUR","Euro","2001","2001","400","A"
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


def test_year_filter_keeps_only_selected_years(con, tmp_path):
    # MAIN_CSV holds years 2000 (6 rows) and 2001 (2 rows). Filtering to 2000
    # keeps exactly the 6 matching rows, and every stored year is 2000.
    result = importer_mod.import_archive(
        con, _make_archive(tmp_path), "QCL", tmp_path / "b", years={2000}
    )
    assert result.row_count == 6
    assert result.year_filter == (2000,)
    # The full delivered CSV count is still recorded for provenance...
    assert result.source_row_count == 8
    # ...and a filtered subset is reported lossless (no matching row dropped).
    assert result.lossless
    # year is protected from constant-column removal (so a later --years build can
    # merge on it), so it stays a real column even when the subset is one year.
    years = {r[0] for r in con.execute("SELECT DISTINCT year FROM data_qcl").fetchall()}
    assert years == {2000}


def test_year_filter_range_selects_all_matching(con, tmp_path):
    result = importer_mod.import_archive(
        con, _make_archive(tmp_path), "QCL", tmp_path / "b", years={2000, 2001}
    )
    assert result.row_count == 8  # both years present -> whole file
    assert result.year_filter == (2000, 2001)


def test_year_filter_open_ended_selects_matching_years(con, tmp_path):
    result = importer_mod.import_archive(
        con, _make_multi(tmp_path), "QCL", tmp_path / "b", years=parse_years("2001-")
    )
    assert result.row_count == 3
    assert result.year_filter == (2001, 2002)
    years = {r[0] for r in con.execute("SELECT DISTINCT year FROM data_qcl").fetchall()}
    assert years == {2001, 2002}


def test_year_filter_empty_result_when_no_year_matches(con, tmp_path):
    result = importer_mod.import_archive(
        con, _make_archive(tmp_path), "QCL", tmp_path / "b", years={1975}
    )
    assert result.row_count == 0
    assert result.year_filter == (1975,)


NO_YEAR_CSV = """\
"Area Code","Area","Item Code","Item","Value","Flag"
"2","Afghanistan","15","Wheat","3200","A"
"68","France","15","Wheat","37000","A"
"""

# A three-year dataset used to exercise accumulate-across-builds. France gains a
# new item (Barley) in 2002 so a new dimension member must be merged in too.
MULTI_YEAR_CSV = """\
"Domain Code","Domain","Area Code","Area Code (M49)","Area","Item Code","Item","Element Code","Element","Year Code","Year","Unit","Value","Flag"
"QCL","Crops","2","'004","Afghanistan","15","Wheat","5510","Production","2000","2000","t","3200","A"
"QCL","Crops","68","'250","France","15","Wheat","5510","Production","2000","2000","t","37000","A"
"QCL","Crops","2","'004","Afghanistan","15","Wheat","5510","Production","2001","2001","t","3300","E"
"QCL","Crops","68","'250","France","15","Wheat","5510","Production","2001","2001","t","36500","E"
"QCL","Crops","68","'250","France","44","Barley","5510","Production","2002","2002","t","900","A"
"""


def _make_multi(tmp_path, code="QCL"):
    archive = tmp_path / f"{code}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{code}_E_All_Data.csv", MULTI_YEAR_CSV)
        zf.writestr(f"{code}_E_Flags.csv", FLAG_CSV)
    return archive


def test_accumulate_years_across_builds_keeps_both(con, tmp_path):
    # Build 2000, then 2001 into the SAME database: both years must end up present,
    # and 2000 must not be reprocessed/lost.
    importer_mod.import_archive(con, _make_multi(tmp_path), "QCL", tmp_path / "b1", years={2000})
    assert {r[0] for r in con.execute("SELECT DISTINCT year FROM data_qcl").fetchall()} == {2000}

    result = importer_mod.import_archive(
        con, _make_multi(tmp_path), "QCL", tmp_path / "b2", years={2001}
    )
    assert result.appended_rows == 2          # the two 2001 rows
    assert result.row_count == 4              # 2 (2000) + 2 (2001) now in the table
    assert result.year_filter == (2001,)
    assert result.lossless
    years = {r[0] for r in con.execute("SELECT DISTINCT year FROM data_qcl").fetchall()}
    assert years == {2000, 2001}


def test_accumulate_merges_new_dimension_member(con, tmp_path):
    # 2002 introduces Barley (item 44); accumulating it must add the new dim_item
    # member without dropping Wheat from the earlier years.
    importer_mod.import_archive(con, _make_multi(tmp_path), "QCL", tmp_path / "b1", years={2000})
    importer_mod.import_archive(con, _make_multi(tmp_path), "QCL", tmp_path / "b2", years={2002})

    items = {
        r[0]
        for r in con.execute(
            "SELECT item_label FROM dim_item WHERE dataset_code = 'QCL'"
        ).fetchall()
    }
    assert {"Wheat", "Barley"} <= items
    # The labelled view resolves Barley (2002) and Wheat (2000) side by side.
    rows = con.execute(
        "SELECT year, item_label, value FROM view_qcl_labelled ORDER BY year, value"
    ).fetchall()
    assert (2000, "Wheat", 3200) in rows
    assert (2002, "Barley", 900) in rows


def test_accumulate_same_year_is_idempotent(con, tmp_path):
    # Re-running a year that is already present refreshes it rather than duplicating.
    importer_mod.import_archive(con, _make_multi(tmp_path), "QCL", tmp_path / "b1", years={2000})
    first = con.execute("SELECT COUNT(*) FROM data_qcl").fetchone()[0]
    result = importer_mod.import_archive(
        con, _make_multi(tmp_path), "QCL", tmp_path / "b2", years={2000}
    )
    assert result.appended_rows == 2
    assert con.execute("SELECT COUNT(*) FROM data_qcl").fetchone()[0] == first  # no dupes


def test_accumulate_open_ended_years_refreshes_actual_incoming_years(con, tmp_path):
    importer_mod.import_archive(con, _make_multi(tmp_path), "QCL", tmp_path / "b1", years={2000})
    result = importer_mod.import_archive(
        con, _make_multi(tmp_path), "QCL", tmp_path / "b2", years=parse_years("2001-")
    )
    assert result.appended_rows == 3
    assert result.row_count == 5
    assert result.year_filter == (2001, 2002)
    years = {r[0] for r in con.execute("SELECT DISTINCT year FROM data_qcl").fetchall()}
    assert years == {2000, 2001, 2002}


def test_full_build_replaces_not_accumulates(con, tmp_path):
    # A build WITHOUT a year filter is a full rebuild: it replaces the dataset even
    # if a table already exists (accumulation is a year-slicing feature only).
    importer_mod.import_archive(con, _make_multi(tmp_path), "QCL", tmp_path / "b1", years={2000})
    result = importer_mod.import_archive(con, _make_multi(tmp_path), "QCL", tmp_path / "b2")
    assert result.appended_rows is None
    assert result.row_count == 5  # the whole 3-year dataset
    years = {r[0] for r in con.execute("SELECT DISTINCT year FROM data_qcl").fetchall()}
    assert years == {2000, 2001, 2002}


def test_year_filter_ignored_when_no_year_column(con, tmp_path):
    # A dataset without a "Year" column can't be filtered; it imports in full and
    # year_filter is None so normal full-CSV losslessness still applies.
    result = importer_mod.import_archive(
        con, _make_archive(tmp_path, main=NO_YEAR_CSV), "QCL", tmp_path / "b", years={2000}
    )
    assert result.year_filter is None
    assert result.row_count == 2
    assert result.lossless


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
    # Small integers infer INT32; the value column is all-integer here so it does too.
    assert types["value"] in ("INTEGER", "BIGINT", "DOUBLE")
    assert types["area_code"] == "INTEGER"
    assert types["item_code"] == "INTEGER"
    # The fact table keeps the bare ``year`` (not ``year_code``); numeric years type INTEGER.
    assert types["year"] == "INTEGER"
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
    # (fact column typed INTEGER) while another's is alphanumeric (typed VARCHAR).
    # The shared, text-typed dimension must accept both — importing the numeric one
    # first, which would otherwise fix dim_item.item_code to INTEGER and reject 'F1001'.
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
    assert _coltypes(con, "data_qcl")["item_code"] == "INTEGER"
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


def test_mismatched_name_labels_are_lifted_into_dimensions(con, tmp_path):
    # Reporter/partner-country labels and the currency label have header names that
    # don't share their code's stem; they must still be moved out of the fact table
    # into their dimensions (not left duplicated on every row).
    importer_mod.import_archive(
        con, _make_archive(tmp_path, main=TRADE_CSV), "FT", tmp_path / "b"
    )

    fact_cols = set(_cols(con, "data_ft"))
    # Only the keys stay in the fact table.
    assert {"reporter_country_code", "partner_country_code", "iso_currency_code"} <= fact_cols
    # The labels are gone from the fact table (both the raw plural name and the
    # pinned <stem>_label form).
    assert fact_cols.isdisjoint(
        {
            "reporter_countries",
            "reporter_country_label",
            "partner_countries",
            "partner_country_label",
            "currency",
            "iso_currency_label",
        }
    )

    # Each label landed in its shared dimension, keyed by the code.
    reporters = dict(
        con.execute(
            "SELECT reporter_country_code, reporter_country_label "
            "FROM dim_reporter_country WHERE dataset_code = 'FT'"
        ).fetchall()
    )
    assert reporters == {"4": "Afghanistan", "68": "France"}

    currencies = dict(
        con.execute(
            "SELECT iso_currency_code, iso_currency_label "
            "FROM dim_iso_currency WHERE dataset_code = 'FT'"
        ).fetchall()
    )
    assert currencies == {"USD": "US Dollar", "EUR": "Euro"}

    # The labelled view re-surfaces the lifted labels with no join fan-out (still 4 rows).
    (n,) = con.execute("SELECT COUNT(*) FROM view_ft_labelled").fetchone()
    assert n == 4
    row = con.execute(
        "SELECT reporter_country_label, partner_country_label, iso_currency_label "
        "FROM view_ft_labelled WHERE reporter_country_code = 68 AND value = 200"
    ).fetchone()
    assert row == ("France", "Germany", "Euro")


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
