# FAOSTATdb

A local, **source-preserving DuckDB mirror of FAOSTAT bulk data**.

FAOSTATdb downloads FAOSTAT bulk ZIP archives, validates them, and imports each
dataset into a single DuckDB file — one fact table per dataset (`data_<code>`).
It is **not** a harmonization layer: flags are retained and values are never
altered. It removes storage-level duplication and records reproducibility
metadata (source hashes, timestamps, tool/duckdb/python versions) so a built
database can be audited and cited.

## Install

```bash
pip install -e ".[ui]"      # ui extra adds rich progress + platformdirs cache dirs
# or run without installing:
python -m faostatdb --help
```

Required dependency: `duckdb`. Optional: `rich`, `platformdirs`.

## Usage

```bash
faostatdb list [--remote]                 # list available datasets
faostatdb config show                     # print the effective configuration
faostatdb build [--database faostat.duckdb] [--include QCL,FBS] [--exclude FA,CBH] \
                [--jobs N] [--keep-archives] [--download-dir DIR] [--yes] [--strict]
```

### Build the full database

```bash
faostatdb build --yes
```

### Build a subset

```bash
faostatdb build --include QCL,FBS --database food.duckdb
```

## How the pipeline works

Everything starts at the CLI and flows through the modules below. The entry
point is [`faostatdb/cli.py`](faostatdb/cli.py) (`main`), reachable as either the
installed `faostatdb` command or `python -m faostatdb` (via
[`faostatdb/__main__.py`](faostatdb/__main__.py)).

### What each file does

| File | Role |
| --- | --- |
| [`cli.py`](faostatdb/cli.py) | Parses arguments, dispatches to `list` / `config` / `build`, and orchestrates the build (selection → confirm → download → validate → import → metadata). |
| [`__main__.py`](faostatdb/__main__.py) | Lets you run the tool with `python -m faostatdb`. |
| [`config.py`](faostatdb/config.py) | Loads the committed `faostatdb.toml` (stdlib `tomllib`), applies overrides from `secrets.env` env vars, and powers `config show`. TOML < `secrets.env` < CLI flags. |
| [`metadata.py`](faostatdb/metadata.py) | Fetches and parses the bulk inventory `datasets_E.json`, hashes it for reproducibility, and applies `all` / `include` / `exclude` selection. |
| [`paths.py`](faostatdb/paths.py) | Resolves **where archives are cached** and where the download manifest lives. |
| [`download.py`](faostatdb/download.py) | Downloads archives with retry/backoff, tracks every dataset in a hot-restart **manifest** state machine, and writes via `*.part` → atomic rename. |
| [`validate.py`](faostatdb/validate.py) | Verifies ZIP integrity with stdlib `zipfile.testzip()`, computes the archive SHA256, and optionally checks the declared size. |
| [`importer.py`](faostatdb/importer.py) | Extracts the main CSV, imports it into `data_<code>` via DuckDB's `read_csv` (**never pandas**), and deletes the extracted CSV afterward. |
| [`schema.py`](faostatdb/schema.py) | Normalizes CSV headers to stable `snake_case` (values/flags untouched) and defines the `faostat_dataset` / `faostat_build` metadata-table DDL. |
| [`progress.py`](faostatdb/progress.py) | Human-readable progress to stderr — uses `rich` if installed, plain lines otherwise. |

### Order of calls during `faostatdb build`

1. **Configure** — `cli.main` calls `config.load_config()`: the committed
   `faostatdb.toml` is loaded, then overridden by environment variables (read
   from `secrets.env` if present), then by CLI flags (`_apply_build_overrides`).
2. **Select** — `metadata.fetch_and_parse()` downloads and parses
   `datasets_E.json`; `metadata.select_datasets()` reduces it to the chosen
   datasets per the `[datasets]` config.
3. **Confirm** — unless `--yes`, the CLI prompts before downloading (and refuses
   to proceed non-interactively without `--yes`).
4. **Resolve paths** — `paths.resolve_download_dir()` decides where archives are
   cached; `paths.manifest_path()` locates the manifest inside it.
5. **Download** — for each selected dataset, `download.Manifest.needs_download()`
   decides whether a valid archive already exists (hot restart). Missing ones go
   through `download.download_with_retry()` → `*.part` → atomic rename to `*.zip`.
   State transitions are appended to the manifest at every step.
6. **Validate** — `validate.validate_zip()` runs `testzip()` and computes the
   SHA256; bad archives are marked `zip_invalid` and re-fetched.
7. **Import** — `importer.import_archive()` extracts the largest top-level CSV to
   a temp build dir, then `import_csv()` runs
   `CREATE TABLE data_<code> AS SELECT … FROM read_csv(...)` with headers
   normalized by `schema.normalize_columns()`. The extracted CSV is deleted.
8. **Record** — `schema.create_metadata_tables()` plus per-dataset rows in
   `faostat_dataset` and a build row in `faostat_build` capture the metadata hash,
   per-archive SHA256, timestamps, and tool/DuckDB/Python versions.

> Status: steps 1–4 and the building blocks for 5–8 are implemented; the
> download → validate → import driver loop in `run_build` is the remaining wiring
> (see [PLAN.md](PLAN.md) step 5).

### Where files are stored

| What | Where | Lifetime |
| --- | --- | --- |
| **Output database** | `$FABIO_DUCKDB_DIR/<build.database>` (see below) | permanent — this is the product, kept outside the repo |
| **Downloaded archives** (`*.zip`) | the resolved `download_dir` — project-local `./faostat_temp_download/` by default | temporary; removed after a successful build unless `--keep-archives` |
| **In-progress downloads** (`*.part`) | inside `download_dir` | transient — renamed to `*.zip` on completion |
| **Download manifest** (`manifest.jsonl`) | `<download_dir>/.faostatdb-downloads/` | persists between runs to enable hot restart |
| **Extracted CSVs** | a temp build dir under `download_dir` | deleted immediately after each import |

The **output database** location is resolved from `build.database`:

- An **absolute** path is used as-is.
- A **bare filename** (the default, `faostat.duckdb`) is placed inside the
  `FABIO_DUCKDB_DIR` environment variable. If that variable is unset, it falls
  back to the OS data directory — never the repository — so a built database is
  never committed by accident.

The **`download_dir`** (raw, temporary archives) is resolved in this order
(highest precedence first):

1. `--download-dir DIR` (CLI) or `download_dir` in `faostatdb.toml`
   — with `~`, `${VAR}`, and `%VAR%` expansion applied. A relative path is taken
   relative to the project, so the default `faostat_temp_download` lands in
   `./faostat_temp_download/`.
2. The `FAOSTATDB_DOWNLOAD_DIR` environment variable.
3. `./faostat_temp_download/` (project-local).

#### Setting the path via `secrets.env` (this repo's setup)

This repo keeps the machine-specific output path in a git-ignored
[`secrets.env`](secrets.env) file. The final DuckDB is written under
`FABIO_DUCKDB_DIR`. To point it at `C:\where\it\is\stored`, `secrets.env`
contains:

```dotenv
FABIO_DUCKDB_DIR=C:\where\it\is\stored
```

FAOSTATdb loads `secrets.env` from the current directory automatically at
startup, so you can just run the build — no manual sourcing required:

```bash
faostatdb build --yes
```

Variables already set in your shell take precedence over `secrets.env`, so you
can still override a single run from the command line:

```powershell
$env:FABIO_DUCKDB_DIR = "C:\elsewhere"; faostatdb build --yes
```

Keep `secrets.env` out of version control (it is already covered by
[.gitignore](.gitignore)). See the [Configuration](#configuration) section for
the full list of overridable variables.

## Querying the result

The output is a plain DuckDB file — query it from any language.

**DuckDB CLI**

```sql
SELECT area_label, year, value, flag_code
FROM data_qcl
WHERE item_label = 'Wheat'
ORDER BY year;
```

**Python**

```python
import duckdb
con = duckdb.connect("faostat.duckdb")
df = con.execute("SELECT * FROM data_qcl LIMIT 10").df()
```

**R**

```r
library(duckdb)
con <- dbConnect(duckdb(), "faostat.duckdb")
dbGetQuery(con, "SELECT * FROM data_qcl LIMIT 10")
```

**Julia**

```julia
using DuckDB, DataFrames
con = DBInterface.connect(DuckDB.DB, "faostat.duckdb")
DataFrame(DBInterface.execute(con, "SELECT * FROM data_qcl LIMIT 10"))
```

## Configuration

Configuration comes from two files, by design:

- [`faostatdb.toml`](faostatdb.toml) — **committed** to the repo. It holds the
  general, default configuration in its most generic shape: it is exactly what
  you get when you clone the project. **You are not meant to edit it** for
  machine-specific or personal settings — leave it alone so it stays clean and
  pull-able.
- [`secrets.env`](secrets.env) — **git-ignored**, your own. A simple
  `KEY=value`-per-line file where you override whatever you need. FAOSTATdb loads
  it automatically (from the current directory) at startup; values already set in
  your shell environment are left untouched.

Resolution order, lowest precedence first:

1. `faostatdb.toml` (the committed defaults).
2. Environment variables, loaded from `secrets.env` if present.
3. CLI flags (e.g. `--jobs`, `--include`).

So to change a value you do **not** edit `faostatdb.toml`; you add a line to
`secrets.env`.

### The committed defaults (`faostatdb.toml`)

```toml
[build]
database = "faostat.duckdb"             # filename; final DB is written under $FABIO_DUCKDB_DIR
download_dir = "faostat_temp_download"  # temporary raw ZIPs, project-local, deleted after build
keep_archives = false
jobs = 6
overwrite = false

[datasets]
mode = "all"            # all | include | exclude
include = []
exclude = ["FA", "CBH"]
```

### Overriding via `secrets.env`

Each value above maps to an environment variable. Set only the ones you want to
change; everything else keeps its `faostatdb.toml` value.

```dotenv
# Output location (kept out of the repo) — see "Where files are stored" below.
FABIO_DUCKDB_DIR=C:\where\it\is\stored

# Any of these override the matching faostatdb.toml value:
FAOSTATDB_DATABASE=faostat.duckdb
FAOSTATDB_DOWNLOAD_DIR=faostat_temp_download
FAOSTATDB_KEEP_ARCHIVES=false
FAOSTATDB_JOBS=6
FAOSTATDB_OVERWRITE=false
FAOSTATDB_DATASETS_MODE=include            # all | include | exclude
FAOSTATDB_DATASETS_INCLUDE=QCL,FBS         # comma-separated codes
FAOSTATDB_DATASETS_EXCLUDE=FA,CBH          # comma-separated codes
```

Booleans accept `true`/`false`/`1`/`0`/`yes`/`no`; list variables are
comma-separated. Run `faostatdb config show` to print the effective configuration
after the TOML and `secrets.env` have been merged.

## Reproducibility

Each build records, in the `faostat_dataset` and `faostat_build` tables: the
metadata-JSON snapshot hash, per-archive SHA256, download timestamps, and the
tool / DuckDB / Python versions and command line used.

## Development

```bash
pip install -e ".[dev]"
pytest
```

See [PLAN.md](PLAN.md) for the v0.1 build plan and [FAOSTATdb.md](FAOSTATdb.md)
for the design rationale.
