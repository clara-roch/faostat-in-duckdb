"""CSV extraction and DuckDB import.

Each dataset's CSV is extracted to a temp build dir, imported with DuckDB's
``read_csv`` into ``data_<code>`` (one fact table per dataset), and the extracted
CSV is deleted afterwards. Column names are normalized to ``snake_case`` while the
values and flags are preserved verbatim — **never pandas**.
"""

from __future__ import annotations

import zipfile
from dataclasses import dataclass
from pathlib import Path

from .schema import normalize_columns


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


def read_csv_header(con, csv_path: Path) -> list[str]:
    """Return the raw column names of ``csv_path`` as DuckDB sees them."""
    rel = con.execute(
        "SELECT * FROM read_csv(?, header=true, sample_size=1) LIMIT 0",
        [str(csv_path)],
    )
    return [d[0] for d in rel.description]


def import_csv(con, csv_path: Path, dataset_code: str) -> ImportResult:
    """Create ``data_<code>`` from ``csv_path`` with normalized column names.

    Uses ``read_csv(all_varchar=...)`` is avoided — we let DuckDB infer types but
    rename columns to stable ``snake_case`` via a projected ``SELECT``.
    """
    table = table_name_for(dataset_code)
    raw_cols = read_csv_header(con, csv_path)
    norm_cols = normalize_columns(raw_cols)

    projection = ", ".join(
        f'"{raw}" AS {norm}' for raw, norm in zip(raw_cols, norm_cols)
    )
    con.execute(f'DROP TABLE IF EXISTS "{table}"')
    con.execute(
        f'CREATE TABLE "{table}" AS '
        f"SELECT {projection} FROM read_csv(?, header=true)",
        [str(csv_path)],
    )
    (count,) = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
    return ImportResult(dataset_code=dataset_code, table_name=table, row_count=count)


def import_archive(con, archive: Path, dataset_code: str, build_dir: Path) -> ImportResult:
    """Extract + import one archive, cleaning up the extracted CSV afterwards."""
    csv_path = extract_main_csv(archive, build_dir)
    try:
        return import_csv(con, csv_path, dataset_code)
    finally:
        csv_path.unlink(missing_ok=True)
