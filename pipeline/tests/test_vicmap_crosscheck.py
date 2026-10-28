"""Synthetic tests for pipeline.checks.vicmap_crosscheck (no files, no network)."""

from __future__ import annotations

import json

from shapely.geometry import Point

from pipeline.checks import grade_separation as gs
from pipeline.checks import vicmap_crosscheck as vc
from pipeline.config import SIM_EXTRACT_BBOX

PROJ = gs.Projection.for_bbox(SIM_EXTRACT_BBOX)
LON0, LAT0 = 144.955, -37.820
D = 0.001


def xy(lon: float, lat: float) -> tuple[float, float]:
    return PROJ.xy(lon, lat)


def vline(pfi, ftype, name, a, b, from_ufi, to_ufi) -> vc.VLine:
    return vc.VLine(pfi, pfi, ftype, name, 2, from_ufi, to_ufi, None, None, [xy(*a), xy(*b)])


C = (LON0, LAT0)
W, E = (LON0 - D, LAT0), (LON0 + D, LAT0)
S, N = (LON0, LAT0 - D), (LON0, LAT0 + D)


def vicmap_cross(node_type: str, carried: str) -> tuple[list[vc.VLine], list[dict]]:
    """Vicmap: an east-west road (pfi 1, 2) and a north-south road (3, 4) meeting at node 100."""
    lines = [
        vline(1, "bridge" if carried == "ew" else "road", "Top Street", W, C, 10, 100),
        vline(2, "bridge" if carried == "ew" else "road", "Top Street", C, E, 100, 11),
        vline(3, "road", "Low Road", S, C, 12, 100),
        vline(4, "road", "Low Road", C, N, 100, 13),
    ]
    con = (1, 2) if carried == "ew" else (3, 4)
    infra = [
        {
            "properties": {
                "ufi": 100,
                "feature_type_code": node_type,
                "conpfi1": con[0],
                "conpfi2": con[1],
            }
        }
    ]
    return lines, infra


def osm_crossing(layer_ew: int, layer_ns: int) -> dict:
    return {
        "lon": LON0,
        "lat": LAT0,
        "class": "separated_ok",
        "way_a": 1000,
        "name_a": "Top Street",
        "way_b": 2000,
        "name_b": "Low Road",
        "effective_layer_a": layer_ew,
        "effective_layer_b": layer_ns,
        "z_a_m": 6.5 * max(layer_ew, 0),
        "z_b_m": 6.5 * max(layer_ns, 0),
        "review_area": None,
    }


OSM_XY = {1000: [xy(*W), xy(*E)], 2000: [xy(*S), xy(*N)]}


def order(lines, infra, crossing):
    nodes = vc.build_nodes(lines, infra, PROJ)
    idx = vc.SegmentIndex([v.xy for v in lines])
    (r,) = vc.crossing_order_records([crossing], OSM_XY, lines, idx, nodes, PROJ)
    return r


def test_bridge_node_puts_carried_road_on_top_and_agrees():
    lines, infra = vicmap_cross("bridge", carried="ew")
    r = order(lines, infra, osm_crossing(1, 0))
    assert r["class"] == "agree"
    assert r["vicmap_upper"] == "a"
    assert r["roads_3d_agrees_with_vicmap"] is True
    assert r["names_match"] is True


def test_tunnel_node_puts_carried_road_underneath():
    lines, infra = vicmap_cross("tunnel", carried="ns")
    r = order(lines, infra, osm_crossing(0, -1))
    assert r["vicmap_upper"] == "a"
    assert r["class"] == "agree"


def test_order_disagreement_is_reported():
    lines, infra = vicmap_cross("bridge", carried="ew")
    r = order(lines, infra, osm_crossing(0, 1))  # OSM puts the north-south road on top
    assert r["class"] == "disagree"


def test_conpfi_contradicting_segment_types_is_inconsistent():
    lines, infra = vicmap_cross("bridge", carried="ew")
    infra[0]["properties"].update(conpfi1=3, conpfi2=4)  # node says NS is carried, types say EW
    r = order(lines, infra, osm_crossing(1, 0))
    assert r["class"] == "vicmap_inconsistent"


def test_at_grade_vicmap_node_flags_missing_osm_junction():
    lines, infra = vicmap_cross("int_signal", carried="ew")
    r = order(lines, infra, osm_crossing(1, 0))
    assert r["class"] == "vicmap_at_grade_junction"


def test_no_vicmap_road_nearby():
    lines, infra = vicmap_cross("bridge", carried="ew")
    far = [vline(9, "road", "Elsewhere", (LON0 + 0.01, LAT0), (LON0 + 0.02, LAT0), 1, 2)]
    nodes = vc.build_nodes(far, [], PROJ)
    idx = vc.SegmentIndex([v.xy for v in far])
    (r,) = vc.crossing_order_records([osm_crossing(1, 0)], OSM_XY, far, idx, nodes, PROJ)
    assert r["class"] == "no_vicmap_match"


def test_match_respects_direction_and_name_preference():
    lines = [
        vline(1, "road", "Near Lane", (LON0 - D, LAT0 + 0.00002), (LON0 + D, LAT0 + 0.00002), 1, 2),
        vline(2, "road", "Main Street", (LON0 - D, LAT0 + 0.0001), (LON0 + D, LAT0 + 0.0001), 3, 4),
        vline(3, "road", "Cross Street", S, N, 5, 6),
    ]
    idx = vc.SegmentIndex([v.xy for v in lines])
    x, y = xy(*C)
    assert idx.match(x, y, 0.0)[0] == 0  # nearest east-west line
    assert idx.match(x, y, 90.0)[0] == 2  # direction picks the north-south line
    pref = idx.match(x, y, 0.0, prefer=lambda i: vc.same_name(lines[i].name, "Main Street"))
    assert pref[0] == 1


def test_same_name_is_loose_but_not_empty():
    assert vc.same_name("West Gate Tunnel", "West Gate Tunnel Tollway")
    assert vc.same_name("Kings-West Gate Out Ramp On", "Kings - West Gate Out Ramp On")
    assert not vc.same_name(None, "Kings Way")
    assert not vc.same_name("Collins Street", "Wurundjeri Way")


def test_vehicle_filter_drops_trails_and_closed_roads():
    assert vc.is_vehicle_road({"class_code": 2, "feature_type_code": "bridge"})
    assert not vc.is_vehicle_road({"class_code": 9, "feature_type_code": "trail"})
    assert not vc.is_vehicle_road({"class_code": 5, "feature_type_code": "foot_bridge"})
    assert not vc.is_vehicle_road(
        {"class_code": 5, "feature_type_code": "road", "restrictions": "4"}
    )


# --- clipping, structures, nodes ----------------------------------------------------------------


def osm_net(ways: list[tuple[int, list[int], dict[str, str]]], coords) -> gs.Network:
    w = {wid: gs.Way(wid, nodes, tags) for wid, nodes, tags in ways}
    lines = {wid: [[*coords[n], 0.0] for n in nodes] for wid, nodes, _ in ways}
    return gs.Network(w, coords, lines)


def osm_side(net: gs.Network):
    ids = sorted(net.ways)
    oxy = {w: [xy(*net.coords[n]) for n in net.ways[w].nodes] for w in ids}
    return ids, oxy, vc.SegmentIndex([oxy[w] for w in ids])


def test_read_vicmap_clips_to_the_extract_bbox(tmp_path):
    b = SIM_EXTRACT_BBOX
    lat = LAT0
    fc = {
        "type": "FeatureCollection",
        "features": [
            {  # crosses the east edge: kept, cut at the edge
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[b.east - D, lat], [b.east + D, lat]],
                },
                "properties": {
                    "pfi": 1,
                    "class_code": 2,
                    "feature_type_code": "tunnel",
                    "from_ufi": 10,
                    "to_ufi": 11,
                },
            },
            {  # wholly outside: dropped
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[b.east + D, lat], [b.east + 2 * D, lat]],
                },
                "properties": {"pfi": 2, "class_code": 2, "feature_type_code": "road"},
            },
            {  # a trail: not a vehicle road
                "geometry": {"type": "LineString", "coordinates": [W, E]},
                "properties": {"pfi": 3, "class_code": 9, "feature_type_code": "trail"},
            },
        ],
    }
    path = tmp_path / "roads.geojson"
    path.write_text(json.dumps(fc))
    lines, stats = vc.read_vicmap(path, PROJ)
    assert stats == {
        "features_read": 3,
        "vehicle_features": 2,
        "clipped_at_bbox_edge": 1,
        "outside_bbox_dropped": 1,
    }
    (v,) = lines
    assert v.clipped and v.pfi == 1
    clip = vc.bbox_polygon(b, PROJ)
    assert all(clip.buffer(0.01).covers(Point(p)) for p in v.xy)
    assert v.to_xy == xy(b.east + D, lat)  # the node keeps its true (outside) position


def test_osm_structure_records_matched_road_or_nothing():
    coords = {1: W, 2: E}
    net = osm_net([(1000, [1, 2], {"highway": "primary", "bridge": "yes", "layer": "1"})], coords)
    ids, oxy, _ = osm_side(net)
    for vic_type, expected in (("bridge", "matched"), ("road", "vicmap_not_bridge")):
        lines = [vline(1, vic_type, "Top Street", W, E, 10, 11)]
        idx = vc.SegmentIndex([v.xy for v in lines])
        (r,) = vc.osm_structure_records(net, oxy, lines, idx, PROJ)
        assert (r["check"], r["class"]) == ("osm_bridge", expected)
    far = [vline(1, "bridge", "X", (LON0 + 0.01, LAT0), (LON0 + 0.02, LAT0), 10, 11)]
    (r,) = vc.osm_structure_records(net, oxy, far, vc.SegmentIndex([far[0].xy]), PROJ)
    assert r["class"] == "no_vicmap_counterpart"


def test_vicmap_structure_records_classes():
    coords = {1: W, 2: E}
    vb = vline(1, "bridge", "Top Street", W, E, 10, 11)
    for tags, expected in (
        ({"highway": "primary", "bridge": "yes"}, "matched"),
        ({"highway": "primary"}, "osm_not_bridge"),
    ):
        net = osm_net([(1000, [1, 2], tags)], coords)
        ids, _, oidx = osm_side(net)
        (r,) = vc.vicmap_structure_records(net, ids, oidx, [vb], PROJ)
        assert r["class"] == expected
    far_net = osm_net([(1000, [1, 2], {"highway": "primary"})], {1: S, 2: N})  # crosses, not along
    ids, _, oidx = osm_side(far_net)
    (r,) = vc.vicmap_structure_records(far_net, ids, oidx, [vb], PROJ)
    assert r["class"] == "no_osm_counterpart"
    cut = vline(1, "tunnel", "Edge Tunnel", W, E, 10, 11)
    cut.clipped = True
    (r,) = vc.vicmap_structure_records(far_net, ids, oidx, [cut], PROJ)
    assert r["class"] == "extract_edge"


def test_named_node_near_finds_the_crossing_node_of_the_same_roads():
    lines, infra = vicmap_cross("bridge", carried="ew")
    nodes = vc.build_nodes(lines, infra, PROJ)
    # Matched segments that share no node: the far halves of each road ...
    va = vline(7, "bridge", "Top Street", (LON0 - 2 * D, LAT0), W, 20, 10)
    vb = vline(8, "road", "Low Road", (LON0, LAT0 - 2 * D), S, 21, 12)
    assert vc.shared_node_near(va, vb, nodes, *xy(*C)) is None
    node, sa, sb = vc.named_node_near(va, vb, nodes, lines, *xy(*C))
    assert node.ufi == 100
    assert sa.name == "Top Street" and sb.name == "Low Road"
    assert vc.vicmap_upper(node, sa, sb) == "a"


def test_structure_node_records_osm_separated_vs_osm_junction():
    lines, infra = vicmap_cross("bridge", carried="ew")
    nodes = vc.build_nodes(lines, infra, PROJ)
    top = {"highway": "primary", "name": "Top Street", "bridge": "yes", "layer": "1"}
    low = {"highway": "primary", "name": "Low Road"}
    # OSM agrees: the two roads cross without a shared node.
    net = osm_net([(1000, [1, 2], top), (2000, [3, 4], low)], {1: W, 2: E, 3: S, 4: N})
    ids, _, oidx = osm_side(net)
    crossings, _ = gs.crossing_records(net, PROJ)
    (r,) = vc.structure_node_records(net, ids, oidx, lines, nodes, crossings, PROJ)
    assert r["class"] == "both_grade_separated"
    assert (r["osm_carried_way"], r["osm_crossing_way"]) == (1000, 2000)
    # OSM joins them at a node where Vicmap has a bridge: flagged.
    coords = {1: W, 2: E, 3: S, 4: N, 5: C}
    net = osm_net([(1000, [1, 5, 2], top), (2000, [3, 5, 4], low)], coords)
    ids, _, oidx = osm_side(net)
    crossings, _ = gs.crossing_records(net, PROJ)
    (r,) = vc.structure_node_records(net, ids, oidx, lines, nodes, crossings, PROJ)
    assert r["class"] == "osm_junction_vicmap_grade_separated"
    assert r["osm_junction_node"] == 5


def test_structure_nodes_outside_the_bbox_are_skipped():
    b = SIM_EXTRACT_BBOX
    out = (b.east + D, LAT0)
    lines = [
        vline(1, "bridge", "Top Street", (b.east - D, LAT0), out, 10, 100),
        vline(3, "road", "Low Road", (b.east - D, LAT0 - D), out, 12, 100),
    ]
    infra = [{"properties": {"ufi": 100, "feature_type_code": "bridge", "conpfi1": 1}}]
    nodes = vc.build_nodes(lines, infra, PROJ)
    net = osm_net([(1000, [1, 2], {"highway": "primary"})], {1: W, 2: E})
    ids, _, oidx = osm_side(net)
    assert vc.structure_node_records(net, ids, oidx, lines, nodes, [], PROJ) == []


def test_extract_edge_needs_clipping_or_a_cut_osm_way_not_just_distance():
    """qa follow-up (Swan St Bridge pieces 17483888 / 17483890): a Vicmap bridge right next to
    the bbox edge, not clipped, whose matched OSM way is NOT cut there, is osm_not_bridge."""
    b = SIM_EXTRACT_BBOX
    west, east = (b.east - 3 * D / 10, LAT0), (b.east - D / 10, LAT0)  # ~10-25 m from the edge
    coords = {1: (b.east - 5 * D / 10, LAT0), 2: (b.east - D / 20, LAT0)}
    net = osm_net([(1000, [1, 2], {"highway": "trunk", "name": "Olympic Boulevard"})], coords)
    ids, _, oidx = osm_side(net)
    vb = vline(17483888, "bridge", "Olympic Boulevard", west, east, 10, 11)
    (r,) = vc.vicmap_structure_records(net, ids, oidx, [vb], PROJ, cut_ends={})
    assert r["distance_to_extract_edge_m"] < vc.MATCH_DIST_M
    assert r["class"] == "osm_not_bridge"
    # the same, but the matched OSM way was cut by the extract at that end: extract_edge
    cut = vc.osm_cut_ends(net, {1000: [99, 1, 2, 98]}, PROJ)
    assert set(cut) == {1000} and len(cut[1000]) == 2
    (r,) = vc.vicmap_structure_records(net, ids, oidx, [vb], PROJ, cut_ends=cut)
    assert r["class"] == "extract_edge" and r["osm_ways_cut_at_extract_edge"] == [1000]
    # a clipped Vicmap line is extract_edge regardless
    vb.clipped = True
    (r,) = vc.vicmap_structure_records(net, ids, oidx, [vb], PROJ, cut_ends={})
    assert r["class"] == "extract_edge"


def test_extract_edge_without_a_match_uses_a_nearby_cut_osm_end():
    far = osm_net([(1000, [1, 2], {"highway": "primary"})], {1: S, 2: N})  # crosses, not along
    ids, _, oidx = osm_side(far)
    vt = vline(5, "tunnel", "Edge Tunnel", W, E, 10, 11)
    (r,) = vc.vicmap_structure_records(far, ids, oidx, [vt], PROJ, cut_ends={})
    assert r["class"] == "no_osm_counterpart"
    cut = {1000: [xy(*C)]}  # its cut end sits on the Vicmap tunnel
    (r,) = vc.vicmap_structure_records(far, ids, oidx, [vt], PROJ, cut_ends=cut)
    assert r["class"] == "extract_edge"
