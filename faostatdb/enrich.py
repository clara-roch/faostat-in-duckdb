"""
Optional, clearly-separated enrichment layers (NOT source FAOSTAT content).

FAOSTATdb.md ("Country metadata: useful, but separate source-derived from
package-derived") is emphatic: enrichment must be opt-in and must never be
confused with what FAOSTAT actually published. So this lives in its own table,
carries an explicit ``classification_source`` + ``confidence``, and only runs
when ``[enrichment] area_classification = true`` (or ``--enrich-areas``).

Two enrichment steps live here:

* :func:`enrich_areas` — a **heuristic** country/region/aggregate classification.
  FAOSTAT area codes at or above 5000 are regional/economic aggregates ("World",
  "Africa", "European Union", …); everything below is treated as an individual
  country/territory. Confidence is ``low`` because it is a rule of thumb.
* :func:`enrich_history` — a small **curated gazetteer** filling ``valid_from`` /
  ``valid_to`` for the well-known dissolved / renamed / newly-formed FAOSTAT areas
  (USSR, Czechoslovakia, Sudan (former) → South Sudan, …). These are widely
  documented political transition years, so matched rows are marked ``high``
  confidence with an explicit ``classification_source``. Areas not in the
  gazetteer keep whatever :func:`enrich_areas` left (validity NULL) — we never
  guess a date. This is the opt-in ``historical_validity`` layer from
  FAOSTATdb.md, kept deliberately conservative because, as the spec warns,
  "historical country validity is a delicate issue".
"""

from __future__ import annotations

from .schema import table_exists

DDL_AREA_CLASSIFICATION = """\
CREATE TABLE IF NOT EXISTS area_classification (
    area_code            VARCHAR PRIMARY KEY,
    area_label           VARCHAR,
    is_country           BOOLEAN,
    is_region            BOOLEAN,
    is_aggregate         BOOLEAN,
    classification_source VARCHAR,
    valid_from           VARCHAR,
    valid_to             VARCHAR,
    confidence           VARCHAR
);
"""

# Label keywords that signal an aggregate even if the code heuristic misses it.
_AGG_KEYWORDS = (
    "World",
    "Total",
    "Africa",
    "Americas",
    "Asia",
    "Europe",
    "Oceania",
    "Union",
    "income",
    "developed",
    "developing",
    "Least Developed",
    "Net Food",
    "Small Island",
    "Land Locked",
)


def enrich_areas(con) -> int:
    """(Re)build the optional ``area_classification`` table from ``dim_area``.

    Returns the number of areas classified, or 0 if there is no ``dim_area`` to
    work from. Idempotent: the table is fully rebuilt each call.
    """
    if not table_exists(con, "dim_area"):
        return 0

    con.execute(DDL_AREA_CLASSIFICATION)
    con.execute("DELETE FROM area_classification")

    # Detect whether dim_area carries a label column (it usually does).
    dim_cols = {d[0] for d in con.execute("SELECT * FROM dim_area LIMIT 0").description}
    label_expr = "any_value(area_label)" if "area_label" in dim_cols else "NULL"

    like_clauses = " OR ".join(
        [f"lbl ILIKE '%{kw}%'" for kw in _AGG_KEYWORDS]
    )

    con.execute(
        f"""
        INSERT OR REPLACE INTO area_classification
        WITH areas AS (
            SELECT area_code, {label_expr} AS lbl
            FROM dim_area
            WHERE area_code IS NOT NULL
            GROUP BY area_code
        ),
        flagged AS (
            SELECT
                area_code,
                lbl AS area_label,
                -- Aggregate if the code is >= 5000 OR the label reads like a group.
                (COALESCE(TRY_CAST(area_code AS BIGINT) >= 5000, FALSE)
                 OR COALESCE(({like_clauses}), FALSE)) AS is_agg
            FROM areas
        )
        SELECT
            area_code,
            area_label,
            NOT is_agg              AS is_country,
            is_agg                  AS is_region,
            is_agg                  AS is_aggregate,
            'faostatdb-heuristic-v1' AS classification_source,
            NULL                    AS valid_from,
            NULL                    AS valid_to,
            'low'                   AS confidence
        FROM flagged
        """
    )
    (n,) = con.execute("SELECT COUNT(*) FROM area_classification").fetchone()
    return n


# --- Historical country-validity gazetteer ---------------------------------
#
# A small, hand-curated table of the FAOSTAT areas whose existence as a distinct
# reporting entity started or ended within (or just before) FAOSTAT's coverage.
# Each entry records only the transition year(s) that a FAOSTAT user actually
# needs to interpret a time series correctly:
#
#   * ``valid_to``   — the last year a dissolved / merged entity reports data.
#   * ``valid_from`` — the first year a newly-formed / split-off entity reports.
#
# The *other* bound is left ``None`` on purpose: we assert only the well-agreed
# political transition year, not a founding year we would have to hand-wave. All
# dates below are widely documented (e.g. the USSR dissolved in 1991, South Sudan
# separated from Sudan in 2011), so matches are recorded at ``high`` confidence.
# China-family codes are deliberately omitted — the mainland / Taiwan / Hong Kong
# / Macao split is genuinely delicate and we would rather under-claim than guess.
#
# Keys are the canonical FAOSTAT English area labels, matched case-insensitively
# against ``dim_area.area_label``. Matching by label (not numeric code) keeps this
# auditable and stable across the different code systems FAOSTAT uses.
GAZETTEER_SOURCE = "faostatdb-gazetteer-v1"

# label -> (valid_from, valid_to)
HISTORICAL_VALIDITY: dict[str, tuple[str | None, str | None]] = {
    # Dissolved / merged entities: we know the last reporting year.
    "USSR": (None, "1991"),
    "Czechoslovakia": (None, "1992"),
    "Yugoslav SFR": (None, "1992"),
    "Serbia and Montenegro": ("1992", "2006"),
    "Ethiopia PDR": (None, "1992"),
    "Sudan (former)": (None, "2011"),
    "Belgium-Luxembourg": (None, "1999"),
    "Netherlands Antilles (former)": (None, "2010"),
    "Yemen Ar Rp": (None, "1990"),
    "Yemen Dem": (None, "1990"),
    "Pacific Islands Trust Territory": (None, "1991"),
    # Newly-formed / split-off entities: we know the first reporting year.
    "South Sudan": ("2011", None),
    "Serbia": ("2006", None),
    "Montenegro": ("2006", None),
    "Eritrea": ("1993", None),
    "Czechia": ("1993", None),
    "Slovakia": ("1993", None),
}


def enrich_history(con) -> int:
    """Fill ``valid_from`` / ``valid_to`` in ``area_classification`` from the gazetteer.

    Requires ``area_classification`` to already exist (build it first with
    :func:`enrich_areas`). For every row whose ``area_label`` matches a curated
    entry (case-insensitively), the known validity bound(s) are written and the
    row is re-stamped ``classification_source = 'faostatdb-gazetteer-v1'`` /
    ``confidence = 'high'``. Rows with no gazetteer entry are left untouched, so
    the layer only ever *adds* explicitly-sourced facts.

    Returns the number of area rows updated (0 if there is no
    ``area_classification`` table or nothing matched). Idempotent.
    """
    if not table_exists(con, "area_classification"):
        return 0
    if "area_label" not in {
        d[0] for d in con.execute("SELECT * FROM area_classification LIMIT 0").description
    }:
        return 0  # nothing to match on

    updated = 0
    for label, (valid_from, valid_to) in HISTORICAL_VALIDITY.items():
        # Case-insensitive exact-label match; TRIM guards stray whitespace.
        con.execute(
            """
            UPDATE area_classification
            SET valid_from = ?,
                valid_to = ?,
                classification_source = ?,
                confidence = 'high'
            WHERE lower(trim(area_label)) = lower(?)
            """,
            [valid_from, valid_to, GAZETTEER_SOURCE, label],
        )
        # DuckDB's UPDATE doesn't return a rowcount via execute(); count matches.
        (matched,) = con.execute(
            "SELECT COUNT(*) FROM area_classification "
            "WHERE lower(trim(area_label)) = lower(?)",
            [label],
        ).fetchone()
        updated += matched
    return updated
