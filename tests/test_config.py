"""Config merge / selection tests (offline, deterministic)."""

import pytest

from faostatdb.config import (
    Config,
    default_config,
    load_config,
    merge_config,
    parse_years,
)
from faostatdb.metadata import DatasetRecord, select_datasets


def test_default_config():
    cfg = default_config()
    assert cfg.build.database == "faostat.duckdb"
    assert cfg.datasets.mode == "all"
    assert cfg.datasets.exclude == ["FA", "CBH"]


def test_merge_overrides_only_provided_keys():
    base = default_config()
    merged = merge_config(base, {"build": {"jobs": 12}, "datasets": {"mode": "include"}})
    assert merged.build.jobs == 12
    assert merged.build.database == "faostat.duckdb"  # untouched
    assert merged.datasets.mode == "include"


def test_merge_ignores_unknown_keys():
    merged = merge_config(default_config(), {"build": {"nonsense": 1}})
    assert isinstance(merged, Config)


def test_load_config_uses_defaults_when_no_local_toml(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # empty dir: no faostatdb.toml
    assert load_config() == default_config()


def test_local_toml_overrides_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "faostatdb.toml").write_text(
        "[build]\n"
        "jobs = 9\n"
        "database = \"fromfile.duckdb\"\n"
        "\n"
        "[datasets]\n"
        'mode = "include"\n'
        'include = ["QCL", "FBS"]\n',
        encoding="utf-8",
    )
    cfg = load_config()  # picks up ./faostatdb.toml over built-in defaults
    assert cfg.build.jobs == 9
    assert cfg.build.database == "fromfile.duckdb"
    assert cfg.build.overwrite is False  # unspecified key: falls back to default
    assert cfg.datasets.mode == "include"
    assert cfg.datasets.include == ["QCL", "FBS"]


def test_cli_flags_override_local_toml(tmp_path, monkeypatch):
    from faostatdb import cli

    monkeypatch.chdir(tmp_path)
    (tmp_path / "faostatdb.toml").write_text(
        "[build]\njobs = 9\n", encoding="utf-8"
    )
    cfg = load_config()
    assert cfg.build.jobs == 9  # local TOML beats the built-in default
    # A CLI flag then beats the local TOML.
    args = cli.build_parser().parse_args(["build", "--jobs", "3"])
    merged = cli._apply_build_overrides(args, cfg)
    assert merged.build.jobs == 3


def _records(*codes: str) -> list[DatasetRecord]:
    return [
        DatasetRecord(c, f"name {c}", None, None, None, None) for c in codes
    ]


def test_select_all():
    recs = _records("QCL", "FBS", "FA")
    from faostatdb.config import DatasetsConfig

    out = select_datasets(recs, DatasetsConfig(mode="all"))
    assert [r.code for r in out] == ["QCL", "FBS", "FA"]


def test_select_include_exclude():
    from faostatdb.config import DatasetsConfig

    recs = _records("QCL", "FBS", "FA")
    inc = select_datasets(recs, DatasetsConfig(mode="include", include=["QCL"]))
    assert [r.code for r in inc] == ["QCL"]

    exc = select_datasets(recs, DatasetsConfig(mode="exclude", exclude=["FA"]))
    assert [r.code for r in exc] == ["QCL", "FBS"]


# --- enrichment default + opt-out -------------------------------------------


def test_enrichment_on_by_default():
    enrich = default_config().enrichment
    assert enrich.area_classification is True
    assert enrich.historical_validity is True


def test_default_toml_renders_enrichment_true():
    from faostatdb.config import config_to_toml

    toml = config_to_toml(default_config())
    assert "area_classification = true" in toml
    assert "historical_validity = true" in toml


def test_local_toml_can_disable_enrichment(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "faostatdb.toml").write_text(
        "[enrichment]\n"
        "area_classification = false\n"
        "historical_validity = false\n",
        encoding="utf-8",
    )
    cfg = load_config()
    assert cfg.enrichment.area_classification is False
    assert cfg.enrichment.historical_validity is False


def _build_args(argv: list[str]):
    from faostatdb import cli

    return cli.build_parser().parse_args(["build", *argv])


def test_build_keeps_enrichment_on_without_flags():
    from faostatdb import cli

    cfg = cli._apply_build_overrides(_build_args([]), default_config())
    assert cfg.enrichment.area_classification is True
    assert cfg.enrichment.historical_validity is True


def test_no_enrich_flags_opt_out():
    from faostatdb import cli

    cfg = cli._apply_build_overrides(
        _build_args(["--no-enrich-areas", "--no-enrich-history"]), default_config()
    )
    assert cfg.enrichment.area_classification is False
    assert cfg.enrichment.historical_validity is False


def test_negation_wins_over_positive_flag():
    from faostatdb import cli

    # If both a flag and its negation are given, the opt-out wins (applied last).
    cfg = cli._apply_build_overrides(
        _build_args(["--enrich-areas", "--no-enrich-areas"]), default_config()
    )
    assert cfg.enrichment.area_classification is False


# --- year filter parsing ----------------------------------------------------


def test_parse_years_none_when_empty():
    assert parse_years("") is None
    assert parse_years("   ") is None
    assert parse_years(None) is None


def test_parse_years_single_and_list():
    assert parse_years("2010") == {2010}
    assert parse_years("2000,2005,2010") == {2000, 2005, 2010}
    # Whitespace and empty tokens are tolerated.
    assert parse_years(" 2000 , 2001 ,") == {2000, 2001}


def test_parse_years_inclusive_ranges():
    assert parse_years("1990-1992") == {1990, 1991, 1992}
    assert parse_years("1990-1992,2000") == {1990, 1991, 1992, 2000}


@pytest.mark.parametrize("spec", ["abc", "2000-", "20x0", "2010-2000", "0", "10000"])
def test_parse_years_rejects_bad_specs(spec):
    with pytest.raises(ValueError):
        parse_years(spec)


def test_default_years_is_all():
    assert default_config().build.years == ""


def test_years_flag_overrides_config():
    from faostatdb import cli

    cfg = cli._apply_build_overrides(_build_args(["--years", "2000-2010"]), default_config())
    assert cfg.build.years == "2000-2010"
    assert parse_years(cfg.build.years) == set(range(2000, 2011))


def test_years_toml_renders_and_roundtrips():
    from faostatdb.config import config_to_toml

    toml = config_to_toml(replace_years(default_config(), "2000,2010"))
    assert 'years = "2000,2010"' in toml


def replace_years(cfg, years):
    from dataclasses import replace

    return Config(
        build=replace(cfg.build, years=years),
        datasets=cfg.datasets,
        performance=cfg.performance,
        enrichment=cfg.enrichment,
    )
