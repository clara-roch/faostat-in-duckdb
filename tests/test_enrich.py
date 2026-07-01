"""Tests for the optional area-enrichment layers (offline, deterministic).

Covers both the heuristic classification (``enrich_areas``) and the curated
historical-validity gazetteer (``enrich_history``). A tiny in-memory ``dim_area``
stands in for a real import so no download/build is needed.
"""

from __future__ import annotations

import duckdb
import pytest

from faostatdb import enrich as enrich_mod


@pytest.fixture()
def con():
    c = duckdb.connect()  # in-memory
    # Minimal dim_area shaped like the importer's output: (dataset_code,
    # area_code, area_label). A country, an aggregate, and two historical areas.
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
        "SELECT is_country, is_aggregate, valid_from, valid_to, "
        "classification_source, confidence "
        "FROM area_classification WHERE area_label = ?",
        [label],
    ).fetchone()


def test_enrich_areas_classifies_country_vs_aggregate(con):
    n = enrich_mod.enrich_areas(con)
    assert n == 5  # every distinct area classified

    france = _row(con, "France")
    assert france[0] is True and france[1] is False  # country, not aggregate

    world = _row(con, "World")
    assert world[0] is False and world[1] is True  # aggregate (code >= 5000)


def test_enrich_areas_without_dim_area_is_noop():
    c = duckdb.connect()
    try:
        assert enrich_mod.enrich_areas(c) == 0
    finally:
        c.close()


def test_enrich_history_fills_validity_for_known_areas(con):
    enrich_mod.enrich_areas(con)
    updated = enrich_mod.enrich_history(con)
    # USSR, Sudan (former) and South Sudan are in the gazetteer; France/World not.
    assert updated == 3

    ussr = _row(con, "USSR")
    assert ussr[2] is None          # valid_from left NULL (no founding-year guess)
    assert ussr[3] == "1991"        # dissolved 1991
    assert ussr[4] == enrich_mod.GAZETTEER_SOURCE
    assert ussr[5] == "high"

    ssd = _row(con, "South Sudan")
    assert ssd[2] == "2011"         # split off in 2011
    assert ssd[3] is None


def test_enrich_history_leaves_current_countries_untouched(con):
    enrich_mod.enrich_areas(con)
    enrich_mod.enrich_history(con)
    france = _row(con, "France")
    assert france[2] is None and france[3] is None       # no validity asserted
    assert france[4] == "faostatdb-heuristic-v1"         # still the heuristic stamp
    assert france[5] == "low"


def test_enrich_history_requires_classification_table(con):
    # Without a prior enrich_areas there is no area_classification to fill.
    assert enrich_mod.enrich_history(con) == 0


def test_enrich_history_is_idempotent(con):
    enrich_mod.enrich_areas(con)
    first = enrich_mod.enrich_history(con)
    second = enrich_mod.enrich_history(con)
    assert first == second == 3
