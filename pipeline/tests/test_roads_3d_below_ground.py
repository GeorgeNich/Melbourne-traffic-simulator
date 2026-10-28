"""Stage B of pipeline.roads_3d on synthetic networks: tunnels and cuttings below street level.

Tunnel depth -10 m x level ramped at the portals within the grade limits, the river-bed rule under
water, roads over tunnels, measured cutting profiles (trenches), and what must stay unchanged
(building passages, positive-layer tunnels). Networks use the helpers of test_roads_3d_heights.
"""

import pytest

from pipeline.roads_3d import (
    CHECK_CLEARANCE_M,
    DEFAULT_SEPARATION_M,
    MAX_GRADE,
    OPEN_DECK_M,
    UNDER_WATER_MAX_Z_M,
    tunnel_separation,
    tunnel_target,
)
from pipeline.tests.test_roads_3d_heights import EPS, Net, ll, max_grade, z_at, zs

GRADE_SLACK = 0.003  # z rounding over 10 m segments


def tunnel_net(length: float = 600.0, layer: str | None = "-1", **tags: str) -> Net:
    """Way 1 (ground, x -300..0), tunnel way 2 (x 0..length), way 3 (ground beyond)."""
    n = Net()
    a, b, c, d = n.node(-300), n.node(0), n.node(length), n.node(length + 300)
    n.way(1, [a, b], highway="motorway")
    t = {"tunnel": "yes", **tags}
    if layer is not None:
        t["layer"] = layer
    n.way(2, [b, c], highway="motorway", **t)
    n.way(3, [c, d], highway="motorway")
    return n


# --- targets ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tags, target",
    [
        ({"tunnel": "yes", "layer": "-1"}, -10.0),
        ({"tunnel": "yes", "layer": "-2"}, -20.0),
        ({"tunnel": "yes", "layer": "-3"}, -30.0),
        ({"tunnel": "yes"}, -10.0),  # untagged layer
        ({"tunnel": "yes", "layer": "0"}, -10.0),
        ({"tunnel": "building_passage", "layer": "-1"}, None),
        ({"tunnel": "building_passage", "layer": "2"}, None),  # Kings Way 12278361 / 23212187
        ({"tunnel": "yes", "layer": "1"}, None),  # a positive-layer tunnel keeps its deck
        ({"tunnel": "covered"}, None),  # a roofed road at street level
        ({"tunnel": "no", "layer": "-1"}, None),
        ({"layer": "-1"}, None),  # not a tunnel (e.g. a road under a building)
    ],
)
def test_tunnel_target(tags, target):
    assert tunnel_target({"highway": "service", **tags}) == target


def test_tunnel_separation_uses_the_clearance_tag_or_the_default():
    assert tunnel_separation({"maxheight": "5.1"}) == pytest.approx(6.3)
    assert tunnel_separation({}) == DEFAULT_SEPARATION_M
    assert tunnel_separation({"maxheight": "2.1"}) >= CHECK_CLEARANCE_M  # never below the check


# --- depth and portal ramps -------------------------------------------------------------------


@pytest.mark.parametrize("layer, depth", [("-1", -10.0), ("-2", -20.0), ("-3", -30.0)])
def test_tunnel_reaches_its_target_and_ramps_at_the_portals(layer, depth):
    n = tunnel_net(length=1200.0, layer=layer)
    f = n.build()
    z2 = zs(f[2])
    assert min(z2) == depth
    assert z2[0] == z2[-1] == 0.0  # portals at street level
    assert max_grade(f[2]) <= MAX_GRADE + GRADE_SLACK
    # the ramp is as short as the grade allows: target reached |depth| / 6 % from each portal
    reach = -depth / MAX_GRADE
    assert z_at(f[2], reach + 10, 0) == depth
    assert z_at(f[2], reach - 20, 0) > depth
    assert set(zs(f[1])) == {0.0} and set(zs(f[3])) == {0.0}
    assert f[2]["properties"]["z_source"] == "estimated"


def test_short_tunnel_is_as_deep_as_the_grade_allows():
    f = tunnel_net(length=100.0).build()
    assert min(zs(f[2])) == pytest.approx(-MAX_GRADE * 50, abs=0.01)


def test_shared_nodes_keep_one_z_and_steps_stay_small():
    """A link tunnel (layer -1) joins a deep tunnel (layer -3): one z at the shared node; the
    link comes down to meet the deep tunnel (Net.build checks steps <= 1 m, no mismatches)."""
    n = tunnel_net(length=1200.0, layer="-3")
    j = n.ways[1].nodes[1]
    n.coords[j] = ll(1200)
    mid = n.node(600)
    n.ways[1].nodes = [n.ways[1].nodes[0], mid, j]
    n.way(4, [mid, n.node(600, 150), n.node(600, 450)], highway="motorway_link", tunnel="yes")
    n.way(5, [n.ways[-1].nodes[-1], n.node(600, 700)], highway="motorway_link")
    f = n.build()
    # the junction is as deep as the link's own portal (450 m away) allows: 450 x 6 % = 27 m
    assert z_at(f[4], 600, 0) == pytest.approx(z_at(f[2], 600, 0), abs=0.01)
    assert z_at(f[4], 600, 0) == pytest.approx(-27.0, abs=0.01)
    assert min(zs(f[2])) == -30.0
    assert max_grade(f[4]) <= MAX_GRADE + GRADE_SLACK
    assert zs(f[4])[-1] == 0.0  # its own portal


def test_building_passages_and_positive_layer_tunnels_are_not_lowered():
    for tags in ({"tunnel": "building_passage", "layer": "-1"}, {"tunnel": "building_passage"}):
        n = Net()
        a, b, c, d = n.node(-100), n.node(0), n.node(40), n.node(140)
        n.way(1, [a, b], highway="secondary")
        n.way(2, [b, c], highway="secondary", **tags)
        n.way(3, [c, d], highway="secondary")
        f = n.build()
        assert set(zs(f[2])) == {0.0}, tags
    # Kings Way style: a building passage at layer 2 between two layer-2 decks keeps the deck height
    n = Net()
    a, b, c, d, e, h = (n.node(x) for x in (-400, -100, 100, 150, 350, 650))
    n.way(1, [a, b], highway="trunk")
    n.way(2, [b, c], highway="trunk", bridge="yes", layer="2")
    n.way(3, [c, d], highway="trunk", tunnel="building_passage", layer="2")
    n.way(4, [d, e], highway="trunk", bridge="yes", layer="2")
    n.way(5, [e, h], highway="trunk")
    n.way(10, [n.node(125, -301), n.node(125, 299)])  # a ground road under the passage
    f = n.build()
    assert min(zs(f[3])) > 0
    assert z_at(f[3], 125, 0) >= DEFAULT_SEPARATION_M - EPS


# --- under water ------------------------------------------------------------------------------


def river(x0: float, x1: float, label: dict | None = None) -> tuple:
    ring = [ll(x0, -60), ll(x1, -60), ll(x1, 60), ll(x0, 60), ll(x0, -60)]
    return (ring, [], label if label is not None else {"natural": "water", "water": "river"})


def test_tunnel_under_a_river_stays_below_the_assumed_river_bed():
    """Layer -1 targets -10 m, but under a river the road must be <= water surface - river depth
    - cover - tunnel roof (UNDER_WATER_MAX_Z_M, -17 m): it goes deeper there, ramped."""
    n = tunnel_net(length=800.0)
    f = n.build(tunnel_water=[river(380, 420)])
    for x in range(380, 421, 5):
        assert z_at(f[2], x, 0) <= UNDER_WATER_MAX_Z_M + 0.01
    assert z_at(f[2], 200, 0) == -10.0  # back at its target away from the river
    assert max_grade(f[2]) <= MAX_GRADE + GRADE_SLACK
    (t,) = n.info["below_ground"]["tunnels"]
    assert t["under_water_vertices"] > 0 and t["under_water_max_z_m"] <= UNDER_WATER_MAX_Z_M
    assert t["under_water"][0]["water"] == "river"


def test_a_pond_above_a_tunnel_is_not_a_river():
    n = tunnel_net(length=800.0)
    f = n.build(tunnel_water=[river(380, 420, {"natural": "water", "water": "pond"})])
    assert min(zs(f[2])) == -10.0
    (t,) = n.info["below_ground"]["tunnels"]
    assert t["under_water_vertices"] == 0 and t["under_shallow_water"]


def test_river_next_to_a_portal_lowers_the_approach_ramp():
    """A river 20 m inside the portal: the tunnel is still below the bed there, so the approach
    road ramps down outside the portal (estimated), within the grade limits."""
    n = tunnel_net(length=800.0)
    f = n.build(tunnel_water=[river(20, 60)])
    assert z_at(f[2], 40, 0) <= UNDER_WATER_MAX_Z_M + 0.01
    assert min(zs(f[1])) < 0 and max_grade(f[1]) <= MAX_GRADE + GRADE_SLACK
    assert n.info["below_ground"]["water_caps_unmet"] == []


def test_river_bed_out_of_reach_of_a_held_portal_is_reported_not_forced():
    """With the portal held at street level (its outside road clipped from the extract), the
    tunnel goes as deep as the grade allows under the river, and the shortfall is reported."""
    n = tunnel_net(length=800.0)
    f = n.build(tunnel_water=[river(20, 60)], portal_nodes={n.ways[1].nodes[0]})
    assert z_at(f[2], 40, 0) == pytest.approx(-MAX_GRADE * 40, abs=0.05)
    assert set(zs(f[1])) == {0.0}
    unmet = n.info["below_ground"]["water_caps_unmet"]
    assert unmet and all(u["z_m"] > UNDER_WATER_MAX_Z_M for u in unmet)


# --- roads over tunnels -----------------------------------------------------------------------


def test_road_over_a_tunnel_near_its_portal_lowers_the_approach_just_enough():
    """St Kilda Rd over the City Road underpass, 20 m from the portal: the tunnel must be a full
    separation below it, so the approach road ramps down outside the portal (estimated)."""
    n = Net()
    a, b, c, d = n.node(-400), n.node(0), n.node(120), n.node(520)
    n.way(1, [a, b], highway="trunk")
    n.way(2, [b, c], highway="trunk", tunnel="yes", layer="-1")
    n.way(3, [c, d], highway="trunk")
    n.way(10, [n.node(20, -301), n.node(20, 299)])
    f = n.build()
    dz = z_at(f[10], 20, 0) - z_at(f[2], 20, 0)
    assert dz >= DEFAULT_SEPARATION_M - EPS
    assert min(zs(f[1])) < 0 and zs(f[1])[0] == 0.0  # the approach ramps, its far end stays
    assert max_grade(f[1]) <= MAX_GRADE + GRADE_SLACK
    assert f[1]["properties"]["z_source"] == "estimated"
    assert set(zs(f[10])) == {0.0}  # the road above is not touched
    bg = n.info["below_ground"]
    assert bg["approach_ways_lowered"] == [1]
    assert not [t for t in bg["tunnel_crossings"] if t["short_of_separation"]]


def test_railway_over_a_tunnel_counts():
    n = tunnel_net(length=120.0)
    n.rail(20, [n.node(30, -301), n.node(30, 299)])
    f = n.build()
    assert -z_at(f[2], 30, 0) >= DEFAULT_SEPARATION_M - EPS


def test_portal_whose_outside_road_was_clipped_stays_at_street_level():
    """Domain tunnel style: the east end of the tunnel way is its portal, but the road beyond it
    is outside the extract. Without the raw-OSM portal it would dangle at depth."""
    n = Net()
    a, b, c = n.node(-300), n.node(0), n.node(800)
    n.way(1, [a, b], highway="motorway")
    n.way(2, [b, c], highway="motorway", tunnel="yes", layer="-3")
    f0 = n.build()
    assert zs(f0[2])[-1] == -30.0
    f1 = n.build(portal_nodes={c})
    assert zs(f1[2])[-1] == 0.0 and min(zs(f1[2])) <= -23.0
    assert max_grade(f1[2]) <= MAX_GRADE + GRADE_SLACK


# --- the cutting (trench) ---------------------------------------------------------------------


def cutting_net(depth: float = 6.0, with_deck: bool = True):
    """Motorway way 10 (x -400..400) at grade, in a cutting from x -150 to 150 that is `depth`
    deep for |x| <= 50 and ramps to 0 at 4 %. Deck way 21 (layer 1) crosses it along y at
    x = 0. Returns (net, measure hook)."""
    n = Net()
    n.way(10, [n.node(-400), n.node(400)], highway="motorway")
    if with_deck:
        p, q, r, s = n.node(0, -400), n.node(0, -60), n.node(0.5, 60), n.node(0.5, 400)
        n.way(20, [p, q], highway="secondary")
        n.way(21, [q, r], highway="secondary", bridge="yes", layer="1")
        n.way(22, [r, s], highway="secondary")

    def prof(x: float) -> float:
        return max(0.0, depth - max(0.0, abs(x) - 50.0) * 0.04)

    def measure(g, crossings, sep=None):
        import math

        lon0 = ll(0)[0]
        kx = 111_320.0 * math.cos(math.radians(ll(0)[1]))
        depths = [round(prof((g.lon[v] - lon0) * kx), 2) for v in g.lines[10]]
        g.cutting_profiles = [{"kind": "cutting_profile", "way": 10, "used": True,
                               "depths_m": depths}]  # fmt: skip
        if sep is not None:
            for c in crossings:
                c.separation, c.source = sep, "pointcloud"
        return {}, []

    return n, measure


def test_cutting_is_lowered_to_its_measured_profile_and_labelled_measured():
    n, measure = cutting_net()
    f = n.build(measure)
    assert z_at(f[10], 0, 0) == pytest.approx(-6.0, abs=0.02)
    assert z_at(f[10], 100, 0) == pytest.approx(-4.0, abs=0.02)
    assert zs(f[10])[0] == zs(f[10])[-1] == 0.0  # ramped to 0 at the cutting ends
    assert max_grade(f[10]) <= MAX_GRADE + GRADE_SLACK
    assert f[10]["properties"]["z_source"] == "measured"
    # the deck was placed over the cutting at street level (Stage A); it now clears the floor
    dz = z_at(f[21], 0, 0) - z_at(f[10], 0, 0)
    assert dz >= DEFAULT_SEPARATION_M + 6.0 - EPS
    assert n.info["below_ground"]["pins_held"] > 0


def test_separation_measured_to_the_cutting_floor_is_drawn_as_measured():
    """A deck measured 9.0 m above the cutting FLOOR (6 m below street) stands 3.0 m above
    street level: Stage A holds 9.0 - 6.0 over the flat ground, Stage B lowers the floor."""
    n, measure = cutting_net()
    f = n.build(lambda g, cs: measure(g, cs, sep=9.0))
    assert z_at(f[21], 0, 0) - z_at(f[10], 0, 0) == pytest.approx(9.0, abs=EPS)
    assert z_at(f[21], 0, 0) == pytest.approx(3.0, abs=EPS)
    (c,) = n.info["crossings"]
    assert c.lower_depth == pytest.approx(6.0, abs=0.01)


def test_cutting_pin_steeper_than_the_grade_is_demoted_and_reported():
    """A measured cutting vertex 3 m deep 10 m from a raised deck end cannot hold at 6 %: it is
    drawn as close as the grade allows and listed."""
    n = Net()
    a, b, c = n.node(-300), n.node(-100), n.node(0)
    n.way(1, [a, b])
    n.way(2, [b, c], bridge="yes", layer="1")  # a primary bridge: the open-deck height, no floor
    n.way(10, [c, n.node(300)], highway="motorway")

    def measure(g, crossings):
        line = g.lines[10]
        g.cutting_profiles = [{"kind": "cutting_profile", "way": 10, "used": True,
                               "depths_m": [None, 3.0] + [None] * (len(line) - 2)}]  # fmt: skip
        return {}, []

    f = n.build(measure)
    bg = n.info["below_ground"]
    assert len(bg["pins_demoted"]) == 1
    assert bg["pins_demoted"][0]["final_z_m"] > -3.0
    assert zs(f[10])[1] == pytest.approx(OPEN_DECK_M - MAX_GRADE * 10, abs=0.02)


# --- contract validation of the extended tunnel clip ------------------------------------------


def test_validator_allows_only_motorway_tunnels_past_the_extract_east_edge():
    from pipeline.config import SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX
    from pipeline.roads_3d import validate_feature_collection

    east = round((SIM_EXTRACT_BBOX.east + 0.02 + TUNNEL_EXTRACT_BBOX.east) / 2, 6)
    lat = -37.825

    def fc(highway: str, tunnel: bool) -> dict:
        props = {"way_id": 1, "highway": highway, "layer": -3, "bridge": False, "tunnel": tunnel,
                 "name": None, "z_source": "estimated"}  # fmt: skip
        coords = [[144.97, lat, -10.0], [east, lat, -30.0]]
        geom = {"type": "LineString", "coordinates": coords}
        feat = {"type": "Feature", "geometry": geom, "properties": props}
        return {"type": "FeatureCollection", "features": [feat]}

    assert validate_feature_collection(fc("motorway", True)) == []
    assert validate_feature_collection(fc("motorway", False))
    assert validate_feature_collection(fc("primary", True))


def test_river_bed_limit_never_lifts_a_tunnel_that_is_already_deeper():
    """The bed limit and the target are both upper bounds: the tighter one wins (a layer -3
    tunnel at -30 m stays at -30 m under a river whose limit is -17 m)."""
    n = tunnel_net(length=1400.0, layer="-3")
    f = n.build(tunnel_water=[river(650, 750)])
    for x in range(600, 801, 10):
        assert z_at(f[2], x, 0) == -30.0
