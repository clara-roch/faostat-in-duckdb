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
faostatdb config init                     # write a default faostatdb.toml
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
| [`config.py`](faostatdb/config.py) | Loads `faostatdb.toml` (stdlib `tomllib`), merges it over built-in defaults, and powers `config init` / `config show`. Defaults < TOML < CLI flags. |
| [`metadata.py`](faostatdb/metadata.py) | Fetches and parses the bulk inventory `datasets_E.json`, hashes it for reproducibility, and applies `all` / `include` / `exclude` selection. |
| [`paths.py`](faostatdb/paths.py) | Resolves **where archives are cached** and where the download manifest lives. |
| [`download.py`](faostatdb/download.py) | Downloads archives with retry/backoff, tracks every dataset in a hot-restart **manifest** state machine, and writes via `*.part` → atomic rename. |
| [`validate.py`](faostatdb/validate.py) | Verifies ZIP integrity with stdlib `zipfile.testzip()`, computes the archive SHA256, and optionally checks the declared size. |
| [`importer.py`](faostatdb/importer.py) | Extracts the main CSV, imports it into `data_<code>` via DuckDB's `read_csv` (**never pandas**), and deletes the extracted CSV afterward. |
| [`schema.py`](faostatdb/schema.py) | Normalizes CSV headers to stable `snake_case` (values/flags untouched) and defines the `faostat_dataset` / `faostat_build` metadata-table DDL. |
| [`progress.py`](faostatdb/progress.py) | Human-readable progress to stderr — uses `rich` if installed, plain lines otherwise. |

### Order of calls during `faostatdb build`

1. **Configure** — `cli.main` calls `config.load_config()`: built-in defaults are
   overridden by `faostatdb.toml` (if present in the cwd), then by CLI flags
   (`_apply_build_overrides`).
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
| **Output database** | `build.database` (default `./faostat.duckdb`) | permanent — this is the product |
| **Downloaded archives** (`*.zip`) | the resolved `download_dir` (see below) | kept across runs for hot restart; removed after a successful build unless `--keep-archives` |
| **In-progress downloads** (`*.part`) | inside `download_dir` | transient — renamed to `*.zip` on completion |
| **Download manifest** (`manifest.jsonl`) | `<download_dir>/.faostatdb-downloads/` | persists between runs to enable hot restart |
| **Extracted CSVs** | a temp build dir under `download_dir` | deleted immediately after each import |

The **`download_dir`** itself is resolved in this order (highest precedence first):

1. `--download-dir DIR` (CLI) or `download_dir` in `faostatdb.toml`
   — with `~`, `${VAR}`, and `%VAR%` expansion applied.
2. The `FAOSTATDB_DOWNLOAD_DIR` environment variable.
3. `./faostatdb_archives/` (project-local) — only when `--keep-archives` is set.
4. The OS cache directory — via `platformdirs` if installed, otherwise the system
   temp dir (e.g. `%TEMP%\faostatdb` on Windows, `/tmp/faostatdb` elsewhere).

## Secrets: keeping `download_dir` out of the repo

`faostatdb.toml` is meant to be committed, so it should **not** contain a private
or machine-specific path (a personal home directory, a mounted scratch volume, a
CI runner path). Manage that path as a secret instead of hard-coding it:

**Option A — reference an environment variable from the TOML** (recommended):

```toml
[build]
download_dir = "${FAOSTATDB_DOWNLOAD_DIR}"
```

Then set the real path in your environment, never in the committed file:

```bash
# Linux / macOS — add to ~/.bashrc, ~/.zshrc, direnv .envrc, etc.
export FAOSTATDB_DOWNLOAD_DIR="/mnt/data/faostat-cache"
```

```powershell
# Windows PowerShell — current session
$env:FAOSTATDB_DOWNLOAD_DIR = "D:\faostat-cache"
# or persist for your user account:
setx FAOSTATDB_DOWNLOAD_DIR "D:\faostat-cache"
```

If the variable is unset, the `${...}` reference is ignored and resolution falls
through to the OS cache dir — so the committed config stays portable.

**Option B — leave the TOML empty and set only the env var.** With
`download_dir = ""`, `FAOSTATDB_DOWNLOAD_DIR` (if set) is used automatically; this
is the natural fit for **CI/CD secret stores** (e.g. a GitHub Actions secret
exported as an env var) and `.env` files.

**Option C — pass it per-invocation** with `--download-dir DIR`, which overrides
both the config and the env var and never touches any file.

In all cases, keep caches out of version control. The archive cache, manifest,
and `*.zip` files are already covered by [.gitignore](.gitignore); if you point
`download_dir` at a folder inside the repo, add it there too, and keep `.env`
files ignored.

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

## Configuration (`faostatdb.toml`)

```toml
[build]
database = "faostat.duckdb"
download_dir = ""        # "" = OS cache; "${FAOSTATDB_DOWNLOAD_DIR}" = from env (see Secrets)
keep_archives = false
jobs = 6
overwrite = false

[datasets]
mode = "all"            # all | include | exclude
include = []
exclude = ["FA", "CBH"]
```

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
