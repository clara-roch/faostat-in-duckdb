"""Command-line interface: argument parsing and command dispatch.

Commands (v0.1): ``list``, ``config init|show``, ``build``. The CLI resolves
configuration (defaults < ``faostatdb.toml`` < flags), then delegates to the
relevant modules.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from . import config as config_mod
from . import metadata as metadata_mod
from .config import Config, DatasetsConfig


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="faostatdb",
        description="Build a local, source-preserving DuckDB mirror of FAOSTAT bulk data.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", help="list available datasets")
    p_list.add_argument(
        "--remote", action="store_true", help="fetch the live remote inventory"
    )

    # config
    p_config = sub.add_parser("config", help="manage configuration")
    config_sub = p_config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("init", help="write a default faostatdb.toml in the cwd")
    config_sub.add_parser("show", help="print the effective configuration")

    # build
    p_build = sub.add_parser("build", help="download and import datasets")
    p_build.add_argument("--database", default=None, help="output DuckDB path")
    p_build.add_argument("--include", default=None, help="comma-separated codes to include")
    p_build.add_argument("--exclude", default=None, help="comma-separated codes to exclude")
    p_build.add_argument("--jobs", type=int, default=None, help="parallel download jobs")
    p_build.add_argument("--keep-archives", action="store_true", help="keep ZIPs after build")
    p_build.add_argument("--download-dir", default=None, help="archive download directory")
    p_build.add_argument("--yes", action="store_true", help="assume yes for prompts")
    p_build.add_argument("--strict", action="store_true", help="fail build on any error")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = config_mod.load_config()

    if args.command == "list":
        return _cmd_list(args, cfg)
    if args.command == "config":
        return _cmd_config(args, cfg)
    if args.command == "build":
        return _cmd_build(args, cfg)
    parser.error(f"unknown command: {args.command}")
    return 2


def _cmd_list(args: argparse.Namespace, cfg: Config) -> int:
    snapshot = metadata_mod.fetch_and_parse()
    selected = metadata_mod.select_datasets(snapshot.datasets, cfg.datasets)
    for d in selected:
        print(f"{d.code:<8} {d.dataset_name}")
    print(f"\n{len(selected)} dataset(s) selected of {len(snapshot.datasets)} available.")
    return 0


def _cmd_config(args: argparse.Namespace, cfg: Config) -> int:
    if args.config_command == "init":
        try:
            path = config_mod.write_default_config()
        except FileExistsError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        print(f"wrote {path}")
        return 0
    if args.config_command == "show":
        print(config_mod.config_to_toml(cfg))
        return 0
    return 2


def _cmd_build(args: argparse.Namespace, cfg: Config) -> int:
    cfg = _apply_build_overrides(args, cfg)
    return run_build(cfg, assume_yes=args.yes, strict=args.strict)


def run_build(cfg: Config, *, assume_yes: bool, strict: bool) -> int:
    """Wire download -> validate -> import -> metadata for the selected datasets.

    v0.1 orchestration skeleton: this resolves selection and the download dir,
    then drives the per-dataset state machine. The parallel download loop, hot
    restart, and archive cleanup are filled in per PLAN.md step 5.
    """
    from . import paths as paths_mod
    from . import progress

    snapshot = metadata_mod.fetch_and_parse()
    selected = metadata_mod.select_datasets(snapshot.datasets, cfg.datasets)

    if not selected:
        print("no datasets selected", file=sys.stderr)
        return 1

    if not assume_yes and not _confirm(selected, cfg):
        print("aborted", file=sys.stderr)
        return 1

    download_dir = paths_mod.resolve_download_dir(
        cfg.build.download_dir or None, keep_archives=cfg.build.keep_archives
    )
    progress.log(
        f"building {cfg.build.database} from {len(selected)} dataset(s) "
        f"(archives in {download_dir})"
    )
    raise NotImplementedError(
        "build orchestration (download/validate/import loop) — PLAN.md step 5"
    )


def _confirm(selected: list, cfg: Config) -> bool:
    if not sys.stdin.isatty():
        print("non-interactive: pass --yes to build", file=sys.stderr)
        return False
    answer = input(f"Build {len(selected)} dataset(s) into {cfg.build.database}? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def _apply_build_overrides(args: argparse.Namespace, cfg: Config) -> Config:
    """Layer CLI build flags over the loaded config."""
    from dataclasses import replace

    build = cfg.build
    if args.database is not None:
        build = replace(build, database=args.database)
    if args.download_dir is not None:
        build = replace(build, download_dir=args.download_dir)
    if args.jobs is not None:
        build = replace(build, jobs=args.jobs)
    if args.keep_archives:
        build = replace(build, keep_archives=True)

    datasets = cfg.datasets
    if args.include is not None:
        datasets = DatasetsConfig(
            mode="include", include=_split_codes(args.include), exclude=datasets.exclude
        )
    elif args.exclude is not None:
        datasets = DatasetsConfig(
            mode="exclude", include=datasets.include, exclude=_split_codes(args.exclude)
        )
    return Config(build=build, datasets=datasets)


def _split_codes(value: str) -> list[str]:
    return [c.strip() for c in value.split(",") if c.strip()]
