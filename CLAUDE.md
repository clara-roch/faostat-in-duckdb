# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`FAOSTATdb` — a Python tool that builds a **local, source-preserving DuckDB mirror of
FAOSTAT bulk data**. It is not a harmonization layer: it preserves the statistical content
of FAOSTAT exactly (flags retained, values unaltered) while removing storage-level
duplication and recording reproducibility metadata so a built database can be audited and
cited.

The project is in early implementation. Authoritative references:

- [FAOSTATdb.md](FAOSTATdb.md) — the design discussion / spec (the "why" behind decisions).
- [PLAN.md](PLAN.md) — the concrete **v0.1 MVP** build plan, package layout, data model, and CLI surface.

## Architecture (settled decisions)

Do not re-litigate these without reason — they are settled in `FAOSTATdb.md`:

- **Language**: Python ≥ 3.11.
- **Dependencies**: required `duckdb` only; optional `rich` (progress UI) and `platformdirs` (cache dirs).
  Use stdlib `zipfile` (archive integrity via `testzip()`) and `tomllib` (config) — no external `zip`, no YAML.
- **Import**: read CSVs with DuckDB's `read_csv` directly — **never pandas**.
- **Storage model**: one fact table per dataset (`data_<code>`). Repeated labels are lifted
  into shared `dim_<stem>` tables keyed by `(dataset_code, <stem>_code)`; flag descriptions
  go to `dim_flag`; constant columns are dropped into `faostat_constant_column`; and a
  `view_<code>_labelled` re-joins the labels. All lossless. (v0.2/v0.3 — implemented.)
- **Smallest file**: after building, the DB is rewritten with `COPY FROM DATABASE`
  (see `compact.py`) because DuckDB's `DROP COLUMN` does not reclaim space on its own.
- **Config**: TOML. `keep_archives` defaults to **false** (delete cached `.zip` on a
  *successful* build), but the hot-restart manifest still reuses archives after an
  interrupted/failed run — they are never deleted before the build succeeds. Set
  `keep_archives = true` to persist the cache across successful builds.
- **Enrichment is opt-in and clearly non-source** (`faostatdb/enrich.py`, v0.3 — implemented).
  `--enrich-areas` builds a heuristic `area_classification` (country/region/aggregate,
  `confidence='low'`); `--enrich-history` fills `valid_from`/`valid_to` from a small curated
  gazetteer of well-known former/successor FAOSTAT areas (`confidence='high'`, and it implies
  `--enrich-areas`). Never let enrichment leak into the source tables, and never guess a
  validity date not in the gazetteer.
- **Concurrency is benchmarked, not guessed** (`faostatdb bench`, `faostatdb/bench.py`, v0.3).
  The one concurrency knob is `build.jobs` (parallel downloads); `bench` measures throughput
  at several `--jobs` levels. Its scheduling/timing core is network-free and unit-tested;
  the command refuses to run against the whole inventory.

## Conventions

- **Source-preservation is the prime directive.** Never drop flags or alter values. When in
  doubt about a transformation, keep the source information and add rather than replace.
- **Normalize column names to stable `snake_case`** while preserving the mapping
  (e.g. `"Area Code (M49)" → area_code_m49`, `"Item" → item_label`, `"Value" → value`,
  `"Flag" → flag_code`).
- **Hot restart must hold**: downloads are tracked in a manifest
  (`.faostatdb-downloads/manifest.jsonl`) with an explicit state machine; never delete
  archives until the build succeeds.
- **Reproducibility**: persist the metadata-JSON hash, per-archive SHA256, download
  timestamps, and tool/duckdb/python versions into the `faostat_dataset` / `faostat_build` tables.

## Environment & commands

The Python package does not exist yet; build it per [PLAN.md](PLAN.md) starting with
`pyproject.toml` and the `faostatdb/` package skeleton. Once it exists, the expected
workflow is:

- Install for development: `pip install -e ".[ui]"` (or `uv pip install -e ".[ui]"`)
- Run without installing: `python -m faostatdb ...`
- Run tests: `pytest`
- CLI surface: `faostatdb list`, `tables`, `config init|show`, `build`
  (`--include` / `--exclude` / `--jobs` / `--keep-archives` / `--download-dir` / `--yes` /
  `--strict` / `--enrich-areas` / `--enrich-history` / `--keep-raw-tables` / `--no-compact`),
  `info`, `validate`, `clean-cache`, `sql`, `self-contained`, and `bench`
  (`--include` / `--jobs-list`).

CI runs the test suite on a Linux/macOS/Windows matrix; keep CI tests small and
deterministic — never trigger a full FAOSTAT download in CI.

## Source data

FAOSTAT bulk metadata: https://bulks-faostat.fao.org/production/datasets_E.json — the
inventory of dataset codes, names, update dates, file sizes/rows, and download URLs that
drives selection, download, and validation.
