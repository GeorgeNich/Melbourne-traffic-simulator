"""Stacked-tier build steps in pipeline/buildings.py on small inline fixtures (no raw files)."""

import json

import pytest
from shapely.geometry import Point, box, shape

from pipeline.buildings import (
    ROAD_CLEARANCE_M,
    Void,
    carve,
    clean_geometry,
    drop_stale_over_roads,
    is_underground,
    match_osm_to_com,
    osm_min_height,
    osm_voids,
    road_voids,
    to_feature,
    validate_feature_collection,
)
from pipeline.config import BBox

BB = BBox(west=144.950, south=-37.820, east=144.960, north=-37.810)
X, Y = 144.955, -37.815
M = 1 / 100_000  # ~1 m in degrees


def sq(half_m, dx_m=0.0, dy_m=0.0):
    cx, cy = X + dx_m * M, Y + dy_m * M
    return box(cx - half_m * M, cy - half_m * M, cx + half_m * M, cy + half_m * M)


def piece(fid, geom, h, base=0.0, src="osm_height"):
    return (fid, geom, {"height_m": h, "levels": None, "height_source": src, "min_height_m": base})


# --- underground exclusion ------------------------------------------------------------------


@pytest.mark.parametrize(
    "tags, under",
    [
        ({"building:part": "yes", "level": "-2"}, True),  # Metro Tunnel station box
        ({"building:part": "yes", "layer": "-1", "level": "-1"}, True),
        ({"building:part": "yes", "level": "-3;-1"}, True),
        ({"building": "yes", "location": "underground"}, True),
        ({"building": "yes", "level": "-1;0"}, False),  # reaches street level: drawn
        ({"building": "yes", "level": "1"}, False),
        ({"building": "yes", "layer": "-1"}, False),  # layer alone is not a level
        ({"building": "yes"}, False),
    ],
)
def test_underground_only_parts(tags, under):
    assert is_underground(tags) is under


def test_osm_min_height_from_tag_or_min_level():
    assert osm_min_height({"min_height": "4.5"}) == 4.5
    assert osm_min_height({"building:min_level": "2"}) == pytest.approx(6.4)
    assert osm_min_height({"min_height": "5 m", "building:min_level": "9"}) == 5.0
    assert osm_min_height({}) is None


# --- OSM voids and carving -------------------------------------------------------------------


def test_sky_bridge_is_lifted_but_tower_on_podium_is_not_a_void():
    items = [
        piece("w1", sq(5, dx_m=-20), 30),  # building west
        piece("w2", sq(5, dx_m=20), 30),  # building east
        piece("w3", box(X - 15 * M, Y - 2 * M, X + 15 * M, Y + 2 * M), 14),  # bridge over lane
        piece("w4", sq(20, dy_m=60), 20),  # podium
        piece("w5", sq(5, dy_m=60), 150),  # tower part min_height 20 over the podium
    ]
    tags = {"w3": {"min_height": "5"}, "w5": {"min_height": "20"}}
    voids = osm_voids(items, tags, inside=None)
    assert [v.source for v in voids] == ["osm:w3"]
    out, st = carve(items, voids, BB)
    by = {fid: p for fid, _, p in out}
    assert by["w3"]["min_height_m"] == 5.0  # the bridge floats over the lane
    assert by["w5"]["min_height_m"] == 0.0  # (towers are de-overlapped from the ground)
    assert st["pieces_carved"] >= 1


def test_carve_splits_a_ground_piece_under_a_void_and_keeps_ids_unique():
    items = [piece("c1_1", sq(20), 16.5, src="com_pointcloud_2018")]
    void = Void(box(X - 20 * M, Y - 3 * M, X + 20 * M, Y + 3 * M), 4.5, "road:1", "c1_1")
    out, _ = carve(items, [void], BB)
    ids = [fid for fid, _, _ in out]
    assert ids == ["c1_1", "c1_1-v1"]
    lifted = out[1][2]
    assert lifted["min_height_m"] == 4.5 and lifted["height_m"] == 16.5
    assert lifted["height_source"] == "com_pointcloud_2018"
    assert out[0][2]["min_height_m"] == 0.0


def test_carve_removes_a_low_piece_entirely_under_a_void():
    items = [piece("w1", sq(3), 3.0)]
    void = Void(sq(10), 4.5, "osm:w9")
    out, _ = carve(items, [void], BB)
    assert out == []


def test_carve_respects_eligibility_and_applies():
    items = [piece("w1", sq(10), 20), piece("c1_1", sq(10, dx_m=40), 20)]
    voids = [Void(sq(60), 5.0, "osm:w9")]
    out, _ = carve(items, voids, BB, eligible=lambda g: g.centroid.x < X + 20 * M)
    by = {fid: p for fid, _, p in out}
    # w1 lies wholly over the void: its only fragment starts at the clearance and keeps its id
    assert set(by) == {"w1", "c1_1"}
    assert by["w1"]["min_height_m"] == 5.0 and by["c1_1"]["min_height_m"] == 0.0
    out, _ = carve(
        items,
        voids,
        BB,
        applies=lambda v, fid: not fid.startswith("c") or v.source.startswith("road:"),
    )
    by = {fid: p for fid, _, p in out}
    assert by["c1_1"]["min_height_m"] == 0.0  # CoM tiers are not carved by OSM voids


# --- roads through buildings -----------------------------------------------------------------


def _road(way_id, coords, highway="unclassified", tunnel=False):
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {"way_id": way_id, "highway": highway, "tunnel": tunnel},
    }


def test_road_through_a_building_gets_clearance_and_osm_bridge_wins():
    lane = [[X - 40 * M, Y, 0.0], [X + 40 * M, Y, 0.0]]
    fc = {"type": "FeatureCollection", "features": [_road(7, lane)]}
    pieces = [piece("c1_1", sq(20), 16.5)]
    voids = road_voids(fc, {7: {"highway": "unclassified", "lanes": "1"}}, pieces, [])
    assert voids and all(v.clearance_m == ROAD_CLEARANCE_M for v in voids)
    assert voids[0].piece_id == "c1_1" and voids[0].highway == "unclassified"
    # under an OSM bridge (min_height 4.0) the whole open space under it is cleared at 4.0
    bridge = Void(box(X - 5 * M, Y - 8 * M, X + 5 * M, Y + 8 * M), 4.0, "osm:w3")
    voids = road_voids(fc, {7: {"highway": "unclassified", "lanes": "1"}}, pieces, [bridge])
    assert {v.clearance_m for v in voids} == {4.0, ROAD_CLEARANCE_M}
    under = next(v for v in voids if v.clearance_m == 4.0)
    assert under.geom.area == pytest.approx(bridge.geom.area, rel=1e-6)
    # maxheight tag sets the clearance; tunnels and raised roads are ignored
    voids = road_voids(fc, {7: {"highway": "service", "maxheight": "3.2"}}, pieces, [])
    assert {v.clearance_m for v in voids} == {3.2}
    raised = [[X - 40 * M, Y, 6.0], [X + 40 * M, Y, 6.0]]
    fc2 = {"type": "FeatureCollection", "features": [_road(8, raised), _road(9, lane, tunnel=True)]}
    assert road_voids(fc2, {}, pieces, []) == []


def test_road_just_grazing_a_building_corner_is_not_a_passage():
    graze = [[X - 40 * M, Y + 19.5 * M, 0.0], [X - 18 * M, Y + 21 * M, 0.0]]
    fc = {"type": "FeatureCollection", "features": [_road(7, graze)]}
    assert road_voids(fc, {}, [piece("c1_1", sq(20), 10)], []) == []


def test_stale_com_structure_over_a_street_is_dropped_only_without_osm():
    com = [piece("c5_1", sq(20), 17), piece("c5_2", sq(5), 25, base=17)]
    v = Void(sq(3), 4.5, "road:1", "c5_1", "unclassified")
    kept, rv, stale = drop_stale_over_roads(com, [v], osm_pieces=[piece("w1", sq(5, 200), 9)])
    assert kept == [] and rv == [] and stale[0]["structure"] == "c5"
    kept, _, stale = drop_stale_over_roads(com, [v], osm_pieces=[piece("w1", sq(19), 9)])
    assert len(kept) == 2 and stale == []
    alley = Void(sq(3), 4.5, "road:1", "c5_1", "service")  # car-park aisle / alley: carved
    kept, _, stale = drop_stale_over_roads(com, [alley], osm_pieces=[])
    assert len(kept) == 2 and stale == []


# --- CoM replaces OSM inside the municipality ------------------------------------------------


def test_osm_replaced_trimmed_or_kept():
    cover = sq(20)
    inside = lambda g: g.representative_point().x < X + 100 * M  # noqa: E731
    osm = [
        piece("w1", sq(18), 30),  # covered: replaced by CoM
        piece("w2", box(X + 10 * M, Y - 5 * M, X + 40 * M, Y + 5 * M), 12),  # 1/3 covered
        piece("w3", sq(5, dx_m=60), 9),  # no CoM: kept
        piece("w4", sq(18, dx_m=200), 9),  # outside the municipality: untouched
    ]
    out, st = match_osm_to_com(osm, cover, inside, BB)
    by = {fid: g for fid, g, _ in out}
    assert "w1" not in by and {"w2", "w3", "w4"} <= set(by)
    assert not by["w2"].intersection(cover).area > 1e-12  # trimmed to outside the CoM cover
    assert st == {
        "inside_com_replaced_by_com": 1,
        "inside_com_osm_only_trimmed": 1,
        "inside_com_osm_only": 1,
        "outside_com": 1,
    }


# --- extent clipping and the validator -------------------------------------------------------


def test_clip_to_an_area_polygon():
    area = Point(X, Y).buffer(30 * M)
    g = clean_geometry(sq(20, dx_m=25), area)
    assert g is not None and area.buffer(1e-6).contains(g)
    assert clean_geometry(sq(5, dx_m=100), area) is None


def test_validator_min_height_and_area():
    area = sq(50)
    ok = to_feature(
        "c1_1",
        clean_geometry(sq(10), BB),
        {
            "height_m": 30.0,
            "levels": None,
            "height_source": "com_pointcloud_2018",
            "min_height_m": 20.0,
        },
    )
    fc = {"type": "FeatureCollection", "features": [ok]}
    assert validate_feature_collection(fc, BB, area) == []
    for bad in (30.0, -1.0, None, True):
        f = json.loads(json.dumps(ok))
        f["properties"]["min_height_m"] = bad
        assert validate_feature_collection({"type": "FeatureCollection", "features": [f]}, BB)
    out = to_feature("w1", clean_geometry(sq(5, dx_m=200), BB), dict(ok["properties"]))
    errs = validate_feature_collection({"type": "FeatureCollection", "features": [out]}, BB, area)
    assert any("outside the building area" in e for e in errs)
    missing = json.loads(json.dumps(ok))
    del missing["properties"]["min_height_m"]
    assert validate_feature_collection({"type": "FeatureCollection", "features": [missing]}, BB)
    assert shape(ok["geometry"]).is_valid
