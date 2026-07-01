"""Column-name normalization, dimension logic, and metadata-table DDL.

Source preservation is the prime directive: we normalize *names* to stable
``snake_case`` but never drop columns or alter values. Storage-level duplication
is removed by moving repeated dimension attributes into ``dim_<stem>`` tables and
by dropping columns that are constant across a whole dataset (both are lossless
and reconstructable). The metadata tables ``faostat_dataset`` / ``faostat_build``
/ ``faostat_column_mapping`` / ``faostat_constant_column`` record provenance so a
built database can be audited and cited.
"""

from __future__ import annotations

import re

# Explicit overrides for names that the generic algorithm would mangle or that
# we want to pin for query stability. Keys are matched case-insensitively against
# the raw header after stripping surrounding whitespace.
#
# Note on "Unit": FAOSTAT does not publish a *unit code* column, so there is no
# ``unit_code`` to key a ``dim_unit`` on. We therefore keep the unit inline as a
# plain ``unit`` column rather than renaming it ``unit_label`` (a lone label with
# no code would just clutter the fact table).
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


def infer_role(normalized: str) -> str:
    """Classify a normalized column name into a coarse role.

    Used only to populate ``faostat_column_mapping.inferred_role`` — a convenience
    annotation for humans inspecting the schema, never a correctness input.
    """
    if normalized == "value":
        return "value"
    if normalized == "flag_code":
        return "flag"
    if normalized == "year":
        return "year"
    if normalized.endswith("_code"):
        return "code"
    if normalized.endswith("_label"):
        return "label"
    return "attribute"


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


# --- Small DuckDB introspection helpers ------------------------------------


def table_exists(con, name: str) -> bool:
    """True if a base table named ``name`` exists in the connected database."""
    (n,) = con.execute(
        "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = ?", [name]
    ).fetchone()
    return n > 0


def column_names(con, name: str) -> list[str]:
    """Return the column names of table/view ``name`` in declaration order."""
    return [d[0] for d in con.execute(f'SELECT * FROM "{name}" LIMIT 0').description]


# --- Labelled convenience views --------------------------------------------
#
# FAOSTATdb.md ("Schema implication: provide labelled views" + "Concrete change
# I would make to the specs") asks for a view per dataset that has the dimension
# labels already joined back onto the compact fact table, so that R/Python/Julia
# users can query with dataframe verbs and never write a JOIN by hand. Views cost
# almost nothing on disk (they are not materialized), so we build them by default.


def labelled_view_for(dataset_code: str) -> str:
    """Return the labelled-view name for a dataset code: ``view_<code>_labelled``."""
    return f"view_{dataset_code.lower()}_labelled"


def create_labelled_view(con, dataset_code: str, fact_table: str) -> str | None:
    """(Re)create ``view_<code>_labelled`` joining ``fact_table`` to its dimensions.

    Every ``<stem>_code`` column in the fact table that has a matching
    ``dim_<stem>`` gets LEFT JOINed (on ``dataset_code`` + the code) and its label
    / alternate-code attributes are surfaced in the view. ``flag_code`` is joined
    to ``dim_flag`` as well when flag descriptions were imported. The join is a
    LEFT JOIN so rows never disappear if a code is missing from a dimension.

    Returns the view name, or ``None`` if the fact table has no joinable
    dimensions (in which case a labelled view would add nothing over the fact
    table and is skipped).
    """
    view = labelled_view_for(dataset_code)
    fact_cols = column_names(con, fact_table)

    joins: list[str] = []
    extra_selects: list[str] = []
    alias_i = 0
    for col in fact_cols:
        m = _CODE_SUFFIX.match(col)
        if not m:
            continue
        stem = m.group("stem")
        dim = dimension_table_for(stem) if stem != "flag" else "dim_flag"
        if not table_exists(con, dim):
            continue
        # Pull the dimension's attribute columns (everything except the join keys).
        dim_cols = [
            c
            for c in column_names(con, dim)
            if c not in ("dataset_code", col)
        ]
        if not dim_cols:
            continue
        alias = f"d{alias_i}"
        alias_i += 1
        joins.append(
            f'LEFT JOIN "{dim}" AS {alias} '
            f"ON {alias}.dataset_code = '{dataset_code}' "
            f'AND {alias}."{col}" = f."{col}"'
        )
        for c in dim_cols:
            extra_selects.append(f'{alias}."{c}" AS "{c}"')

    if not joins:
        con.execute(f'DROP VIEW IF EXISTS "{view}"')
        return None

    select_list = ", ".join(["f.*", *extra_selects])
    con.execute(f'DROP VIEW IF EXISTS "{view}"')
    con.execute(
        f"CREATE VIEW \"{view}\" AS SELECT '{dataset_code}' AS dataset_code, "
        f'{select_list} FROM "{fact_table}" AS f ' + " ".join(joins)
    )
    return view


# --- Metadata tables -------------------------------------------------------

DDL_FAOSTAT_DATASET = """\
CREATE TABLE IF NOT EXISTS faostat_dataset (
    dataset_code         VARCHAR PRIMARY KEY,
    dataset_name         VARCHAR,
    topic                VARCHAR,
    dataset_description  VARCHAR,
    contact              VARCHAR,
    email                VARCHAR,
    date_update          VARCHAR,
    compression_format   VARCHAR,
    file_type            VARCHAR,
    file_location        VARCHAR,
    file_size_raw        BIGINT,
    file_rows_declared   BIGINT,
    rows_imported        BIGINT,
    downloaded_at        TIMESTAMP,
    source_metadata_url  VARCHAR,
    source_metadata_hash VARCHAR,
    source_metadata_json VARCHAR,
    archive_sha256       VARCHAR,
    import_status        VARCHAR
);
"""

DDL_FAOSTAT_BUILD = """\
CREATE TABLE IF NOT EXISTS faostat_build (
    build_id                 VARCHAR PRIMARY KEY,
    started_at               TIMESTAMP,
    completed_at             TIMESTAMP,
    faostatdb_version        VARCHAR,
    duckdb_version           VARCHAR,
    python_version           VARCHAR,
    os                       VARCHAR,
    metadata_snapshot_sha256 VARCHAR,
    command_line             VARCHAR,
    config_sha256            VARCHAR,
    datasets_imported        BIGINT,
    datasets_failed          BIGINT
);
"""

# Every raw->normalized header rename is recorded here so the mapping is auditable
# and reversible without re-reading the source CSV (FAOSTATdb.md > Naming).
DDL_FAOSTAT_COLUMN_MAPPING = """\
CREATE TABLE IF NOT EXISTS faostat_column_mapping (
    dataset_code           VARCHAR,
    table_name             VARCHAR,
    original_column_name   VARCHAR,
    normalized_column_name VARCHAR,
    inferred_role          VARCHAR,
    PRIMARY KEY (dataset_code, original_column_name)
);
"""

# Columns that are constant across an entire dataset are dropped from the fact
# table and recorded here (lossless: the value is reconstructable). See
# importer.extract_constant_columns.
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
    con.execute(DDL_FAOSTAT_COLUMN_MAPPING)
    con.execute(DDL_FAOSTAT_CONSTANT_COLUMN)


def record_column_mapping(
    con, dataset_code: str, table_name: str, raw_cols: list[str], norm_cols: list[str]
) -> None:
    """Persist the raw->normalized header mapping for one dataset."""
    con.execute(DDL_FAOSTAT_COLUMN_MAPPING)
    con.execute(
        "DELETE FROM faostat_column_mapping WHERE dataset_code = ?", [dataset_code]
    )
    for raw, norm in zip(raw_cols, norm_cols):
        con.execute(
            "INSERT OR REPLACE INTO faostat_column_mapping VALUES (?, ?, ?, ?, ?)",
            [dataset_code, table_name, raw, norm, infer_role(norm)],
        )
