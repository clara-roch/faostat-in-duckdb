"""
CSV extraction and DuckDB import.

Each dataset's main CSV is extracted to a temp build dir, imported with DuckDB's
``read_csv`` into ``data_<code>`` (one fact table per dataset), and the extracted
CSV is deleted afterwards. Column names are normalized to ``snake_case`` while the
values and flags are preserved verbatim — **never pandas**.

On top of the raw import we do three *lossless* storage reductions and add two
convenience layers:

* **Dimension extraction** — repeated attribute columns (``area_label``,
  ``item_label``, …) are moved into shared ``dim_<stem>`` tables, leaving only the
  dimension key in the fact table (the ``<stem>_code``, except for year where the
  bare ``year`` is kept and ``year_code`` is moved into ``dim_year``).
* **Constant-column removal** — columns that never vary across a dataset are
  dropped and their single value recorded in ``faostat_constant_column``.
* **Flag descriptions** — the archive's flag sidecar CSV (if present) is loaded
  into ``dim_flag`` so flag codes can be labelled.
* **Column-mapping** — every raw→normalized rename is recorded.
* **Labelled view** — ``view_<code>_labelled`` re-joins the labels for no-SQL use.

Everything above is reversible/auditable, so the mirror stays faithful to source.

The one value-level normalization is stripping the leading *text-marker apostrophe*
that FAOSTAT prefixes onto the international-code columns (``Area Code (M49)`` →
``'004``, ``Item Code (CPC)`` → ``'0111``). That apostrophe is a spreadsheet
artifact that forces leading zeros to survive as text, not part of the code, so we
remove it (leaving the value ``VARCHAR`` with its zeros intact). See
:func:`strip_leading_apostrophe` — it only touches columns whose *every* value
carries the apostrophe, so genuine text is never altered.
"""

from __future__ import annotations

import codecs
import csv
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path

from .schema import (
    DDL_FAOSTAT_CONSTANT_COLUMN,
    column_names,
    create_labelled_view,
    dimension_groups,
    dimension_table_for,
    normalize_columns,
    record_column_mapping,
    table_exists,
)


def detect_encoding(csv_path: Path) -> str:
    """Detect the encoding of a FAOSTAT CSV for DuckDB's ``read_csv``.

    DuckDB only supports ``utf-8``, ``utf-16`` and ``latin-1``. FAOSTAT bulk files
    are inconsistent: most are UTF-8 but some are Latin-1 (e.g. an unescaped ``ô``
    in "Côte d'Ivoire"). We check for a BOM, then stream the whole file through an
    incremental UTF-8 decoder; if every byte validates it is UTF-8, otherwise we
    fall back to Latin-1 (which maps every byte and so never fails).
    """
    with open(csv_path, "rb") as f:
        head = f.read(4)
        if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return "utf-16"
        if head.startswith(codecs.BOM_UTF8):
            return "utf-8"

        decoder = codecs.getincrementaldecoder("utf-8")()
        f.seek(0)
        try:
            while chunk := f.read(1 << 20):
                decoder.decode(chunk)
            decoder.decode(b"", final=True)
        except UnicodeDecodeError:
            return "latin-1"
    return "utf-8"


def count_physical_data_rows(csv_path: Path) -> int:
    """Data-row count assuming one record per physical line, via a raw byte scan.

    Counts newline-terminated lines (adding one for a missing final newline) and
    drops the header. This equals the true record count whenever no field holds an
    embedded, quoted newline — the normal case for FAOSTAT bulk CSVs. It is a fast
    C-level scan (``bytes.count``) used as the first pass of :func:`count_source_rows`.
    """
    newlines = 0
    last = b""
    with open(csv_path, "rb") as f:
        while chunk := f.read(1 << 20):
            newlines += chunk.count(b"\n")
            last = chunk[-1:]
    if last == b"":
        return 0  # empty file: no header, no rows
    physical_lines = newlines + (0 if last == b"\n" else 1)
    return max(physical_lines - 1, 0)  # drop the header line


def count_csv_records(csv_path: Path, encoding: str) -> int:
    """Exact data-record count using Python's stdlib CSV reader (header excluded).

    This is an *independent* parser — deliberately not DuckDB — that honours
    RFC 4180 quoting, so a quoted embedded newline is counted as part of a single
    record. It streams the file and is only used to cross-check DuckDB when the
    fast :func:`count_physical_data_rows` disagrees with the imported count.
    """
    py_encoding = {
        "utf-8": "utf-8-sig",  # tolerate a BOM without turning it into data
        "utf-16": "utf-16",
        "latin-1": "latin-1",
    }.get(encoding, "utf-8-sig")
    with open(csv_path, "r", encoding=py_encoding, newline="") as f:
        reader = csv.reader(f)
        try:
            next(reader)  # discard the header row
        except StopIteration:
            return 0
        return sum(1 for _ in reader)


def count_source_rows(csv_path: Path, encoding: str, imported: int) -> tuple[int, str]:
    """Return ``(source_rows, method)``: the record count of the *delivered CSV*.

    Fast path — a physical line count — is exact when the file has no multi-line
    quoted fields, so if it already matches ``imported`` the import is provably
    lossless and we stop there. Only on a mismatch do we pay for the exact,
    quote-aware :func:`count_csv_records`, which resolves whether the difference is
    a benign multi-line field (still lossless) or a genuine parsing discrepancy.
    """
    physical = count_physical_data_rows(csv_path)
    if physical == imported:
        return physical, "line-count"
    return count_csv_records(csv_path, encoding), "csv-parse"


@dataclass(frozen=True)
class ImportResult:
    """What one dataset import produced, for logging and metadata recording.

    ``row_count`` is what DuckDB loaded into the fact table; ``source_row_count`` is
    the number of data records found in the *delivered CSV*, counted independently
    of DuckDB (see :func:`count_source_rows`). ``count_method`` records how that
    reference count was obtained. The import is lossless iff the two agree.
    """

    dataset_code: str
    table_name: str
    row_count: int
    source_row_count: int = 0
    count_method: str = "line-count"
    labelled_view: str | None = None
    flag_rows: int = 0
    #: Sorted years the import was filtered to, or ``None`` for a full import.
    #: When set, ``row_count`` is an intentional subset of ``source_row_count``
    #: (the full delivered CSV), so the two are not expected to agree.
    year_filter: tuple[int, ...] | None = None
    #: Rows added by an *accumulate* import that merged into a pre-existing fact
    #: table (see :func:`import_csv`). ``None`` for a normal (replace) import. When
    #: set, ``row_count`` is the dataset's total row count *after* the merge, and
    #: ``appended_rows`` is how many rows this build contributed for its years.
    appended_rows: int | None = None

    @property
    def lossless(self) -> bool:
        """True when the import kept every row it was meant to.

        For a full import that means DuckDB loaded exactly as many rows as the
        delivered CSV holds. For a year-filtered import the row count is a
        deliberate subset of the source, so full-CSV equality is not expected —
        losslessness there is scoped to "no matching row was dropped", which the
        deterministic ``WHERE`` guarantees, so we report it as lossless.
        """
        if self.year_filter is not None:
            return True
        return self.row_count == self.source_row_count


def table_name_for(dataset_code: str) -> str:
    """Return the fact-table name for a dataset code: ``data_<code>``."""
    return f"data_{dataset_code.lower()}"


def raw_table_name_for(dataset_code: str) -> str:
    """Return the debug raw-table name for a dataset code: ``raw_<code>``."""
    return f"raw_{dataset_code.lower()}"


def _csv_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """All non-directory ``*.csv`` members of an archive."""
    return [
        m
        for m in zf.infolist()
        if m.filename.lower().endswith(".csv") and not m.is_dir()
    ]


def extract_main_csv(archive: Path, dest_dir: Path) -> Path:
    """Extract the dataset's main CSV from the archive into ``dest_dir``.

    FAOSTAT bulk archives contain one primary ``*.csv`` (plus flag/note sidecars);
    we pick the largest top-level ``.csv`` as the main table.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        csv_members = _csv_members(zf)
        if not csv_members:
            raise ValueError(f"no CSV member in {archive}")
        main = max(csv_members, key=lambda m: m.file_size)
        extracted = Path(zf.extract(main, dest_dir))
    return extracted


def read_csv_header(con, csv_path: Path, encoding: str = "utf-8") -> list[str]:
    """Return the raw column names of ``csv_path`` as DuckDB sees them."""
    rel = con.execute(
        f"SELECT * FROM read_csv(?, header=true, sample_size=1, encoding='{encoding}') LIMIT 0",
        [str(csv_path)],
    )
    return [d[0] for d in rel.description]


#: Types DuckDB's auto-detector is allowed to pick for a column, in priority
#: order. Restricting the candidate list to *numeric-or-text* is deliberate: it
#: keeps genuinely numeric columns typed while guaranteeing that anything else
#: falls back to ``VARCHAR`` rather than being silently reinterpreted. DuckDB's
#: default candidates also include ``BOOLEAN`` and the date/time family, which
#: change the *meaning* of a value — e.g. the FAOSTAT unit ``'t'`` (tonnes) is
#: read as boolean ``true`` — and that would violate source preservation.
#:
#: ``INTEGER`` (INT32) precedes ``BIGINT`` so the sniffer picks the *narrowest*
#: integer type that holds every value: FAOSTAT dimension codes and years are
#: small integers, so storing them in 4 bytes instead of 8 is a sizable saving
#: across the fact rows. ``BIGINT`` stays in the list purely as an overflow guard
#: — a column with an all-integer value above the INT32 ceiling (~2.1e9) keeps an
#: exact 64-bit type instead of falling through to lossy ``DOUBLE`` (exact only
#: below 2**53). No fact/dim column in the current inventory needs it, but it
#: costs nothing and protects source fidelity if one ever does.
_AUTO_TYPE_CANDIDATES = "['INTEGER', 'BIGINT', 'DOUBLE', 'VARCHAR']"


def _year_where_clause(raw_cols: list[str], norm_cols: list[str], years: set[int]) -> str | None:
    """Build a ``WHERE`` filtering ``read_csv`` output to ``years`` on the year column.

    Returns the SQL fragment (referencing the *raw* header, which is what
    ``read_csv`` exposes) or ``None`` when the dataset has no ``year`` column —
    the caller then imports the dataset in full. The comparison uses ``TRY_CAST``
    so a non-integer period label (e.g. a "2019-2021" three-year period) yields
    ``NULL`` and is simply excluded rather than raising.
    """
    if "year" not in norm_cols:
        return None
    raw_year = raw_cols[norm_cols.index("year")]
    in_list = ", ".join(str(y) for y in sorted(years))
    return f'WHERE TRY_CAST("{raw_year}" AS BIGINT) IN ({in_list})'


def import_csv(
    con, csv_path: Path, dataset_code: str, *, keep_raw: bool = False,
    years: set[int] | None = None,
) -> ImportResult:
    """Create ``data_<code>`` from ``csv_path`` with normalized column names.

    Columns keep proper types where the data supports it: DuckDB infers each
    column's type from a **full-file scan** (``sample_size=-1``) restricted to
    the numeric-or-text candidates in :data:`_AUTO_TYPE_CANDIDATES`. A column that
    is integer everywhere becomes ``INTEGER`` (or ``BIGINT`` if any value exceeds
    the INT32 range), an everywhere-decimal one ``DOUBLE``, and anything with mixed
    or non-numeric content falls back to ``VARCHAR`` — so text is used *only where
    the data is genuinely not a plain number*.

    Two things make this both correct and robust:

    * **Full-file inference, not a sample.** The original conversion errors
      (``Could not convert string '210400TSUB'/'1685.01.01' to INT64``) came from
      DuckDB sampling only the first rows, guessing ``INT64``, then aborting on a
      later out-of-pattern token. Scanning every row makes it pick a type that
      fits *all* values, so those code/date-like strings land in ``VARCHAR`` on
      their own and never raise.
    * **Numeric-or-text candidates only.** Left to its defaults DuckDB would also
      guess ``BOOLEAN``/``DATE``/``TIMESTAMP`` and reinterpret values (the unit
      ``'t'`` → boolean ``true``, dotted "dates", …). Limiting the candidates
      means a value's storage type is only ever *narrowed*, never redefined —
      keeping the mirror faithful to source.

    After the raw load we record the column mapping, extract dimensions, and drop
    constant columns. When ``keep_raw`` is set, an untouched copy of the fully
    projected import is kept as ``raw_<code>`` for debugging losslessness.

    **Accumulate mode.** When a year filter is active *and* ``data_<code>`` already
    exists (a prior ``--years`` build wrote it), the new years are merged into that
    table instead of replacing it: rows for the incoming years are refreshed and
    other years are left untouched. So ``build --years 2017`` followed by
    ``build --years 2018`` leaves both years in the database, and re-running a year
    just refreshes it (idempotent). The 2017 rows are not re-parsed — only the new
    archive is read and merged with in-database SQL. See :func:`_merge_incoming`.
    """
    table = table_name_for(dataset_code)
    encoding = detect_encoding(csv_path)
    raw_cols = read_csv_header(con, csv_path, encoding)
    norm_cols = normalize_columns(raw_cols)

    # Optional year filter: FAOSTAT ships every year in one bulk archive, so the
    # whole file is already on disk; here we keep only the requested years' rows.
    # A dataset without a ``year`` column can't be filtered, so it imports in full
    # (year_filter stays None and normal full-CSV losslessness still applies).
    where = _year_where_clause(raw_cols, norm_cols, years) if years else None
    applied_years = tuple(sorted(years)) if (years and where) else None

    # Accumulate into an existing dataset only when we have both a year key to merge
    # on and a table already present. Otherwise this is a normal (replacing) import,
    # for which we build the fact table in place under its final name.
    append_mode = applied_years is not None and table_exists(con, table)
    target = f"_incoming_{dataset_code.lower()}" if append_mode else table

    # Project every source column through "Raw Name" AS snake_name so no column is
    # dropped and no value/flag is changed at load time.
    projection = ", ".join(
        f'"{raw}" AS {norm}' for raw, norm in zip(raw_cols, norm_cols)
    )
    con.execute(f'DROP TABLE IF EXISTS "{target}"')
    try:
        con.execute(
            f'CREATE TABLE "{target}" AS '
            f"SELECT {projection} "
            f"FROM read_csv(?, header=true, encoding='{encoding}', "
            f"sample_size=-1, auto_type_candidates={_AUTO_TYPE_CANDIDATES}) "
            f"{where or ''}",
            [str(csv_path)],
        )
    except Exception as exc:  # noqa: BLE001 — re-raised with import context
        raise ValueError(
            f"reading CSV for dataset {dataset_code!r} "
            f"(file={csv_path.name}, encoding={encoding}, "
            f"columns={list(zip(raw_cols, norm_cols))}): {exc}"
        ) from exc

    (count,) = con.execute(f'SELECT COUNT(*) FROM "{target}"').fetchone()

    # Verify losslessness against the delivered CSV itself — counted independently
    # of DuckDB — rather than the approximate FileRows metadata. Dimension/constant
    # reduction below only drops columns, never rows, so this count stays valid.
    # With a year filter the imported rows are an intentional subset, so we record
    # the full-file record count for provenance but don't expect it to match (the
    # cheap line count is exact here bar rare multi-line quoted fields).
    if applied_years is None:
        source_rows, method = count_source_rows(csv_path, encoding, count)
    else:
        source_rows, method = count_physical_data_rows(csv_path), "line-count"

    # Keep an untouched copy *before* any reduction, for debugging losslessness.
    # (Kept before the apostrophe strip too, so raw_<code> shows the source verbatim.)
    if keep_raw:
        raw_table = raw_table_name_for(dataset_code)
        con.execute(f'DROP TABLE IF EXISTS "{raw_table}"')
        con.execute(f'CREATE TABLE "{raw_table}" AS SELECT * FROM "{target}"')

    # Remove FAOSTAT's leading text-marker apostrophe from the M49/CPC code columns
    # before dimensions are extracted, so the cleaned code flows into dim_<stem>.
    strip_leading_apostrophe(con, target)

    # The column mapping is a property of the dataset, so record it under the final
    # fact-table name regardless of whether we imported into a temp merge staging.
    record_column_mapping(con, dataset_code, table, raw_cols, norm_cols)
    extract_dimensions(con, target, dataset_code, norm_cols, append=append_mode)

    if not append_mode:
        extract_constant_columns(con, target, dataset_code)
        return ImportResult(
            dataset_code=dataset_code,
            table_name=table,
            row_count=count,
            source_row_count=source_rows,
            count_method=method,
            year_filter=applied_years,
        )

    # Merge the reduced incoming rows into the existing fact table, then discard the
    # staging table. ``count`` here is the rows this build contributes for its years.
    total = _merge_incoming(con, dataset_code, table, target, applied_years)
    con.execute(f'DROP TABLE IF EXISTS "{target}"')
    return ImportResult(
        dataset_code=dataset_code,
        table_name=table,
        row_count=total,
        source_row_count=source_rows,
        count_method=method,
        year_filter=applied_years,
        appended_rows=count,
    )


def _column_types(con, table: str) -> dict[str, str]:
    """Map each column of ``table`` to its DuckDB type (via ``DESCRIBE``)."""
    return {r[0]: r[1] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}


def _merge_incoming(
    con, dataset_code: str, existing: str, incoming: str, years: tuple[int, ...]
) -> int:
    """Merge the reduced ``incoming`` staging table into the existing fact table.

    The two tables come from independent year slices, so their reduced shapes can
    differ: a column that was constant (and therefore dropped) in the years already
    stored may vary in the incoming years, or vice-versa. We reconcile the schemas
    first, then replace exactly the incoming years — ``DELETE`` the rows for
    ``years`` from ``existing`` and ``INSERT`` the staged rows — so other years are
    left untouched and re-running a year is idempotent. Returns the dataset's total
    row count after the merge.
    """
    existing_cols = column_names(con, existing)

    # The merge key is ``year``. A table written by an older version (before year
    # was protected from constant-column removal) may have dropped it into
    # faostat_constant_column when it held a single year; restore it so we can merge.
    if "year" not in existing_cols:
        row = con.execute(
            "SELECT value FROM faostat_constant_column "
            "WHERE dataset_code = ? AND column_name = 'year'",
            [dataset_code],
        ).fetchone()
        if row is None:
            raise ValueError(
                f"cannot accumulate years into {existing!r}: it has no 'year' column "
                f"to merge on. Rebuild the dataset (e.g. without --years, or with all "
                f"wanted years in one --years run)."
            )
        year_type = _column_types(con, incoming).get("year", "BIGINT")
        con.execute(f'ALTER TABLE "{existing}" ADD COLUMN "year" {year_type}')
        con.execute(
            f'UPDATE "{existing}" SET "year" = CAST(? AS {year_type})', [row[0]]
        )
        con.execute(
            "DELETE FROM faostat_constant_column "
            "WHERE dataset_code = ? AND column_name = 'year'",
            [dataset_code],
        )
        existing_cols = column_names(con, existing)

    # Reconcile columns the existing table dropped as constant but that the incoming
    # slice carries. If the incoming values match the recorded constant it stays
    # dropped; otherwise the column now varies across years, so we restore it on the
    # existing rows (backfilled from the recorded constant) and keep it in both.
    incoming_types = _column_types(con, incoming)
    for col in [c for c in column_names(con, incoming) if c not in existing_cols]:
        n, ndist = con.execute(
            f'SELECT COUNT("{col}"), COUNT(DISTINCT "{col}") FROM "{incoming}"'
        ).fetchone()
        total_rows = con.execute(f'SELECT COUNT(*) FROM "{incoming}"').fetchone()[0]
        rec = con.execute(
            "SELECT value FROM faostat_constant_column "
            "WHERE dataset_code = ? AND column_name = ?",
            [dataset_code, col],
        ).fetchone()
        recorded = rec[0] if rec else None
        constant = ndist == 0 or (ndist == 1 and n == total_rows)
        incoming_value = None
        if constant and ndist == 1:
            (incoming_value,) = con.execute(
                f'SELECT CAST("{col}" AS VARCHAR) FROM "{incoming}" '
                f'WHERE "{col}" IS NOT NULL LIMIT 1'
            ).fetchone()
        if constant and rec is not None and incoming_value == recorded:
            # Still the same constant across the new years — keep it dropped.
            con.execute(f'ALTER TABLE "{incoming}" DROP COLUMN "{col}"')
            continue
        # The column is not the recorded constant for these years: restore it on the
        # existing rows so both slices carry it, then drop its constant record.
        col_type = incoming_types.get(col, "VARCHAR")
        con.execute(f'ALTER TABLE "{existing}" ADD COLUMN "{col}" {col_type}')
        con.execute(
            f'UPDATE "{existing}" SET "{col}" = CAST(? AS {col_type})', [recorded]
        )
        con.execute(
            "DELETE FROM faostat_constant_column WHERE dataset_code = ? AND column_name = ?",
            [dataset_code, col],
        )
    existing_cols = column_names(con, existing)

    # Any column the existing table has but the incoming slice lacks is added to the
    # staging table as NULL so the INSERT lines up. (Shouldn't normally happen —
    # incoming carries every non-dimension source column — but keeps the merge safe.)
    incoming_cols = column_names(con, incoming)
    for col in [c for c in existing_cols if c not in incoming_cols]:
        con.execute(
            f'ALTER TABLE "{incoming}" ADD COLUMN "{col}" '
            f'{_column_types(con, existing).get(col, "VARCHAR")}'
        )

    # Replace exactly the incoming years, leaving every other year in place.
    in_list = ", ".join(str(y) for y in years)
    con.execute(
        f'DELETE FROM "{existing}" WHERE TRY_CAST("year" AS BIGINT) IN ({in_list})'
    )
    col_list = ", ".join(f'"{c}"' for c in existing_cols)
    con.execute(
        f'INSERT INTO "{existing}" ({col_list}) SELECT {col_list} FROM "{incoming}"'
    )
    return con.execute(f'SELECT COUNT(*) FROM "{existing}"').fetchone()[0]


def extract_dimensions(
    con, table: str, dataset_code: str, norm_cols: list[str], *, append: bool = False
) -> None:
    """Move redundant dimension attributes out of ``table`` into ``dim_<stem>``.

    For every dimension group (e.g. ``area_code`` + ``area_code_m49`` +
    ``area_label``), the attribute columns are deduplicated into a shared
    ``dim_<stem>`` table keyed by ``(dataset_code, <key>)`` and then dropped from
    the fact table, which retains only that key. The key is the ``<stem>_code``
    for every dimension except year, where the bare ``year`` is retained and
    ``year_code`` is moved into ``dim_year``. No values are altered: the dimension
    table holds the exact source attributes, deduplicated.

    Dimension columns are stored as ``VARCHAR`` on purpose. A ``dim_<stem>`` table
    is *shared* across datasets, but the same logical code is typed differently
    from one dataset to the next — FAOSTAT writes some ``item_code``/``area_code``
    columns as plain integers (inferred ``INTEGER``) and others with alphanumeric
    codes (inferred ``VARCHAR``). Storing every dimension attribute as text keeps
    the shared table consistent regardless of per-dataset typing and import order;
    the fact table keeps its own inferred type and the labelled view casts the key
    to text when it joins. Casting to text never loses information here.
    """
    for stem, key, others in dimension_groups(norm_cols):
        dim = dimension_table_for(stem)
        members = [key, *others]
        # Text projection: read the fact column (whatever its inferred type) as the
        # canonical VARCHAR the shared dimension table stores.
        member_sql = ", ".join(f'CAST("{m}" AS VARCHAR) AS "{m}"' for m in members)

        # Create the dimension table on first sight. IF NOT EXISTS keeps a table
        # shared across datasets; every attribute column is VARCHAR.
        con.execute(
            f'CREATE TABLE IF NOT EXISTS "{dim}" AS '
            f'SELECT CAST(NULL AS VARCHAR) AS dataset_code, {member_sql} '
            f'FROM "{table}" WHERE FALSE'
        )
        # Datasets are heterogeneous (e.g. some lack item_code_cpc); widen the
        # shared dimension table to fit any column this dataset contributes.
        existing = {d[0] for d in con.execute(f'SELECT * FROM "{dim}" LIMIT 0').description}
        for m in members:
            if m not in existing:
                con.execute(f'ALTER TABLE "{dim}" ADD COLUMN IF NOT EXISTS "{m}" VARCHAR')

        insert_cols = ", ".join(['dataset_code', *(f'"{m}"' for m in members)])
        if append:
            # Accumulate mode (a later --years build merging into an existing DB):
            # keep the dimension members already recorded for earlier years and add
            # only the ones this build's rows introduce, matched on the key column.
            con.execute(
                f'INSERT INTO "{dim}" ({insert_cols}) '
                f'SELECT DISTINCT ?, {member_sql} FROM "{table}" AS s '
                f'WHERE NOT EXISTS (SELECT 1 FROM "{dim}" AS d '
                f'WHERE d.dataset_code = ? AND d."{key}" = CAST(s."{key}" AS VARCHAR))',
                [dataset_code, dataset_code],
            )
        else:
            # Re-import is idempotent: replace this dataset's dimension rows.
            con.execute(f'DELETE FROM "{dim}" WHERE dataset_code = ?', [dataset_code])
            con.execute(
                f'INSERT INTO "{dim}" ({insert_cols}) '
                f'SELECT DISTINCT ?, {member_sql} FROM "{table}"',
                [dataset_code],
            )

        # Drop the now-redundant attribute columns from the fact table.
        for col in others:
            con.execute(f'ALTER TABLE "{table}" DROP COLUMN "{col}"')


def extract_constant_columns(
    con, table: str, dataset_code: str, protect: tuple[str, ...] = ("value", "year")
) -> None:
    """Drop columns that hold a single value across *every* row of ``table``.

    A column whose value never varies carries no per-row information, so it is
    removed from the fact table and its constant value recorded in
    ``faostat_constant_column``. This is lossless — the value is reconstructable
    from the metadata. The check scans the whole table (not a sample): a column is
    only dropped if it is genuinely constant everywhere.

    ``value`` and ``year`` are protected by default. ``value`` keeps a fact table's
    measurement column even in the degenerate case where every value is equal.
    ``year`` is the temporal key: a single-year build (``--years 2017``) makes it
    constant, but dropping it would leave no year column to merge on when a later
    ``--years 2018`` build accumulates into the same database (see
    :func:`import_csv`), so we always keep it in the fact table.
    """
    con.execute(DDL_FAOSTAT_CONSTANT_COLUMN)

    cols = [d[0] for d in con.execute(f'SELECT * FROM "{table}" LIMIT 0').description]
    checkable = [c for c in cols if c not in protect]
    if not checkable:
        return

    # One full scan: total rows, plus non-null count and distinct count per column.
    parts = ["COUNT(*)"]
    for c in checkable:
        parts.append(f'COUNT("{c}")')
        parts.append(f'COUNT(DISTINCT "{c}")')
    stats = con.execute(f'SELECT {", ".join(parts)} FROM "{table}"').fetchone()

    n_rows = stats[0]
    if n_rows == 0:
        return

    con.execute(
        'DELETE FROM faostat_constant_column WHERE dataset_code = ? AND table_name = ?',
        [dataset_code, table],
    )

    idx = 1
    for c in checkable:
        non_null, distinct = stats[idx], stats[idx + 1]
        idx += 2
        # Constant iff all rows are NULL (distinct == 0) or all rows share one
        # non-null value (distinct == 1 and there are no NULLs). A mix of one
        # value and some NULLs is NOT constant and is left in place.
        all_null = distinct == 0
        single_value = distinct == 1 and non_null == n_rows
        if not (all_null or single_value):
            continue

        if all_null:
            value = None
        else:
            (raw,) = con.execute(
                f'SELECT "{c}" FROM "{table}" WHERE "{c}" IS NOT NULL LIMIT 1'
            ).fetchone()
            value = None if raw is None else str(raw)

        con.execute(
            'INSERT INTO faostat_constant_column VALUES (?, ?, ?, ?)',
            [dataset_code, table, c, value],
        )
        con.execute(f'ALTER TABLE "{table}" DROP COLUMN "{c}"')


def strip_leading_apostrophe(con, table: str) -> list[str]:
    """Strip FAOSTAT's leading text-marker apostrophe from code columns in ``table``.

    FAOSTAT bulk CSVs prefix the international-code columns — ``Area Code (M49)``
    and ``Item Code (CPC)`` — with a single apostrophe (an Excel text marker that
    keeps the leading zeros of e.g. M49 ``'004`` or CPC ``'0111`` from being read as
    a number). The apostrophe is a spreadsheet formatting artifact, not part of the
    code, so we remove it while keeping the column ``VARCHAR`` (an ``UPDATE`` never
    changes the column type, so the leading zeros survive as text).

    To stay conservative we only touch a column whose *every* non-null value carries
    the apostrophe — the signature of a uniform text-marker column. Label columns,
    where a stray leading quote could be genuine content, are left untouched. Numeric
    columns (the internal ``area_code`` / ``item_code`` / ``value`` / ``year``) are
    never candidates because a leading apostrophe would have forced them to ``VARCHAR``
    at load time. Returns the names of the columns that were stripped.
    """
    types = {r[0]: r[1] for r in con.execute(f'DESCRIBE "{table}"').fetchall()}
    text_cols = [c for c, t in types.items() if t == "VARCHAR"]
    if not text_cols:
        return []

    # One scan: per text column, count non-null values and apostrophe-prefixed ones.
    # (``LIKE '''%'`` matches a leading single quote — the apostrophe is doubled to
    # escape it inside the SQL string literal.)
    parts: list[str] = []
    for c in text_cols:
        parts.append(f'COUNT("{c}")')
        parts.append(f"COUNT(*) FILTER (WHERE \"{c}\" LIKE '''%')")
    stats = con.execute(f'SELECT {", ".join(parts)} FROM "{table}"').fetchone()

    stripped: list[str] = []
    idx = 0
    for c in text_cols:
        non_null, prefixed = stats[idx], stats[idx + 1]
        idx += 2
        # Strip only when the apostrophe is present on *every* non-null value, i.e.
        # it is a column-wide text marker and not incidental content on a few rows.
        if non_null > 0 and prefixed == non_null:
            con.execute(f'UPDATE "{table}" SET "{c}" = substr("{c}", 2)')
            stripped.append(c)
    return stripped


def extract_flag_dimension(
    con, archive: Path, dataset_code: str, build_dir: Path
) -> int:
    """Load the archive's flag sidecar CSV into ``dim_flag`` (if one exists).

    FAOSTAT bulk archives ship a small flag/symbol legend alongside the main data
    (a CSV whose name contains "Flag"), typically two columns: a short flag code
    and its human description. FAOSTATdb.md ("Flags: preserve them fully") asks us
    to keep both, so we build ``dim_flag(dataset_code, flag_code, flag_description)``
    — never collapsing flags to a boolean. The fact table keeps ``flag_code``;
    ``dim_flag`` supplies the description for the labelled view.

    Returns the number of flag rows loaded for this dataset (0 if no sidecar).
    """
    with zipfile.ZipFile(archive) as zf:
        flag_members = [m for m in _csv_members(zf) if "flag" in m.filename.lower()]
        if not flag_members:
            return 0
        # Pick the smallest matching CSV (the legend, not a data file that merely
        # has "flag" in its path).
        member = min(flag_members, key=lambda m: m.file_size)
        extracted = Path(zf.extract(member, build_dir))

    try:
        encoding = detect_encoding(extracted)
        raw_cols = read_csv_header(con, extracted, encoding)
        norm = normalize_columns(raw_cols)

        # Identify the code column and the description column. The code column
        # normalizes to 'flag_code' (our override for a bare "Flag"); the
        # description is whatever remains (commonly "Description").
        code_idx = _first_index(norm, lambda c: c == "flag_code" or c.endswith("flag_code"))
        if code_idx is None:
            code_idx = _first_index(norm, lambda c: "flag" in c)
        if code_idx is None:
            return 0  # can't tell which column is the code — skip rather than guess
        desc_idx = _first_index(
            norm,
            lambda c: c in ("description", "flag_description", "label", "flags"),
        )
        if desc_idx is None:
            desc_idx = _first_index(
                list(range(len(norm))), lambda i: i != code_idx
            )  # first other column

        code_raw = raw_cols[code_idx]
        con.execute(
            "CREATE TABLE IF NOT EXISTS dim_flag ("
            "dataset_code VARCHAR, flag_code VARCHAR, flag_description VARCHAR, "
            "PRIMARY KEY (dataset_code, flag_code))"
        )
        con.execute("DELETE FROM dim_flag WHERE dataset_code = ?", [dataset_code])

        if desc_idx is None:
            select = f'"{code_raw}" AS flag_code, CAST(NULL AS VARCHAR) AS flag_description'
        else:
            desc_raw = raw_cols[desc_idx]
            select = f'"{code_raw}" AS flag_code, "{desc_raw}" AS flag_description'

        con.execute(
            f"INSERT OR REPLACE INTO dim_flag "
            f"SELECT DISTINCT ? AS dataset_code, {select} "
            f"FROM read_csv(?, header=true, encoding='{encoding}', all_varchar=true) "
            f"WHERE \"{code_raw}\" IS NOT NULL",
            [dataset_code, str(extracted)],
        )
        (n,) = con.execute(
            "SELECT COUNT(*) FROM dim_flag WHERE dataset_code = ?", [dataset_code]
        ).fetchone()
        return n
    finally:
        extracted.unlink(missing_ok=True)


def _first_index(seq, pred) -> int | None:
    """Index of the first element satisfying ``pred``; ``None`` if none do.

    When ``seq`` is a list of indices, ``pred`` receives the index; otherwise it
    receives the element. (Small helper used to locate flag columns.)
    """
    for i, item in enumerate(seq):
        if pred(item):
            return item if isinstance(item, int) else i
    return None


def import_archive(
    con, archive: Path, dataset_code: str, build_dir: Path, *, keep_raw: bool = False,
    years: set[int] | None = None,
) -> ImportResult:
    """Extract + import one archive, then build flag dim + labelled view.

    Cleans up the extracted main CSV afterwards. The order matters: flags are
    loaded before the labelled view so the view can join flag descriptions.
    ``years`` optionally restricts the fact table to those years (see
    :func:`import_csv`).
    """
    csv_path = extract_main_csv(archive, build_dir)
    try:
        result = import_csv(con, csv_path, dataset_code, keep_raw=keep_raw, years=years)
    finally:
        csv_path.unlink(missing_ok=True)

    flag_rows = extract_flag_dimension(con, archive, dataset_code, build_dir)
    view = create_labelled_view(con, dataset_code, result.table_name)
    return replace(result, labelled_view=view, flag_rows=flag_rows)
