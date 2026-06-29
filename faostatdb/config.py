"""Configuration: TOML load/merge with defaults, plus ``config init`` / ``config show``.

Config is read with the stdlib ``tomllib`` (no external YAML). The on-disk file is
``faostatdb.toml`` in the current working directory. CLI flags override config values,
which in turn override the built-in defaults defined here.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

CONFIG_FILENAME = "faostatdb.toml"

DEFAULT_CONFIG_TOML = """\
[build]
database = "faostat.duckdb"
download_dir = "${FAOSTATDB_DOWNLOAD_DIR}" #where ZIP archives are cached
keep_archives = false
jobs = 6
overwrite = false

[datasets]
mode = "all"            # all | include | exclude
include = []
exclude = ["FA", "CBH"]
"""


@dataclass(frozen=True)
class BuildConfig:
    database: str = "faostat.duckdb"
    download_dir: str = ""
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


def load_config(path: Path | None = None) -> Config:
    """Load config from ``path`` (or auto-discover), merged over the defaults.

    A missing file yields the defaults. Unknown keys are ignored so that newer
    config files remain loadable by older tools.
    """
    cfg_path = path or find_config_file()
    if cfg_path is None:
        return default_config()
    with cfg_path.open("rb") as fh:
        raw = tomllib.load(fh)
    return merge_config(default_config(), raw)


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


def _field_names(cls: type) -> set[str]:
    return set(getattr(cls, "__dataclass_fields__", {}).keys())


def write_default_config(path: Path | None = None, *, overwrite: bool = False) -> Path:
    """Write a default ``faostatdb.toml`` (``config init``). Returns the path written."""
    target = path or (Path.cwd() / CONFIG_FILENAME)
    if target.exists() and not overwrite:
        raise FileExistsError(f"{target} already exists (use overwrite=True to replace)")
    target.write_text(DEFAULT_CONFIG_TOML, encoding="utf-8")
    return target


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
