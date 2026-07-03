"""Tests for the optional area-enrichment layers (offline, deterministic).

Covers the curated classification (``enrich_areas``) and the historical-validity
fill (``enrich_history``), both driven by the committed ``area_classification.csv``.
A tiny in-memory ``dim_area`` stands in for a real import so no download/build is
needed; a few well-known anchor areas are asserted against the real CSV.
"""

from __future__ import annotations

import duckdb
import pytest

from faostatdb import enrich as enrich_mod


@pytest.fixture()
def con():
    c = duckdb.connect()  # in-memory
    # Minimal dim_area shaped like the importer's output: (dataset_code, area_code,
    # area_label). Anchor areas whose classification/validity are stable in
    # area_classification.csv: a current country, an aggregate, a former single
    # state, and a former/successor pair. (Classification is by label now, so the
    # codes are just realistic filler.)
    c.execute(
        "CREATE TABLE dim_area (dataset_code VARCHAR, area_code VARCHAR, area_label VARCHAR)"
    )
    c.executemany(
        "INSERT INTO dim_area VALUES (?, ?, ?)",
        [
            ("QCL", "68", "France"),
            ("QCL", "5000", "World"),
            ("QCL", "228", "USSR"),
            ("QCL", "206", "Sudan (former)"),
            ("QCL", "277", "South Sudan"),
        ],
    )
    try:
        yield c
    finally:
        c.close()


def _row(con, label):
    return con.execute(
        "SELECT is_country, valid_from, valid_to "
        "FROM area_classification WHERE area_label = ?",
        [label],
    ).fetchone()


def test_enrich_areas_classifies_country_vs_aggregate(con):
    n = enrich_mod.enrich_areas(con)
    assert n == 5  # every distinct area written

    assert _row(con, "France")[0] is True   # a single country
    assert _row(con, "World")[0] is False   # an aggregate
    assert _row(con, "USSR")[0] is True     # a former single state is still a country


def test_area_classification_schema_is_minimal(con):
    # The table stores only the facts the CSV carries: is_country plus
    # valid_from/valid_to. No is_aggregate (just NOT is_country) and no per-row
    # confidence/source column — guard against redundant columns creeping back.
    enrich_mod.enrich_areas(con)
    cols = {
        d[0] for d in con.execute("SELECT * FROM area_classification LIMIT 0").description
    }
    assert cols == {"area_code", "area_label", "is_country", "valid_from", "valid_to"}


def test_area_classification_uses_integer_codes_and_years(con):
    # This is package-derived, not source, so the FAO area_code and the transition
    # years are stored as INTEGER (not the VARCHAR the shared source dims use).
    enrich_mod.enrich_areas(con)
    enrich_mod.enrich_history(con)
    types = {r[0]: r[1] for r in con.execute("DESCRIBE area_classification").fetchall()}
    assert types["area_code"] == "INTEGER"
    assert types["valid_from"] == "INTEGER"
    assert types["valid_to"] == "INTEGER"
    # Values come back as ints, not strings.
    (france_code,) = con.execute(
        "SELECT area_code FROM area_classification WHERE area_label = 'France'"
    ).fetchone()
    assert france_code == 68
    assert _row(con, "USSR")[2] == 1991


def test_enrich_areas_without_dim_area_is_noop():
    c = duckdb.connect()
    try:
        assert enrich_mod.enrich_areas(c) == 0
    finally:
        c.close()


def test_unknown_area_is_left_unclassified(con, tmp_path):
    # An area absent from the CSV is written with is_country = NULL, never guessed.
    csv = tmp_path / "areas.csv"
    csv.write_text(
        "area_name,is_country,valid_from,valid_to\nFrance,true,,\n", encoding="utf-8"
    )
    enrich_mod.enrich_areas(con, csv_path=csv)
    assert _row(con, "France")[0] is True
    assert _row(con, "World")[0] is None  # not in this CSV -> unclassified


def test_enrich_history_fills_validity_for_known_areas(con):
    enrich_mod.enrich_areas(con)
    updated = enrich_mod.enrich_history(con)
    # USSR, Sudan (former) and South Sudan carry validity in the CSV; France/World not.
    assert updated == 3

    ussr = _row(con, "USSR")
    assert ussr[1] is None          # valid_from left NULL (no founding-year guess)
    assert ussr[2] == 1991          # dissolved 1991 (stored as INTEGER)

    ssd = _row(con, "South Sudan")
    assert ssd[1] == 2011           # split off in 2011 (stored as INTEGER)
    assert ssd[2] is None


def test_enrich_history_leaves_current_countries_untouched(con):
    enrich_mod.enrich_areas(con)
    enrich_mod.enrich_history(con)
    france = _row(con, "France")
    assert france[0] is True                             # still classified a country
    assert france[1] is None and france[2] is None       # no validity asserted


def test_enrich_history_requires_classification_table(con):
    # Without a prior enrich_areas there is no area_classification to fill.
    assert enrich_mod.enrich_history(con) == 0


def test_enrich_history_is_idempotent(con):
    enrich_mod.enrich_areas(con)
    first = enrich_mod.enrich_history(con)
    second = enrich_mod.enrich_history(con)
    assert first == second == 3
