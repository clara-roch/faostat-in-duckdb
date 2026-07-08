# FAOSTATdb — Developer Reference

Internal documentation: module roles, the build pipeline, and the data-model decisions behind the importer. See [README.md](README.md) for installation, usage, and querying.

## Local development setup

From a local checkout, install FAOSTATdb in editable mode:

```bash
python3 -m pip install -e ".[ui,dev]"
```

That gives you the `faostatdb` command, the optional UI dependencies (`rich`, `platformdirs`), and the test dependency (`pytest`). If you do not want the optional UI extras, use `python -m pip install -e ".[dev]"` instead.

## The build pipeline

Everything starts at the CLI and flows through the modules below. The entry point is [`faostatdb/cli.py`](faostatdb/cli.py) (`main`), reachable as the installed `faostatdb` command or `python -m faostatdb` ([`faostatdb/__main__.py`](faostatdb/__main__.py)).

### What each file does

| File                                   | Role                                                                                                                                                                                                           |
| -------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`cli.py`](faostatdb/cli.py)           | Parses arguments, dispatches every command, and orchestrates the build (select → confirm → download → validate → import → enrich → record → compact).                                                          |
| [`__main__.py`](faostatdb/__main__.py) | Lets you run the tool with `python -m faostatdb`.                                                                                                                                                              |
| [`config.py`](faostatdb/config.py)     | Loads a launch-directory `faostatdb.toml` (stdlib `tomllib`) over built-in defaults, powers `config show`/`init`. Precedence: built-in defaults \< `./faostatdb.toml` \< CLI flags.                            |
| [`metadata.py`](faostatdb/metadata.py) | Fetches + parses `datasets_E.json`, hashes it for reproducibility, keeps every field (incl. the raw entry JSON), and applies `all`/`include`/`exclude` selection.                                              |
| [`paths.py`](faostatdb/paths.py)       | Resolves where archives are cached and where the output database is written.                                                                                                                                   |
| [`download.py`](faostatdb/download.py) | Parallel download with retry/backoff, the hot-restart **manifest** state machine, and `*.part` → atomic rename.                                                                                                |
| [`validate.py`](faostatdb/validate.py) | ZIP integrity via `zipfile.testzip()`, archive SHA256, optional declared-size check.                                                                                                                           |
| [`importer.py`](faostatdb/importer.py) | Extracts the main CSV, imports it into `data_<code>` via `read_csv` (**never pandas**), extracts dimensions + the flag legend, records column mappings, and builds the labelled view.                          |
| [`schema.py`](faostatdb/schema.py)     | Column-name normalization, dimension detection, labelled-view generation, and metadata-table DDL.                                                                                                              |
| [`compact.py`](faostatdb/compact.py)   | Rewrites the finished database into a fresh file (`COPY FROM DATABASE`) to reclaim space from dropped columns.                                                                                                 |
| [`enrich.py`](faostatdb/enrich.py)     | Optional, clearly-separated enrichment: builds `area_classification` (`is_country` + `valid_from`/`valid_to`) from the committed, hand-curated [`area_classification.csv`](faostatdb/area_classification.csv). |
| [`bench.py`](faostatdb/bench.py)       | Download-concurrency benchmarking core (network-free, injectable) behind `faostatdb bench`.                                                                                                                    |
| [`progress.py`](faostatdb/progress.py) | Human/JSON/ASCII/quiet progress reporting — `rich` bars if installed, plain lines otherwise.                                                                                                                   |

### Order of calls during `faostatdb build`

1. **Configure** — `config.load_config()` merges built-in defaults \< `./faostatdb.toml` \< CLI flags.
2. **Select** — `metadata.fetch_and_parse()` + `metadata.select_datasets()`.
3. **Confirm** — unless `--yes`, print an estimated-size summary and prompt (refuse non-interactively without `--yes`).
4. **Resolve paths** — `paths.resolve_download_dir()` + `paths.manifest_path()`.
5. **Download** (parallel) — `manifest.needs_download()` skips cached archives; the rest go through `download_with_retry()` → `*.part` → atomic rename.
6. **Validate** — `validate.validate_zip()` runs `testzip()` + SHA256.
7. **Import** (sequential) — `importer.import_archive()`: `read_csv` → `data_<code>` → dimension extraction → constant-column removal → flag legend → column mapping → labelled view.
8. **Enrich** (on by default) — `enrich.enrich_areas()` unless `--no-enrich-areas`, then `enrich.enrich_history()` unless `--no-enrich-history` (fills `valid_from`/`valid_to`).
9. **Record** — provenance rows in `faostat_dataset` / `faostat_build`.
10. **Compact** — `compact.compact_database()` rewrites the file to its smallest form.

### Where files are stored

| What                                     | Where                                                                             | Lifetime                                                                                    |
| ---------------------------------------- | --------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| **Output database**                      | project-local `./<build.database>` by default (see below)                         | permanent — the product; git-ignored, not committed by accident                             |
| **Cached archives** (`*.zip`)            | the resolved `download_dir` — project-local `./faostat_temp_download/` by default | deleted after a *successful* build (kept after a failure, or always with `--keep-archives`) |
| **In-progress downloads** (`*.part`)     | inside `download_dir`                                                             | transient — renamed to `*.zip` on completion                                                |
| **Download manifest** (`manifest.jsonl`) | `<download_dir>/.faostatdb-downloads/`                                            | persists between runs (hot restart)                                                         |
| **Extracted CSVs**                       | a temp build dir under `download_dir`                                             | deleted immediately after each import                                                       |

The **output database** location is resolved from `build.database`: an absolute path is used as-is; a bare filename (default `faostat.duckdb`) is written project-local, in the current working directory. Set `$FAOSTATDB_DATABASE_DIR` to redirect a bare filename to a specific directory (e.g. a large external volume). The built database is git-ignored (see `.gitignore`) rather than hidden in an OS data directory, so it's easy to find yet never committed by accident.

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

- **Column types are inferred per column — text only where the data isn't purely numeric.** Each column's type is detected from a **full-file scan** (DuckDB `sample_size=-1`), so a column that is an integer on every row becomes `INTEGER` (or `BIGINT` if some value overflows the INT32 range), an everywhere-decimal one `DOUBLE`, and anything with mixed or non-numeric content falls back to `VARCHAR`. That is why `value` is a real `DOUBLE` in most datasets but stays text in a few — e.g. Food Security keeps censored thresholds like `<0.1` verbatim — and why alphanumeric code columns (`item_code` values such as `210400TSUB`, or `Area Code (M49)` values whose leading zeros must survive) land in `VARCHAR` on their own. The earlier "conversion" failures (`Could not convert '210400TSUB' to INT64`) came from DuckDB guessing a type off the first few rows and then aborting; scanning the whole file picks a type that fits every value, so those errors can't recur.
- **Inference is restricted to numeric-or-text so a value's meaning is never reinterpreted.** DuckDB is allowed to choose only `INTEGER`, `BIGINT`, `DOUBLE`, or `VARCHAR` (`auto_type_candidates`), tried narrowest-first: small dimension codes and years land in 4-byte `INTEGER`, and `BIGINT` is kept only as an overflow guard so a genuinely huge integer stays exact rather than degrading to `DOUBLE`. Its default candidates also include `BOOLEAN` and the date/time family, which silently *redefine* values — the FAOSTAT unit `t` (tonnes) would be read as boolean `true`, dotted strings as dates. Constraining the candidates means a column's storage type is only ever *narrowed* to a number when it truly is one, never redefined — keeping the mirror faithful to source.
- **Dimension tables (`dim_<stem>`) store codes as `VARCHAR`.** They are shared across datasets, and FAOSTAT types the same logical code numerically in one dataset and alphanumerically in another; keeping dimension keys as text makes the shared table consistent regardless of how a given dataset's fact column was typed. The fact table keeps its own inferred type; the labelled view casts the key to text when it joins.
- **Encoding is detected per archive.** Most files are UTF-8; some carry a UTF-16 BOM, others are Latin-1 (e.g. an unescaped `ô` in "Côte d'Ivoire"). FAOSTATdb checks for a BOM, validates as UTF-8, and falls back to Latin-1 only when needed.
- **Errors name the dataset, file, encoding, and columns.** If a read still fails, the message carries the dataset code, the CSV file name, the detected encoding, and the full raw→normalized column list, so it's clear which dataset (and column) is at fault rather than a bare DuckDB stack trace.

### Labelled convenience views

For each dataset the build also creates `view_<code>_labelled` — the compact fact table with the dimension labels (and flag descriptions) already joined back on. Views cost nothing on disk, so they are built by default.

### Making the database as small as possible

Three lossless reductions shrink the file — dimension extraction, constant-column removal, and then a **compaction pass**. That last step matters: DuckDB's `DROP COLUMN` is a catalog change that leaves the old column's bytes in place, and a plain `CHECKPOINT` does **not** reclaim them. So at the end of a build FAOSTATdb rewrites the whole database into a fresh file with `COPY FROM DATABASE`, which materializes only the columns that still exist. (On a rebuild into an existing file this routinely halves the size — measured 8.3 MB → 4.3 MB on a re-run of a single small dataset.) Disable with `--no-compact` if you prefer speed.

### Embedded provenance tables

| Table                     | One row per             | Key columns                                                                                                                                                                                                                                                                                                                                                 |
| ------------------------- | ----------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `faostat_dataset`         | imported dataset        | `dataset_code`, `dataset_name`, `topic`, `dataset_description`, `contact`, `email`, `date_update`, `compression_format`, `file_type`, `file_location`, `file_size_raw`, `file_rows_declared`, `rows_imported`, `source_csv_rows`, `downloaded_at`, `source_metadata_url`, `source_metadata_hash`, `source_metadata_json`, `archive_sha256`, `import_status` |
| `faostat_build`           | build run               | `build_id`, `started_at`, `completed_at`, `faostatdb_version`, `duckdb_version`, `python_version`, `os`, `metadata_snapshot_sha256`, `command_line`, `config_sha256`, `datasets_imported`, `datasets_failed`                                                                                                                                                |
| `faostat_column_mapping`  | renamed column          | `dataset_code`, `table_name`, `original_column_name`, `normalized_column_name`, `inferred_role`                                                                                                                                                                                                                                                             |
| `faostat_constant_column` | dropped constant column | `dataset_code`, `table_name`, `column_name`, `value`                                                                                                                                                                                                                                                                                                        |

`import_status` flags any dataset that failed (`failed`, `zip_invalid`) rather than silently omitting it. Failures are non-fatal by default; pass `--strict` to abort on the first error.
