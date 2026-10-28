"""Synthetic tests for pipeline.checks.grade_separation (no files, no network)."""

from __future__ import annotations

import pytest

from pipeline.checks import grade_separation as gs
from pipeline.config import SIM_EXTRACT_BBOX

LON0, LAT0 = 144.955, -37.820  # middle of the sim area, far from the extract edge
D = 0.001  # ~88 m east-west, ~111 m north-south

PROJ = gs.Projection.for_bbox(SIM_EXTRACT_BBOX)


def net_of(ways: list[tuple[int, list[int], dict[str, str], float]], coords) -> gs.Network:
    """ways: (id, node ids, tags, constant z); roads_3d lines follow the nodes at that z."""
    w = {wid: gs.Way(wid, nodes, tags) for wid, nodes, tags, _ in ways}
    lines = {wid: [[*coords[n], z] for n in nodes] for wid, nodes, _, z in ways}
    return gs.Network(w, coords, lines)


def cross_coords(shared: bool = False) -> dict[int, tuple[float, float]]:
    """An east-west way (1-2) and a north-south way (3-4) crossing at the centre (node 5)."""
    c = {
        1: (LON0 - D, LAT0),
        2: (LON0 + D, LAT0),
        3: (LON0, LAT0 - D),
        4: (LON0, LAT0 + D),
    }
    if shared:
        c[5] = (LON0, LAT0)
    return c


GROUND = {"highway": "primary"}
BRIDGE1 = {"highway": "primary", "bridge": "yes", "layer": "1"}
TUNNEL = {"highway": "primary", "tunnel": "yes", "layer": "-1"}


def records(net: gs.Network):
    recs, _ = gs.crossing_records(net, PROJ)
    return recs


def test_crossing_at_different_layers_is_ok():
    net = net_of([(10, [1, 2], BRIDGE1, 6.5), (20, [3, 4], GROUND, 0.0)], cross_coords())
    recs = records(net)
    assert len(recs) == 1
    r = recs[0]
    assert r["class"] == "separated_ok"
    assert r["kind"] == "cross"
    assert r["z_a_m"] == 6.5 and r["z_b_m"] == 0.0
    assert r["dz_upper_minus_lower_m"] == 6.5
    assert r["lon"] == pytest.approx(LON0, abs=1e-6)
    assert r["lat"] == pytest.approx(LAT0, abs=1e-6)


def test_same_layer_crossing_is_flagged():
    net = net_of([(10, [1, 2], GROUND, 0.0), (20, [3, 4], GROUND, 0.0)], cross_coords())
    (r,) = records(net)
    assert r["class"] == "same_layer_no_junction"
    assert r["dz_upper_minus_lower_m"] is None


def test_order_mismatch_inverted_and_flat():
    # The layer-1 bridge sits below the ground road: inverted.
    net = net_of([(10, [1, 2], BRIDGE1, 2.0), (20, [3, 4], GROUND, 8.0)], cross_coords())
    (r,) = records(net)
    assert (r["class"], r["detail"]) == ("order_mismatch", "inverted")
    assert r["dz_upper_minus_lower_m"] == -6.0
    # Both at the same height although the layers differ: flat.
    net = net_of([(10, [1, 2], BRIDGE1, 0.0), (20, [3, 4], GROUND, 0.0)], cross_coords())
    (r,) = records(net)
    assert (r["class"], r["detail"]) == ("order_mismatch", "flat")


def test_low_clearance_is_separated_low():
    net = net_of([(10, [1, 2], BRIDGE1, 3.0), (20, [3, 4], GROUND, 0.0)], cross_coords())
    (r,) = records(net)
    assert r["class"] == "separated_low"


def test_shared_node_is_not_a_crossing():
    coords = cross_coords(shared=True)
    net = net_of([(10, [1, 5, 2], GROUND, 0.0), (20, [3, 5, 4], GROUND, 0.0)], coords)
    recs, stats = gs.crossing_records(net, PROJ)
    assert recs == []
    assert stats["shared_node_touches_ignored"] == 1


def test_end_touching_a_way_without_shared_node_is_a_touch():
    coords = cross_coords()
    coords[6] = (LON0, LAT0)  # end of the side road lies on way 10, but is not one of its nodes
    net = net_of([(10, [1, 2], GROUND, 0.0), (20, [3, 6], GROUND, 0.0)], coords)
    (r,) = records(net)
    assert (r["kind"], r["class"]) == ("touch", "same_layer_no_junction")


def test_duplicate_nodes_at_one_position_are_coincident():
    coords = cross_coords()
    coords[5], coords[6] = (LON0, LAT0), (LON0, LAT0)  # two node ids, one position
    net = net_of([(10, [1, 5, 2], GROUND, 0.0), (20, [3, 6, 4], GROUND, 0.0)], coords)
    (r,) = records(net)
    assert r["kind"] == "coincident"


def test_tunnel_left_at_street_level_under_a_road_is_an_order_mismatch():
    """Stage B gives tunnels depth: one still at z = 0 under a ground road is a real error."""
    net = net_of([(10, [1, 2], TUNNEL, 0.0), (20, [3, 4], GROUND, 0.0)], cross_coords())
    (r,) = records(net)
    assert (r["class"], r["stage_a_class"]) == ("order_mismatch", "order_mismatch")


def test_lowered_tunnel_under_a_road_is_separated():
    net = net_of([(10, [1, 2], TUNNEL, -10.0), (20, [3, 4], GROUND, 0.0)], cross_coords())
    (r,) = records(net)
    assert r["class"] == "separated_ok" and r["dz_upper_minus_lower_m"] == 10.0


def test_building_passage_is_not_pending_and_tunnel_layer_defaults_below():
    bp = {"highway": "service", "tunnel": "building_passage"}
    assert gs.effective_layer(bp) == 0 and not gs.pending_tunnel(bp)
    assert gs.effective_layer({"highway": "primary", "tunnel": "yes"}) == -1
    assert gs.effective_layer({"highway": "primary", "bridge": "yes"}) == 1  # roads_3d rule
    assert gs.effective_layer({"highway": "primary", "bridge": "no", "layer": "2"}) == 2


def test_service_service_pairs_are_noise_candidates_not_dropped():
    svc = {"highway": "service"}
    net = net_of([(10, [1, 2], svc, 0.0), (20, [3, 4], svc, 0.0)], cross_coords())
    (r,) = records(net)
    assert r["noise_candidate"] is True
    assert r["class"] == "same_layer_no_junction"


def test_dangling_bridge_end_is_flagged():
    coords = {
        1: (LON0 - D, LAT0),
        2: (LON0, LAT0),
        3: (LON0 + D, LAT0),
        4: (LON0 - 2 * D, LAT0),
    }
    # Bridge 10 runs 1 -> 3: its start (node 1) joins ground road 20, its end joins nothing.
    net = net_of([(10, [1, 2, 3], BRIDGE1, 6.5), (20, [4, 1], GROUND, 0.0)], coords)
    ends = {e["end"]: e for e in gs.bridge_end_records(net, PROJ)}
    assert ends["start"]["class"] == "connected"
    assert ends["start"]["connected_ways"] == [20]
    assert ends["end"]["class"] == "dangling_bridge_end"
    assert ends["end"]["node_id"] == 3


def test_bridge_end_clipped_at_extract_edge_is_not_dangling():
    edge_lon = SIM_EXTRACT_BBOX.east - 0.0001
    coords = {1: (edge_lon - 3 * D, LAT0), 2: (edge_lon, LAT0)}
    net = net_of([(10, [1, 2], BRIDGE1, 6.5)], coords)
    ends = {e["end"]: e for e in gs.bridge_end_records(net, PROJ)}
    assert ends["end"]["class"] == "extract_edge"
    assert ends["start"]["class"] == "dangling_bridge_end"
    # With the regional extract, "clipped" means the raw way continues past this node.
    raw = gs.RawContext(way_nodes={10: [1, 2]}, node_ways={})
    ends = {e["end"]: e for e in gs.bridge_end_records(net, PROJ, raw=raw)}
    assert ends["end"]["class"] == "dangling_bridge_end"


def test_mid_deck_junction_is_reported():
    coords = cross_coords(shared=True)
    net = net_of([(10, [1, 5, 2], BRIDGE1, 6.5), (20, [3, 5], GROUND, 0.0)], coords)
    (m,) = gs.mid_structure_junction_records(net)
    assert (m["way_a"], m["way_b"], m["node_id"]) == (10, 20, 5)


def test_flags_geojson_has_only_flagged_points():
    report = {
        "crossings": [
            {"class": "separated_ok", "lon": 1.0, "lat": 2.0},
            {"class": "order_mismatch", "lon": 3.0, "lat": 4.0, "connected": [1]},
        ],
        "bridge_ends": [
            {"class": "connected", "lon": 5.0, "lat": 6.0},
            {"class": "dangling_bridge_end", "lon": 7.0, "lat": 8.0},
        ],
        "mid_structure_junctions": [],
    }
    fc = gs.flags_geojson(report)
    assert [f["properties"]["class"] for f in fc["features"]] == [
        "order_mismatch",
        "dangling_bridge_end",
    ]
    assert fc["features"][0]["geometry"] == {"type": "Point", "coordinates": [3.0, 4.0]}
    assert fc["features"][0]["properties"]["connected"] == "[1]"


def test_read_raw_context_lists_every_kind_of_way_at_a_node(tmp_path):
    pytest.importorskip("osmium")
    osm = tmp_path / "raw.osm"
    nodes = "".join(
        f'<node id="{i}" version="1" lat="{LAT0 + i * 1e-4}" lon="{LON0}"/>' for i in range(1, 7)
    )
    osm.write_text(
        '<?xml version="1.0" encoding="UTF-8"?><osm version="0.6">'
        + nodes
        + '<way id="10" version="1"><nd ref="1"/><nd ref="2"/><nd ref="3"/>'
        '<tag k="highway" v="service"/><tag k="bridge" v="yes"/></way>'
        '<way id="20" version="1"><nd ref="3"/><nd ref="4"/><nd ref="5"/><nd ref="3"/>'
        '<tag k="building" v="yes"/><tag k="amenity" v="parking"/><tag k="name" v="Car Park"/>'
        '<tag k="note" v="not a context tag"/></way>'
        '<way id="30" version="1"><nd ref="3"/><nd ref="6"/><tag k="barrier" v="fence"/></way>'
        '<way id="40" version="1"><nd ref="5"/><nd ref="6"/><tag k="railway" v="rail"/></way>'
        "</osm>"
    )
    raw = gs.read_raw_context(osm, way_ids={10}, node_ids={3})
    assert raw.way_nodes == {10: [1, 2, 3]}
    at3 = {r["way_id"]: r["tags"] for r in raw.node_ways[3]}
    assert set(at3) == {10, 20, 30}  # the bridge itself, a building and a fence; not the railway
    assert at3[20] == {"name": "Car Park", "building": "yes", "amenity": "parking"}
    assert at3[30] == {"barrier": "fence"}


def test_lift_source_names_the_deck_that_lifts_the_lower_road():
    # Ground road 20 (nodes 2 -> 3) starts at the end of a layer-2 deck 10 (1 -> 2) and passes
    # under layer-1 deck 30 (4 -> 5) about 88 m further east.
    coords = {
        1: (LON0 - 2 * D, LAT0),
        2: (LON0 - D, LAT0),
        3: (LON0 + D, LAT0),
        4: (LON0, LAT0 - D),
        5: (LON0, LAT0 + D),
    }
    deck2 = {"highway": "trunk", "bridge": "yes", "layer": "2"}
    deck1 = {"highway": "motorway", "bridge": "yes", "layer": "1"}
    net = net_of(
        [(10, [1, 2], deck2, 13.0), (20, [2, 3], GROUND, 8.0), (30, [4, 5], deck1, 6.5)], coords
    )
    (r,) = records(net)
    assert (r["class"], r["detail"]) == ("order_mismatch", "inverted")
    gs.add_lift_sources(net, PROJ, [r])
    assert r["lifted_by_way"] == 10 and r["lifted_by_deck_z_m"] == 13.0
    d = PROJ.xy(LON0, LAT0)[0] - PROJ.xy(LON0 - D, LAT0)[0]
    assert r["lift_distance_m"] == pytest.approx(d, abs=0.5)
    # From 13 m down to 4 m under the 6.5 m deck: 10.5 m over ~88 m.
    assert r["grade_needed"] == pytest.approx((13.0 - (6.5 - gs.CLEARANCE_M)) / d, abs=0.002)


def test_low_crossing_over_a_listed_cutting_is_pending_trench_depth():
    """A deck measured ~2 m above street level over a freeway in a cutting (still at z = 0 in
    Stage A) is pending_trench_depth when roads_3d lists it, separated_low otherwise."""
    net = net_of([(10, [1, 2], BRIDGE1, 2.2), (20, [3, 4], GROUND, 0.0)], cross_coords())
    trench = [{"upper_way": 10, "lower_way": 20, "lon": LON0, "lat": LAT0, "trench_depth_m": 5.3}]
    (r,) = gs.crossing_records(net, PROJ, trenches=trench)[0]
    assert (r["class"], r["stage_a_class"]) == ("pending_trench_depth", "separated_low")
    assert r["trench_depth_m"] == 5.3
    (r,) = records(net)
    assert r["class"] == "separated_low"
    far = [{**trench[0], "lon": LON0 + 0.01}]  # listed, but somewhere else
    (r,) = gs.crossing_records(net, PROJ, trenches=far)[0]
    assert r["class"] == "separated_low"
    assert "pending_trench_depth" in gs.CLASSES
