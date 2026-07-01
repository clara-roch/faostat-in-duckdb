"""
Compact a finished DuckDB file to the smallest possible size.

Why this exists: DuckDB's ``ALTER TABLE ... DROP COLUMN`` is a *catalog* change.
The dropped column's data stays in the existing row groups on disk, and a plain
``CHECKPOINT`` does **not** rewrite them — so the disk savings from our dimension
extraction and constant-column removal are not realized until the file is
rewritten from scratch. (Measured: dropping a big column then checkpointing left
the file byte-for-byte identical.)

``COPY FROM DATABASE`` copies every table **and view** into a brand-new database
file, materializing only the columns that still exist. The result is the compact
mirror we want. We then atomically swap it in for the original.
"""

from __future__ import annotations

import os
from pathlib import Path


def _sql_str(path: Path) -> str:
    """Render a path as a single-quoted SQL string literal (doubling quotes)."""
    return "'" + str(path).replace("'", "''") + "'"


def compact_database(db_path: Path) -> tuple[int, int]:
    """Rewrite ``db_path`` into a fresh, compact file in place.

    Returns ``(size_before, size_after)`` in bytes. Safe to call on a closed
    database: it opens its own throwaway in-memory controller connection, attaches
    the source read-only and a fresh destination, copies everything across, then
    replaces the original file (and clears any stale ``.wal``).
    """
    import duckdb

    size_before = db_path.stat().st_size if db_path.exists() else 0
    tmp = db_path.with_name(db_path.name + ".compact")
    tmp_wal = tmp.with_name(tmp.name + ".wal")
    for stale in (tmp, tmp_wal):
        stale.unlink(missing_ok=True)

    con = duckdb.connect()  # in-memory controller; touches neither file's catalog
    try:
        con.execute(f"ATTACH {_sql_str(db_path)} AS src (READ_ONLY)")
        con.execute(f"ATTACH {_sql_str(tmp)} AS dst")
        con.execute("COPY FROM DATABASE src TO dst")
        con.execute("CHECKPOINT dst")
        con.execute("DETACH src")
        con.execute("DETACH dst")
    finally:
        con.close()

    # Swap the compacted file in for the original and drop the original's WAL.
    os.replace(tmp, db_path)
    db_path.with_name(db_path.name + ".wal").unlink(missing_ok=True)

    size_after = db_path.stat().st_size if db_path.exists() else 0
    return size_before, size_after
