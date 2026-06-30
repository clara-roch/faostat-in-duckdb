"""Configuration: load the shipped ``faostatdb.toml``, then apply ``secrets.env``.

The repository ships a single ``faostatdb.toml`` holding the general, default
configuration — that is what people get when they clone the project. It is meant
to be committed and left alone. Machine-specific or personal overrides go in a
git-ignored ``secrets.env`` (a simple ``KEY=value`` file) instead of editing the
TOML, so the shared defaults stay clean and pull-able.

Resolution order (lowest precedence first):

1. Built-in defaults (this module) — a safety net if ``faostatdb.toml`` is absent.
2. ``faostatdb.toml`` in the current working directory (the shipped defaults).
3. Environment variables, loaded from ``secrets.env`` if present (see
   :data:`ENV_OVERRIDES`).
4. CLI flags (applied later in :mod:`faostatdb.cli`).

Config is parsed with the stdlib ``tomllib`` and ``secrets.env`` with a tiny
hand-rolled reader — no external dependencies.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "faostatdb.toml"
SECRETS_FILENAME = "secrets.env"

# Environment-variable names that override individual config values. These are
# what users set in their own ``secrets.env``. ``FABIO_DUCKDB_DIR`` and
# ``FAOSTATDB_DOWNLOAD_DIR`` (consumed in faostatdb.paths) point at directories;
# the variables below override values inside ``faostatdb.toml``.
ENV_DATABASE = "FAOSTATDB_DATABASE"
ENV_DOWNLOAD_DIR = "FAOSTATDB_DOWNLOAD_DIR"
ENV_KEEP_ARCHIVES = "FAOSTATDB_KEEP_ARCHIVES"
ENV_JOBS = "FAOSTATDB_JOBS"
ENV_OVERWRITE = "FAOSTATDB_OVERWRITE"
ENV_DATASETS_MODE = "FAOSTATDB_DATASETS_MODE"
ENV_DATASETS_INCLUDE = "FAOSTATDB_DATASETS_INCLUDE"
ENV_DATASETS_EXCLUDE = "FAOSTATDB_DATASETS_EXCLUDE"


@dataclass(frozen=True)
class BuildConfig:
    database: str = "faostat.duckdb"
    download_dir: str = "faostat_temp_download"
    keep_archives: bool = False
    jobs: int = 6
    overwrite: bool = False


@dataclass(frozen=True)
class DatasetsConfig:
    mode: str = "all"  # all | include | exclude
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=lambda: ["FA", "CBH"])


@dataclass(frozen=True)
class Config:
    build: BuildConfig = field(default_factory=BuildConfig)
    datasets: DatasetsConfig = field(default_factory=DatasetsConfig)


def default_config() -> Config:
    """Return the built-in default configuration."""
    return Config()


def find_config_file(start: Path | None = None) -> Path | None:
    """Return the path to ``faostatdb.toml`` in ``start`` (default: cwd), or None."""
    base = start or Path.cwd()
    candidate = base / CONFIG_FILENAME
    return candidate if candidate.is_file() else None


def load_config(path: Path | None = None, *, load_secrets: bool = True) -> Config:
    """Load the effective configuration.

    Starts from the built-in defaults, merges ``faostatdb.toml`` over them, then
    applies any overriding environment variables. When ``load_secrets`` is true
    (the default), values from a ``secrets.env`` in the cwd are loaded into the
    environment first (without clobbering variables already set in the shell).

    A missing ``faostatdb.toml`` yields the built-in defaults. Unknown TOML keys
    are ignored so newer config files stay loadable by older tools.
    """
    if load_secrets:
        load_dotenv()

    cfg = default_config()
    cfg_path = path or find_config_file()
    if cfg_path is not None:
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh)
        cfg = merge_config(cfg, raw)
    return apply_env_overrides(cfg)


def merge_config(base: Config, raw: dict[str, Any]) -> Config:
    """Merge a parsed TOML mapping over a base :class:`Config`."""
    build_raw = raw.get("build", {}) or {}
    datasets_raw = raw.get("datasets", {}) or {}

    build = replace(
        base.build,
        **{k: v for k, v in build_raw.items() if k in _field_names(BuildConfig)},
    )
    datasets = replace(
        base.datasets,
        **{k: v for k, v in datasets_raw.items() if k in _field_names(DatasetsConfig)},
    )
    return Config(build=build, datasets=datasets)


def apply_env_overrides(base: Config, env: dict[str, str] | None = None) -> Config:
    """Override config values from environment variables (see module docstring).

    Only variables that are actually set take effect; everything else falls
    through to the TOML / built-in value.
    """
    src = os.environ if env is None else env

    build_updates: dict[str, Any] = {}
    if (v := src.get(ENV_DATABASE)) is not None:
        build_updates["database"] = v
    if (v := src.get(ENV_DOWNLOAD_DIR)) is not None:
        build_updates["download_dir"] = v
    if (v := src.get(ENV_KEEP_ARCHIVES)) is not None:
        build_updates["keep_archives"] = _as_bool(v)
    if (v := src.get(ENV_JOBS)) is not None:
        build_updates["jobs"] = _as_int(v, base.build.jobs)
    if (v := src.get(ENV_OVERWRITE)) is not None:
        build_updates["overwrite"] = _as_bool(v)

    datasets_updates: dict[str, Any] = {}
    if (v := src.get(ENV_DATASETS_MODE)) is not None:
        datasets_updates["mode"] = v
    if (v := src.get(ENV_DATASETS_INCLUDE)) is not None:
        datasets_updates["include"] = _as_list(v)
    if (v := src.get(ENV_DATASETS_EXCLUDE)) is not None:
        datasets_updates["exclude"] = _as_list(v)

    build = replace(base.build, **build_updates) if build_updates else base.build
    datasets = (
        replace(base.datasets, **datasets_updates) if datasets_updates else base.datasets
    )
    return Config(build=build, datasets=datasets)


def load_dotenv(path: Path | None = None) -> dict[str, str]:
    """Load a ``secrets.env`` file into ``os.environ`` and return what it set.

    The format is one ``KEY=value`` per line; blank lines and ``#`` comments are
    ignored, surrounding whitespace is trimmed, and matching single/double quotes
    around the value are stripped. Variables already present in the environment
    are *not* overwritten, so an explicit shell export always wins. A missing
    file is a no-op.
    """
    target = path or (Path.cwd() / SECRETS_FILENAME)
    if not target.is_file():
        return {}

    loaded: dict[str, str] = {}
    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if not key or key in os.environ:
            continue
        os.environ[key] = value
        loaded[key] = value
    return loaded


def _as_bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _as_int(value: str, fallback: int) -> int:
    try:
        return int(value.strip())
    except ValueError:
        return fallback


def _as_list(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _field_names(cls: type) -> set[str]:
    return set(getattr(cls, "__dataclass_fields__", {}).keys())


def config_to_toml(cfg: Config) -> str:
    """Render a :class:`Config` back to a TOML string (for ``config show``)."""
    def _arr(values: list[str]) -> str:
        return "[" + ", ".join(f'"{v}"' for v in values) + "]"

    return (
        "[build]\n"
        f'database = "{cfg.build.database}"\n'
        f'download_dir = "{cfg.build.download_dir}"\n'
        f"keep_archives = {str(cfg.build.keep_archives).lower()}\n"
        f"jobs = {cfg.build.jobs}\n"
        f"overwrite = {str(cfg.build.overwrite).lower()}\n"
        "\n"
        "[datasets]\n"
        f'mode = "{cfg.datasets.mode}"\n'
        f"include = {_arr(cfg.datasets.include)}\n"
        f"exclude = {_arr(cfg.datasets.exclude)}\n"
    )
