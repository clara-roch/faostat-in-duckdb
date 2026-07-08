"""
Configuration: built-in defaults overlaid by a launch-directory ``faostatdb.toml``.

Configuration is deliberately simple. The package ships built-in defaults (this
module). To persist settings, a user runs ``faostatdb config init`` to write a
``faostatdb.toml`` into the directory they launch ``faostatdb`` from, then edits
it. CLI flags override individual values for one-off runs.

Resolution order (lowest precedence first):

1. Built-in defaults (this module).
2. ``faostatdb.toml`` in the current working directory (the launch directory).
3. CLI flags (applied later in :mod:`faostatdb.cli`).

The ``faostatdb.toml`` committed to the repository is only the package's
example/default configuration used during development — end users neither clone
the repo nor edit that file; they use their own launch-directory copy.

Config is parsed with the stdlib ``tomllib`` — no external dependencies.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "faostatdb.toml"


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
    # Restrict imported rows to selected year(s). Empty == all years. A spec of
    # comma-separated single years and inclusive ranges, e.g. "2010",
    # "2000,2005,2010" or "1990-1995,2020". FAOSTAT ships every year in one bulk
    # archive, so the whole ZIP is still downloaded; the filter drops non-matching
    # rows at import time (see importer.import_csv). Parsed by parse_years.
    years: str = ""


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


def parse_years(spec: str | None) -> set[int] | None:
    """Parse a ``[build] years`` / ``--years`` spec into a set of years.

    Returns ``None`` when no filter is requested (empty / whitespace) so callers
    can treat "all years" distinctly from "an empty selection". A spec is a
    comma-separated list of single years and inclusive ``lo-hi`` ranges::

        >>> sorted(parse_years("2010"))
        [2010]
        >>> sorted(parse_years("2000,2005,2010"))
        [2000, 2005, 2010]
        >>> sorted(parse_years("1990-1992,2000"))
        [1990, 1991, 1992, 2000]
        >>> parse_years("") is None
        True

    Raises :class:`ValueError` on a malformed token or an inverted range so a
    typo fails the build up front rather than silently importing nothing.
    """
    if spec is None or not spec.strip():
        return None
    years: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token.lstrip("-"):
            # A range "lo-hi" (the lstrip guards against a leading sign so a bare
            # negative year still reads as one malformed token, not a range).
            lo_str, sep, hi_str = token.partition("-")
            lo, hi = _parse_year(lo_str, spec), _parse_year(hi_str, spec)
            if lo > hi:
                raise ValueError(f"inverted year range {token!r} in {spec!r}")
            years.update(range(lo, hi + 1))
        else:
            years.add(_parse_year(token, spec))
    if not years:
        raise ValueError(f"no years parsed from {spec!r}")
    return years


def _parse_year(text: str, spec: str) -> int:
    """Parse one year token, bounding it to a plausible 1..9999 calendar year."""
    text = text.strip()
    try:
        year = int(text)
    except ValueError:
        raise ValueError(f"invalid year {text!r} in {spec!r}") from None
    if not 1 <= year <= 9999:
        raise ValueError(f"year {year} out of range (1..9999) in {spec!r}")
    return year


def default_config() -> Config:
    """Return the built-in default configuration."""
    return Config()


def find_config_file(start: Path | None = None) -> Path | None:
    """Return the path to ``faostatdb.toml`` in ``start`` (default: cwd), or None."""
    base = start or Path.cwd()
    candidate = base / CONFIG_FILENAME
    return candidate if candidate.is_file() else None


def load_config(path: Path | None = None) -> Config:
    """Load the effective configuration.

    Starts from the built-in defaults and merges a launch-directory
    ``faostatdb.toml`` over them. CLI flags are applied later, in
    :mod:`faostatdb.cli`, on top of the returned config.

    A missing ``faostatdb.toml`` yields the built-in defaults. Unknown TOML keys
    are ignored so newer config files stay loadable by older tools.
    """
    cfg = default_config()
    cfg_path = path or find_config_file()
    if cfg_path is not None:
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh)
        cfg = merge_config(cfg, raw)
    return cfg


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
        f'years = "{cfg.build.years}"\n'
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
