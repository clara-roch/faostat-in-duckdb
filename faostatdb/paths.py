"""
Path resolution for raw archives and the output database.

Two distinct locations:

* **Raw ZIP archives** live in a *cache* directory. By default we keep them (so a
  re-run reuses them instead of re-downloading — see FAOSTATdb.md > hot restart),
  which means the project-local ``./faostat_temp_download/`` by default. See
  :func:`resolve_download_dir`.
* **The final ``.duckdb``** is written *project-local* by default (a bare filename
  like ``faostat.duckdb`` lands in the current working directory, next to the
  project that built it). Keep the built DB out of git via ``.gitignore``, not by
  hiding it in an OS data directory.

Download-dir resolution order (highest precedence first):

1. ``--download-dir DIR`` (explicit CLI flag / config ``download_dir``), with
   ``~`` expansion applied.
2. If keeping archives: project-local ``./faostat_temp_download/``.
3. Otherwise: an OS-appropriate cache directory (never the repo).

The download manifest always lives under ``<download_dir>/.faostatdb-downloads/``.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

PROJECT_LOCAL_DIRNAME = "faostat_temp_download"
MANIFEST_DIRNAME = ".faostatdb-downloads"
MANIFEST_FILENAME = "manifest.jsonl"
APP_NAME = "faostatdb"


def resolve_download_dir(
    explicit: str | None = None,
    *,
    keep_archives: bool = False,
    cwd: Path | None = None,
) -> Path:
    """Resolve the directory where raw archives are downloaded / cached.

    The directory is created if necessary. A relative explicit path is resolved
    against ``cwd`` so archives stay inside the project by default. When no
    explicit path is set, the location depends on ``keep_archives``: project-local
    when we mean to keep them around, otherwise an OS cache dir so they don't
    clutter the working tree.
    """
    base = cwd or Path.cwd()
    chosen = _expand(explicit)
    if chosen:
        resolved = Path(chosen).expanduser()
    elif keep_archives:
        resolved = base / PROJECT_LOCAL_DIRNAME
    else:
        resolved = _os_cache_dir()
    if not resolved.is_absolute():
        resolved = base / resolved
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def resolve_database_path(database: str, *, cwd: Path | None = None) -> Path:
    """Resolve where the final ``.duckdb`` is written.

    An absolute ``database`` is used verbatim. A bare filename (or relative path)
    resolves against ``cwd`` so the built database lives project-local, next to
    whatever built it. Keep it out of git via ``.gitignore`` rather than by hiding
    it elsewhere. The parent directory is created.
    """
    base = cwd or Path.cwd()
    db = Path(database).expanduser()
    if db.is_absolute():
        target = db
    else:
        target = base / db
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _expand(value: str | None) -> str | None:
    """Return a non-empty configured path after trimming whitespace.

    Environment variables are intentionally not expanded. Path location should be
    controlled by configuration or CLI flags, not ambient process state.
    """
    if not value:
        return None
    expanded = value.strip()
    if not expanded:
        return None
    return expanded


def _os_cache_dir() -> Path:
    """OS-appropriate cache dir (``platformdirs`` if available, else tempdir)."""
    try:
        from platformdirs import user_cache_dir  # type: ignore

        return Path(user_cache_dir(APP_NAME))
    except ImportError:
        return Path(tempfile.gettempdir()) / APP_NAME


def manifest_path(download_dir: Path) -> Path:
    """Return the manifest path under ``download_dir`` (parent dir is created)."""
    d = download_dir / MANIFEST_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d / MANIFEST_FILENAME


def clean_cache(download_dir: Path, *, remove_dir: bool = False) -> tuple[int, int]:
    """Delete cached archives and the manifest under ``download_dir``.

    Removes every ``*.zip`` / ``*.part`` in the directory and the whole
    ``.faostatdb-downloads`` manifest/build subdir. Returns
    ``(archives_removed, bytes_freed)`` for reporting. Used by ``faostatdb
    clean-cache``.

    When ``remove_dir`` is true, also remove ``download_dir`` if it is empty after
    the managed cache files are deleted. Any unrelated files keep the directory in
    place.
    """
    removed = 0
    freed = 0
    if download_dir.is_dir():
        for pattern in ("*.zip", "*.part"):
            for f in download_dir.glob(pattern):
                try:
                    freed += f.stat().st_size
                    f.unlink()
                    removed += 1
                except OSError:
                    pass
    manifest_dir = download_dir / MANIFEST_DIRNAME
    if manifest_dir.is_dir():
        shutil.rmtree(manifest_dir, ignore_errors=True)
    if remove_dir:
        try:
            download_dir.rmdir()
        except OSError:
            pass
    return removed, freed
