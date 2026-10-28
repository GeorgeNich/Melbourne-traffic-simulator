"""Approach -> SUMO edge matching on synthetic junctions (no network file needed)."""

from __future__ import annotations

import pytest

from pipeline import count_mapping as cm


def test_street_names_normalise():
    assert cm.names_match("Flinders St", "Flinders Street")
    assert cm.names_match("Kings Way", "Kingsway")
    assert cm.names_match("La Trobe St", "La Trobe Street")
    assert cm.names_match("Russell St", "Russell Street Extension")
    assert not cm.names_match("Lonsdale St", "Albert Street")
    assert not cm.names_match("", "Flinders Street")


def test_bearings_and_headings():
    assert cm.bearing_deg(0, 0, 0, 10) == 0
    assert cm.bearing_deg(0, 0, 10, 0) == 90
    assert cm.heading_of(70) == "EB"  # Hoddle Grid "east" streets run about 70 deg
    assert cm.heading_of(340) == "NB"
    assert cm.angle_diff(350, 10) == 20
    # Only the last ~25 m count: a bend upstream does not change the approach bearing.
    shape = [(0, -200), (100, -100), (100, -30), (100, 0)]
    assert cm.tail_bearing(shape) == pytest.approx(0, abs=1)


def _sj(*apps):
    sj = cm.SiteJunction(1, ["n"], ["t"], 0.0)
    sj.approaches = [
        cm.ApproachEdge(e, name, brg, lanes, [], "a", "n") for e, name, brg, lanes in apps
    ]
    return sj


GRID = _sj(
    ("lon_eb", "Lonsdale Street", 68.0, 3),
    ("lon_wb", "Lonsdale Street", 249.0, 3),
    ("ex_nb", "Exhibition Street", 338.0, 2),
    ("ex_sb", "Exhibition Street", 158.0, 2),
)


@pytest.mark.parametrize(
    "street, heading, edge",
    [
        ("Lonsdale St", "EB", "lon_eb"),
        ("Lonsdale St", "WB", "lon_wb"),
        ("Exhibition St", "NB", "ex_nb"),
        ("Exhibition St", "SB", "ex_sb"),
    ],
)
def test_grid_approaches_match_by_name_and_heading(street, heading, edge):
    m = cm.match_approach(GRID, street, heading)
    assert m.edge_id == edge and m.name_match and m.reason == "ok"


def test_no_edge_in_that_heading_is_reported_not_guessed():
    sj = _sj(("lon_wb", "Lonsdale Street", 249.0, 3))
    m = cm.match_approach(sj, "Swanston St", "NB")
    assert m.edge_id is None and "no approach edge" in m.reason


def test_single_unnamed_edge_in_heading_is_accepted_but_flagged():
    sj = _sj(("alb", "Albert Street", 250.0, 2), ("lon", "Lonsdale Street", 68.0, 3))
    m = cm.match_approach(sj, "Lonsdale St", "WB")
    assert m.edge_id == "alb" and not m.name_match


def test_two_unnamed_edges_in_heading_fail():
    sj = _sj(("a", "Foo Street", 250.0, 2), ("b", "Bar Street", 260.0, 2))
    assert cm.match_approach(sj, "Lonsdale St", "WB").edge_id is None


def test_lane_group_split_resolved_only_by_unique_lane_count():
    sj = _sj(("w3", "William Street", 336.0, 3), ("w2", "William Street", 338.0, 2))
    assert cm.match_approach(sj, "William St", "NB", n_lanes=3).edge_id == "w3"
    assert cm.match_approach(sj, "William St", "NB", n_lanes=2).edge_id == "w2"
    m = cm.match_approach(sj, "William St", "NB", n_lanes=4)
    assert m.edge_id is None and "splits" in m.reason


def test_rotated_heading_tolerance():
    sj = _sj(("cr", "City Road", 228.0, 4), ("cr2", "City Road", 48.0, 5))
    assert cm.match_approach(sj, "City Rd", "SW").edge_id == "cr"
    assert cm.match_approach(sj, "City Rd", "NE").edge_id == "cr2"
