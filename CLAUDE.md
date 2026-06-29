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
- **Storage model**: one fact table per dataset (`data_<code>`); dimension tables and label
  removal are deferred to v0.2.
- **Config**: TOML.

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
- CLI surface (v0.1): `faostatdb list`, `faostatdb config init|show`, `faostatdb build`
  (`--include` / `--exclude` / `--jobs` / `--keep-archives` / `--download-dir` / `--yes` / `--strict`).

CI runs the test suite on a Linux/macOS/Windows matrix; keep CI tests small and
deterministic — never trigger a full FAOSTAT download in CI.

## Source data

FAOSTAT bulk metadata: https://bulks-faostat.fao.org/production/datasets_E.json — the
inventory of dataset codes, names, update dates, file sizes/rows, and download URLs that
drives selection, download, and validation.
