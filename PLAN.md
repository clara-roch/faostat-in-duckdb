# FAOSTATdb — Implementation Plan (v0.1 MVP)

Derived from the design discussion in [FAOSTATdb.md](FAOSTATdb.md). This plan covers the
**v0.1 milestone only**; v0.2+ items are listed at the end as out of scope for now.

## Context

`FAOSTATdb` builds a **local, source-preserving DuckDB mirror of FAOSTAT bulk data**. It is
not a harmonization layer: it preserves the statistical content of FAOSTAT exactly (flags
retained, no values altered) while removing storage-level duplication and adding
reproducibility metadata so a database can be audited and cited.


## Settled architecture decisions

These were settled in the design doc and are not re-litigated here:

1. **Language**: Python (≥ 3.11) — DuckDB bindings, stdlib `zipfile`/`tomllib`, easy CLI packaging.
2. **Storage model**: one fact table per FAOSTAT dataset (`data_<code>`). Unified views deferred.
3. **Config**: TOML (read via stdlib `tomllib`).
4. **Dependencies**: required `duckdb` only; optional `rich` (UI) and `platformdirs` (cache dirs).
5. **Archive handling**: validate with Python `zipfile.testzip()`, never external `zip -T`.
6. **Import**: DuckDB `read_csv` directly — **no pandas**.
7. **Reproducibility**: store metadata-JSON hash, archive hashes, download timestamps, tool/duckdb/python versions.
8. **Default full build**: confirm interactively; require `--yes` (or `--all`) when non-interactive.

## Package layout

```
faostatdb/
  __init__.py
  __main__.py        # python -m faostatdb
  cli.py             # argument parsing + command dispatch (build/list/config)
  config.py          # TOML load/merge with defaults; `config init` / `config show`
  metadata.py        # fetch + parse datasets_E.json; dataset selection (all/include/exclude)
  paths.py           # download-dir resolver (--download-dir > project-local > OS cache)
  download.py        # parallel download, .part→.zip atomic rename, manifest, retry/backoff
  validate.py        # zip integrity (testzip) + size check
  importer.py        # extract CSV to temp, DuckDB read_csv, snake_case columns, one table/dataset
  schema.py          # column-name normalization + metadata tables (faostat_dataset, faostat_build)
  progress.py        # human-readable progress; rich if available, plain fallback
pyproject.toml       # project metadata, console_scripts entry point, optional [ui] extra
README.md            # usage + R/Python/Julia/DuckDB query examples
tests/
.github/workflows/ci.yml
```

## v0.1 scope (features)

- Fetch `datasets_E.json` (https://bulks-faostat.fao.org/production/datasets_E.json).
- Dataset selection: `all` / `include` / `exclude` from CLI flags or config.
- Parallel ZIP download; default `jobs = min(8, 2 * cpu_count)`, overridable via `--jobs`.
- Hot restart: a download **manifest** (`.faostatdb-downloads/manifest.jsonl`) tracks a state
  machine `pending → downloading → downloaded → zip_valid|zip_invalid → importing → imported|failed`.
  Valid archives are reused on relaunch; archives are never deleted until the build succeeds.
- Integrity: download to `*.part`, atomic rename, `zipfile.testzip()`, optional size check vs metadata.
- Retry: exponential backoff (`2, 5, 15s`, `max_retries = 3`); non-strict marks dataset failed
  and continues; `--strict` fails the whole build on any failure.
- Import: extract each CSV to a temp build dir, `CREATE TABLE data_<code> AS SELECT … FROM read_csv(...)`,
  delete extracted CSV after import. Normalize column names to stable `snake_case`
  (`"Area Code (M49)" → area_code_m49`, `"Item" → item_label`, `"Value" → value`, `"Flag" → flag_code`, …).
- Preserve flags fully (codes retained in fact tables).
- Store source metadata in `faostat_dataset` and build metadata in `faostat_build`.
- Download-dir resolution: `--download-dir` > project-local `./faostatdb_archives/` (when
  `keep_archives`) > OS cache dir. Delete valid archives on success unless `--keep-archives`.
- README with four user stories: build full DB, build subset, query in DuckDB CLI, query in R/Python/Julia.
- CI matrix on Linux/macOS/Windows.

## Data model (v0.1)

```text
faostat_dataset            faostat_build                 data_<code>  (one per dataset)
- dataset_code (PK)        - build_id                    - <normalized source columns>
- dataset_name             - started_at / completed_at   - flag_code retained
- date_update              - faostatdb_version           - value, year, *_code, *_label
- file_location            - duckdb_version / python     ...
- file_size_raw            - os
- file_rows_declared       - metadata_snapshot_sha256
- downloaded_at            - command_line
- source_metadata_url      - config_sha256
- source_metadata_hash
- archive_sha256
- import_status
```

Dimension tables and label removal are **v0.2** — v0.1 keeps source columns (minus blind
deduplication) so the first usable version is not blocked on normalization edge cases.

## CLI surface (v0.1)

```bash
faostatdb list [--remote]
faostatdb config init        # write a default faostatdb.toml in the cwd
faostatdb config show
faostatdb build [--database faostat.duckdb] [--include QCL,FBS] [--exclude FA,CBH] \
                [--jobs N] [--keep-archives] [--download-dir DIR] [--yes] [--strict]
```

## Config (TOML)

```toml
[build]
database = "faostat.duckdb"
download_dir = ""
keep_archives = false
jobs = 6
overwrite = false

[datasets]
mode = "all"            # all | include | exclude
include = []
exclude = ["FA", "CBH"]
```

## Suggested implementation order

1. `pyproject.toml` + package skeleton + `cli.py` dispatch + `config.py` (with `config init/show`).
2. `metadata.py`: fetch/parse `datasets_E.json`, selection logic, `faostatdb list`.
3. `paths.py` + `download.py` + `validate.py`: manifest, parallel download, integrity, retry, hot restart.
4. `importer.py` + `schema.py`: extract + `read_csv` import, snake_case columns, metadata tables.
5. `build` command wiring (confirmation prompt, `--yes`/`--strict`, archive cleanup).
6. `progress.py` (plain first; `rich` optional).
7. README examples + `tests/` + CI matrix.

## Testing & CI

- **Unit**: column-name normalization, config merge, selection logic, manifest state transitions,
  download-dir resolution — all offline/deterministic.
- **Integration**: download + import of one small dataset against a fixture/recorded archive.
- **CI**: GitHub Actions matrix on `ubuntu-latest`, `macos-latest`, `windows-latest`; small/deterministic tests only (no full FAOSTAT download in CI).

## Status beyond v0.1 (now implemented)

The following v0.2 / v0.3 items from `FAOSTATdb.md` have since been implemented and are
covered by tests + a real end-to-end smoke build:

- Dimension tables + label removal (`dim_<stem>`), keyed by `(dataset_code, <stem>_code)`.
- Flag legend → `dim_flag`; `faostat_column_mapping`; constant-column removal.
- Labelled convenience views (`view_<code>_labelled`).
- `faostatdb info` / `validate` / `clean-cache` / `sql` / `config init` / `self-contained`.
- Row-count validation vs declared `FileRows` (advisory), declared-size check helper.
- Adaptive default concurrency (`jobs = min(8, 2*cpu)`), `[performance]` PRAGMAs.
- JSON / ASCII / no-progress reporting modes; rich download bars.
- Final DB compaction (`COPY FROM DATABASE`) to reclaim space.
- Optional `raw_<code>` tables (`--keep-raw-tables`) and area enrichment (`--enrich-areas`).
- **Year filtering and accumulation** (`--years`): imports selected years from the
  already-downloaded bulk archive and, when the dataset already exists, refreshes the
  incoming years while preserving other years. Accumulation assumes those year slices
  come from the same upstream FAOSTAT release/vintage; it is meant to add or refresh
  slices, not to merge independently updated FAOSTAT vintages. If FAOSTAT changes
  dimension labels, alternate codes, or other dimension attributes between accumulated
  runs, the local database may retain attributes from the earlier import. This is an
  edge case of mixing source vintages, not a normal update workflow.
- Hot-restart archive reuse; `keep_archives` (default false) deletes archives only on a
  *successful* build, and can be set true to persist the cache for fast re-runs.
- **Historical country-validity metadata** (`--enrich-history` / `[enrichment]
  historical_validity`): the committed `faostatdb/area_classification.csv` fills
  `valid_from`/`valid_to` on `area_classification` for the well-known dissolved/renamed/
  newly-formed FAOSTAT areas (USSR, Czechoslovakia, Sudan (former) → South Sudan, …) —
  documented transition years only; areas without a curated date keep NULL validity (we
  never guess). See `faostatdb/enrich.py`.
- **Concurrency benchmarking** (`faostatdb bench --include CODE,… [--jobs-list 1,2,4,8]`):
  re-downloads a small explicit dataset set at each `--jobs` level and reports wall-clock
  time and MB/s so the best concurrency can be chosen empirically. The scheduling/timing
  core (`faostatdb/bench.py`) is network-free and unit-tested; the command refuses to run
  against the whole inventory. This completes the v0.3 list from `FAOSTATdb.md`.

### Still out of scope

Founding-year `valid_from` for long-lived dissolved entities (we assert only the
well-documented transition year) and China-family area splits — both deliberately left to
avoid guessing; a unified `faostat_data_long` view across all datasets.
