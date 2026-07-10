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

import importlib.resources
from contextlib import contextmanager
from pathlib import Path

from .schema import table_exists

# Curated, committed classification data — authored from world knowledge, NOT
# FAOSTAT source content. Ships inside the package so an installed build can find
# it; edit this file (not the code) to change how areas are classified.
AREA_CLASSIFICATION_RESOURCE = "area_classification.csv"
# On-disk path when the package is a real directory. Reads go through
# :func:`_classification_csv` (not this path directly) so a ``self-contained``
# ``.pyz`` build — where the packaged CSV is not a real filesystem file — works too.
AREA_CLASSIFICATION_CSV = Path(__file__).resolve().parent / AREA_CLASSIFICATION_RESOURCE


@contextmanager
def _classification_csv(csv_path: "str | Path | None"):
    """Yield a real filesystem path to the classification CSV for DuckDB.

    An explicit ``csv_path`` (used by tests) is honoured verbatim. Otherwise the
    committed package resource is resolved with :mod:`importlib.resources` and, when
    it lives inside a zip (a ``self-contained`` ``.pyz``), extracted to a temporary
    file — DuckDB's ``read_csv`` needs a genuine path, and ``Path(__file__).parent``
    does not resolve to one inside a zipapp. Raises :class:`FileNotFoundError` if the
    file is genuinely absent.
    """
    if csv_path is not None:
        p = Path(csv_path)
        if not p.exists():
            raise FileNotFoundError(f"area classification CSV not found: {p}")
        yield p
        return
    resource = importlib.resources.files(__package__).joinpath(
        AREA_CLASSIFICATION_RESOURCE
    )
    with importlib.resources.as_file(resource) as p:
        if not p.exists():
            raise FileNotFoundError(f"area classification CSV not found: {resource}")
        yield p

# This table is package-derived (not source FAOSTAT content), so it is free to use
# natural types: the FAO internal ``area_code`` and the transition years are stored
# as ``INTEGER``.
#
# This intentionally differs from source ``dim_area.area_code``, which is stored as
# ``VARCHAR`` by the generic shared-dimension policy. That text storage keeps all
# dim_* tables robust to heterogeneous code types across datasets. In the complete
# FAOSTAT bulk inventory checked during development, however, every source
# ``area_code`` value is canonical integer text (no non-integer values, no leading
# zeros, no collisions after integer casting), and every fact-table ``area_code``
# infers as INTEGER. The code that must stay text is the alternate M49 code
# (``area_code_m49``), where values such as "001" carry leading zeros.
DDL_AREA_CLASSIFICATION = """\
CREATE TABLE IF NOT EXISTS area_classification (
    area_code            INTEGER PRIMARY KEY,
    area_label           VARCHAR,
    is_country           BOOLEAN,
    valid_from           INTEGER,
    valid_to             INTEGER
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

    The same FAO ``area_code`` can carry slightly different labels across datasets
    (e.g. ``"Least Developed Countries"`` vs ``"Least Developed Countries (LDCs)"``).
    We classify a code if **any** of its label variants matches the curated file, and
    pick the representative label and classification with deterministic aggregates
    (``bool_or`` / ``max``) — so a rebuild always yields the same result, rather than
    depending on which variant an arbitrary ``any_value`` happened to pick.
    """
    if not table_exists(con, "dim_area"):
        return 0

    con.execute(DDL_AREA_CLASSIFICATION)
    con.execute("DELETE FROM area_classification")

    # Detect whether dim_area carries a label column (it usually does).
    dim_cols = {d[0] for d in con.execute("SELECT * FROM dim_area LIMIT 0").description}

    if "area_label" not in dim_cols:
        # No label to match on: write every area unclassified (is_country NULL).
        con.execute(
            "INSERT INTO area_classification "
            "SELECT DISTINCT TRY_CAST(area_code AS INTEGER), NULL, NULL, "
            "CAST(NULL AS INTEGER), CAST(NULL AS INTEGER) "
            "FROM dim_area WHERE area_code IS NOT NULL"
        )
        (n,) = con.execute("SELECT COUNT(*) FROM area_classification").fetchone()
        return n

    with _classification_csv(csv_path) as csv_file:
        con.execute(
            """
            INSERT INTO area_classification
            WITH areas AS (
                SELECT DISTINCT area_code, area_label
                FROM dim_area
                WHERE area_code IS NOT NULL
            ),
            curated AS (
                SELECT lower(trim(area_name)) AS key,
                       CAST(is_country AS BOOLEAN) AS is_country
                FROM read_csv(?, header = true, all_varchar = true)
            ),
            matched AS (
                SELECT a.area_code, a.area_label, c.is_country,
                       (c.key IS NOT NULL) AS is_match
                FROM areas a
                LEFT JOIN curated c ON lower(trim(a.area_label)) = c.key
            )
            SELECT TRY_CAST(area_code AS INTEGER) AS area_code,
                   -- prefer a label that matched the CSV; fall back to any label.
                   COALESCE(max(area_label) FILTER (WHERE is_match),
                            max(area_label)) AS area_label,
                   -- classified if any variant matched; deterministic, NULL if none.
                   bool_or(is_country) AS is_country,
                   CAST(NULL AS INTEGER) AS valid_from,
                   CAST(NULL AS INTEGER) AS valid_to
            FROM matched
            GROUP BY area_code
            """,
            [str(csv_file)],
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

    with _classification_csv(csv_path) as csv_file:
        con.execute(
            """
            UPDATE area_classification AS ac
            SET valid_from = c.valid_from,
                valid_to   = c.valid_to
            FROM (
                SELECT lower(trim(area_name)) AS key,
                       TRY_CAST(NULLIF(valid_from, '') AS INTEGER) AS valid_from,
                       TRY_CAST(NULLIF(valid_to, '')   AS INTEGER) AS valid_to
                FROM read_csv(?, header = true, all_varchar = true)
            ) AS c
            WHERE lower(trim(ac.area_label)) = c.key
              AND (c.valid_from IS NOT NULL OR c.valid_to IS NOT NULL)
            """,
            [str(csv_file)],
        )
    (updated,) = con.execute(
        "SELECT COUNT(*) FROM area_classification "
        "WHERE valid_from IS NOT NULL OR valid_to IS NOT NULL"
    ).fetchone()
    return updated
