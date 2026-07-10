"""
Command-line interface: argument parsing and command dispatch.

Commands: ``list``, ``tables``, ``config show|init``, ``build``, ``info``,
``validate``, ``clean-cache``, ``sql``, ``self-contained`` and ``bench``. The CLI
resolves configuration (built-in defaults < ``./faostatdb.toml`` < CLI flags),
then delegates to the relevant modules.

Read the module top-to-bottom as the pipeline: :func:`main` parses args and
dispatches; :func:`run_build` is the download → validate → import → enrich →
record → compact driver.
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from . import config as config_mod
from . import metadata as metadata_mod
from .config import Config, DatasetsConfig

_TOP_LEVEL_DESCRIPTION = "Build a local, source-preserving DuckDB mirror of FAOSTAT bulk data."
_TOP_LEVEL_COMMANDS = [
    ("build", "[options]", "Download and import datasets into DuckDB."),
    ("config", "<command>", "Manage configuration."),
    ("list", "[options]", "List available FAOSTAT datasets."),
    ("tables", "[options]", "List tables in a built database."),
    ("info", "[database]", "Summarize a built database."),
    ("validate", "[database]", "Check a built database's integrity."),
    ("sql", "<query> [options]", "Run a SQL query against a built database."),
    ("clean-cache", "[options]", "Delete cached archives and the download manifest."),
    ("bench", "[options]", "Benchmark download throughput at several --jobs levels."),
    ("self-contained", "[options]", "Build a single-file executable (.pyz) launcher."),
]


class _TopLevelHelpParser(argparse.ArgumentParser):
    """ArgumentParser with a Quarto-like top-level help screen."""

    def format_help(self) -> str:
        if self.prog != "faostatdb":
            return super().format_help()
        return _format_top_level_help()


def _format_top_level_help() -> str:
    """Return the hand-formatted ``faostatdb --help`` text."""
    command_width = max(len(name) for name, _, _ in _TOP_LEVEL_COMMANDS)
    usage_width = max(len(usage) for _, usage, _ in _TOP_LEVEL_COMMANDS)
    rows = [
        "Usage:   faostatdb <command> [options]",
        f"Version: {__version__}",
        "",
        "Description:",
        "",
        f"  {_TOP_LEVEL_DESCRIPTION}",
        "",
        "Options:",
        "",
        "  -h, --help     - Show this help.",
        "  --version      - Show the version number for this program.",
        "",
        "Commands:",
        "",
    ]
    for name, usage, help_text in _TOP_LEVEL_COMMANDS:
        rows.append(f"  {name:<{command_width}}  {usage:<{usage_width}}  - {help_text}")
    rows.extend(
        [
            "",
            "Run 'faostatdb <command> --help' for command-specific options.",
        ]
    )
    return "\n".join(rows) + "\n"


def build_parser() -> argparse.ArgumentParser:
    """Construct the full argument parser (all commands and their flags)."""
    parser = _TopLevelHelpParser(
        prog="faostatdb",
        description=_TOP_LEVEL_DESCRIPTION,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    # list -----------------------------------------------------------------
    p_list = sub.add_parser("list", help="list available datasets")
    p_list.add_argument(
        "--remote", action="store_true", help="fetch the live remote inventory"
    )

    # tables ---------------------------------------------------------------
    p_tables = sub.add_parser("tables", help="list tables in a built database")
    p_tables.add_argument("--database", default=None, help="DuckDB path to inspect")

    # config ---------------------------------------------------------------
    p_config = sub.add_parser("config", help="manage configuration")
    config_sub = p_config.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("show", help="print the effective configuration")
    p_config_init = config_sub.add_parser(
        "init", help="write a default faostatdb.toml in the current directory"
    )
    p_config_init.add_argument(
        "--force", action="store_true", help="overwrite an existing faostatdb.toml"
    )

    # build ----------------------------------------------------------------
    p_build = sub.add_parser("build", help="download and import datasets")
    p_build.add_argument("--database", default=None, help="output DuckDB path")
    p_build.add_argument("--include", default=None, help="comma-separated codes to include")
    p_build.add_argument("--exclude", default=None, help="comma-separated codes to exclude")
    p_build.add_argument("--jobs", type=int, default=None, help="parallel download jobs")
    p_build.add_argument(
        "--years",
        default=None,
        help="only import rows for these year(s), e.g. '2010', '2000,2005,2010', "
        "'1990-1995,2020' or '2000-' (the whole archive is still downloaded)",
    )
    p_build.add_argument("--keep-archives", action="store_true", help="keep ZIPs after build")
    p_build.add_argument(
        "--no-keep-archives",
        action="store_true",
        help="delete ZIPs after a successful build",
    )
    p_build.add_argument("--download-dir", default=None, help="archive download directory")
    p_build.add_argument(
        "--overwrite",
        action="store_true",
        help="delete an existing output database before building",
    )
    p_build.add_argument("--yes", "--all", action="store_true", help="assume yes for prompts")
    p_build.add_argument("--strict", action="store_true", help="fail build on any error")
    p_build.add_argument(
        "--no-compact", action="store_true", help="skip the final compaction pass"
    )
    p_build.add_argument(
        "--keep-raw-tables", action="store_true", help="keep untouched raw_<code> tables"
    )
    p_build.add_argument(
        "--enrich-areas",
        action="store_true",
        help="build the (non-source) area classification table (on by default)",
    )
    p_build.add_argument(
        "--no-enrich-areas",
        action="store_true",
        help="skip the (non-source) area classification table",
    )
    p_build.add_argument(
        "--enrich-history",
        action="store_true",
        help="fill valid_from/valid_to for former areas from the curated "
        "area_classification.csv (implies --enrich-areas; on by default)",
    )
    p_build.add_argument(
        "--no-enrich-history",
        action="store_true",
        help="skip filling valid_from/valid_to from the curated CSV",
    )
    p_build.add_argument("--json", action="store_true", help="emit JSON-lines progress")
    p_build.add_argument("--ascii", action="store_true", help="use ASCII status icons")
    p_build.add_argument(
        "--no-progress", action="store_true", help="suppress animated progress bars"
    )

    # info -----------------------------------------------------------------
    p_info = sub.add_parser("info", help="summarize a built database")
    p_info.add_argument("database", nargs="?", default=None, help="DuckDB path")

    # validate -------------------------------------------------------------
    p_validate = sub.add_parser("validate", help="check a built database's integrity")
    p_validate.add_argument("database", nargs="?", default=None, help="DuckDB path")

    # clean-cache ----------------------------------------------------------
    p_clean = sub.add_parser("clean-cache", help="delete cached archives + manifest")
    p_clean.add_argument("--download-dir", default=None, help="archive download directory")

    # sql ------------------------------------------------------------------
    p_sql = sub.add_parser("sql", help="run a SQL query against a built database")
    p_sql.add_argument("query", help="SQL to execute (e.g. 'SELECT * FROM faostat_dataset')")
    p_sql.add_argument("--database", default=None, help="DuckDB path")

    # self-contained -------------------------------------------------------
    p_self = sub.add_parser(
        "self-contained", help="build a single-file executable (.pyz) launcher"
    )
    p_self.add_argument(
        "--output", "-o", default="faostatdb.pyz", help="output .pyz path"
    )

    # bench ----------------------------------------------------------------
    p_bench = sub.add_parser(
        "bench",
        help="benchmark download throughput at several --jobs levels",
    )
    p_bench.add_argument(
        "--include",
        default=None,
        help="comma-separated dataset codes to benchmark (required: benchmarking "
        "the whole inventory would hammer the FAO server)",
    )
    p_bench.add_argument(
        "--jobs-list",
        default="1,2,4,8",
        help="comma-separated concurrency levels to test (default: 1,2,4,8)",
    )
    p_bench.add_argument("--download-dir", default=None, help="scratch download dir")
    p_bench.add_argument(
        "--yes", "--all", action="store_true", help="assume yes for prompts"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, load config, and dispatch to the chosen command."""
    parser = build_parser()
    args = parser.parse_args(argv)
    cfg = config_mod.load_config()

    dispatch = {
        "list": _cmd_list,
        "tables": _cmd_tables,
        "config": _cmd_config,
        "build": _cmd_build,
        "info": _cmd_info,
        "validate": _cmd_validate,
        "clean-cache": _cmd_clean_cache,
        "sql": _cmd_sql,
        "self-contained": _cmd_self_contained,
        "bench": _cmd_bench,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        parser.error(f"unknown command: {args.command}")
        return 2
    return handler(args, cfg)


# --- list / tables ----------------------------------------------------------


def _cmd_list(args: argparse.Namespace, cfg: Config) -> int:
    """Print the datasets the current selection would build."""
    snapshot = metadata_mod.fetch_and_parse()
    selected = metadata_mod.select_datasets(snapshot.datasets, cfg.datasets)
    for d in selected:
        print(f"{d.code:<8} {d.dataset_name}")
    print(f"\n{len(selected)} dataset(s) selected of {len(snapshot.datasets)} available.")
    return 0


def _cmd_tables(args: argparse.Namespace, cfg: Config) -> int:
    """List every table/view in a built database with an estimated row count."""
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


# --- config -----------------------------------------------------------------


def _cmd_config(args: argparse.Namespace, cfg: Config) -> int:
    """``config show`` prints the effective config; ``config init`` writes a TOML."""
    if args.config_command == "show":
        print(config_mod.config_to_toml(cfg))
        return 0
    if args.config_command == "init":
        return _config_init(args)
    return 2


def _config_init(args: argparse.Namespace) -> int:
    """Write a default ``faostatdb.toml`` into the current directory."""
    from pathlib import Path

    target = Path.cwd() / config_mod.CONFIG_FILENAME
    if target.exists() and not args.force:
        print(f"{target} already exists (use --force to overwrite)", file=sys.stderr)
        return 1
    # Render the built-in defaults so a fresh file matches the shipped shape.
    target.write_text(config_mod.config_to_toml(config_mod.default_config()), encoding="utf-8")
    print(f"wrote {target}")
    return 0


# --- build ------------------------------------------------------------------


def _cmd_build(args: argparse.Namespace, cfg: Config) -> int:
    """Apply CLI overrides, then run the build driver."""
    from . import progress

    cfg = _apply_build_overrides(args, cfg)
    reporter = progress.Reporter(
        json_mode=args.json, ascii_mode=args.ascii, no_progress=args.no_progress
    )
    return run_build(cfg, assume_yes=args.yes, strict=args.strict, reporter=reporter)


def run_build(cfg: Config, *, assume_yes: bool, strict: bool, reporter=None) -> int:
    """Wire download -> validate -> import -> enrich -> record -> compact.

    Drives the per-dataset state machine recorded in the download manifest:
    archives are downloaded in parallel (with hot restart of already-valid
    archives), validated with ``zipfile.testzip()``, then imported sequentially
    into one ``data_<code>`` fact table each (plus dimension tables, flag legend,
    labelled views). Source metadata and build provenance are persisted to the
    ``faostat_*`` tables. On success the database is compacted to reclaim space,
    and valid archives are deleted unless ``keep_archives`` is set.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from pathlib import Path

    from . import download as download_mod
    from . import importer as importer_mod
    from . import paths as paths_mod
    from . import progress as progress_mod
    from . import schema as schema_mod
    from . import validate as validate_mod
    from .download import State

    reporter = reporter or progress_mod.Reporter()

    snapshot = metadata_mod.fetch_and_parse()
    selected = metadata_mod.select_datasets(snapshot.datasets, cfg.datasets)

    if not selected:
        print("no datasets selected", file=sys.stderr)
        return 1

    try:
        years = config_mod.parse_years(cfg.build.years)
    except ValueError as exc:
        print(f"invalid years filter: {exc}", file=sys.stderr)
        return 2

    if not assume_yes and not _confirm(selected, cfg):
        print("aborted", file=sys.stderr)
        return 1

    download_dir = paths_mod.resolve_download_dir(
        cfg.build.download_dir or None, keep_archives=cfg.build.keep_archives
    )
    jobs = config_mod.resolve_jobs(cfg.build.jobs)
    reporter.log(
        f"building {cfg.build.database} from {len(selected)} dataset(s) "
        f"(archives cached in {download_dir}, {jobs} download job(s))"
    )
    if years:
        reporter.log(_year_filter_message(cfg, years))

    manifest = download_mod.Manifest(paths_mod.manifest_path(download_dir))

    def archive_path_for(rec) -> Path:
        # Prefer the archive's real filename from its URL; fall back to <CODE>.zip.
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
    reused = sum(
        1
        for rec in selected
        if rec.file_location and not manifest.needs_download(rec.code, archives[rec.code])
    )
    if reused:
        reporter.log(f"reusing {reused} already-cached archive(s)")

    download_failed: set[str] = set()
    for rec in to_download:
        manifest.update_state(
            rec.code,
            state=State.DOWNLOADING.value,
            archive_path=str(archives[rec.code]),
            archive_sha256=None,
            url=rec.file_location,
            expected_size=rec.file_size,
            expected_rows=rec.file_rows,
            attempts=manifest.attempts(rec.code) + 1,
            error=None,
            downloaded_at=None,
            now=_now(),
        )
        # A live progress bar already shows the "downloading" state, so only
        # emit the textual transition when there is no bar to make it redundant.
        if not reporter.shows_live_progress:
            reporter.event(rec.code, "download", "downloading")

    with reporter.download_phase(len(to_download)) as tracker:
        def worker(rec):
            tracker.start(rec.code, metadata_mod.parse_size_bytes(rec.file_size))
            return download_mod.download_with_retry(
                rec.file_location,
                archives[rec.code],
                on_progress=lambda done, total: tracker.advance(rec.code, done, total),
            )

        with ThreadPoolExecutor(max_workers=jobs) as pool:
            futures = {pool.submit(worker, rec): rec for rec in to_download}
            for future in as_completed(futures):
                rec = futures[future]
                tracker.finish(rec.code)
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001 — recorded per-dataset
                    download_failed.add(rec.code)
                    manifest.update_state(
                        rec.code,
                        state=State.FAILED.value,
                        archive_path=str(archives[rec.code]),
                        url=rec.file_location,
                        error=f"download: {exc}",
                        now=_now(),
                    )
                    reporter.event(rec.code, "download", "failed", message=f"download failed: {exc}")
                    if strict:
                        print(f"strict: download failed for {rec.code}", file=sys.stderr)
                        return 1
                else:
                    manifest.update_state(
                        rec.code,
                        state=State.DOWNLOADED.value,
                        archive_path=str(archives[rec.code]),
                        archive_sha256=None,
                        url=rec.file_location,
                        error=None,
                        downloaded_at=_now(),
                        now=_now(),
                    )
                    size = _human_bytes(archives[rec.code].stat().st_size)
                    reporter.event(
                        rec.code, "download", "downloaded", message=f"{size} downloaded"
                    )

    # --- Phase 2: validate + import sequentially into DuckDB -----------------
    import duckdb

    db_path = paths_mod.resolve_database_path(cfg.build.database)
    if cfg.build.overwrite and db_path.exists():
        db_path.unlink()
        db_path.with_name(db_path.name + ".wal").unlink(missing_ok=True)

    build_dir = download_dir / paths_mod.MANIFEST_DIRNAME / "build"
    build_dir.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect(str(db_path))
    _configure_duckdb(con, cfg)
    build_id = _build_id()
    started_at = _now()
    imported: list[str] = []
    failed: list[str] = []
    downloaded_at = {e.dataset_code: e.downloaded_at for e in manifest.all()}
    try:
        schema_mod.create_metadata_tables(con)
        for rec in selected:
            archive = archives[rec.code]
            dl_at = downloaded_at.get(rec.code)
            if rec.code in download_failed or not rec.file_location:
                failed.append(rec.code)
                _record_dataset(con, rec, snapshot, archive, None, "failed", dl_at, None)
                continue
            if not archive.exists():
                failed.append(rec.code)
                manifest.update_state(
                    rec.code,
                    state=State.FAILED.value,
                    archive_path=str(archive),
                    url=rec.file_location,
                    error="archive missing",
                    now=_now(),
                )
                _record_dataset(con, rec, snapshot, archive, None, "failed", dl_at, None)
                reporter.event(rec.code, "validate", "failed", message="archive missing")
                if strict:
                    return 1
                continue

            result = validate_mod.validate_zip(archive)
            if not result.ok:
                failed.append(rec.code)
                manifest.update_state(
                    rec.code,
                    state=State.ZIP_INVALID.value,
                    archive_path=str(archive),
                    url=rec.file_location,
                    error=result.reason,
                    now=_now(),
                )
                _record_dataset(con, rec, snapshot, archive, None, "zip_invalid", dl_at, None)
                reporter.event(rec.code, "validate", "invalid", message=f"invalid archive: {result.reason}")
                if strict:
                    return 1
                continue

            manifest.update_state(
                rec.code,
                state=State.IMPORTING.value,
                archive_path=str(archive),
                archive_sha256=result.sha256,
                url=rec.file_location,
                error=None,
                now=_now(),
            )
            try:
                imp = importer_mod.import_archive(
                    con, archive, rec.code, build_dir,
                    keep_raw=cfg.build.keep_raw_tables, years=years,
                )
            except Exception as exc:  # noqa: BLE001 — recorded per-dataset
                failed.append(rec.code)
                manifest.update_state(
                    rec.code,
                    state=State.FAILED.value,
                    archive_path=str(archive),
                    archive_sha256=result.sha256,
                    url=rec.file_location,
                    error=f"import: {exc}",
                    now=_now(),
                )
                _record_dataset(con, rec, snapshot, archive, result.sha256, "failed", dl_at, None)
                reporter.event(rec.code, "import", "failed", message=f"import failed: {exc}")
                if strict:
                    return 1
                continue

            imported.append(rec.code)
            manifest.update_state(
                rec.code,
                state=State.IMPORTED.value,
                archive_path=str(archive),
                archive_sha256=result.sha256,
                url=rec.file_location,
                error=None,
                now=_now(),
            )
            _record_dataset(
                con, rec, snapshot, archive, result.sha256, "imported", dl_at,
                imp.row_count, imp.source_row_count,
            )
            # Verify losslessness against the delivered CSV (hard check), and note
            # any divergence from the approximate FileRows metadata (advisory).
            lossless = _check_row_count(rec, imp, reporter)
            if lossless:
                if imp.appended_rows is not None:
                    message = (
                        f"accumulated {imp.appended_rows:,} row(s) into "
                        f"{imp.table_name} (now {imp.row_count:,} rows)"
                    )
                else:
                    message = f"imported {imp.row_count:,} rows into {imp.table_name}"
                reporter.event(
                    rec.code, "import", "imported", rows=imp.row_count, message=message
                )
            elif strict:
                return 1

            # Archive is now fully imported; drop it unless the user asked to keep
            # archives. Done per-dataset so a later failure doesn't strand
            # already-imported archives on disk.
            if not cfg.build.keep_archives:
                archive.unlink(missing_ok=True)

        # Optional enrichment (clearly non-source; opt-in). Historical validity
        # augments the classification table, so it requires the base table too.
        # It is *best-effort*, like compaction: enrichment is a non-source cosmetic
        # layer and must never abort an otherwise-successful build (every dataset is
        # already imported at this point). Any failure — e.g. a bad curated-CSV edit
        # or an unexpected area code — is warned and swallowed so the build still
        # finalizes (records provenance + checkpoints + compacts). Not gated by
        # --strict, which concerns source-data losslessness, not this layer.
        if cfg.enrichment.area_classification or cfg.enrichment.historical_validity:
            try:
                from . import enrich as enrich_mod

                n = enrich_mod.enrich_areas(con)
                if n:
                    reporter.log(f"enriched {n:,} area(s) into area_classification")
                if cfg.enrichment.historical_validity:
                    h = enrich_mod.enrich_history(con)
                    if h:
                        reporter.log(
                            f"filled historical validity for {h:,} former/successor area(s)"
                        )
            except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
                reporter.log(f"enrichment skipped ({exc})")

        _record_build(con, build_id, started_at, snapshot, cfg, len(imported), len(failed))
        con.execute("CHECKPOINT")
    finally:
        con.close()

    # --- Phase 3: compact + report ------------------------------------------
    if cfg.build.compact and imported:
        from . import compact as compact_mod

        try:
            before, after = compact_mod.compact_database(db_path)
            saved = before - after
            reporter.log(
                f"compacted database: {_human_bytes(before)} -> {_human_bytes(after)} "
                f"(saved {_human_bytes(saved)})"
            )
        except Exception as exc:  # noqa: BLE001 — compaction is best-effort
            reporter.log(f"compaction skipped ({exc})")

    reporter.log(
        f"done: {len(imported)} imported, {len(failed)} failed -> {db_path}"
    )
    if failed:
        print(f"{len(failed)} dataset(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1 if strict else 0
    if not cfg.build.keep_archives:
        paths_mod.clean_cache(download_dir, remove_dir=True)
    return 0


# --- info / validate / clean-cache / sql / self-contained -------------------


def _cmd_info(args: argparse.Namespace, cfg: Config) -> int:
    """Print a reproducibility summary of a built database (FAOSTATdb.md > info)."""
    import duckdb

    from . import paths as paths_mod

    db_path = paths_mod.resolve_database_path(args.database or cfg.build.database)
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 1

    con = duckdb.connect(str(db_path), read_only=True)
    try:
        n_datasets = _scalar(con, "SELECT COUNT(*) FROM faostat_dataset WHERE import_status = 'imported'")
        n_failed = _scalar(con, "SELECT COUNT(*) FROM faostat_dataset WHERE import_status <> 'imported'")
        build = con.execute(
            "SELECT build_id, completed_at, faostatdb_version, duckdb_version, "
            "python_version, os, metadata_snapshot_sha256 "
            "FROM faostat_build ORDER BY completed_at DESC LIMIT 1"
        ).fetchone()
    except duckdb.Error:
        print(f"{db_path} is not a FAOSTATdb database (no metadata tables)", file=sys.stderr)
        con.close()
        return 1
    finally:
        con.close()

    print(f"FAOSTATdb database: {db_path}")
    print(f"Size on disk:       {_human_bytes(db_path.stat().st_size)}")
    print(f"Datasets:           {n_datasets}")
    print(f"Failed datasets:    {n_failed}")
    if build:
        (bid, completed, fver, dver, pyver, os_, meta_sha) = build
        print(f"Built at:           {completed}")
        print(f"Build id:           {bid}")
        print(f"FAOSTATdb version:  {fver}")
        print(f"DuckDB version:     {dver}")
        print(f"Python version:     {pyver}")
        print(f"OS:                 {os_}")
        print(f"Metadata SHA256:    {meta_sha}")
    return 0


def _cmd_validate(args: argparse.Namespace, cfg: Config) -> int:
    """Check a built database opens and that each fact table is queryable."""
    import duckdb

    from . import paths as paths_mod

    db_path = paths_mod.resolve_database_path(args.database or cfg.build.database)
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 1

    problems = 0
    facts: list[str] = []
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        facts = [
            r[0]
            for r in con.execute(
                "SELECT table_name FROM duckdb_tables() "
                "WHERE table_name LIKE 'data\\_%' ESCAPE '\\' ORDER BY table_name"
            ).fetchall()
        ]
        if not facts:
            print("no data_<code> fact tables found", file=sys.stderr)
            problems += 1
        for t in facts:
            try:
                n = _scalar(con, f'SELECT COUNT(*) FROM "{t}"')
                if n == 0:
                    print(f"warning: {t} has 0 rows", file=sys.stderr)
                    problems += 1
            except duckdb.Error as exc:
                print(f"error: {t} not queryable: {exc}", file=sys.stderr)
                problems += 1
    finally:
        con.close()

    if problems:
        print(f"validate: {problems} problem(s) in {db_path}", file=sys.stderr)
        return 1
    print(f"validate: OK ({len(facts)} fact table(s)) -> {db_path}")
    return 0


def _cmd_clean_cache(args: argparse.Namespace, cfg: Config) -> int:
    """Delete cached archives + manifest from the download directory."""
    from . import paths as paths_mod

    download_dir = paths_mod.resolve_download_dir(
        args.download_dir or cfg.build.download_dir or None,
        keep_archives=cfg.build.keep_archives,
    )
    removed, freed = paths_mod.clean_cache(download_dir)
    print(f"removed {removed} archive(s), freed {_human_bytes(freed)} from {download_dir}")
    return 0


def _cmd_sql(args: argparse.Namespace, cfg: Config) -> int:
    """Run a one-off SQL query against a built database (read-only)."""
    import duckdb

    from . import paths as paths_mod

    db_path = paths_mod.resolve_database_path(args.database or cfg.build.database)
    if not db_path.exists():
        print(f"database not found: {db_path}", file=sys.stderr)
        return 1
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rel = con.execute(args.query)
        headers = [d[0] for d in rel.description] if rel.description else []
        rows = rel.fetchall()
    except duckdb.Error as exc:
        print(f"query error: {exc}", file=sys.stderr)
        return 1
    finally:
        con.close()
    _print_table(headers, rows)
    return 0


def _print_table(headers: list[str], rows: list[tuple]) -> None:
    """Print query results as a simple aligned text table (no pandas needed)."""
    if not headers:
        return
    str_rows = [["" if v is None else str(v) for v in row] for row in rows]
    widths = [len(h) for h in headers]
    for row in str_rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    print("  ".join(h.ljust(widths[i]) for i, h in enumerate(headers)))
    print("  ".join("-" * widths[i] for i in range(len(headers))))
    for row in str_rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    print(f"\n({len(rows)} row(s))")


def _cmd_self_contained(args: argparse.Namespace, cfg: Config) -> int:
    """Bundle the package into a single executable ``.pyz`` (stdlib zipapp).

    The result runs with ``python faostatdb.pyz build ...`` and needs only
    ``duckdb`` installed at runtime — a genuinely single-file launcher, per the
    optional single-file workflow in FAOSTATdb.md.
    """
    import shutil
    import tempfile
    import zipapp
    from pathlib import Path

    pkg_dir = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp) / "app"
        shutil.copytree(pkg_dir, staging / "faostatdb")
        # zipapp entry point: run the CLI's main().
        (staging / "__main__.py").write_text(
            "from faostatdb.cli import main\n"
            "import sys\n"
            "if __name__ == '__main__':\n"
            "    sys.exit(main())\n",
            encoding="utf-8",
        )
        out = Path(args.output)
        zipapp.create_archive(staging, out, interpreter="/usr/bin/env python3")
    print(f"wrote {out} (run: python {out} build --help)")
    return 0


def _cmd_bench(args: argparse.Namespace, cfg: Config) -> int:
    """Benchmark download throughput at several ``--jobs`` levels (FAOSTATdb.md v0.3).

    Deliberately requires an explicit ``--include`` list: benchmarking downloads
    the archives fresh at *every* concurrency level, so pointing it at the whole
    inventory would download hundreds of files repeatedly and hammer the FAO
    server. Each level re-downloads into a scratch directory (cleared between
    levels) so timings reflect real cold fetches, not the OS/HTTP cache.
    """
    from . import bench as bench_mod
    from . import download as download_mod
    from . import paths as paths_mod

    if not args.include:
        print(
            "bench: pass --include with a small set of dataset codes "
            "(e.g. --include QCL,FBS,RL) — refusing to benchmark the whole inventory",
            file=sys.stderr,
        )
        return 2

    try:
        jobs_levels = [int(x) for x in args.jobs_list.split(",") if x.strip()]
    except ValueError:
        print(f"bench: bad --jobs-list {args.jobs_list!r}", file=sys.stderr)
        return 2
    if not jobs_levels:
        print("bench: --jobs-list produced no levels", file=sys.stderr)
        return 2

    codes = _split_codes(args.include)
    snapshot = metadata_mod.fetch_and_parse()
    by_code = {d.code: d for d in snapshot.datasets}
    tasks: list = []
    missing: list[str] = []
    for code in codes:
        rec = by_code.get(code)
        if rec is None or not rec.file_location:
            missing.append(code)
            continue
        tasks.append(bench_mod.BenchTask(code=code, url=rec.file_location))
    if missing:
        print(f"bench: unknown/downloadless codes ignored: {', '.join(missing)}",
              file=sys.stderr)
    if not tasks:
        print("bench: no benchmarkable datasets selected", file=sys.stderr)
        return 1

    total_mb = sum(
        (metadata_mod.parse_size_bytes(by_code[t.code].file_size) or 0)
        for t in tasks
    ) / 1_000_000
    print(
        f"benchmarking {len(tasks)} dataset(s) at jobs levels {jobs_levels} "
        f"(~{total_mb:.0f} MB per level, re-downloaded each level)",
        file=sys.stderr,
    )
    if not args.yes and sys.stdin.isatty():
        if input("Continue? [y/N] ").strip().lower() not in {"y", "yes"}:
            print("aborted", file=sys.stderr)
            return 1
    elif not args.yes and not sys.stdin.isatty():
        print("non-interactive: pass --yes to benchmark", file=sys.stderr)
        return 1

    bench_dir = paths_mod.resolve_download_dir(
        args.download_dir or None, keep_archives=False
    ) / "bench"
    bench_dir.mkdir(parents=True, exist_ok=True)

    def clear_dir(_jobs: int) -> None:
        # Force a cold download at each level: remove any archives from the last.
        for f in bench_dir.glob("*"):
            if f.is_file():
                f.unlink(missing_ok=True)

    def download(task) -> int:
        dest = bench_dir / f"{task.code}.zip"
        download_mod.download_with_retry(task.url, dest)
        return dest.stat().st_size if dest.exists() else 0

    results = bench_mod.run_download_benchmark(
        tasks, jobs_levels, downloader=download, before_level=clear_dir
    )
    # Clean up the scratch archives (bench never keeps a cache).
    clear_dir(0)
    for f in bench_dir.glob("*"):
        f.unlink(missing_ok=True)
    try:
        bench_dir.rmdir()
    except OSError:
        pass

    print(bench_mod.format_bench_table(results))
    return 0


# --- helpers ----------------------------------------------------------------


def _configure_duckdb(con, cfg: Config) -> None:
    """Apply the ``[performance]`` PRAGMAs to a build connection.

    ``import_threads`` maps to DuckDB's ``threads`` and ``memory_limit`` to its
    ``memory_limit``; both are left at DuckDB's own default when unset (0 / "").
    """
    if cfg.performance.import_threads and cfg.performance.import_threads > 0:
        con.execute(f"PRAGMA threads={int(cfg.performance.import_threads)}")
    if cfg.performance.memory_limit:
        # Quote defensively; the value is user-supplied config.
        limit = cfg.performance.memory_limit.replace("'", "")
        con.execute(f"PRAGMA memory_limit='{limit}'")


def _year_filter_message(cfg: Config, years) -> str:
    """Human-readable note for a build's year-filter semantics."""
    base = (
        f"year filter active: keeping only rows for {years.describe()} "
        f"({years.count_description()}). "
    )
    if cfg.build.overwrite:
        return (
            base +
            f"{cfg.build.database} will be overwritten before import, so selected "
            "datasets are built from scratch with this year filter; datasets without "
            "a year column import in full"
        )
    return (
        base +
        f"Datasets already present in {cfg.build.database} accumulate "
        "(these years are merged in, other years kept); datasets without a year "
        "column import in full"
    )


def _now() -> str:
    """Current UTC time as an ISO-8601 string (used for manifest / metadata)."""
    import datetime

    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _build_id() -> str:
    """A fresh random build id."""
    import uuid

    return uuid.uuid4().hex


def _scalar(con, sql: str):
    """Fetch a single scalar from a query."""
    row = con.execute(sql).fetchone()
    return row[0] if row else None


def _human_bytes(n: int | None) -> str:
    """Format a byte count as a short human-readable string."""
    if n is None:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _check_row_count(rec, imp, reporter) -> bool:
    """Verify the import is lossless against the *delivered CSV*; returns that verdict.

    The authoritative reference is ``imp.source_row_count`` — the record count of
    the CSV FAOSTAT actually shipped, counted independently of DuckDB. If DuckDB
    loaded exactly that many rows the import is lossless, *even when* it disagrees
    with the declared ``FileRows``: that metadata is approximate and FAOSTAT does
    not keep it byte-accurate against the bulk file (e.g. MK ships ~2k more rows
    than it declares).

    A mismatch against the source CSV is a genuine problem — rows dropped or a
    record split — and is surfaced as an ``invalid`` event so the caller can fail
    the build under ``--strict``. A disagreement with only the metadata is reported
    as a plainly-labelled note so it can't be mistaken for data loss.
    """
    if imp.year_filter is not None:
        # Intentional subset: the imported rows are those matching the year filter,
        # so they are expected to be fewer than the full delivered CSV. Report the
        # kept/total split plainly so it can't be mistaken for data loss.
        years_str = ",".join(str(y) for y in imp.year_filter)
        if imp.appended_rows is not None:
            reporter.log(
                f"note: {rec.code} accumulated year(s) {years_str} into the existing "
                f"database: added {imp.appended_rows:,} row(s); the dataset now holds "
                f"{imp.row_count:,} row(s) across all imported years"
            )
        else:
            reporter.log(
                f"note: {rec.code} filtered to year(s) {years_str}: kept "
                f"{imp.row_count:,} of {imp.source_row_count:,} source record(s)"
            )
        return True

    if not imp.lossless:
        reporter.event(
            rec.code, "import", "invalid",
            message=(
                f"row-count mismatch vs source CSV: imported {imp.row_count:,} "
                f"but the delivered file holds {imp.source_row_count:,} records "
                f"(counted by {imp.count_method})"
            ),
        )
        return False

    declared = rec.file_rows
    if declared is not None and declared != imp.row_count:
        diff = imp.row_count - declared
        reporter.log(
            f"note: {rec.code} imported all {imp.row_count:,} records present in the "
            f"source CSV; FAOSTAT's declared FileRows is {declared:,} "
            f"({diff:+,} — approximate metadata, not data loss)"
        )
    return True


def _record_dataset(
    con, rec, snapshot, archive, archive_sha256, status, downloaded_at,
    rows_imported, source_csv_rows=None,
) -> None:
    """Insert/replace one dataset's provenance row in ``faostat_dataset``.

    ``source_csv_rows`` is the record count of the delivered CSV (independent of
    DuckDB); persisting it alongside ``rows_imported`` and the approximate
    ``file_rows_declared`` makes losslessness auditable straight from the DB.
    """
    size = (
        archive.stat().st_size
        if archive.exists()
        else metadata_mod.parse_size_bytes(rec.file_size)
    )
    con.execute(
        "INSERT OR REPLACE INTO faostat_dataset VALUES "
        "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            rec.code,
            rec.dataset_name,
            rec.topic,
            rec.dataset_description,
            rec.contact,
            rec.email,
            rec.date_update,
            rec.compression_format,
            rec.file_type,
            rec.file_location,
            size,
            rec.file_rows,
            rows_imported,
            source_csv_rows,
            downloaded_at,
            snapshot.url,
            snapshot.sha256,
            rec.raw_json,
            archive_sha256,
            status,
        ],
    )


def _record_build(
    con, build_id, started_at, snapshot, cfg, n_imported, n_failed
) -> None:
    """Insert the build-provenance row into ``faostat_build``."""
    import hashlib
    import platform

    import duckdb

    config_sha256 = hashlib.sha256(
        config_mod.config_to_toml(cfg).encode("utf-8")
    ).hexdigest()
    con.execute(
        "INSERT OR REPLACE INTO faostat_build VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            n_imported,
            n_failed,
        ],
    )


def _confirm(selected: list, cfg: Config) -> bool:
    """Interactive pre-build confirmation, with an estimated-size summary.

    Refuses to proceed without ``--yes`` when there is no TTY (so CI / scripts
    must be explicit), per FAOSTATdb.md's "all by default may be too aggressive".
    """
    total_bytes = sum(
        metadata_mod.parse_size_bytes(r.file_size) or 0 for r in selected
    )
    total_rows = sum(r.file_rows or 0 for r in selected)
    print(
        f"This will download {len(selected)} dataset(s) "
        f"(~{_human_bytes(total_bytes)} compressed, ~{total_rows:,} rows) "
        f"and build {cfg.build.database}.",
        file=sys.stderr,
    )
    print(
        "The database size depends on the data but is typically of a similar "
        "order after compaction.",
        file=sys.stderr,
    )
    if not sys.stdin.isatty():
        print("non-interactive: pass --yes to build", file=sys.stderr)
        return False
    answer = input("Continue? [y/N] ")
    return answer.strip().lower() in {"y", "yes"}


def _apply_build_overrides(args: argparse.Namespace, cfg: Config) -> Config:
    """Layer CLI build flags over the loaded config (flags win)."""
    from dataclasses import replace

    build = cfg.build
    if args.database is not None:
        build = replace(build, database=args.database)
    if args.download_dir is not None:
        build = replace(build, download_dir=args.download_dir)
    if args.jobs is not None:
        build = replace(build, jobs=args.jobs)
    if args.overwrite:
        build = replace(build, overwrite=True)
    if args.keep_archives:
        build = replace(build, keep_archives=True)
    if args.no_keep_archives:
        build = replace(build, keep_archives=False)
    if args.no_compact:
        build = replace(build, compact=False)
    if args.keep_raw_tables:
        build = replace(build, keep_raw_tables=True)
    if args.years is not None:
        build = replace(build, years=args.years)

    # Enrichment is on by default (see EnrichmentConfig); the --enrich-* flags are
    # kept for explicitness while the --no-enrich-* flags opt back out. When both a
    # flag and its negation are given, the negation wins (applied last).
    enrichment = cfg.enrichment
    if args.enrich_areas:
        enrichment = replace(enrichment, area_classification=True)
    if args.enrich_history:
        # History fills valid_from/valid_to on the classification table, so it
        # implies the base area classification as well.
        enrichment = replace(
            enrichment, area_classification=True, historical_validity=True
        )
    if args.no_enrich_areas:
        # Historical validity is an augmentation of area_classification, so
        # disabling the base area table must disable history as well.
        enrichment = replace(
            enrichment, area_classification=False, historical_validity=False
        )
    if args.no_enrich_history:
        enrichment = replace(enrichment, historical_validity=False)

    datasets = cfg.datasets
    if args.include is not None:
        datasets = DatasetsConfig(
            mode="include", include=_split_codes(args.include), exclude=datasets.exclude
        )
    elif args.exclude is not None:
        datasets = DatasetsConfig(
            mode="exclude", include=datasets.include, exclude=_split_codes(args.exclude)
        )
    return Config(
        build=build,
        datasets=datasets,
        performance=cfg.performance,
        enrichment=enrichment,
    )


def _split_codes(value: str) -> list[str]:
    """Split a comma-separated code list into cleaned upper-ish tokens."""
    return [c.strip() for c in value.split(",") if c.strip()]
