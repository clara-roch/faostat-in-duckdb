"""Path resolution for raw archives and the output database.

Two distinct locations:

* **Raw ZIP archives** are *temporary* and live in the project by default
  (``./faostat_temp_download/``). They are deleted after a successful build unless
  ``keep_archives`` is set. See :func:`resolve_download_dir`.
* **The final ``.duckdb``** is written *outside* the repository, in the directory
  named by the ``FABIO_DUCKDB_DIR`` environment variable. See
  :func:`resolve_database_path`.

Download-dir resolution order (highest precedence first):

1. ``--download-dir DIR`` (explicit CLI flag / config ``download_dir``), with
   ``${VAR}`` / ``%VAR%`` / ``~`` expansion applied.
2. ``FAOSTATDB_DOWNLOAD_DIR`` environment variable.
3. Project-local ``./faostat_temp_download/``.

The download manifest always lives under ``<download_dir>/.faostatdb-downloads/``.
"""

from __future__ import annotations

import os
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
    """Resolve the directory where raw archives are downloaded (temporary).

    The directory is created if necessary. A relative path is resolved against
    ``cwd`` so archives stay inside the project by default.
    """
    base = cwd or Path.cwd()
    chosen = _expand(explicit) or _expand(os.environ.get(ENV_DOWNLOAD_DIR))
    resolved = Path(chosen).expanduser() if chosen else base / PROJECT_LOCAL_DIRNAME
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
    try:
        from platformdirs import user_cache_dir  # type: ignore

        return Path(user_cache_dir(APP_NAME))
    except ImportError:
        return Path(tempfile.gettempdir()) / APP_NAME


def _os_data_dir() -> Path:
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
