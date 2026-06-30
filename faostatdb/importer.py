"""CSV extraction and DuckDB import.

Each dataset's CSV is extracted to a temp build dir, imported with DuckDB's
``read_csv`` into ``data_<code>`` (one fact table per dataset), and the extracted
CSV is deleted afterwards. Column names are normalized to ``snake_case`` while the
values and flags are preserved verbatim — **never pandas**.
"""

from __future__ import annotations

import codecs
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .schema import dimension_groups, dimension_table_for, normalize_columns


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
    dataset_code: str
    table_name: str
    row_count: int


def table_name_for(dataset_code: str) -> str:
    """Return the fact-table name for a dataset code: ``data_<code>``."""
    return f"data_{dataset_code.lower()}"


def extract_main_csv(archive: Path, dest_dir: Path) -> Path:
    """Extract the dataset's main CSV from the archive into ``dest_dir``.

    FAOSTAT bulk archives contain one primary ``*.csv`` (plus flag/note sidecars);
    we pick the largest top-level ``.csv`` as the main table.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as zf:
        csv_members = [
            m for m in zf.infolist()
            if m.filename.lower().endswith(".csv") and not m.is_dir()
        ]
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


def import_csv(con, csv_path: Path, dataset_code: str) -> ImportResult:
    """Create ``data_<code>`` from ``csv_path`` with normalized column names.

    Uses ``read_csv(all_varchar=...)`` is avoided — we let DuckDB infer types but
    rename columns to stable ``snake_case`` via a projected ``SELECT``.
    """
    table = table_name_for(dataset_code)
    encoding = detect_encoding(csv_path)
    raw_cols = read_csv_header(con, csv_path, encoding)
    norm_cols = normalize_columns(raw_cols)

    projection = ", ".join(
        f'"{raw}" AS {norm}' for raw, norm in zip(raw_cols, norm_cols)
    )
    con.execute(f'DROP TABLE IF EXISTS "{table}"')
    con.execute(
        f'CREATE TABLE "{table}" AS '
        f"SELECT {projection} FROM read_csv(?, header=true, encoding='{encoding}')",
        [str(csv_path)],
    )
    (count,) = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
    extract_dimensions(con, table, dataset_code, norm_cols)
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


def import_archive(con, archive: Path, dataset_code: str, build_dir: Path) -> ImportResult:
    """Extract + import one archive, cleaning up the extracted CSV afterwards."""
    csv_path = extract_main_csv(archive, build_dir)
    try:
        return import_csv(con, csv_path, dataset_code)
    finally:
        csv_path.unlink(missing_ok=True)
