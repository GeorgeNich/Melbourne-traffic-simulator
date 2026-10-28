"""Unit tests for pipeline.roads (no network, no raw data needed)."""

import math

import pytest

from pipeline.config import SIM_BBOX, SIM_BUFFER_M, SIM_EXTRACT_BBOX, BBox, buffered
from pipeline.roads import (
    RawData,
    clip_box,
    clip_span,
    extended_tunnel_clip,
    filter_tags,
    is_drivable,
    keep_tag,
    select,
    write_osm,
)

BOX = BBox(144.95, -37.82, 144.96, -37.81)


# --- buffered bbox ----------------------------------------------------------------------------


def test_buffer_is_about_300_m_each_side():
    assert pytest.approx(300.0) == SIM_BUFFER_M
    b = SIM_EXTRACT_BBOX
    assert b.contains(SIM_BBOX)
    mid = math.radians((SIM_BBOX.south + SIM_BBOX.north) / 2)
    assert (SIM_BBOX.south - b.south) * 111_320 == pytest.approx(300, abs=1)
    assert (b.north - SIM_BBOX.north) * 111_320 == pytest.approx(300, abs=1)
    assert (SIM_BBOX.west - b.west) * 111_320 * math.cos(mid) == pytest.approx(300, abs=1)
    assert (b.east - SIM_BBOX.east) * 111_320 * math.cos(mid) == pytest.approx(300, abs=1)


def test_buffered_zero_is_identity():
    assert buffered(SIM_BBOX, 0) == pytest.approx(SIM_BBOX)


# --- tag filter -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hw",
    [
        "motorway",
        "trunk",
        "primary",
        "secondary",
        "tertiary",
        "motorway_link",
        "trunk_link",
        "primary_link",
        "secondary_link",
        "tertiary_link",
        "unclassified",
        "residential",
        "living_street",
        "service",
    ],
)
def test_car_roads_kept(hw):
    assert is_drivable({"highway": hw})


@pytest.mark.parametrize(
    "hw",
    [
        "footway",
        "cycleway",
        "path",
        "steps",
        "pedestrian",
        "busway",
        "construction",
        "proposed",
        "track",
        "bus_guideway",
    ],
)
def test_non_car_ways_dropped(hw):
    assert not is_drivable({"highway": hw})


@pytest.mark.parametrize(
    "tags,ok",
    [
        ({"highway": "service", "access": "no"}, False),
        ({"highway": "tertiary", "motor_vehicle": "no"}, False),
        ({"highway": "tertiary", "motorcar": "no"}, False),
        ({"highway": "tertiary", "vehicle": "no"}, False),
        # the most specific key wins
        ({"highway": "service", "access": "no", "motor_vehicle": "yes"}, True),
        ({"highway": "service", "access": "yes", "motorcar": "no"}, False),
        ({"highway": "service", "access": "private"}, True),
        ({"highway": "service", "area": "yes"}, False),
    ],
)
def test_motor_access(tags, ok):
    assert is_drivable(tags) is ok


def test_tag_keys_kept():
    tags = {
        "highway": "primary",
        "name": "Kings Way",
        "ref": "1",
        "oneway": "yes",
        "lanes": "3",
        "lanes:forward": "2",
        "maxspeed": "60",
        "turn:lanes": "left|through",
        "turn:lanes:backward": "through",
        "bridge": "yes",
        "tunnel": "no",
        "layer": "1",
        "junction": "roundabout",
        "access": "yes",
        "motor_vehicle": "yes",
        "motorcar": "yes",
        "surface": "asphalt",
        "source": "survey",
        "cycleway": "lane",
        "note": "x",
    }
    kept = filter_tags(tags)
    assert set(kept) == set(tags) - {"surface", "source", "cycleway", "note"}
    assert keep_tag("restriction") and keep_tag("type") and keep_tag("restriction:motorcar")


def test_bus_lane_and_conditional_speed_tags_kept_exactly():
    """sim-engineer's Stage C request: bus lanes and conditional limits, kept as tagged."""
    tags = {
        "highway": "primary",
        "bus:lanes": "|designated",
        "bus:lanes:forward": "designated|",
        "bus:lanes:backward": "|designated",
        "bus:lanes:conditional": "designated @ (Mo-Fr 07:00-09:00)",
        "bus:lanes:forward:conditional": "designated @ (Mo-Fr 07:00-09:00)",
        "bus:lanes:backward:conditional": "designated @ (Mo-Fr 16:00-18:30)",
        "lanes:bus": "1",
        "lanes:bus:forward": "1",
        "lanes:bus:backward:conditional": "1 @ (Mo-Fr 07:00-09:00)",
        "busway": "lane",
        "busway:left": "lane",
        "busway:right": "lane",
        "busway:both": "lane",
        "maxspeed:conditional": "40 @ (Mo-Fr 07:00-19:00)",
        "busway:designation": "x",  # not a lane tag: dropped
        "maxspeed:type": "AU:urban",  # not requested: dropped
    }
    kept = filter_tags(tags)
    assert set(kept) == set(tags) - {"busway:designation", "maxspeed:type"}
    assert kept == {k: tags[k] for k in kept}  # values exactly as tagged


# --- the extended CityLink tunnel clip (Stage B) ------------------------------------------------

TBOX = BBox(BOX.west, BOX.south, 144.97, BOX.north)  # like TUNNEL_EXTRACT_BBOX: east edge further


def test_extended_tunnel_clip_is_motorway_tunnels_only():
    assert extended_tunnel_clip({"highway": "motorway", "tunnel": "yes"})
    assert extended_tunnel_clip({"highway": "motorway_link", "tunnel": "yes", "layer": "-2"})
    assert not extended_tunnel_clip({"highway": "motorway"})
    assert not extended_tunnel_clip({"highway": "trunk", "tunnel": "yes"})
    assert not extended_tunnel_clip({"highway": "motorway", "tunnel": "building_passage"})
    assert clip_box({"highway": "motorway", "tunnel": "yes"}, BOX, TBOX) is TBOX
    assert clip_box({"highway": "motorway", "tunnel": "yes"}, BOX, None) is BOX
    assert clip_box({"highway": "primary"}, BOX, TBOX) is BOX


def test_motorway_tunnels_are_clipped_at_the_wider_east_edge():
    raw = RawData()
    line = [(1, 144.955, -37.815), (2, 144.959, -37.815), (3, 144.963, -37.815),
            (4, 144.968, -37.815), (5, 144.975, -37.815)]  # fmt: skip
    raw.ways = [
        (10, line, {"highway": "motorway", "tunnel": "yes", "layer": "-3"}),
        (20, [(n + 10, lo, la - 0.001) for n, lo, la in line], {"highway": "motorway"}),
        # entirely east of BOX: only a tunnel may come in through the wider clip
        (30, [(41, 144.962, -37.812), (42, 144.966, -37.812)],
         {"highway": "motorway", "tunnel": "yes"}),
    ]  # fmt: skip
    ex = select(raw, BOX, TBOX)
    assert ex.ways[10][0] == [1, 2, 3, 4]  # 144.975 is outside TBOX
    assert ex.ways[20][0] == [11, 12]  # the ordinary motorway stops at BOX
    assert ex.ways[30][0] == [41, 42]
    assert ex.stats["extended_tunnel_clip_ways"] == 2
    assert ex.stats["extended_tunnel_clip_ways_added"] == 1
    assert ex.stats["extended_tunnel_clip_nodes_added"] == 2 + 2
    # without the tunnel bbox, the old clip
    ex0 = select(raw, BOX)
    assert ex0.ways[10][0] == [1, 2] and 30 not in ex0.ways


# --- clipping and restrictions ----------------------------------------------------------------


def test_clip_span_keeps_first_to_last_inside_node():
    nodes = [
        (1, 144.94, -37.815),  # outside (west)
        (2, 144.951, -37.815),
        (3, 144.97, -37.815),  # outside, between two inside nodes: kept
        (4, 144.955, -37.815),
        (5, 144.99, -37.815),  # outside
    ]
    assert [n[0] for n in clip_span(nodes, BOX)] == [2, 3, 4]
    assert clip_span([(1, 144.94, -37.815), (2, 144.951, -37.815)], BOX) == []
    assert clip_span([(1, 144.94, -37.815), (2, 144.94, -37.816)], BOX) == []


def _raw() -> RawData:
    raw = RawData()
    # way 10: n1-n2-n3 (all inside); way 20: n3-n4 (inside); way 30: n5 inside, n6 outside
    raw.ways = [
        (10, [(1, 144.951, -37.815), (2, 144.952, -37.815), (3, 144.953, -37.815)], {
            "highway": "primary", "surface": "asphalt"}),
        (20, [(3, 144.953, -37.815), (4, 144.953, -37.816)], {"highway": "residential"}),
        (30, [(5, 144.959, -37.815), (6, 144.97, -37.815)], {"highway": "residential"}),
    ]  # fmt: skip
    raw.node_tags = {2: {"highway": "traffic_signals"}, 99: {"highway": "traffic_signals"}}
    raw.restrictions = [
        # kept: from 10 via n3 to 20
        (100, [("w", 10, "from"), ("n", 3, "via"), ("w", 20, "to")],
         {"type": "restriction", "restriction": "no_right_turn"}),
        # dropped: `to` way not in the extract
        (101, [("w", 10, "from"), ("n", 3, "via"), ("w", 77, "to")],
         {"type": "restriction", "restriction": "no_left_turn"}),
        # dropped: way 30 was clipped away (single node inside)
        (102, [("w", 30, "from"), ("n", 5, "via"), ("w", 20, "to")],
         {"type": "restriction", "restriction": "no_u_turn"}),
        # dropped: no via member
        (103, [("w", 10, "from"), ("w", 20, "to")],
         {"type": "restriction", "restriction": "no_u_turn"}),
    ]  # fmt: skip
    return raw


def test_select_keeps_restriction_with_all_members():
    ex = select(_raw(), BOX)
    assert set(ex.ways) == {10, 20}
    assert set(ex.relations) == {100}
    assert ex.relations[100][0] == [("w", 10, "from"), ("n", 3, "via"), ("w", 20, "to")]
    assert ex.ways[10][1] == {"highway": "primary"}  # unlisted tags dropped
    assert ex.node_tags == {2: {"highway": "traffic_signals"}}  # node 99 is not used
    assert set(ex.nodes) == {1, 2, 3, 4}
    assert ex.stats["restrictions_dropped"] == 3


def test_written_osm_reads_back(tmp_path):
    osmium = pytest.importorskip("osmium")
    ex = select(_raw(), BOX)
    out = tmp_path / "x.osm.gz"
    write_osm(ex, out, BOX)
    nodes, ways, rels = {}, {}, {}
    for o in osmium.FileProcessor(str(out)):
        if o.is_node():
            nodes[o.id] = (dict(o.tags), o.user)
        elif o.is_way():
            ways[o.id] = [n.ref for n in o.nodes]
        else:
            rels[o.id] = dict(o.tags)
    assert set(nodes) == {1, 2, 3, 4}
    assert nodes[2][0] == {"highway": "traffic_signals"}
    assert all(user == "" for _, user in nodes.values())  # no OSM user names
    assert ways == {10: [1, 2, 3], 20: [3, 4]}
    assert rels == {100: {"type": "restriction", "restriction": "no_right_turn"}}
