"""Optional, clearly-separated enrichment layers (NOT source FAOSTAT content).

FAOSTATdb.md ("Country metadata: useful, but separate source-derived from
package-derived") is emphatic: enrichment must be opt-in and must never be
confused with what FAOSTAT actually published. So this lives in its own table,
carries an explicit ``classification_source`` + ``confidence``, and only runs
when ``[enrichment] area_classification = true`` (or ``--enrich-areas``).

The classification here is a **heuristic**, not an authoritative gazetteer:
FAOSTAT area codes at or above 5000 are regional/economic aggregates ("World",
"Africa", "European Union", …), everything below is treated as an individual
country/territory. Historical validity (``valid_from`` / ``valid_to`` — e.g. the
USSR dissolving in 1991) needs a curated external source we don't ship, so those
columns exist for schema stability but are left NULL rather than guessed.
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
