"""Column-name normalization and metadata-table DDL.

Source preservation is the prime directive: we normalize *names* to stable
``snake_case`` but never drop columns or alter values. The metadata tables
``faostat_dataset`` and ``faostat_build`` record reproducibility provenance.
"""

from __future__ import annotations

import re

# Explicit overrides for names that the generic algorithm would mangle or that
# we want to pin for query stability. Keys are matched case-insensitively against
# the raw header after stripping surrounding whitespace.
COLUMN_OVERRIDES: dict[str, str] = {
    "item": "item_label",
    "element": "element_label",
    "area": "area_label",
    "flag": "flag_code",
    "value": "value",
    "year": "year",
    "unit": "unit",
}

_PAREN = re.compile(r"\(([^)]*)\)")
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def normalize_column(name: str) -> str:
    """Normalize a single FAOSTAT CSV header to stable ``snake_case``.

    Examples
    --------
    >>> normalize_column("Area Code (M49)")
    'area_code_m49'
    >>> normalize_column("Item")
    'item_label'
    >>> normalize_column("Value")
    'value'
    >>> normalize_column("Flag")
    'flag_code'
    >>> normalize_column("Months Code")
    'months_code'
    """
    raw = name.strip()
    override = COLUMN_OVERRIDES.get(raw.lower())
    if override is not None:
        return override

    # Bring parenthesised qualifiers inline: "Area Code (M49)" -> "Area Code M49".
    inlined = _PAREN.sub(lambda m: " " + m.group(1), raw)
    lowered = inlined.lower()
    snake = _NON_ALNUM.sub("_", lowered).strip("_")
    return snake


def normalize_columns(names: list[str]) -> list[str]:
    """Normalize a list of headers, disambiguating any collisions with a suffix."""
    out: list[str] = []
    seen: dict[str, int] = {}
    for name in names:
        base = normalize_column(name)
        if base in seen:
            seen[base] += 1
            out.append(f"{base}_{seen[base]}")
        else:
            seen[base] = 0
            out.append(base)
    return out


# --- Metadata tables -------------------------------------------------------

DDL_FAOSTAT_DATASET = """\
CREATE TABLE IF NOT EXISTS faostat_dataset (
    dataset_code        VARCHAR PRIMARY KEY,
    dataset_name        VARCHAR,
    date_update         VARCHAR,
    file_location       VARCHAR,
    file_size_raw       BIGINT,
    file_rows_declared  BIGINT,
    downloaded_at       TIMESTAMP,
    source_metadata_url VARCHAR,
    source_metadata_hash VARCHAR,
    archive_sha256      VARCHAR,
    import_status       VARCHAR
);
"""

DDL_FAOSTAT_BUILD = """\
CREATE TABLE IF NOT EXISTS faostat_build (
    build_id                VARCHAR PRIMARY KEY,
    started_at              TIMESTAMP,
    completed_at            TIMESTAMP,
    faostatdb_version       VARCHAR,
    duckdb_version          VARCHAR,
    python_version          VARCHAR,
    os                      VARCHAR,
    metadata_snapshot_sha256 VARCHAR,
    command_line            VARCHAR,
    config_sha256           VARCHAR
);
"""


def create_metadata_tables(con) -> None:
    """Create the ``faostat_dataset`` / ``faostat_build`` tables if absent."""
    con.execute(DDL_FAOSTAT_DATASET)
    con.execute(DDL_FAOSTAT_BUILD)
