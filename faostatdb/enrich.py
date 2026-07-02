"""
Optional, clearly-separated enrichment layers (NOT source FAOSTAT content).

FAOSTATdb.md ("Country metadata: useful, but separate source-derived from
package-derived") is emphatic: enrichment must be opt-in and must never be
confused with what FAOSTAT actually published. So this lives in its own
``area_classification`` table (never the source ``data_<code>`` / ``dim_*``
tables) and only runs when ``[enrichment] area_classification = true`` (or
``--enrich-areas``).

Both facts come from one committed, hand-curated file — ``area_classification.csv``
next to this module — authored from world knowledge, *not* from FAOSTAT. It is the
package's editable source of truth (columns: ``area_name, is_country, valid_from,
valid_to``) and is matched to ``dim_area`` by label:

* :func:`enrich_areas` writes ``is_country`` — TRUE for a single country or
  territory (including former single states such as the USSR or Sudan (former)),
  FALSE for a group of countries/areas: continents, sub-regions, economic/political
  unions, income groups, FAO fishing areas, and contemporaneous rollups such as
  "China" (= mainland + Taiwan + Hong Kong + Macao) or "Belgium-Luxembourg".
* :func:`enrich_history` fills ``valid_from`` / ``valid_to`` from the same file for
  the areas whose existence as a distinct entity started or ended within FAOSTAT's
  coverage (USSR → 1991, Sudan (former) → 2011, South Sudan 2011 →, …). Only genuine,
  well-documented political transition years are recorded; every other area is left
  NULL — we never guess a date.

Editing the classification is just editing the CSV and rebuilding — no code change
needed, and the diff is reviewable. Areas present in ``dim_area`` but absent from the
CSV are inserted with ``is_country = NULL`` (unclassified) rather than guessed.
"""

from __future__ import annotations

from pathlib import Path

from .schema import table_exists

# Curated, committed classification data — authored from world knowledge, NOT
# FAOSTAT source content. Ships inside the package so an installed build can find
# it; edit this file (not the code) to change how areas are classified.
AREA_CLASSIFICATION_CSV = Path(__file__).resolve().parent / "area_classification.csv"

DDL_AREA_CLASSIFICATION = """\
CREATE TABLE IF NOT EXISTS area_classification (
    area_code            VARCHAR PRIMARY KEY,
    area_label           VARCHAR,
    is_country           BOOLEAN,
    valid_from           VARCHAR,
    valid_to             VARCHAR
);
"""


def enrich_areas(con, csv_path: "str | Path | None" = None) -> int:
    """(Re)build the optional ``area_classification`` table from ``dim_area`` + the CSV.

    ``is_country`` is looked up from ``area_classification.csv`` (override the path
    with ``csv_path``) by matching ``dim_area.area_label`` case-insensitively to the
    file's ``area_name``. ``valid_from`` / ``valid_to`` are left NULL here — call
    :func:`enrich_history` to fill them. Returns the number of areas written, or 0
    if there is no ``dim_area`` to work from. Idempotent: the table is fully rebuilt
    each call. An area with no matching CSV row is written with ``is_country = NULL``.
    """
    if not table_exists(con, "dim_area"):
        return 0

    csv_path = Path(csv_path) if csv_path is not None else AREA_CLASSIFICATION_CSV
    if not csv_path.exists():
        raise FileNotFoundError(f"area classification CSV not found: {csv_path}")

    con.execute(DDL_AREA_CLASSIFICATION)
    con.execute("DELETE FROM area_classification")

    # Detect whether dim_area carries a label column (it usually does).
    dim_cols = {d[0] for d in con.execute("SELECT * FROM dim_area LIMIT 0").description}
    label_expr = "any_value(area_label)" if "area_label" in dim_cols else "NULL"

    con.execute(
        f"""
        INSERT INTO area_classification
        WITH areas AS (
            SELECT area_code, {label_expr} AS area_label
            FROM dim_area
            WHERE area_code IS NOT NULL
            GROUP BY area_code
        ),
        curated AS (
            SELECT lower(trim(area_name)) AS key,
                   CAST(is_country AS BOOLEAN) AS is_country
            FROM read_csv(?, header = true, all_varchar = true)
        )
        SELECT a.area_code,
               a.area_label,
               c.is_country,
               NULL AS valid_from,
               NULL AS valid_to
        FROM areas a
        LEFT JOIN curated c ON lower(trim(a.area_label)) = c.key
        """,
        [str(csv_path)],
    )
    (n,) = con.execute("SELECT COUNT(*) FROM area_classification").fetchone()
    return n


def enrich_history(con, csv_path: "str | Path | None" = None) -> int:
    """Fill ``valid_from`` / ``valid_to`` in ``area_classification`` from the CSV.

    Requires ``area_classification`` to already exist (build it first with
    :func:`enrich_areas`). For every row whose ``area_label`` matches a CSV entry
    that carries a validity bound, the bound(s) are written. Rows whose entry has no
    date keep NULL validity, so the layer only ever *adds* explicitly-curated,
    well-documented transition years. Returns the number of area rows that now carry
    a validity bound (0 if there is no ``area_classification`` table or nothing
    matched). Idempotent.
    """
    if not table_exists(con, "area_classification"):
        return 0
    if "area_label" not in {
        d[0] for d in con.execute("SELECT * FROM area_classification LIMIT 0").description
    }:
        return 0  # nothing to match on

    csv_path = Path(csv_path) if csv_path is not None else AREA_CLASSIFICATION_CSV
    if not csv_path.exists():
        raise FileNotFoundError(f"area classification CSV not found: {csv_path}")

    con.execute(
        """
        UPDATE area_classification AS ac
        SET valid_from = c.valid_from,
            valid_to   = c.valid_to
        FROM (
            SELECT lower(trim(area_name)) AS key,
                   NULLIF(valid_from, '') AS valid_from,
                   NULLIF(valid_to, '')   AS valid_to
            FROM read_csv(?, header = true, all_varchar = true)
        ) AS c
        WHERE lower(trim(ac.area_label)) = c.key
          AND (c.valid_from IS NOT NULL OR c.valid_to IS NOT NULL)
        """,
        [str(csv_path)],
    )
    (updated,) = con.execute(
        "SELECT COUNT(*) FROM area_classification "
        "WHERE valid_from IS NOT NULL OR valid_to IS NOT NULL"
    ).fetchone()
    return updated
