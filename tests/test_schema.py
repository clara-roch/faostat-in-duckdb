"""Column-name normalization tests (offline, deterministic)."""

from faostatdb.schema import dimension_groups, normalize_column, normalize_columns


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


def test_mismatched_label_headers_are_pinned_to_stem_label():
    # Labels whose header doesn't share the code's stem are pinned to <stem>_label
    # so dimension extraction lifts them into the shared dimension rather than
    # leaving them duplicated on every fact row.
    assert normalize_column("Reporter Countries") == "reporter_country_label"
    assert normalize_column("Partner Countries") == "partner_country_label"
    assert normalize_column("Currency") == "iso_currency_label"


def test_pinned_labels_group_with_their_code():
    # After normalization the labels share their code's stem, so dimension_groups
    # sweeps them into the dimension alongside the alternate code.
    assert dimension_groups(
        ["reporter_country_code", "reporter_country_code_m49", "reporter_country_label", "value"]
    ) == [
        (
            "reporter_country",
            "reporter_country_code",
            ["reporter_country_code_m49", "reporter_country_label"],
        )
    ]
    assert dimension_groups(["iso_currency_code", "iso_currency_label", "value"]) == [
        ("iso_currency", "iso_currency_code", ["iso_currency_label"])
    ]
