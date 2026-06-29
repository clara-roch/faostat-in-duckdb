"""Config merge / selection tests (offline, deterministic)."""

from faostatdb.config import Config, default_config, merge_config
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
