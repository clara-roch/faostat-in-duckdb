"""Path resolution for raw archives and the output database.

Two distinct locations:

* **Raw ZIP archives** live in a *cache* directory. By default we keep them (so a
  re-run reuses them instead of re-downloading — see FAOSTATdb.md > hot restart),
  which means the project-local ``./faostat_temp_download/`` by default. See
  :func:`resolve_download_dir`.
* **The final ``.duckdb``** is written *outside* the repository, in the directory
  named by the ``FABIO_DUCKDB_DIR`` environment variable. See
  :func:`resolve_database_path`.

Download-dir resolution order (highest precedence first):

1. ``--download-dir DIR`` (explicit CLI flag / config ``download_dir``), with
   ``${VAR}`` / ``%VAR%`` / ``~`` expansion applied.
2. ``FAOSTATDB_DOWNLOAD_DIR`` environment variable.
3. If keeping archives: project-local ``./faostat_temp_download/``.
4. Otherwise: an OS-appropriate cache directory (never the repo).

The download manifest always lives under ``<download_dir>/.faostatdb-downloads/``.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

PROJECT_LOCAL_DIRNAME = "faostat_temp_download"
MANIFEST_DIRNAME = ".faostatdb-downloads"
MANIFEST_FILENAME = "manifest.jsonl"
APP_NAME = "faostatdb"
ENV_DOWNLOAD_DIR = "FAOSTATDB_DOWNLOAD_DIR"
ENV_DUCKDB_DIR = "FABIO_DUCKDB_DIR"


def resolve_download_dir(
    explicit: str | None = None,
    *,
    keep_archives: bool = False,
    cwd: Path | None = None,
) -> Path:
    """Resolve the directory where raw archives are downloaded / cached.

    The directory is created if necessary. A relative explicit/env path is
    resolved against ``cwd`` so archives stay inside the project by default.
    When neither an explicit path nor the env var is set, the location depends on
    ``keep_archives``: project-local when we mean to keep them around, otherwise
    an OS cache dir so they don't clutter the working tree.
    """
    base = cwd or Path.cwd()
    chosen = _expand(explicit) or _expand(os.environ.get(ENV_DOWNLOAD_DIR))
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
    is placed inside ``$FABIO_DUCKDB_DIR``; if that variable is unset, it falls
    back to the OS data dir — never the repository — so built databases don't get
    committed by accident. The parent directory is created.
    """
    base = cwd or Path.cwd()
    db = Path(os.path.expandvars(database)).expanduser()
    if db.is_absolute():
        target = db
    else:
        dir_env = _expand(os.environ.get(ENV_DUCKDB_DIR))
        parent = Path(dir_env).expanduser() if dir_env else _os_data_dir()
        if not parent.is_absolute():
            parent = base / parent
        target = parent / db
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _expand(value: str | None) -> str | None:
    """Expand environment variables in a configured path.

    Returns ``None`` for an empty value or one that still contains an unexpanded
    reference (e.g. ``${FAOSTATDB_DOWNLOAD_DIR}`` when that variable is unset), so
    resolution falls through to the next source instead of creating a literal
    ``${...}`` directory.
    """
    if not value:
        return None
    expanded = os.path.expandvars(value).strip()
    if not expanded:
        return None
    if "${" in expanded or "%" in expanded:
        return None
    return expanded


def _os_cache_dir() -> Path:
    """OS-appropriate cache dir (``platformdirs`` if available, else tempdir)."""
    try:
        from platformdirs import user_cache_dir  # type: ignore

        return Path(user_cache_dir(APP_NAME))
    except ImportError:
        return Path(tempfile.gettempdir()) / APP_NAME


def _os_data_dir() -> Path:
    """OS-appropriate data dir (``platformdirs`` if available, else tempdir)."""
    try:
        from platformdirs import user_data_dir  # type: ignore

        return Path(user_data_dir(APP_NAME))
    except ImportError:
        return Path(tempfile.gettempdir()) / APP_NAME


def manifest_path(download_dir: Path) -> Path:
    """Return the manifest path under ``download_dir`` (parent dir is created)."""
    d = download_dir / MANIFEST_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d / MANIFEST_FILENAME


def clean_cache(download_dir: Path) -> tuple[int, int]:
    """Delete cached archives and the manifest under ``download_dir``.

    Removes every ``*.zip`` / ``*.part`` in the directory and the whole
    ``.faostatdb-downloads`` manifest/build subdir. Returns
    ``(archives_removed, bytes_freed)`` for reporting. Used by ``faostatdb
    clean-cache``.
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
    return removed, freed
