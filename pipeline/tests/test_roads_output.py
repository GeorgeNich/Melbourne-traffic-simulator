"""Checks on the committed osm_sim_area.osm.gz and roads_3d.geojson. Skipped if absent."""

import json

import pytest

from pipeline import deck_pointcloud, roads, roads_3d
from pipeline.config import SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX


@pytest.fixture(scope="module")
def extract():
    if not roads.OUT_PATH.exists():
        pytest.skip("osm_sim_area.osm.gz not built")
    osmium = pytest.importorskip("osmium")
    nodes, ways, rels = {}, {}, {}
    for o in osmium.FileProcessor(str(roads.OUT_PATH)):
        if o.is_node():
            nodes[o.id] = (dict(o.tags), o.user, o.uid)
        elif o.is_way():
            ways[o.id] = ([n.ref for n in o.nodes], dict(o.tags))
        else:
            rels[o.id] = ([(m.type, m.ref, m.role) for m in o.members], dict(o.tags))
    return nodes, ways, rels


@pytest.fixture(scope="module")
def fc():
    if not roads_3d.OUT_PATH.exists():
        pytest.skip("roads_3d.geojson not built")
    return json.loads(roads_3d.OUT_PATH.read_text())


# --- osm_sim_area.osm.gz ----------------------------------------------------------------------


def test_extract_ways_are_drivable_with_known_tags(extract):
    nodes, ways, _ = extract
    assert len(ways) > 1000
    for refs, tags in ways.values():
        assert roads.is_drivable(tags), tags
        assert all(roads.keep_tag(k) for k in tags), tags
        assert all(r in nodes for r in refs)
    assert all(roads.keep_tag(k) for t, _, _ in nodes.values() for k in t)


def test_extract_has_signals_and_restrictions(extract):
    nodes, ways, rels = extract
    assert sum(1 for t, _, _ in nodes.values() if t.get("highway") == "traffic_signals") > 100
    assert len(rels) > 50
    for members, tags in rels.values():
        assert tags.get("type") == "restriction"
        for mtype, ref, _ in members:
            assert ref in (ways if mtype == "w" else nodes)


def test_extract_has_no_user_metadata(extract):
    nodes, _, _ = extract
    assert all(user == "" and uid == 0 for _, user, uid in nodes.values())


def test_extract_ways_touch_the_buffered_bbox(extract):
    # every way starts and ends at a node inside SIM_EXTRACT_BBOX (clip_span); CityLink motorway
    # tunnels inside TUNNEL_EXTRACT_BBOX (Stage B, the Yarra crossings)
    pytest.importorskip("osmium")
    import osmium

    loc = {}
    for o in osmium.FileProcessor(str(roads.OUT_PATH)).with_filter(
        osmium.filter.EntityFilter(osmium.osm.NODE)
    ):
        loc[o.id] = (o.location.lon, o.location.lat)
    _, ways, _ = extract
    for refs, tags in ways.values():
        box = roads.clip_box(tags, SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX)
        for end in (refs[0], refs[-1]):
            assert roads.inside(box, *loc[end])


def test_extract_extends_only_the_citylink_tunnels_to_the_yarra(extract):
    pytest.importorskip("osmium")
    import osmium

    loc = {}
    for o in osmium.FileProcessor(str(roads.OUT_PATH)).with_filter(
        osmium.filter.EntityFilter(osmium.osm.NODE)
    ):
        loc[o.id] = (o.location.lon, o.location.lat)
    _, ways, _ = extract
    past = {
        wid
        for wid, (refs, _) in ways.items()
        if any(loc[r][0] > SIM_EXTRACT_BBOX.east + 0.001 for r in refs)
    }
    assert {8066124, 8066126} <= past
    assert all(roads.extended_tunnel_clip(ways[w][1]) for w in past)
    # both cross the Yarra (~144.9784 Burnley, ~144.9797 Domain)
    assert max(loc[r][0] for r in ways[8066124][0]) > 144.985
    assert max(loc[r][0] for r in ways[8066126][0]) > 144.984


def test_extract_keeps_bus_lane_and_conditional_speed_tags(extract):
    _, ways, _ = extract
    keys = {k for _, tags in ways.values() for k in tags}
    assert {"bus:lanes", "lanes:bus", "busway", "maxspeed:conditional"} <= keys


# --- roads_3d.geojson -------------------------------------------------------------------------


def test_roads_3d_contract(fc):
    assert roads_3d.validate_feature_collection(fc) == []


def test_roads_3d_one_feature_per_extract_way(fc, extract):
    _, ways, _ = extract
    assert {f["properties"]["way_id"] for f in fc["features"]} == set(ways)


def test_roads_3d_smoothness(fc):
    s = roads_3d.smoothness(fc)
    assert s["max_step_m"] <= roads_3d.MAX_STEP_M
    assert s["shared_position_mismatches"] == 0
    assert s["max_spacing_m"] <= roads_3d.MAX_SPACING_M + 0.1
    # measured service ramps may be steeper (George 2026-10-26)
    assert s["max_grade"] <= roads_3d.SERVICE_RAMP_GRADE + 0.005
    assert s["pct_vertices_z0"] > 70


@pytest.fixture(scope="module")
def below():
    if not deck_pointcloud.REPORT_PATH.exists():
        pytest.skip("deck_pointcloud.json not built")
    return json.loads(deck_pointcloud.REPORT_PATH.read_text())["below_ground"]


def test_roads_3d_z_source_matches_heights(fc, below):
    counts = {"ground": 0, "estimated": 0, "measured": 0}
    allowed_below = (
        set(below["cutting_ways"])
        | set(below["approach_ways_lowered"])
        | {t["way"] for t in below["tunnels"]}
    )
    for f in fc["features"]:
        z = [c[2] for c in f["geometry"]["coordinates"]]
        p = f["properties"]
        counts[p["z_source"]] += 1
        if min(z) < 0:  # Stage B: tunnels, the CityLink cutting and ramps into tunnels only
            assert p["way_id"] in allowed_below, p
        assert (p["z_source"] == "ground") == all(v == 0 for v in z), p
    assert counts["measured"] > 50  # point cloud + maxheight anchors + the cutting
    assert counts["estimated"] > 50


def test_burnley_and_domain_tunnels_are_deep_and_below_the_yarra_bed(fc, below):
    t = {r["way"]: r for r in below["tunnels"]}
    for w in (8066124, 8066126):  # Burnley (eastbound), Domain (westbound): layer -3
        assert t[w]["min_z_m"] == -3 * roads_3d.TUNNEL_DEPTH_PER_LEVEL_M
        assert t[w]["under_water_vertices"] > 0
        assert t[w]["under_water_max_z_m"] <= roads_3d.UNDER_WATER_MAX_Z_M
        assert any(u.get("water") == "river" for u in t[w]["under_water"])
    assert below["water_caps_unmet"] == []
    zs = {f["properties"]["way_id"]: [c[2] for c in f["geometry"]["coordinates"]]
          for f in fc["features"] if f["properties"]["way_id"] in (8066124, 8066126)}  # fmt: skip
    assert zs[8066126][0] == 0.0  # the Domain tunnel's east portal (outside road clipped)
    assert not [r for r in below["tunnel_crossings"] if r["short_of_separation"]]


def test_tunnels_left_alone_keep_their_heights(fc):
    by_id = {f["properties"]["way_id"]: f for f in fc["features"]}
    for w in (12278361, 23212187):  # Kings Way, building_passage at layer 2: on the flyover
        assert min(c[2] for c in by_id[w]["geometry"]["coordinates"]) > 5.0
    for w in (734406093, 734406095):  # Lonsdale St, building_passage at layer -1: at grade
        assert {c[2] for c in by_id[w]["geometry"]["coordinates"]} == {0.0}


def test_power_street_clears_the_lowered_citylink_cutting():
    if not deck_pointcloud.REPORT_PATH.exists():
        pytest.skip("deck_pointcloud.json not built")
    rep = json.loads(deck_pointcloud.REPORT_PATH.read_text())
    assert rep["pending_trench_depth"] == []
    tc = [t for t in rep["trench_crossings"] if t["measured"]]
    assert len(tc) >= 9
    for t in tc:
        assert t["dz_m"] >= 4.0, t
        assert t["cutting_z_m"] <= -0.9 * t["trench_depth_m"], t


def test_roads_3d_every_bridge_is_off_the_ground(fc):
    for f in fc["features"]:
        if f["properties"]["bridge"]:
            assert max(c[2] for c in f["geometry"]["coordinates"]) > 0, f["properties"]


def test_kings_way_interchange_is_stacked(fc):
    # Kings Way / West Gate Freeway interchange: decks at more than one level
    box = (144.955, -37.832, 144.962, -37.826)
    tops = set()
    for f in fc["features"]:
        if f["properties"]["bridge"]:
            for lon, lat, z in f["geometry"]["coordinates"]:
                if box[0] <= lon <= box[2] and box[1] <= lat <= box[3]:
                    tops.add(z)
    assert max(tops) > 2 * 6.0  # a deck over a deck
    assert any(5.0 <= z <= 9.0 for z in tops)  # a single-level deck


def test_file_sizes_under_limit():
    for p in (roads.OUT_PATH, roads_3d.OUT_PATH):
        if p.exists():
            assert p.stat().st_size < 50 * 1024 * 1024
