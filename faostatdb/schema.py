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


# --- Dimension extraction --------------------------------------------------
#
# FAOSTAT fact rows repeat dimension attributes on every line: a single area is
# carried as ``area_code`` + ``area_code_m49`` + ``area_label`` on millions of
# rows, an item as ``item_code`` + ``item_code_cpc`` + ``item_label``, a year as
# ``year_code`` + ``year``. We keep only the ``<stem>_code`` key in the fact
# table and move the redundant attribute columns into a shared ``dim_<stem>``
# table (keyed by ``(dataset_code, <stem>_code)``). This removes storage-level
# duplication without dropping or altering any source information.

_CODE_SUFFIX = re.compile(r"^(?P<stem>.+)_code$")


def dimension_groups(norm_cols: list[str]) -> list[tuple[str, str, list[str]]]:
    """Identify dimension column groups among normalized fact-table columns.

    A dimension is keyed by a ``<stem>_code`` column and groups every sibling
    column that is the bare ``<stem>`` or starts with ``<stem>_`` (which includes
    the key itself). Groups whose only member is the key are ignored — there is
    nothing redundant to extract.

    Returns a list of ``(stem, key_column, attribute_columns)`` tuples, where
    ``attribute_columns`` are the redundant columns to move into ``dim_<stem>``.

    Examples
    --------
    >>> dimension_groups(
    ...     ["area_code", "area_code_m49", "area_label", "year_code", "year", "value"]
    ... )
    [('area', 'area_code', ['area_code_m49', 'area_label']), ('year', 'year_code', ['year'])]
    >>> dimension_groups(["flag_code", "value"])
    []
    """
    cols = list(norm_cols)
    groups: list[tuple[str, str, list[str]]] = []
    for key in cols:
        m = _CODE_SUFFIX.match(key)
        if not m:
            continue
        stem = m.group("stem")
        members = [
            c for c in cols if c == stem or c == key or c.startswith(f"{stem}_")
        ]
        others = [c for c in members if c != key]
        if others:
            groups.append((stem, key, others))
    return groups


def dimension_table_for(stem: str) -> str:
    """Return the dimension-table name for a stem: ``dim_<stem>``."""
    return f"dim_{stem}"


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


DDL_FAOSTAT_CONSTANT_COLUMN = """\
CREATE TABLE IF NOT EXISTS faostat_constant_column (
    dataset_code VARCHAR,
    table_name   VARCHAR,
    column_name  VARCHAR,
    value        VARCHAR,
    PRIMARY KEY (dataset_code, column_name)
);
"""


def create_metadata_tables(con) -> None:
    """Create the metadata / provenance tables if absent."""
    con.execute(DDL_FAOSTAT_DATASET)
    con.execute(DDL_FAOSTAT_BUILD)
    con.execute(DDL_FAOSTAT_CONSTANT_COLUMN)
