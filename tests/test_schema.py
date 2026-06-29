"""Column-name normalization tests (offline, deterministic)."""

from faostatdb.schema import normalize_column, normalize_columns


def test_known_normalizations():
    assert normalize_column("Area Code (M49)") == "area_code_m49"
    assert normalize_column("Item") == "item_label"
    assert normalize_column("Value") == "value"
    assert normalize_column("Flag") == "flag_code"
    assert normalize_column("Months Code") == "months_code"


def test_strips_and_lowercases():
    assert normalize_column("  Element Code  ") == "element_code"


def test_collisions_get_suffixed():
    assert normalize_columns(["Value", "Value"]) == ["value", "value_1"]
