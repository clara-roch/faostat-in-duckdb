"""
Configuration: load the shipped ``faostatdb.toml``, then apply ``secrets.env``.

The repository ships a single ``faostatdb.toml`` holding the general, default
configuration — that is what people get when they clone the project. It is meant
to be committed and left alone. Machine-specific or personal overrides go in a
git-ignored ``secrets.env`` (a simple ``KEY=value`` file) instead of editing the
TOML, so the shared defaults stay clean and pull-able.

Resolution order (lowest precedence first):

1. Built-in defaults (this module) — a safety net if ``faostatdb.toml`` is absent.
2. ``faostatdb.toml`` in the current working directory (the shipped defaults).
3. Environment variables, loaded from ``secrets.env`` if present (see
   :data:`apply_env_overrides`).
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
ENV_COMPACT = "FAOSTATDB_COMPACT"
ENV_KEEP_RAW_TABLES = "FAOSTATDB_KEEP_RAW_TABLES"
ENV_DATASETS_MODE = "FAOSTATDB_DATASETS_MODE"
ENV_DATASETS_INCLUDE = "FAOSTATDB_DATASETS_INCLUDE"
ENV_DATASETS_EXCLUDE = "FAOSTATDB_DATASETS_EXCLUDE"
ENV_IMPORT_THREADS = "FAOSTATDB_IMPORT_THREADS"
ENV_MEMORY_LIMIT = "FAOSTATDB_MEMORY_LIMIT"
ENV_ENRICH_AREAS = "FAOSTATDB_ENRICH_AREAS"
ENV_ENRICH_HISTORY = "FAOSTATDB_ENRICH_HISTORY"


@dataclass(frozen=True)
class BuildConfig:
    """The ``[build]`` section."""

    database: str = "faostat.duckdb"
    download_dir: str = "faostat_temp_download"
    # Delete downloaded archives after a *successful* build (the source-faithful
    # default from FAOSTATdb.md). Note hot restart still works: archives are never
    # deleted until the build succeeds, so an interrupted/failed run reuses them.
    # Set true to persist archives across successful builds (fast iteration).
    keep_archives: bool = False
    # 0 == "auto": resolve to min(8, 2 * cpu_count) at build time (see auto_jobs).
    jobs: int = 0
    overwrite: bool = False
    # Rewrite the finished database into a fresh file so dropped columns / dead
    # space are reclaimed, producing the smallest possible ``.duckdb``.
    compact: bool = True
    # Keep an untouched ``raw_<code>`` copy of each import for debugging.
    keep_raw_tables: bool = False


@dataclass(frozen=True)
class DatasetsConfig:
    """The ``[datasets]`` selection section."""

    mode: str = "all"  # all | include | exclude
    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=lambda: ["FA", "CBH"])


@dataclass(frozen=True)
class PerformanceConfig:
    """The ``[performance]`` section — DuckDB import tuning.

    Network download concurrency (``build.jobs``) and database import parallelism
    are deliberately separate knobs (FAOSTATdb.md > Parallel downloads). ``0`` /
    ``""`` mean "let DuckDB decide".
    """

    import_threads: int = 0  # 0 == DuckDB default (all cores)
    memory_limit: str = ""  # e.g. "8GB"; "" == DuckDB default


@dataclass(frozen=True)
class EnrichmentConfig:
    """The ``[enrichment]`` section — non-source-derived additions.

    Built by default so a fresh database ships with the country classification and
    historical validity ready to use. The additions are still clearly separated
    into their own ``area_classification`` table so they are never confused with
    source FAOSTAT content (FAOSTATdb.md > Country metadata); both columns come from
    a committed, hand-curated file (``faostatdb/area_classification.csv``) rather
    than from FAOSTAT, so the table stores no per-row confidence/source column.
    Disable with ``--no-enrich-areas`` / ``--no-enrich-history`` (or set these to
    ``false``) for a strictly source-only build.
    """

    area_classification: bool = True
    # Fill valid_from / valid_to in area_classification from the curated
    # area_classification.csv (implies area_classification). See enrich.enrich_history.
    historical_validity: bool = True


@dataclass(frozen=True)
class Config:
    build: BuildConfig = field(default_factory=BuildConfig)
    datasets: DatasetsConfig = field(default_factory=DatasetsConfig)
    performance: PerformanceConfig = field(default_factory=PerformanceConfig)
    enrichment: EnrichmentConfig = field(default_factory=EnrichmentConfig)


def auto_jobs() -> int:
    """Adaptive default download concurrency: ``min(8, 2 * cpu_count)``.

    FAOSTATdb.md pushes back on hard-coding an "optimal" number; this scales with
    the machine while capping at 8 to stay polite to the FAO server.
    """
    cpu = os.cpu_count() or 4
    return max(1, min(8, 2 * cpu))


def resolve_jobs(jobs: int) -> int:
    """Turn a configured ``jobs`` value into a concrete worker count (0 == auto)."""
    return auto_jobs() if jobs <= 0 else jobs


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
    """Merge a parsed TOML mapping over a base :class:`Config`.

    Only keys that exist as dataclass fields are applied, so an unknown key in a
    newer config file is silently ignored rather than raising.
    """
    def _section(cls, current, key):
        section_raw = raw.get(key, {}) or {}
        return replace(
            current,
            **{k: v for k, v in section_raw.items() if k in _field_names(cls)},
        )

    return Config(
        build=_section(BuildConfig, base.build, "build"),
        datasets=_section(DatasetsConfig, base.datasets, "datasets"),
        performance=_section(PerformanceConfig, base.performance, "performance"),
        enrichment=_section(EnrichmentConfig, base.enrichment, "enrichment"),
    )


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
    if (v := src.get(ENV_COMPACT)) is not None:
        build_updates["compact"] = _as_bool(v)
    if (v := src.get(ENV_KEEP_RAW_TABLES)) is not None:
        build_updates["keep_raw_tables"] = _as_bool(v)

    datasets_updates: dict[str, Any] = {}
    if (v := src.get(ENV_DATASETS_MODE)) is not None:
        datasets_updates["mode"] = v
    if (v := src.get(ENV_DATASETS_INCLUDE)) is not None:
        datasets_updates["include"] = _as_list(v)
    if (v := src.get(ENV_DATASETS_EXCLUDE)) is not None:
        datasets_updates["exclude"] = _as_list(v)

    perf_updates: dict[str, Any] = {}
    if (v := src.get(ENV_IMPORT_THREADS)) is not None:
        perf_updates["import_threads"] = _as_int(v, base.performance.import_threads)
    if (v := src.get(ENV_MEMORY_LIMIT)) is not None:
        perf_updates["memory_limit"] = v

    enrich_updates: dict[str, Any] = {}
    if (v := src.get(ENV_ENRICH_AREAS)) is not None:
        enrich_updates["area_classification"] = _as_bool(v)
    if (v := src.get(ENV_ENRICH_HISTORY)) is not None:
        enrich_updates["historical_validity"] = _as_bool(v)

    return Config(
        build=replace(base.build, **build_updates) if build_updates else base.build,
        datasets=(
            replace(base.datasets, **datasets_updates)
            if datasets_updates
            else base.datasets
        ),
        performance=(
            replace(base.performance, **perf_updates)
            if perf_updates
            else base.performance
        ),
        enrichment=(
            replace(base.enrichment, **enrich_updates)
            if enrich_updates
            else base.enrichment
        ),
    )


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
    """Render a :class:`Config` back to a TOML string (for ``config show`` / init)."""
    def _arr(values: list[str]) -> str:
        return "[" + ", ".join(f'"{v}"' for v in values) + "]"

    def _b(v: bool) -> str:
        return str(v).lower()

    return (
        "[build]\n"
        f'database = "{cfg.build.database}"\n'
        f'download_dir = "{cfg.build.download_dir}"\n'
        f"keep_archives = {_b(cfg.build.keep_archives)}\n"
        f"jobs = {cfg.build.jobs}\n"
        f"overwrite = {_b(cfg.build.overwrite)}\n"
        f"compact = {_b(cfg.build.compact)}\n"
        f"keep_raw_tables = {_b(cfg.build.keep_raw_tables)}\n"
        "\n"
        "[datasets]\n"
        f'mode = "{cfg.datasets.mode}"\n'
        f"include = {_arr(cfg.datasets.include)}\n"
        f"exclude = {_arr(cfg.datasets.exclude)}\n"
        "\n"
        "[performance]\n"
        f"import_threads = {cfg.performance.import_threads}\n"
        f'memory_limit = "{cfg.performance.memory_limit}"\n'
        "\n"
        "[enrichment]\n"
        f"area_classification = {_b(cfg.enrichment.area_classification)}\n"
        f"historical_validity = {_b(cfg.enrichment.historical_validity)}\n"
    )
