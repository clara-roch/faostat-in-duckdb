# FAOSTATdb

A local, **source-preserving DuckDB mirror of FAOSTAT bulk data**.

FAOSTATdb downloads FAOSTAT bulk ZIP archives, validates them, and imports each dataset into a single DuckDB file — one fact table per dataset (`data_<code>`), with repeated labels lifted into shared dimension tables. It is **not** a harmonization layer: flags are retained and values are never altered. It removes storage-level duplication and records reproducibility metadata (source hashes, timestamps, tool/duckdb/python versions) so a built database can be audited and cited.

**Design principle (the tie-breaker for every borderline decision):**

> FAOSTATdb preserves the statistical content of FAOSTAT exactly, while removing storage-level duplication and adding reproducibility metadata.

New to command-line tools? Jump to [New to CLI tools?](#new-to-cli-tools-a-2-minute-primer) for a gentle primer, then come back here.

------------------------------------------------------------------------

## Install

``` bash
pip install -e ".[ui]"      # ui extra adds rich progress + platformdirs cache dirs
# or run without installing:
python -m faostatdb --help
```

Required dependency: `duckdb`. Optional: `rich` (nicer progress), `platformdirs` (OS-appropriate cache dirs). Everything else is Python standard library (`zipfile`, `tomllib`, `urllib`, `zipapp`).

## Quick start

``` bash
faostatdb list                              # what would a build download?
faostatdb build --include AE --yes          # build one tiny dataset (~77 KB)
faostatdb build --yes                        # build everything (asks first, unless --yes)
faostatdb info                               # summarize the built database
faostatdb sql "SELECT * FROM faostat_dataset LIMIT 5"
```

By default, downloaded archives are **deleted after a successful build** (`keep_archives = false`). Hot restart still saves you: archives are never deleted until the build *succeeds*, so an interrupted or partially-failed run reuses what it already fetched. If you're iterating (rebuilding repeatedly), set `keep_archives = true` to keep the `.zip` cache across successful builds too. See [Caching & re-runs](#caching--re-runs).

------------------------------------------------------------------------

## Commands

`faostatdb` exposes the commands below. Run `faostatdb --help` (or `--version`) for the top-level summary, and `faostatdb <command> --help` for any one of them.

| Command | What it does |
|------------------------------------|------------------------------------|
| `faostatdb list` | Fetches the FAOSTAT bulk inventory (`datasets_E.json`), applies your selection (`all` / `include` / `exclude`), and prints the selected dataset codes + names and a count of selected-of-available. Preview exactly what a build would download. |
| `faostatdb build` | The main command: selects datasets, downloads and validates archives, imports each into the DuckDB file, extracts dimensions + flags, builds labelled views, records provenance, and compacts the result. See the flags below. |
| `faostatdb tables` | Opens a built database read-only and lists every table with an estimated row count. |
| `faostatdb info` | Prints a **reproducibility summary** of a built database: size, dataset count, build timestamp, metadata SHA256, and tool/DuckDB/Python versions. |
| `faostatdb validate` | Opens a built database read-only and checks that every `data_<code>` fact table exists and is queryable (non-empty). Exits non-zero on problems. |
| `faostatdb config show` | Prints the **effective** configuration as TOML (committed defaults after `secrets.env` env vars are merged). |
| `faostatdb config init` | Writes a default `faostatdb.toml` into the current directory (`--force` to overwrite). |
| `faostatdb clean-cache` | Deletes cached archives (`*.zip`/`*.part`) and the download manifest from the download directory, and reports how much was freed. |
| `faostatdb sql "<query>"` | Runs one SQL query against a built database (read-only) and prints an aligned text table — a convenience wrapper, no pandas required. |
| `faostatdb self-contained -o faostatdb.pyz` | Bundles the package into a single executable `.pyz` (stdlib `zipapp`) you can drop in `~/.local/bin`. Run it with `python faostatdb.pyz build …`. |
| `faostatdb bench --include QCL,FBS` | Measures **download throughput** at several `--jobs` levels (re-downloading the given datasets each time) so you can pick the best concurrency for your connection. Requires an explicit `--include`; never benchmarks the whole inventory. |

### `faostatdb build`

``` bash
faostatdb build [--database PATH] [--include QCL,FBS] [--exclude FA,CBH] \
                [--jobs N] [--keep-archives | --no-keep-archives] \
                [--download-dir DIR] [--yes] [--strict] \
                [--no-compact] [--keep-raw-tables] \
                [--no-enrich-areas] [--no-enrich-history] \
                [--json] [--ascii] [--no-progress]
```

| Flag | Effect |
|------------------------------------|------------------------------------|
| `--database PATH` | Output DuckDB path/filename (overrides `build.database`). A bare filename lands under `$FABIO_DUCKDB_DIR`; see [Where files are stored](#where-files-are-stored). |
| `--include QCL,FBS` | Build **only** these codes (selection mode → `include`). |
| `--exclude FA,CBH` | Build everything **except** these codes (mode → `exclude`). `--include` wins if both are given. |
| `--jobs N` | Parallel download workers (overrides `build.jobs`; `0`/unset = auto `min(8, 2×cpu)`). |
| `--keep-archives` / `--no-keep-archives` | Force keeping / deleting the cached `*.zip` after a successful build. Default: **delete** on success (`keep_archives = false`); hot restart still reuses them after a failure. |
| `--download-dir DIR` | Where raw archives are cached (overrides `build.download_dir`). |
| `--yes` (alias `--all`) | Skip the confirmation prompt. **Required** for non-interactive runs (CI/scripts). |
| `--strict` | Abort the whole build on the first error. Without it, failed datasets are recorded + skipped and the rest continue. |
| `--no-compact` | Skip the final compaction pass (faster, but a larger file — see [Making the database small](#making-the-database-as-small-as-possible)). |
| `--keep-raw-tables` | Also keep an untouched `raw_<code>` copy of each import (debugging losslessness). |
| `--enrich-areas` / `--no-enrich-areas` | Build (default) / skip the clearly-labelled `area_classification` table (curated `is_country` flag). **On by default**; still not source FAOSTAT content — see [How `area_classification` is computed](#how-area_classification-is-computed). |
| `--enrich-history` / `--no-enrich-history` | Fill (default) / skip `valid_from`/`valid_to` on `area_classification` for well-known former/successor areas (USSR, Sudan (former) → South Sudan, …) from the same curated CSV. Implies `--enrich-areas`. **On by default**. |
| `--json` | Emit machine-readable JSON-lines progress on **stdout** (human logs stay on stderr). Great for CI. |
| `--ascii` | Use ASCII status icons (`[OK]`/`[X]`) instead of Unicode (`✓`/`✗`). |
| `--no-progress` | Suppress animated progress bars (per-dataset event lines still print). |

``` bash
faostatdb build --yes                                      # full database
faostatdb build --include QCL,FBS --database food.duckdb   # a subset
faostatdb build --yes --json > build-events.jsonl          # CI-friendly log
```

#### Re-running only the missing / failed datasets

A build is **incremental and non-destructive** as long as `overwrite` stays `false` (the default). The build opens the existing `.duckdb` in place and only touches the tables for datasets it imports — each import does `DROP TABLE IF EXISTS data_<code>` for *that* code alone, then recreates it. Any `data_<code>` not in the current selection is left exactly as it was.

So if some datasets failed (a dropped download, a corrupt archive) while the rest imported fine, re-run with `--include` listing only the codes to redo:

``` bash
faostatdb build --yes --include CBH,SXS,WCAD
```

Failed datasets keep their cached archives, so the re-run reuses them via the hot-restart manifest instead of downloading again.

> ⚠️ Do **not** set `overwrite = true` for this — it wipes the whole database file before building, losing the datasets you already have.

------------------------------------------------------------------------

## Caching & re-runs

The slow, flaky part of a build is downloading \~70 archives from FAO's server. FAOSTATdb is built so you pay that cost **once**:

- Every download is tracked in a **manifest** (`.faostatdb-downloads/manifest.jsonl`) with an explicit state machine: `pending → downloading → downloaded → zip_valid|zip_invalid → importing → imported|failed`.
- Archives download to `*.part` and are **atomically renamed** to `*.zip` only on completion, so a killed process never leaves a half-file masquerading as valid.
- Within a run (and across interrupted/failed runs), any archive present on disk **and** recorded as `downloaded`/`imported` is **reused verbatim** — no re-download. (Phase 2 re-validates every archive with `zipfile.testzip()` anyway, so a corrupt reuse is caught and re-fetched.)
- `keep_archives` defaults to **false**, so archives are deleted once the build *succeeds* — but never before, so a crash/failure still leaves them for the next run. If you're iterating on the import/schema and want fast repeated builds, set `--keep-archives` (or `keep_archives = true`) to persist the cache across successful builds; use `faostatdb clean-cache` to wipe it on demand.

### Tuning download concurrency (`faostatdb bench`)

The one performance knob for downloads is `--jobs` (how many archives fetch in parallel). The best value depends on your connection and the FAO server, so rather than guess, measure it:

``` bash
faostatdb bench --include QCL,FBS,RL --jobs-list 1,2,4,8 --yes
```

This downloads the listed datasets fresh at each concurrency level and prints a small table of wall-clock time and MB/s, flagging the fastest level with `*`:

``` text
 jobs    wall_s      MB/s   files   fail
--------------------------------------------
    1     12.40      6.1        3      0
    2      6.80     11.1        3      0
    4      4.10     18.4        3      0 *
    8      4.30     17.6        3      0

fastest: 4 job(s) at 4.10s
```

Pick the winner as your `--jobs` (or `jobs` in config). `bench` **requires an explicit `--include`** — it re-downloads at every level, so it refuses to point at the whole inventory and hammer the server. It keeps no cache (the scratch archives are deleted afterwards).

------------------------------------------------------------------------

## The build pipeline

Everything starts at the CLI and flows through the modules below. The entry point is [`faostatdb/cli.py`](faostatdb/cli.py) (`main`), reachable as the installed `faostatdb` command or `python -m faostatdb` ([`faostatdb/__main__.py`](faostatdb/__main__.py)).

### What each file does

| File | Role |
|------------------------------------|------------------------------------|
| [`cli.py`](faostatdb/cli.py) | Parses arguments, dispatches every command, and orchestrates the build (select → confirm → download → validate → import → enrich → record → compact). |
| [`__main__.py`](faostatdb/__main__.py) | Lets you run the tool with `python -m faostatdb`. |
| [`config.py`](faostatdb/config.py) | Loads `faostatdb.toml` (stdlib `tomllib`), applies `secrets.env` env-var overrides, powers `config show`/`init`. Precedence: TOML \< `secrets.env` \< CLI flags. |
| [`metadata.py`](faostatdb/metadata.py) | Fetches + parses `datasets_E.json`, hashes it for reproducibility, keeps every field (incl. the raw entry JSON), and applies `all`/`include`/`exclude` selection. |
| [`paths.py`](faostatdb/paths.py) | Resolves where archives are cached and where the output database is written. |
| [`download.py`](faostatdb/download.py) | Parallel download with retry/backoff, the hot-restart **manifest** state machine, and `*.part` → atomic rename. |
| [`validate.py`](faostatdb/validate.py) | ZIP integrity via `zipfile.testzip()`, archive SHA256, optional declared-size check. |
| [`importer.py`](faostatdb/importer.py) | Extracts the main CSV, imports it into `data_<code>` via `read_csv` (**never pandas**), extracts dimensions + the flag legend, records column mappings, and builds the labelled view. |
| [`schema.py`](faostatdb/schema.py) | Column-name normalization, dimension detection, labelled-view generation, and metadata-table DDL. |
| [`compact.py`](faostatdb/compact.py) | Rewrites the finished database into a fresh file (`COPY FROM DATABASE`) to reclaim space from dropped columns. |
| [`enrich.py`](faostatdb/enrich.py) | Optional, clearly-separated enrichment: builds `area_classification` (`is_country` + `valid_from`/`valid_to`) from the committed, hand-curated [`area_classification.csv`](faostatdb/area_classification.csv). |
| [`bench.py`](faostatdb/bench.py) | Download-concurrency benchmarking core (network-free, injectable) behind `faostatdb bench`. |
| [`progress.py`](faostatdb/progress.py) | Human/JSON/ASCII/quiet progress reporting — `rich` bars if installed, plain lines otherwise. |

### Order of calls during `faostatdb build`

1.  **Configure** — `config.load_config()` merges TOML \< `secrets.env` \< CLI flags.
2.  **Select** — `metadata.fetch_and_parse()` + `metadata.select_datasets()`.
3.  **Confirm** — unless `--yes`, print an estimated-size summary and prompt (refuse non-interactively without `--yes`).
4.  **Resolve paths** — `paths.resolve_download_dir()` + `paths.manifest_path()`.
5.  **Download** (parallel) — `manifest.needs_download()` skips cached archives; the rest go through `download_with_retry()` → `*.part` → atomic rename.
6.  **Validate** — `validate.validate_zip()` runs `testzip()` + SHA256.
7.  **Import** (sequential) — `importer.import_archive()`: `read_csv` → `data_<code>` → dimension extraction → constant-column removal → flag legend → column mapping → labelled view.
8.  **Enrich** (on by default) — `enrich.enrich_areas()` unless `--no-enrich-areas`, then `enrich.enrich_history()` unless `--no-enrich-history` (fills `valid_from`/`valid_to`).
9.  **Record** — provenance rows in `faostat_dataset` / `faostat_build`.
10. **Compact** — `compact.compact_database()` rewrites the file to its smallest form.

### Where files are stored {#where-files-are-stored}

| What | Where | Lifetime |
|------------------------|------------------------|------------------------|
| **Output database** | `$FABIO_DUCKDB_DIR/<build.database>` (see below) | permanent — the product, kept outside the repo |
| **Cached archives** (`*.zip`) | the resolved `download_dir` — project-local `./faostat_temp_download/` by default | deleted after a *successful* build (kept after a failure, or always with `--keep-archives`) |
| **In-progress downloads** (`*.part`) | inside `download_dir` | transient — renamed to `*.zip` on completion |
| **Download manifest** (`manifest.jsonl`) | `<download_dir>/.faostatdb-downloads/` | persists between runs (hot restart) |
| **Extracted CSVs** | a temp build dir under `download_dir` | deleted immediately after each import |

The **output database** location is resolved from `build.database`: an absolute path is used as-is; a bare filename (default `faostat.duckdb`) is placed inside `$FABIO_DUCKDB_DIR` (or the OS data dir if unset) — **never** the repository, so a built database is never committed by accident.

#### This repo's setup (`secrets.env`)

Machine-specific paths live in a git-ignored [`secrets.env`](secrets.env). To write the database under `C:\where\it\is\stored`:

``` dotenv
FABIO_DUCKDB_DIR=C:\where\it\is\stored
```

FAOSTATdb loads `secrets.env` automatically at startup, so `faostatdb build --yes` just works. Variables already set in your shell win over `secrets.env`.

------------------------------------------------------------------------

## How the database is constructed

A built `.duckdb` mirrors FAOSTAT's bulk data **without altering it**, following a few principles: one fact table per dataset, labels deduplicated into dimensions, stable column names, conservative per-column types (numeric where the data is numeric, text everywhere else), and embedded provenance.

### One fact table per dataset

For each dataset `<code>`, the importer creates `data_<code>` (lower-cased, e.g. `QCL` → `data_qcl`) directly from the dataset's main CSV (the largest top-level `.csv` in the archive), read with DuckDB's `read_csv` — **never pandas**.

### Dimension tables (deduplicated labels)

FAOSTAT repeats each dimension's attributes on every row — a single area carries `area_code` + `area_code_m49` + `area_label` across millions of rows. After the fact table is built, those redundant attributes move into shared `dim_<stem>` tables and only the `<stem>_code` key stays in the fact table:

- Any `<stem>_code` column with sibling attribute columns (the bare `<stem>` or anything starting with `<stem>_`) becomes a dimension: `area`, `item`, `element`, `year`, …
- Each `dim_<stem>` is keyed by `(dataset_code, <stem>_code)` — codes are **not** globally unique across FAOSTAT domains, so the dataset code namespaces them.
- This is lossless: the dimension table holds the exact source attributes, deduplicated. Re-importing a dataset replaces only its own rows.

### Flag legend (`dim_flag`)

FAOSTAT archives ship a small flag/symbol legend CSV. FAOSTATdb loads it into `dim_flag(dataset_code, flag_code, flag_description)` so flag codes can be labelled. The fact table keeps `flag_code` verbatim; flags are **never** collapsed to a boolean.

### Constant-column removal

Columns that never vary across a whole dataset (e.g. `Domain Code`, or a single-element dataset's `element_code`) carry no per-row information. They are dropped from the fact table and their single value recorded in `faostat_constant_column` — lossless and reconstructable, and it shrinks the file.

### Column-name normalization

Only column **names** are normalized (to stable `snake_case`); values/flags are verbatim. Every rename is recorded in `faostat_column_mapping` (`original_column_name` → `normalized_column_name` + an `inferred_role`). Rules:

- Parenthesised qualifiers inline: `"Area Code (M49)"` → `area_code_m49`.
- Lower-cased, non-alphanumeric runs → `_`: `"Months Code"` → `months_code`.
- A small override map pins common names: `Item` → `item_label`, `Element` → `element_label`, `Area` → `area_label`, `Flag` → `flag_code`; `Value`, `Year`, `Unit` keep their names (FAOSTAT has no unit *code*, so unit stays inline). It also pins the few labels whose header doesn't share their code's stem — `Reporter Countries`/`Partner Countries` → `reporter_country_label`/`partner_country_label` (codes `reporter_country_code`/`partner_country_code`) and `Currency` → `iso_currency_label` (code `iso_currency_code`) — so those trade/price labels are lifted into their dimensions instead of repeating on every fact row.
- Collisions get a numeric suffix (`name`, `name_1`, …).

### Text-marker apostrophes

FAOSTAT prefixes its international-code columns — `Area Code (M49)` and `Item Code (CPC)` — with a single leading apostrophe (`'004`, `'0111`). It is an Excel text marker that keeps the leading zeros from being read as a number, not part of the code, so it is stripped. The value stays `VARCHAR`, so `'004` becomes `004` (zeros intact), never the number `4`. This is the only value-level change; to stay safe it touches a column **only when every non-null value carries the apostrophe** (a column-wide marker), so a label that happens to start with a quote is never altered.

### Types and encoding

- **Column types are inferred per column — text only where the data isn't purely numeric.** Each column's type is detected from a **full-file scan** (DuckDB `sample_size=-1`), so a column that is an integer on every row becomes `BIGINT`, an everywhere-decimal one `DOUBLE`, and anything with mixed or non-numeric content falls back to `VARCHAR`. That is why `value` is a real `DOUBLE` in most datasets but stays text in a few — e.g. Food Security keeps censored thresholds like `<0.1` verbatim — and why alphanumeric code columns (`item_code` values such as `210400TSUB`, or `Area Code (M49)` values whose leading zeros must survive) land in `VARCHAR` on their own. The earlier "conversion" failures (`Could not convert '210400TSUB' to INT64`) came from DuckDB guessing a type off the first few rows and then aborting; scanning the whole file picks a type that fits every value, so those errors can't recur.
- **Inference is restricted to numeric-or-text so a value's meaning is never reinterpreted.** DuckDB is allowed to choose only `BIGINT`, `DOUBLE`, or `VARCHAR` (`auto_type_candidates`). Its default candidates also include `BOOLEAN` and the date/time family, which silently *redefine* values — the FAOSTAT unit `t` (tonnes) would be read as boolean `true`, dotted strings as dates. Constraining the candidates means a column's storage type is only ever *narrowed* to a number when it truly is one, never redefined — keeping the mirror faithful to source.
- **Dimension tables (`dim_<stem>`) store codes as `VARCHAR`.** They are shared across datasets, and FAOSTAT types the same logical code numerically in one dataset and alphanumerically in another; keeping dimension keys as text makes the shared table consistent regardless of how a given dataset's fact column was typed. The fact table keeps its own inferred type; the labelled view casts the key to text when it joins.
- **Encoding is detected per archive.** Most files are UTF-8; some carry a UTF-16 BOM, others are Latin-1 (e.g. an unescaped `ô` in "Côte d'Ivoire"). FAOSTATdb checks for a BOM, validates as UTF-8, and falls back to Latin-1 only when needed.
- **Errors name the dataset, file, encoding, and columns.** If a read still fails, the message carries the dataset code, the CSV file name, the detected encoding, and the full raw→normalized column list, so it's clear which dataset (and column) is at fault rather than a bare DuckDB stack trace.

### Labelled convenience views

For each dataset the build also creates `view_<code>_labelled` — the compact fact table with the dimension labels (and flag descriptions) already joined back on. This is what makes no-SQL, dataframe-style querying painless (see [Querying](#querying-the-database)). Views cost nothing on disk, so they are built by default.

### Making the database as small as possible {#making-the-database-as-small-as-possible}

Three lossless reductions shrink the file — dimension extraction, constant-column removal, and then a **compaction pass**. That last step matters: DuckDB's `DROP COLUMN` is a catalog change that leaves the old column's bytes in place, and a plain `CHECKPOINT` does **not** reclaim them. So at the end of a build FAOSTATdb rewrites the whole database into a fresh file with `COPY FROM DATABASE`, which materializes only the columns that still exist. (On a rebuild into an existing file this routinely halves the size — measured 8.3 MB → 4.3 MB on a re-run of a single small dataset.) Disable with `--no-compact` if you prefer speed.

### Embedded provenance tables

| Table | One row per | Key columns |
|------------------------|------------------------|------------------------|
| `faostat_dataset` | imported dataset | `dataset_code`, `dataset_name`, `topic`, `dataset_description`, `contact`, `email`, `date_update`, `compression_format`, `file_type`, `file_location`, `file_size_raw`, `file_rows_declared`, `rows_imported`, `source_csv_rows`, `downloaded_at`, `source_metadata_url`, `source_metadata_hash`, `source_metadata_json`, `archive_sha256`, `import_status` |
| `faostat_build` | build run | `build_id`, `started_at`, `completed_at`, `faostatdb_version`, `duckdb_version`, `python_version`, `os`, `metadata_snapshot_sha256`, `command_line`, `config_sha256`, `datasets_imported`, `datasets_failed` |
| `faostat_column_mapping` | renamed column | `dataset_code`, `table_name`, `original_column_name`, `normalized_column_name`, `inferred_role` |
| `faostat_constant_column` | dropped constant column | `dataset_code`, `table_name`, `column_name`, `value` |

`import_status` flags any dataset that failed (`failed`, `zip_invalid`) rather than silently omitting it. Failures are non-fatal by default; pass `--strict` to abort on the first error.

------------------------------------------------------------------------

## Database schema at a glance

The diagram below shows how the tables relate. `data_<code>` is one fact table per dataset (`data_qcl`, `data_fbs`, …); each `dim_<stem>` is shared across datasets and keyed by `(dataset_code, <stem>_code)` — except `dim_year`, keyed on the bare `year` that the fact table keeps; `view_<code>_labelled` is the join of a fact table to its dimensions. A rendered/standalone version lives in [`docs/schema.qmd`](docs/schema.qmd) (Quarto).

``` mermaid
erDiagram
    faostat_dataset ||--o{ data_CODE : "one fact table per dataset"
    faostat_dataset ||--o{ faostat_column_mapping : "records renames"
    faostat_dataset ||--o{ faostat_constant_column : "records dropped constants"
    faostat_build ||--o{ faostat_dataset : "a build imports datasets"

    data_CODE }o--|| dim_area : "dataset_code, area_code"
    data_CODE }o--|| dim_item : "dataset_code, item_code"
    data_CODE }o--|| dim_element : "dataset_code, element_code"
    data_CODE }o--|| dim_year : "dataset_code, year"
    data_CODE }o--|| dim_flag : "dataset_code, flag_code"
    data_CODE ||--|| view_CODE_labelled : "labels joined back"
    dim_area ||--o| area_classification : "enrichment, on by default (area_code)"

    faostat_dataset {
        VARCHAR dataset_code PK
        VARCHAR dataset_name
        VARCHAR date_update
        BIGINT  file_rows_declared
        BIGINT  rows_imported
        BIGINT  source_csv_rows
        VARCHAR archive_sha256
        VARCHAR import_status
    }
    faostat_build {
        VARCHAR build_id PK
        TIMESTAMP completed_at
        VARCHAR metadata_snapshot_sha256
        VARCHAR duckdb_version
    }
    data_CODE {
        BIGINT  area_code FK
        VARCHAR item_code FK
        BIGINT  element_code FK
        BIGINT  year FK
        VARCHAR unit
        DOUBLE  value
        VARCHAR flag_code FK
    }
    dim_area {
        VARCHAR dataset_code PK
        VARCHAR area_code PK
        VARCHAR area_code_m49
        VARCHAR area_label
    }
    dim_item {
        VARCHAR dataset_code PK
        VARCHAR item_code PK
        VARCHAR item_label
    }
    dim_element {
        VARCHAR dataset_code PK
        VARCHAR element_code PK
        VARCHAR element_label
    }
    dim_year {
        VARCHAR dataset_code PK
        VARCHAR year PK
        VARCHAR year_code
    }
    dim_flag {
        VARCHAR dataset_code PK
        VARCHAR flag_code PK
        VARCHAR flag_description
    }
    view_CODE_labelled {
        VARCHAR dataset_code
        VARCHAR area_code
        VARCHAR area_label
        VARCHAR item_code
        VARCHAR item_label
        VARCHAR value
        VARCHAR flag_code
        VARCHAR flag_description
    }
    area_classification {
        INTEGER area_code PK
        VARCHAR area_label
        BOOLEAN is_country
        INTEGER valid_from
        INTEGER valid_to
    }
```

**How to read it / how the tables interact:**

- **`faostat_dataset` → `data_<code>`** — one metadata row per dataset, one fact table per dataset. There is no foreign-key constraint (table names are dynamic), but the `dataset_code` links them: `data_qcl` ↔ the `faostat_dataset` row with `dataset_code = 'QCL'`.
- **`data_<code>` → `dim_*`** — the fact table keeps only codes (`area_code`, `item_code`, …). To get labels, join each `dim_<stem>` on **both** `dataset_code` and the `<stem>_code` (codes aren't globally unique). The dataset code for `data_qcl` is `'QCL'`. The fact-table types shown are illustrative: each column is typed per dataset (numeric where every value is numeric, `VARCHAR` otherwise), so `value` or a code column may be `VARCHAR` in some datasets. Dimension keys are always `VARCHAR`, so joins cast the fact key to text.
- **`view_<code>_labelled`** — pre-computes those joins for you. Query this and you never write a join by hand.
- **`dim_flag`** — labels flag codes; joined into the labelled view too.
- **`faostat_column_mapping` / `faostat_constant_column`** — audit trails: what was renamed, and which constant columns were lifted out (with their value).
- **`area_classification`** — built **by default** (disable with `--no-enrich-areas`) and explicitly **not** source FAOSTAT content. It holds a curated `is_country` flag plus `valid_from`/`valid_to` (disable the latter with `--no-enrich-history`), all read from the committed [`area_classification.csv`](faostatdb/area_classification.csv). See [How `area_classification` is computed](#how-area_classification-is-computed) for the exact rules and why it carries no per-row `confidence`/`classification_source` column.

### How `area_classification` is computed {#how-area_classification-is-computed}

`area_classification` is **not** derived from FAOSTAT and downloads nothing. It is built from a single committed, hand-curated file — [`faostatdb/area_classification.csv`](faostatdb/area_classification.csv) — authored from world knowledge. That CSV is the package's editable source of truth; the build reads it (with DuckDB's `read_csv`, no pandas) and matches it to `dim_area` by area name. Its columns are exactly `area_name, is_country, valid_from, valid_to`:

- **`is_country`** — `true` for a single country or territory, `false` for a **group of countries/areas**. Crucially, a *former* single state still counts as a country: the USSR, Czechoslovakia, Yugoslav SFR, Sudan (former), the two Yemens and Netherlands Antilles are all `true`. The `false` bucket is genuine aggregates — continents and sub-regions (`Africa`, `Southern Asia`, …), economic/political unions (`European Union (27)`, `OECD`), income and development groups (`Least Developed Countries`, `Low-income economies`), FAO fishing areas, every `… (excluding intra-trade)` variant, and contemporaneous rollups that were never one country: `Belgium-Luxembourg` (Belgium + Luxembourg), `China` (FAOSTAT code 351 = mainland + Taiwan + Hong Kong + Macao — note `China, mainland` is the country), `Channel Islands`, and `Pacific Islands Trust Territory`.
- **`valid_from` / `valid_to`** — filled only for areas whose existence as a distinct entity genuinely started or ended within FAOSTAT's coverage (USSR → `1991`, Czechoslovakia → `1992`, Sudan (former) → `2011`, South Sudan `2011` →, Eritrea `1993` →, Serbia and Montenegro `1992`–`2006`, …). Only well-documented political transition years are recorded; a continuing country (e.g. present-day Sudan) keeps its bounds NULL, and any area not covered stays NULL — **we never guess a date**.

Because the whole table comes from one reviewable CSV, **changing a classification is just editing that file and rebuilding** — no code change, and the diff shows exactly what moved. An area that appears in `dim_area` but is missing from the CSV is written with `is_country = NULL` (unclassified) rather than guessed, so new FAOSTAT areas surface as gaps to fill instead of silent mistakes. Disable the classification with `--no-enrich-areas` and the validity fill with `--no-enrich-history`.

------------------------------------------------------------------------

## Querying the database {#querying-the-database}

The output is a plain DuckDB file — query it from any language. Most FAOSTAT users don't want to write SQL, so the primary examples below use **dataframe-style APIs** on the `view_<code>_labelled` views, with SQL shown afterwards as the advanced/diagnostic path.

> ⚠️ **Use the full path to the built database.** A bare `"faostat.duckdb"` resolves relative to your current directory and usually opens the *wrong* (or an empty) file. The build prints the exact path on its final `done: … -> <path>` line; `faostatdb info` prints it too. Replace the paths below with yours.

> ⚠️ **`value` is a real number in most datasets, but text in a few.** Columns are typed per column, so `value` is usually `DOUBLE` — but where a dataset mixes in non-numeric entries (e.g. Food Security's `<0.1` thresholds) the whole column stays `VARCHAR`. For queries that must work across datasets, prefer `TRY_CAST(value AS DOUBLE)` (yields `NULL` for non-numeric cells, and is a harmless no-op when the column is already numeric) over `CAST`.

We use the same five-step task in each language: **(1)** find an item code from a label, **(2)** get a time series for one country, **(3)** compare several countries, **(4)** rely on joined labels + flags, **(5)** plot.

### Without SQL

#### R — `duckplyr` / `dplyr`

[`duckplyr`](https://duckplyr.tidyverse.org/) runs ordinary `dplyr` code on DuckDB.

``` r
library(duckplyr)
library(dplyr)

con <- DBI::dbConnect(duckdb::duckdb(),
                      "C:/path/to/faostat.duckdb", read_only = TRUE)

# The labelled view already has area/item/element labels + flag descriptions.
qcl <- tbl(con, "view_qcl_labelled")

# (1) Discover item codes from a label — most users search labels first.
qcl |>
  filter(grepl("Wheat", item_label, ignore.case = TRUE)) |>
  distinct(item_code, item_label) |>
  collect()

# (2)+(4) Wheat production time series for France (labels + flags already joined).
wheat_fr <- qcl |>
  filter(area_label == "France",
         item_label == "Wheat",
         element_label == "Production") |>
  mutate(value = as.numeric(value)) |>
  select(year, value, unit, flag_code, flag_description) |>
  arrange(year) |>
  collect()

# (3) Compare several countries.
wheat_multi <- qcl |>
  filter(area_label %in% c("France", "Germany", "Italy"),
         item_label == "Wheat",
         element_label == "Production") |>
  mutate(value = as.numeric(value)) |>
  select(area_label, year, value) |>
  collect()

# (5) Plot.
library(ggplot2)
ggplot(wheat_fr, aes(as.integer(year), value)) +
  geom_line() +
  labs(title = "Wheat production in France", x = NULL, y = unique(wheat_fr$unit))
```

#### Python — Ibis

[Ibis](https://ibis-project.org/) gives a dataframe expression API over DuckDB.

``` python
import ibis

con = ibis.duckdb.connect("C:/path/to/faostat.duckdb", read_only=True)
qcl = con.table("view_qcl_labelled")

# (1) Discover item codes from a label.
wheat_codes = (
    qcl.filter(qcl.item_label.re_search("(?i)wheat"))
       .select("item_code", "item_label")
       .distinct()
       .execute()
)

# (2)+(4) France wheat production time series, labels + flags already present.
wheat_fr = (
    qcl.filter(
        (qcl.area_label == "France")
        & (qcl.item_label == "Wheat")
        & (qcl.element_label == "Production")
    )
    .mutate(value=qcl.value.cast("float64"))
    .select("year", "value", "unit", "flag_code", "flag_description")
    .order_by("year")
    .execute()
)

# (3) Compare several countries.
wheat_multi = (
    qcl.filter(
        qcl.area_label.isin(["France", "Germany", "Italy"])
        & (qcl.item_label == "Wheat")
        & (qcl.element_label == "Production")
    )
    .mutate(value=qcl.value.cast("float64"))
    .select("area_label", "year", "value")
    .execute()
)

# (5) Plot.
wheat_fr.assign(year=wheat_fr.year.astype(int)).plot(x="year", y="value")
```

#### Julia — DataFrames

Julia has no mature `dplyr` translation layer, so the pragmatic path is to pull a slice into a `DataFrame` and use `DataFramesMeta`. The labelled view keeps the initial SQL minimal.

``` julia
using DuckDB, DBInterface, DataFrames, DataFramesMeta

con = DBInterface.connect(DuckDB.DB, "C:/path/to/faostat.duckdb")

# (2)+(4) France wheat production — labels + flags already in the view.
wheat_fr = DataFrame(DBInterface.execute(con, """
    SELECT year, TRY_CAST(value AS DOUBLE) AS value, unit, flag_code, flag_description
    FROM view_qcl_labelled
    WHERE area_label = 'France' AND item_label = 'Wheat'
      AND element_label = 'Production'
    ORDER BY year
"""))

# (3) Compare several countries, then reshape with DataFrames verbs.
multi = DataFrame(DBInterface.execute(con, """
    SELECT area_label, year, TRY_CAST(value AS DOUBLE) AS value
    FROM view_qcl_labelled
    WHERE item_label = 'Wheat' AND element_label = 'Production'
      AND area_label IN ('France','Germany','Italy')
"""))
wide = @chain multi begin
    unstack(:year, :area_label, :value)
end

# (5) Plot.
using Plots
@df wheat_fr plot(:year, :value, title = "Wheat production in France", legend = false)
```

### With SQL

#### DuckDB CLI

``` sql
.open 'C:/path/to/faostat.duckdb'

-- Using the labelled view: no joins needed.
SELECT area_label, year, TRY_CAST(value AS DOUBLE) AS value, flag_code, flag_description
FROM view_qcl_labelled
WHERE area_label = 'France' AND item_label = 'Wheat' AND element_label = 'Production'
ORDER BY year;
```

#### Advanced SQL — joining dimensions yourself

The labelled view is just this join, spelled out. Note dimensions are keyed by **both** `dataset_code` and the code:

``` sql
SELECT a.area_label, i.item_label, d.year,
       TRY_CAST(d.value AS DOUBLE) AS value, d.flag_code, f.flag_description
FROM data_qcl AS d
JOIN dim_area AS a ON a.dataset_code = 'QCL' AND a.area_code = d.area_code
JOIN dim_item AS i ON i.dataset_code = 'QCL' AND i.item_code = d.item_code
LEFT JOIN dim_flag AS f ON f.dataset_code = 'QCL' AND f.flag_code = d.flag_code
WHERE a.area_label = 'France' AND i.item_label = 'Wheat'
ORDER BY d.year;
```

### Bare connection snippets

``` python
import duckdb
con = duckdb.connect(r"C:\path\to\faostat.duckdb", read_only=True)
con.execute("SELECT * FROM data_qcl LIMIT 10").fetchall()
```

``` r
library(duckdb); con <- dbConnect(duckdb(), r"(C:\path\to\faostat.duckdb)")
dbGetQuery(con, "SELECT * FROM data_qcl LIMIT 10")
```

``` julia
using DuckDB, DataFrames
con = DBInterface.connect(DuckDB.DB, raw"C:\path\to\faostat.duckdb")
DataFrame(DBInterface.execute(con, "SELECT * FROM data_qcl LIMIT 10"))
```

------------------------------------------------------------------------

## Configuration

Configuration comes from two files by design:

- [`faostatdb.toml`](faostatdb.toml) — **committed**. The general default shape everyone gets on clone. **Don't edit it** for personal/machine settings.
- [`secrets.env`](secrets.env) — **git-ignored**, yours. A `KEY=value`-per-line file overriding whatever you need. Loaded automatically at startup; values already set in your shell win over it.

Resolution order, lowest precedence first: `faostatdb.toml` → `secrets.env` env vars → CLI flags. So to change a value, add a line to `secrets.env` (or run `faostatdb config init` to scaffold your own TOML).

### The committed defaults

``` toml
[build]
database = "faostat.duckdb"             # filename; final DB under $FABIO_DUCKDB_DIR
download_dir = "faostat_temp_download"  # where raw ZIPs are cached, project-local
keep_archives = false                   # delete cached .zip after a successful build (hot restart still reuses them after a failure)
jobs = 0                                # parallel downloads; 0 = auto min(8, 2*cpu)
overwrite = false                       # true wipes the DB before building
compact = true                          # rewrite the finished DB to reclaim space
keep_raw_tables = false                 # keep untouched raw_<code> copies (debug)

[datasets]
mode = "all"            # all | include | exclude
include = []
exclude = ["FA", "CBH"]

[performance]
import_threads = 0      # DuckDB import threads; 0 = DuckDB default
memory_limit = ""       # e.g. "8GB"; "" = DuckDB default

[enrichment]
area_classification = true    # non-source curated is_country flag from area_classification.csv (false / --no-enrich-areas to skip)
historical_validity = true    # fill valid_from/valid_to for former areas (implies area_classification; false / --no-enrich-history to skip)
```

### Overriding via `secrets.env`

Each value maps to an environment variable; set only the ones you want to change.

``` dotenv
FABIO_DUCKDB_DIR=C:\where\it\is\stored     # output location (kept out of the repo)

FAOSTATDB_DATABASE=faostat.duckdb
FAOSTATDB_DOWNLOAD_DIR=faostat_temp_download
FAOSTATDB_KEEP_ARCHIVES=false
FAOSTATDB_JOBS=0
FAOSTATDB_OVERWRITE=false
FAOSTATDB_COMPACT=true
FAOSTATDB_KEEP_RAW_TABLES=false
FAOSTATDB_DATASETS_MODE=include            # all | include | exclude
FAOSTATDB_DATASETS_INCLUDE=QCL,FBS         # comma-separated
FAOSTATDB_DATASETS_EXCLUDE=FA,CBH          # comma-separated
FAOSTATDB_IMPORT_THREADS=0
FAOSTATDB_MEMORY_LIMIT=8GB
FAOSTATDB_ENRICH_AREAS=true                # false to skip the area classification
FAOSTATDB_ENRICH_HISTORY=true              # false to skip the historical-validity fill
```

Booleans accept `true`/`false`/`1`/`0`/`yes`/`no`; lists are comma-separated. Run `faostatdb config show` to print the effective configuration after merging.

------------------------------------------------------------------------

## Reproducibility

Each build records — in `faostat_dataset` and `faostat_build` — the metadata-JSON snapshot hash (and the full raw metadata per dataset), per-archive SHA256, download timestamps, row counts, the command line, a hash of the effective config, and the tool / DuckDB / Python versions and OS. `faostatdb info` prints the headline of all this, so you can answer *"exactly which FAOSTAT snapshot is this database based on?"* — valuable for citing or auditing.

Three row counts sit side by side: `file_rows_declared` (FAOSTAT's `FileRows` metadata — *approximate*, and not always equal to the file it ships), `source_csv_rows` (the records actually present in the delivered CSV, counted independently of DuckDB), and `rows_imported` (what landed in the fact table). The import is lossless exactly when `rows_imported = source_csv_rows`; a build verifies this per dataset and warns loudly (and fails under `--strict`) on any mismatch, so a disagreement with the *declared* count is never mistaken for data loss:

``` sql
-- Prove every dataset imported completely, regardless of the metadata's estimate:
SELECT dataset_code, file_rows_declared, source_csv_rows, rows_imported
FROM faostat_dataset
WHERE rows_imported <> source_csv_rows;   -- expect zero rows
```

------------------------------------------------------------------------

## New to CLI tools? (a 2-minute primer) {#new-to-cli-tools-a-2-minute-primer}

If you mostly use point-and-click apps, here's how a **command-line interface (CLI)** program like FAOSTATdb works in general — the ideas transfer to almost any CLI tool.

- **You type commands into a terminal** (PowerShell, Terminal, bash) instead of clicking. A command is the program name, then a *subcommand*, then *options*:

  ``` text
  faostatdb   build   --include QCL   --yes
  ^program    ^what    ^option+value   ^flag
  ```

- **Subcommands** (`build`, `list`, `info`, …) are like menu items — each does one job. `faostatdb --help` lists them; `faostatdb build --help` explains one.

- **Options / flags** tune behavior. `--include QCL` takes a value; `--yes` is a *flag* (on/off, no value). Order among options doesn't matter.

- **Exit codes**: a command returns `0` on success and non-zero on failure — that's how scripts and CI know whether it worked. (You usually don't see this number; tools react to it for you.)

- **stdout vs stderr**: normal output goes to *stdout* (you can redirect it to a file with `> out.txt`); progress/warnings go to *stderr* so they don't pollute piped data. That's why `faostatdb build --json > events.jsonl` gives you clean JSON while progress still shows on screen.

- **Config layering**: many tools read a config file, then let environment variables and command-line flags override it. FAOSTATdb does exactly that (`faostatdb.toml` → `secrets.env` → flags), so you can set a default once and override it for a single run.

- **Running it three ways**: installed (`faostatdb …`), as a module (`python -m faostatdb …`), or as a single bundled file (`python faostatdb.pyz …`, produced by `faostatdb self-contained`). All run the same code.

A good rule: when unsure, append `--help`. It never changes anything and always tells you the available subcommands and options.

------------------------------------------------------------------------

## Development

``` bash
pip install -e ".[dev]"      # or ".[dev,ui]" for the rich progress UI
pytest
```

CI (`.github/workflows/ci.yml`) runs the deterministic unit tests on a Linux/macOS/Windows × Python 3.11/3.12/3.13 matrix and **never** triggers a full FAOSTAT download. A separate, opt-in integration job (weekly / manual) builds the single smallest real dataset end to end.

See [PLAN.md](PLAN.md) for the v0.1 build plan and [FAOSTATdb.md](FAOSTATdb.md) for the design rationale.