import pytest

from pipeline.buildings import (
    DEFAULT_HEIGHT_M,
    METRES_PER_LEVEL,
    choose_height,
    levels_value,
    parse_height,
    parse_levels,
    type_default_height,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("45", 45.0),
        ("45.5", 45.5),
        ("45,5", 45.5),
        ("45 m", 45.0),
        ("45m", 45.0),
        ("45 metres", 45.0),
        (" 12 ", 12.0),
        ("~30", 30.0),
        ("150 ft", 150 * 0.3048),
        ("150ft", 150 * 0.3048),
        ("150'", 150 * 0.3048),
        ("150 feet", 150 * 0.3048),
        ("12'6\"", 12 * 0.3048 + 6 * 0.0254),
        ("45;50", 45.0),
    ],
)
def test_parse_height_units(raw, expected):
    assert parse_height(raw) == pytest.approx(expected)


@pytest.mark.parametrize("raw", [None, "", "tall", "0", "-5", "45 km", "9999", "m", "nan"])
def test_parse_height_rejects_bad_values(raw):
    assert parse_height(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("12", 12.0), ("2.5", 2.5), ("3;4", 3.0), ("18/19", 18.0), ("4/5", 4.0), (" 7 ", 7.0)],
)
def test_parse_levels(raw, expected):
    assert parse_levels(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "0", "a few", "Ground Floor", "1000"])
def test_parse_levels_rejects_bad_values(raw):
    assert parse_levels(raw) is None


def test_levels_value_types():
    assert levels_value({"building:levels": "12"}) == 12
    assert isinstance(levels_value({"building:levels": "12"}), int)
    assert levels_value({"building:levels": "2.5"}) is None  # only whole numbers are emitted
    assert levels_value({"building:levels": "18/19"}) == 18
    assert levels_value({"building:levels": "Ground Floor"}) is None
    assert levels_value({}) is None
    assert levels_value({"building:levels": "lots"}) is None


def test_fallback_order_osm_height_first():
    tags = {"building": "office", "height": "100 m", "building:levels": "10"}
    assert choose_height(tags, com_height=80.0) == (100.0, "osm_height")


def test_fallback_order_levels_second():
    tags = {"building": "office", "building:levels": "10"}
    h, src = choose_height(tags, com_height=80.0)
    assert src == "osm_levels"
    assert h == pytest.approx(10 * METRES_PER_LEVEL)


def test_fallback_order_unparseable_height_falls_through_to_levels():
    h, src = choose_height({"building": "yes", "height": "tall", "building:levels": "3"}, None)
    assert (src, h) == ("osm_levels", pytest.approx(3 * METRES_PER_LEVEL))


def test_fallback_order_com_third():
    assert choose_height({"building": "office"}, com_height=80.0) == (80.0, "com_open_data")


def test_fallback_order_type_default_last():
    assert choose_height({"building": "house"}, com_height=None) == (7.0, "type_default")
    assert choose_height({"building": "yes"}, None) == (DEFAULT_HEIGHT_M, "type_default")
    assert choose_height({"building:part": "yes"}, None) == (DEFAULT_HEIGHT_M, "type_default")


def test_implausible_com_height_is_ignored():
    assert choose_height({"building": "yes"}, com_height=0.0)[1] == "type_default"
    assert choose_height({"building": "yes"}, com_height=-3.0)[1] == "type_default"


def test_type_defaults_are_positive_and_unknown_types_get_default():
    assert type_default_height("garage") == 3.0
    assert type_default_height("GARAGE") == 3.0
    assert type_default_height("something_new") == DEFAULT_HEIGHT_M
    assert type_default_height(None) == DEFAULT_HEIGHT_M


def test_fractional_levels_still_give_a_height():
    h, src = choose_height({"building": "yes", "building:levels": "2.5"}, None)
    assert (src, h) == ("osm_levels", pytest.approx(2.5 * METRES_PER_LEVEL))
