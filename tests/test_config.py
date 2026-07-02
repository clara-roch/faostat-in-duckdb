"""Config merge / selection tests (offline, deterministic)."""

from faostatdb.config import (
    Config,
    apply_env_overrides,
    default_config,
    load_config,
    load_dotenv,
    merge_config,
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


def test_env_overrides_typed_values():
    env = {
        "FAOSTATDB_DATABASE": "food.duckdb",
        "FAOSTATDB_JOBS": "12",
        "FAOSTATDB_KEEP_ARCHIVES": "true",
        "FAOSTATDB_DATASETS_MODE": "include",
        "FAOSTATDB_DATASETS_INCLUDE": "QCL, FBS",
    }
    cfg = apply_env_overrides(default_config(), env)
    assert cfg.build.database == "food.duckdb"
    assert cfg.build.jobs == 12
    assert cfg.build.keep_archives is True
    assert cfg.build.overwrite is False  # untouched
    assert cfg.datasets.mode == "include"
    assert cfg.datasets.include == ["QCL", "FBS"]


def test_env_overrides_empty_is_noop():
    cfg = apply_env_overrides(default_config(), {})
    assert cfg == default_config()


def test_load_dotenv_sets_environ_without_clobbering(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("FAOSTATDB_JOBS", raising=False)
    monkeypatch.setenv("FAOSTATDB_DATABASE", "shell.duckdb")  # already set: must win
    (tmp_path / "secrets.env").write_text(
        "# a comment\n"
        "FAOSTATDB_JOBS=9\n"
        'FAOSTATDB_DATASETS_EXCLUDE="FA, CBH"\n'
        "FAOSTATDB_DATABASE=fromfile.duckdb\n",
        encoding="utf-8",
    )
    loaded = load_dotenv()
    assert loaded["FAOSTATDB_JOBS"] == "9"
    assert "FAOSTATDB_DATABASE" not in loaded  # not clobbered
    import os

    assert os.environ["FAOSTATDB_DATABASE"] == "shell.duckdb"

    cfg = load_config()  # no faostatdb.toml in tmp_path -> built-in defaults + env
    assert cfg.build.jobs == 9
    assert cfg.build.database == "shell.duckdb"
    assert cfg.datasets.exclude == ["FA", "CBH"]


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


def test_env_can_disable_enrichment():
    cfg = apply_env_overrides(
        default_config(),
        {"FAOSTATDB_ENRICH_AREAS": "false", "FAOSTATDB_ENRICH_HISTORY": "false"},
    )
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
