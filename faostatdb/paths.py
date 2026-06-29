"""Download-directory resolution.

Resolution order (highest precedence first):

1. ``--download-dir DIR`` (explicit CLI flag / config ``download_dir``), with
   ``${VAR}`` / ``%VAR%`` / ``~`` expansion applied.
2. ``FAOSTATDB_DOWNLOAD_DIR`` environment variable (keep private paths out of the
   committed ``faostatdb.toml`` — see the README "Secrets" section).
3. Project-local ``./faostatdb_archives/`` when ``keep_archives`` is set.
4. OS cache dir (via ``platformdirs`` if installed, else a stdlib fallback).

The download manifest always lives under ``<download_dir>/.faostatdb-downloads/``.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

PROJECT_LOCAL_DIRNAME = "faostatdb_archives"
MANIFEST_DIRNAME = ".faostatdb-downloads"
MANIFEST_FILENAME = "manifest.jsonl"
APP_NAME = "faostatdb"
ENV_DOWNLOAD_DIR = "FAOSTATDB_DOWNLOAD_DIR"


def resolve_download_dir(
    explicit: str | None = None,
    *,
    keep_archives: bool = False,
    cwd: Path | None = None,
) -> Path:
    """Resolve the directory where archives are downloaded.

    The directory is created if necessary.
    """
    base = cwd or Path.cwd()
    chosen = _expand(explicit) or _expand(os.environ.get(ENV_DOWNLOAD_DIR))
    if chosen:
        resolved = Path(chosen).expanduser()
    elif keep_archives:
        resolved = base / PROJECT_LOCAL_DIRNAME
    else:
        resolved = _os_cache_dir()
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


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


def manifest_path(download_dir: Path) -> Path:
    """Return the manifest path under ``download_dir`` (parent dir is created)."""
    d = download_dir / MANIFEST_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d / MANIFEST_FILENAME
