"""CSV extraction and DuckDB import.

Each dataset's main CSV is extracted to a temp build dir, imported with DuckDB's
``read_csv`` into ``data_<code>`` (one fact table per dataset), and the extracted
CSV is deleted afterwards. Column names are normalized to ``snake_case`` while the
values and flags are preserved verbatim — **never pandas**.

On top of the raw import we do three *lossless* storage reductions and add two
convenience layers:

* **Dimension extraction** — repeated attribute columns (``area_label``,
  ``item_label``, …) are moved into shared ``dim_<stem>`` tables, leaving only the
  ``<stem>_code`` key in the fact table.
* **Constant-column removal** — columns that never vary across a dataset are
  dropped and their single value recorded in ``faostat_constant_column``.
* **Flag descriptions** — the archive's flag sidecar CSV (if present) is loaded
  into ``dim_flag`` so flag codes can be labelled.
* **Column-mapping** — every raw→normalized rename is recorded.
* **Labelled view** — ``view_<code>_labelled`` re-joins the labels for no-SQL use.

Everything above is reversible/auditable, so the mirror stays faithful to source.
"""

from __future__ import annotations

import codecs
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path

from .schema import (
    DDL_FAOSTAT_CONSTANT_COLUMN,
    create_labelled_view,
    dimension_groups,
    dimension_table_for,
    normalize_columns,
    record_column_mapping,
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


@dataclass(frozen=True)
class ImportResult:
    """What one dataset import produced, for logging and metadata recording."""

    dataset_code: str
    table_name: str
    row_count: int
    labelled_view: str | None = None
    flag_rows: int = 0


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


def import_csv(
    con, csv_path: Path, dataset_code: str, *, keep_raw: bool = False
) -> ImportResult:
    """Create ``data_<code>`` from ``csv_path`` with normalized column names.

    Every column is read as ``VARCHAR`` (``all_varchar=true``). This is the
    source-preservation choice and, critically, it makes the import robust: DuckDB
    never tries to coerce a value to a narrower type, so FAOSTAT code/date-like
    strings (e.g. ``'210400TSUB'``, ``'1685.01.01'``, ``'210091F'``, leading-zero
    codes) can never trigger a conversion error. Type inference over the full file
    (``sample_size=-1``) was still fragile — a single unusual token in millions of
    rows would flip a column to INT64 and abort the whole dataset. Reading text
    keeps the source values byte-for-byte and downstream queries cast explicitly.

    After the raw load we record the column mapping, extract dimensions, and drop
    constant columns. When ``keep_raw`` is set, an untouched copy of the fully
    projected import is kept as ``raw_<code>`` for debugging losslessness.
    """
    table = table_name_for(dataset_code)
    encoding = detect_encoding(csv_path)
    raw_cols = read_csv_header(con, csv_path, encoding)
    norm_cols = normalize_columns(raw_cols)

    # Project every source column through "Raw Name" AS snake_name so no column is
    # dropped and no value/flag is changed at load time.
    projection = ", ".join(
        f'"{raw}" AS {norm}' for raw, norm in zip(raw_cols, norm_cols)
    )
    con.execute(f'DROP TABLE IF EXISTS "{table}"')
    try:
        con.execute(
            f'CREATE TABLE "{table}" AS '
            f"SELECT {projection} "
            f"FROM read_csv(?, header=true, encoding='{encoding}', "
            f"sample_size=-1, all_varchar=true)",
            [str(csv_path)],
        )
    except Exception as exc:  # noqa: BLE001 — re-raised with import context
        raise ValueError(
            f"reading CSV for dataset {dataset_code!r} "
            f"(file={csv_path.name}, encoding={encoding}, "
            f"columns={list(zip(raw_cols, norm_cols))}): {exc}"
        ) from exc

    (count,) = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()

    # Keep an untouched copy *before* any reduction, for debugging losslessness.
    if keep_raw:
        raw_table = raw_table_name_for(dataset_code)
        con.execute(f'DROP TABLE IF EXISTS "{raw_table}"')
        con.execute(f'CREATE TABLE "{raw_table}" AS SELECT * FROM "{table}"')

    record_column_mapping(con, dataset_code, table, raw_cols, norm_cols)
    extract_dimensions(con, table, dataset_code, norm_cols)
    extract_constant_columns(con, table, dataset_code)
    return ImportResult(dataset_code=dataset_code, table_name=table, row_count=count)


def extract_dimensions(con, table: str, dataset_code: str, norm_cols: list[str]) -> None:
    """Move redundant dimension attributes out of ``table`` into ``dim_<stem>``.

    For every dimension group (e.g. ``area_code`` + ``area_code_m49`` +
    ``area_label``), the attribute columns are deduplicated into a shared
    ``dim_<stem>`` table keyed by ``(dataset_code, <stem>_code)`` and then dropped
    from the fact table, which retains only the ``<stem>_code`` key. No values are
    altered: the dimension table holds the exact source attributes, deduplicated.
    """
    col_types = {row[0]: row[1] for row in con.execute(f'DESCRIBE "{table}"').fetchall()}

    for stem, key, others in dimension_groups(norm_cols):
        dim = dimension_table_for(stem)
        members = [key, *others]
        member_sql = ", ".join(f'"{m}"' for m in members)

        # Create the dimension table on first sight, deriving column types from
        # the fact table. IF NOT EXISTS keeps a table shared across datasets.
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
                con.execute(f'ALTER TABLE "{dim}" ADD COLUMN IF NOT EXISTS "{m}" {col_types[m]}')

        # Re-import is idempotent: replace this dataset's dimension rows.
        con.execute(f'DELETE FROM "{dim}" WHERE dataset_code = ?', [dataset_code])
        insert_cols = ", ".join(['dataset_code', *(f'"{m}"' for m in members)])
        con.execute(
            f'INSERT INTO "{dim}" ({insert_cols}) '
            f'SELECT DISTINCT ?, {member_sql} FROM "{table}"',
            [dataset_code],
        )

        # Drop the now-redundant attribute columns from the fact table.
        for col in others:
            con.execute(f'ALTER TABLE "{table}" DROP COLUMN "{col}"')


def extract_constant_columns(
    con, table: str, dataset_code: str, protect: tuple[str, ...] = ("value",)
) -> None:
    """Drop columns that hold a single value across *every* row of ``table``.

    A column whose value never varies carries no per-row information, so it is
    removed from the fact table and its constant value recorded in
    ``faostat_constant_column``. This is lossless — the value is reconstructable
    from the metadata. The check scans the whole table (not a sample): a column is
    only dropped if it is genuinely constant everywhere.

    The ``value`` column is protected by default so a fact table always keeps its
    measurement column, even in the degenerate case where every value is equal.
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
    con, archive: Path, dataset_code: str, build_dir: Path, *, keep_raw: bool = False
) -> ImportResult:
    """Extract + import one archive, then build flag dim + labelled view.

    Cleans up the extracted main CSV afterwards. The order matters: flags are
    loaded before the labelled view so the view can join flag descriptions.
    """
    csv_path = extract_main_csv(archive, build_dir)
    try:
        result = import_csv(con, csv_path, dataset_code, keep_raw=keep_raw)
    finally:
        csv_path.unlink(missing_ok=True)

    flag_rows = extract_flag_dimension(con, archive, dataset_code, build_dir)
    view = create_labelled_view(con, dataset_code, result.table_name)
    return replace(result, labelled_view=view, flag_rows=flag_rows)
