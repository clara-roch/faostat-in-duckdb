"""Command-line interface: argument parsing and command dispatch.

Commands (v0.1): ``list``, ``config show``, ``build``. The CLI resolves
configuration (``faostatdb.toml`` < ``secrets.env`` env vars < flags), then
delegates to the relevant modules.
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

    # tables
    p_tables = sub.add_parser("tables", help="list tables in a built database")
    p_tables.add_argument("--database", default=None, help="DuckDB path to inspect")

    # config
    p_config = sub.add_parser("config", help="manage configuration")
    config_sub = p_config.add_subparsers(dest="config_command", required=True)
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
    if args.command == "tables":
        return _cmd_tables(args, cfg)
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


def _cmd_tables(args: argparse.Namespace, cfg: Config) -> int:
    import duckdb

    from . import paths as paths_mod

    db_path = paths_mod.resolve_database_path(args.database or cfg.build.database)
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 1

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT table_name, estimated_size "
            "FROM duckdb_tables() ORDER BY table_name"
        ).fetchall()
    finally:
        con.close()

    if not rows:
        print(f"no tables in {db_path}")
        return 0

    for name, est_rows in rows:
        count = "?" if est_rows is None else f"{est_rows:,}"
        print(f"{name:<24} {count:>15} rows")
    print(f"\n{len(rows)} table(s) in {db_path}")
    return 0


def _cmd_config(args: argparse.Namespace, cfg: Config) -> int:
    if args.config_command == "show":
        print(config_mod.config_to_toml(cfg))
        return 0
    return 2


def _cmd_build(args: argparse.Namespace, cfg: Config) -> int:
    cfg = _apply_build_overrides(args, cfg)
    return run_build(cfg, assume_yes=args.yes, strict=args.strict)


def run_build(cfg: Config, *, assume_yes: bool, strict: bool) -> int:
    """Wire download -> validate -> import -> metadata for the selected datasets.

    Drives the per-dataset state machine recorded in the download manifest:
    archives are downloaded in parallel (with hot restart of already-valid
    archives), validated with ``zipfile.testzip()``, then imported sequentially
    into one ``data_<code>`` fact table each. Source metadata and build
    provenance are persisted to ``faostat_dataset`` / ``faostat_build``, and
    valid archives are deleted on success unless ``keep_archives`` is set.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pathlib import Path

    from . import download as download_mod
    from . import importer as importer_mod
    from . import paths as paths_mod
    from . import progress
    from . import schema as schema_mod
    from . import validate as validate_mod
    from .download import ManifestEntry, State

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

    manifest = download_mod.Manifest(paths_mod.manifest_path(download_dir))

    def archive_path_for(rec) -> Path:
        name = None
        if rec.file_location:
            name = rec.file_location.rstrip("/").rsplit("/", 1)[-1]
        if not name or not name.lower().endswith(".zip"):
            name = f"{rec.code}.zip"
        return download_dir / name

    archives = {rec.code: archive_path_for(rec) for rec in selected}

    # --- Phase 1: parallel download (hot restart skips valid archives) -------
    to_download = [
        rec
        for rec in selected
        if rec.file_location and manifest.needs_download(rec.code, archives[rec.code])
    ]
    skipped = len(selected) - len(to_download) - sum(
        1 for rec in selected if not rec.file_location
    )
    if skipped > 0:
        progress.log(f"reusing {skipped} already-downloaded archive(s)")

    download_failed: set[str] = set()
    for rec in to_download:
        manifest.update(
            ManifestEntry(
                dataset_code=rec.code,
                state=State.DOWNLOADING.value,
                archive_path=str(archives[rec.code]),
                url=rec.file_location,
            ),
            now=_now(),
        )

    jobs = max(1, cfg.build.jobs)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = {
            pool.submit(
                download_mod.download_with_retry, rec.file_location, archives[rec.code]
            ): rec
            for rec in to_download
        }
        for future in as_completed(futures):
            rec = futures[future]
            try:
                future.result()
            except Exception as exc:  # noqa: BLE001 — recorded per-dataset
                download_failed.add(rec.code)
                manifest.update(
                    ManifestEntry(
                        dataset_code=rec.code,
                        state=State.FAILED.value,
                        archive_path=str(archives[rec.code]),
                        url=rec.file_location,
                        error=f"download: {exc}",
                    ),
                    now=_now(),
                )
                progress.log(f"✗ {rec.code}: download failed: {exc}")
                if strict:
                    print(f"strict: download failed for {rec.code}", file=sys.stderr)
                    return 1
            else:
                manifest.update(
                    ManifestEntry(
                        dataset_code=rec.code,
                        state=State.DOWNLOADED.value,
                        archive_path=str(archives[rec.code]),
                        url=rec.file_location,
                    ),
                    now=_now(),
                )
                progress.log(f"✓ {rec.code}: downloaded")

    # --- Phase 2: validate + import sequentially into DuckDB -----------------
    import duckdb

    db_path = paths_mod.resolve_database_path(cfg.build.database)
    if cfg.build.overwrite and db_path.exists():
        db_path.unlink()

    build_dir = download_dir / paths_mod.MANIFEST_DIRNAME / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_path))
    build_id = _build_id()
    started_at = _now()
    imported: list[str] = []
    failed: list[str] = []
    try:
        schema_mod.create_metadata_tables(con)
        for rec in selected:
            archive = archives[rec.code]
            if rec.code in download_failed:
                failed.append(rec.code)
                _record_dataset(con, rec, snapshot, archive, None, "failed")
                continue
            if not archive.exists():
                failed.append(rec.code)
                manifest.update(
                    ManifestEntry(
                        dataset_code=rec.code,
                        state=State.FAILED.value,
                        archive_path=str(archive),
                        url=rec.file_location,
                        error="archive missing",
                    ),
                    now=_now(),
                )
                _record_dataset(con, rec, snapshot, archive, None, "failed")
                progress.log(f"✗ {rec.code}: archive missing")
                if strict:
                    return 1
                continue

            result = validate_mod.validate_zip(archive)
            if not result.ok:
                failed.append(rec.code)
                manifest.update(
                    ManifestEntry(
                        dataset_code=rec.code,
                        state=State.ZIP_INVALID.value,
                        archive_path=str(archive),
                        url=rec.file_location,
                        error=result.reason,
                    ),
                    now=_now(),
                )
                _record_dataset(con, rec, snapshot, archive, None, "zip_invalid")
                progress.log(f"✗ {rec.code}: invalid archive: {result.reason}")
                if strict:
                    return 1
                continue

            manifest.update(
                ManifestEntry(
                    dataset_code=rec.code,
                    state=State.IMPORTING.value,
                    archive_path=str(archive),
                    archive_sha256=result.sha256,
                    url=rec.file_location,
                ),
                now=_now(),
            )
            try:
                imp = importer_mod.import_archive(con, archive, rec.code, build_dir)
            except Exception as exc:  # noqa: BLE001 — recorded per-dataset
                failed.append(rec.code)
                manifest.update(
                    ManifestEntry(
                        dataset_code=rec.code,
                        state=State.FAILED.value,
                        archive_path=str(archive),
                        archive_sha256=result.sha256,
                        url=rec.file_location,
                        error=f"import: {exc}",
                    ),
                    now=_now(),
                )
                _record_dataset(con, rec, snapshot, archive, result.sha256, "failed")
                progress.log(f"✗ {rec.code}: import failed: {exc}")
                if strict:
                    return 1
                continue

            imported.append(rec.code)
            manifest.update(
                ManifestEntry(
                    dataset_code=rec.code,
                    state=State.IMPORTED.value,
                    archive_path=str(archive),
                    archive_sha256=result.sha256,
                    url=rec.file_location,
                ),
                now=_now(),
            )
            _record_dataset(con, rec, snapshot, archive, result.sha256, "imported")
            progress.log(f"✓ {rec.code}: imported {imp.row_count:,} rows into {imp.table_name}")

            # Archive is now fully imported into the database; drop it unless the
            # user asked to keep archives. Done per-dataset so a later failure in
            # the build doesn't strand already-imported archives on disk.
            if not cfg.build.keep_archives:
                archive.unlink(missing_ok=True)

        _record_build(con, build_id, started_at, snapshot, cfg)
    finally:
        con.close()

    # --- Phase 3: report -----------------------------------------------------
    # (Successfully-imported archives were already deleted in Phase 2 unless
    # keep_archives is set; failed datasets keep their archives for hot restart.)
    progress.log(
        f"done: {len(imported)} imported, {len(failed)} failed -> {db_path}"
    )
    if failed:
        print(f"{len(failed)} dataset(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1 if strict else 0
    return 0


def _now() -> str:
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _build_id() -> str:
    import uuid

    return uuid.uuid4().hex


def _parse_size_bytes(value: str | None) -> int | None:
    """Best-effort parse of FAOSTAT's human file-size string to bytes."""
    if not value:
        return None
    text = value.strip().upper().replace(" ", "")
    units = {"KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4, "B": 1}
    for suffix, factor in units.items():
        if text.endswith(suffix):
            num = text[: -len(suffix)]
            try:
                return int(float(num) * factor)
            except ValueError:
                return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _record_dataset(con, rec, snapshot, archive, archive_sha256, status: str) -> None:
    size = archive.stat().st_size if archive.exists() else _parse_size_bytes(rec.file_size)
    con.execute(
        "INSERT OR REPLACE INTO faostat_dataset VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            rec.code,
            rec.dataset_name,
            rec.date_update,
            rec.file_location,
            size,
            rec.file_rows,
            None,
            snapshot.url,
            snapshot.sha256,
            archive_sha256,
            status,
        ],
    )


def _record_build(con, build_id: str, started_at: str, snapshot, cfg: Config) -> None:
    import hashlib
    import platform

    import duckdb

    config_sha256 = hashlib.sha256(
        config_mod.config_to_toml(cfg).encode("utf-8")
    ).hexdigest()
    con.execute(
        "INSERT OR REPLACE INTO faostat_build VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            build_id,
            started_at,
            _now(),
            __version__,
            duckdb.__version__,
            platform.python_version(),
            platform.platform(),
            snapshot.sha256,
            " ".join(sys.argv),
            config_sha256,
        ],
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
