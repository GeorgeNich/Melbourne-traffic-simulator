"""pipeline.deck_pointcloud on synthetic point clouds (no downloaded tiles needed).

Scenes are built in MGA metres: ground at z = 10, a deck along x at z = 10 + H over a road along
y. Like the aerial capture, nothing is seen under the deck.
"""

import math

import numpy as np
import pytest

from pipeline import deck_pointcloud as dp
from pipeline.roads_3d import Way, build_model

RNG = np.random.default_rng(7)
DECK_HW = 6.0  # deck half width in the scene (m)


def scene(
    deck_h: float | None = 8.0,
    vehicles: bool = True,
    lower_slope: float = 0.0,
    bus_at: tuple[float, float] | None = None,
    x0: float = 0.0,
    y0: float = 0.0,
) -> dp.PointIndex:
    """~10 pts/m2 over 200 x 200 m. Deck (if any) along x, |y - y0| <= DECK_HW, at 10 + deck_h;
    ground elsewhere at 10 + lower_slope * (y - y0)."""
    n = 400_000
    x = RNG.uniform(-100, 100, n) + x0
    y = RNG.uniform(-100, 100, n) + y0
    z = 10.0 + lower_slope * (y - y0) + RNG.normal(0, 0.03, n)
    if deck_h is not None:
        on = np.abs(y - y0) <= DECK_HW
        z[on] = 10.0 + deck_h + RNG.normal(0, 0.03, on.sum())
    if vehicles:  # cars: ~8 % of points 1.5 m up; lamp posts: a few points 8 m up
        car = RNG.random(n) < 0.08
        z[car] += 1.5
        post = RNG.random(n) < 0.005
        z[post] += 8.0
    if bus_at is not None:
        bx, by = bus_at
        bus = (np.abs(x - bx) <= 6) & (np.abs(y - by) <= 3)
        z[bus] += 3.2
    return dp.PointIndex.build(x, y, z.astype(np.float32))


def crossing(pts: dp.PointIndex, min_sep: float | None = None) -> dict:
    up = dp.Line([(-100.0, 0.0), (100.0, 0.0)])
    low = dp.Line([(0.0, -100.0), (0.0, 100.0)])
    shadow = dp.Shadow({1: up, 2: low}, {1: 1, 2: 0}, {1: DECK_HW, 2: 3.3})
    return dp.measure_crossing(pts, min_sep, up, DECK_HW, low, 3.3, 0.0, 0.0, 1, 0, shadow, [1, 2])


# --- the robust surface -----------------------------------------------------------------------


def test_surface_ignores_vehicles_and_lamp_posts():
    z = np.concatenate(
        [RNG.normal(20.0, 0.03, 400), RNG.normal(21.6, 0.2, 60), RNG.normal(28, 1, 5)]
    )
    s = dp.surface(z)
    assert s.z == pytest.approx(20.0, abs=0.03)
    assert s.n >= 390 and s.iqr < 0.1


def test_surface_rejects_sparse_and_mixed_boxes():
    assert dp.surface(np.full(5, 20.0)).z is None
    mixed = np.concatenate([np.full(100, 20.0), np.full(100, 24.0)])  # deck edge / roof
    assert dp.surface(mixed).z is None


def test_surface_rejects_rough_surfaces():
    tree = RNG.uniform(20, 20.8, 300)  # a crown: points everywhere within the inlier band
    assert dp.surface(tree).reason in ("not flat (inlier IQR too large)", "too few inliers")


# --- crossings --------------------------------------------------------------------------------


def test_crossing_separation_is_measured():
    rec = crossing(scene(8.0))
    assert rec["used"] and rec["separation_m"] == pytest.approx(8.0, abs=0.1)
    assert rec["deck_points"] >= dp.MIN_POINTS


def test_crossing_on_a_sloping_lower_road_interpolates():
    rec = crossing(scene(8.0, lower_slope=0.04))
    assert rec["used"] and rec["separation_m"] == pytest.approx(8.0, abs=0.15)


def test_deck_missing_from_the_2018_cloud_falls_back():
    rec = crossing(scene(None))  # e.g. West Gate Tunnel Project viaducts, built after 2018
    assert not rec["used"]
    assert rec["reason"].startswith("deck at ground level")


def test_a_bus_on_one_side_of_the_lower_road_is_caught():
    rec = crossing(scene(8.0, vehicles=False, bus_at=(0.0, 12.0 + 1.0)))
    assert not rec["used"]
    assert "disagree" in rec["reason"]


def test_separation_below_the_signed_clearance_is_rejected():
    rec = crossing(scene(4.0), min_sep=4.6)
    assert not rec["used"] and "signed clearance" in rec["reason"]


def test_deck_under_a_higher_way_is_not_measured():
    up = dp.Line([(-100.0, 0.0), (100.0, 0.0)])
    low = dp.Line([(0.0, -100.0), (0.0, 100.0)])
    top = dp.Line([(-100.0, -100.0), (100.0, 100.0)])  # a layer-2 deck over the crossing
    shadow = dp.Shadow({1: up, 2: low, 3: top}, {1: 1, 2: 0, 3: 2}, {1: DECK_HW, 2: 3.3, 3: 5})
    rec = dp.measure_crossing(
        scene(8.0), None, up, DECK_HW, low, 3.3, 0.0, 0.0, 1, 0, shadow, [1, 2, 3]
    )
    assert not rec["used"] and "occluded" in rec["reason"]


# --- open decks -------------------------------------------------------------------------------


def river_scene(rise: float) -> dp.PointIndex:
    """Banks at z = 10 for |x| > 60, water at 7 between; a deck along x over the water, 7 m wide,
    arching to 10 + rise at x = 0 (and meeting the banks at x = +-60)."""
    n = 500_000
    x = RNG.uniform(-200, 200, n)
    y = RNG.uniform(-50, 50, n)
    z = np.where(np.abs(x) > 60, 10.0, 7.0) + RNG.normal(0, 0.03, n)
    on = (np.abs(y) <= 3.5) & (np.abs(x) <= 60)
    z[on] = 10.0 + rise * (1 - (x[on] / 60) ** 2) + RNG.normal(0, 0.03, on.sum())
    return dp.PointIndex.build(x, y, z.astype(np.float32))


def test_open_deck_profile_is_measured_against_the_approaches():
    deck = dp.Line([(-60.0, 0.0), (60.0, 0.0)])
    # approach ground is sampled 50-90 m out, beyond any embankment
    west = dp.Line([(-200.0, 0.0), (-60.0, 0.0)])
    east = dp.Line([(60.0, 0.0), (200.0, 0.0)])
    shadow = dp.Shadow({1: deck, 2: west, 3: east}, {1: 1, 2: 0, 3: 0}, {1: 3.5, 2: 3.5, 3: 3.5})
    s = [i * 10.0 for i in range(13)]
    rec = dp.measure_open_deck(
        river_scene(1.2),
        deck,
        3.5,
        s,
        [[(west, west.length, -1)], [(east, 0.0, 1)]],
        1,
        shadow,
        [1, 2, 3],
    )
    assert rec["used"]
    assert rec["max_m"] == pytest.approx(1.2, abs=0.1)
    assert rec["heights_m"][0] == pytest.approx(0.0, abs=0.1)
    assert rec["ground_start"] == pytest.approx(10.0, abs=0.05)


def test_open_deck_with_a_jump_is_rejected():
    """Half the deck at the deck height, half at ground: two surfaces, not one deck."""
    n = 200_000
    x = RNG.uniform(-100, 100, n)
    y = RNG.uniform(-50, 50, n)
    z = np.full(n, 10.0) + RNG.normal(0, 0.03, n)
    z[(np.abs(y) <= 3.5) & (x > 0) & (x <= 60)] += 6.0
    pts = dp.PointIndex.build(x, y, z.astype(np.float32))
    deck = dp.Line([(-60.0, 0.0), (60.0, 0.0)])
    west = dp.Line([(-100.0, 0.0), (-60.0, 0.0)])
    shadow = dp.Shadow({1: deck, 2: west}, {1: 1, 2: 0}, {1: 3.5, 2: 3.5})
    rec = dp.measure_open_deck(
        pts, deck, 3.5, [i * 10.0 for i in range(13)], [[], []], 1, shadow, [1, 2], street=10.0
    )
    assert not rec["used"] and "not smooth" in rec["reason"]


# --- end to end through a LAS tile and roads_3d ---------------------------------------------------


def test_measure_all_from_a_las_tile_sets_a_measured_deck(tmp_path):
    laspy = pytest.importorskip("laspy")
    lon0, lat0 = 144.955, -37.82
    kx = 111_320.0 * math.cos(math.radians(lat0))

    def ll(x, y=0.0):
        return (lon0 + x / kx, lat0 + y / 111_320.0)

    coords = {}
    for i, (x, y) in enumerate([(-300, 0), (-50, 0), (50, 0), (300, 0), (3, -151), (3, 149)], 1):
        coords[i] = ll(x, y)
    ways = [
        Way(1, [1, 2], {"highway": "primary"}),
        Way(2, [2, 3], {"highway": "primary", "bridge": "yes", "layer": "1", "lanes": "2"}),
        Way(3, [3, 4], {"highway": "primary"}),
        Way(10, [5, 6], {"highway": "primary", "lanes": "2"}),
    ]
    # the scene, in MGA metres around the crossing point
    tr = dp.to_mga()
    cx, cy = tr.transform(*ll(3, 0))
    ex, ey = tr.transform(*ll(53, 0))
    ang = math.atan2(ey - cy, ex - cx)  # direction of the deck in MGA (grid convergence)
    n = 300_000
    u = RNG.uniform(-80, 80, n)  # along the deck
    v = RNG.uniform(-80, 80, n)  # across
    z = 10.0 + RNG.normal(0, 0.03, n)
    on = (np.abs(v) <= DECK_HW) & (np.abs(u) <= 50)
    z[on] += 7.5
    x = cx + u * math.cos(ang) - v * math.sin(ang)
    y = cy + u * math.sin(ang) + v * math.cos(ang)
    header = laspy.LasHeader(point_format=2, version="1.2")
    header.scales = [0.01, 0.01, 0.01]
    header.offsets = [float(np.floor(cx)), float(np.floor(cy)), 0.0]
    las = laspy.LasData(header)
    las.x, las.y, las.z = x, y, z
    tile = tmp_path / "Tile_test.las"
    las.write(tile)

    feats, info = build_model(ways, coords, [], {}, lambda g, cs: dp.measure_all(g, cs, [tile]))
    by = {f["properties"]["way_id"]: f for f in feats}
    (rec,) = [r for r in info["pointcloud_records"] if r["kind"] == "crossing"]
    assert rec["used"] and rec["separation_m"] == pytest.approx(7.5, abs=0.15)
    # the separation is an anchor: both ends of the crossing segment are held at it
    held = [a for a in info["anchors"] if a["way"] == 2]
    assert len(held) == 2
    assert all(abs(a["final_z_m"] - a["measured_z_m"]) <= 0.02 for a in held)
    # one measured point on a 100 m deck: `estimated` (needs half the raised vertices held)
    assert by[2]["properties"]["z_source"] == "estimated"
    assert max(c[2] for c in by[2]["geometry"]["coordinates"]) == pytest.approx(7.5, abs=0.15)
    assert by[10]["properties"]["z_source"] == "ground"


def test_cell_keys_round_trip_including_negative_indices():
    for i, j in [(0, 0), (-3, 7), (32_044, 581_219), (-1, -1)]:
        assert dp.cell_of_key(int(dp.cell_key(i, j))) == (i, j)


def trench_scene() -> dp.PointIndex:
    """Street level 10 m everywhere; a freeway cutting along y (|x| <= 8) with its floor at 4.7;
    a street deck along x (|y| <= 6) at 12.2, i.e. 2.2 m above street level."""
    n = 400_000
    x = RNG.uniform(-100, 100, n)
    y = RNG.uniform(-100, 100, n)
    z = 10.0 + RNG.normal(0, 0.03, n)
    cut = np.abs(x) <= 8
    z[cut] = 4.7 + RNG.normal(0, 0.03, cut.sum())
    deck = np.abs(y) <= DECK_HW
    z[deck] = 12.2 + RNG.normal(0, 0.03, deck.sum())
    return dp.PointIndex.build(x, y, z.astype(np.float32))


def test_street_level_is_the_median_of_nearby_at_grade_streets():
    pts = trench_scene()
    # stations on two streets along x at y = +-40, 5 m apart, plus one on the excluded freeway
    stations = [(float(x), yy, 1.0, 0.0, 1) for x in range(-50, 51, 10) for yy in (-40.0, 40.0)]
    stations += [(0.0, float(y), 0.0, 1.0, 2) for y in range(-50, 51, 10)]
    streets = dp.StreetLevel(stations, {1: 3.3, 2: 3.3})
    shadow = dp.Shadow({}, {}, {})
    z, n, radius = streets.at(pts, 0.0, 0.0, {2}, shadow, lambda *a: [])
    assert z == pytest.approx(10.0, abs=0.05) and n >= dp.STREET_MIN_STATIONS
    assert radius == dp.STREET_RADIUS_M
    assert streets.at(pts, 0.0, 0.0, {1, 2}, shadow, lambda *a: [])[0] is None


def test_crossing_over_a_cutting_is_measured_against_street_level():
    """Finding 2 (Power St over the CityLink cutting): the separation used is deck - street
    level (2.2 m), not deck - cutting floor (7.5 m); the cutting's depth is recorded."""
    pts = trench_scene()
    up = dp.Line([(-100.0, 0.0), (100.0, 0.0)])
    low = dp.Line([(0.0, -100.0), (0.0, 100.0)])
    shadow = dp.Shadow({1: up, 2: low}, {1: 1, 2: 0}, {1: DECK_HW, 2: 3.3})
    rec = dp.measure_crossing(
        pts, None, up, DECK_HW, low, 3.3, 0.0, 0.0, 1, 0, shadow, [1, 2], street=10.0
    )
    assert rec["used"]
    assert rec["separation_m"] == pytest.approx(2.2, abs=0.1)
    assert rec["separation_to_lower_surface_m"] == pytest.approx(7.5, abs=0.1)
    assert rec["trench_depth_m"] == pytest.approx(5.3, abs=0.1)
    # without a street level (not a freeway) it is the plain deck - lower surface separation
    rec = dp.measure_crossing(pts, None, up, DECK_HW, low, 3.3, 0.0, 0.0, 1, 0, shadow, [1, 2])
    assert rec["separation_m"] == pytest.approx(7.5, abs=0.1) and "trench_depth_m" not in rec


def test_top_surface_finds_a_deck_over_water_seen_past_its_edge():
    """The Bolte case: the box holds the deck (top) and water far below; the two-pass median
    sees mixed surfaces, the top-surface fallback finds the deck."""
    zs = np.concatenate([RNG.normal(30.0, 0.1, 300), RNG.normal(0.5, 0.05, 300)])
    assert dp.surface(zs).z is None
    s = dp.top_surface(zs)
    assert s.z == pytest.approx(30.0, abs=0.05)


def test_street_level_reaches_far_over_water_and_records_the_radius():
    """Finding 1 (Docklands): no street within 120 m; the nearest ones 400 m away are used."""
    n = 200_000
    x = RNG.uniform(350, 450, n)
    y = RNG.uniform(-20, 20, n)
    pts = dp.PointIndex.build(x, y, (2.29 + RNG.normal(0, 0.03, n)).astype(np.float32))
    stations = [(float(xx), 0.0, 1.0, 0.0, 1) for xx in range(380, 430, 10)]
    streets = dp.StreetLevel(stations, {1: 3.3})
    z, n_st, radius = streets.at(pts, 0.0, 0.0, set(), dp.Shadow({}, {}, {}), lambda *a: [])
    assert z == pytest.approx(2.29, abs=0.05) and radius == 500.0 and n_st >= 3
    assert streets.at(pts, -2000.0, 0.0, set(), dp.Shadow({}, {}, {}), lambda *a: [])[0] is None


def test_service_ramp_profile_may_be_thin_and_steep():
    """George 2026-10-26: a measured car-park ramp (Crown, ~11 %) with occluded stretches."""
    n = 300_000
    x = RNG.uniform(-100, 100, n)
    y = RNG.uniform(-30, 30, n)
    z = 10.0 + RNG.normal(0, 0.03, n)
    on = (np.abs(y) <= 2.0) & (np.abs(x) <= 45)
    z[on] = 10.0 + 2.4 + 0.11 * (x[on] + 45) + RNG.normal(0, 0.03, on.sum())
    hidden = on & (x > -20) & (x < 30)  # most of the ramp is hidden
    z[hidden] += RNG.uniform(3, 15, hidden.sum())
    pts = dp.PointIndex.build(x, y, z.astype(np.float32))
    deck = dp.Line([(-45.0, 0.0), (45.0, 0.0)])
    shadow = dp.Shadow({1: deck}, {1: 1}, {1: 2.5})
    s = [i * 10.0 for i in range(10)]
    kw = dict(street=10.0, over_roads=True)
    rec = dp.measure_open_deck(pts, deck, 2.5, s, [[], []], 1, shadow, [1], service=True, **kw)
    assert rec["used"], rec
    assert rec["heights_m"][0] == pytest.approx(2.4, abs=0.3)
    plain = dp.measure_open_deck(pts, deck, 2.5, s, [[], []], 1, shadow, [1], **kw)
    assert not plain["used"]


def test_top_surface_vertices_that_jump_are_dropped():
    """The Crown ramp 1132332719: the last vertex's top surface is a roof (21.5 m) while its
    neighbours read 2.7 m. Fallback (top-surface) vertices are kept only within the grade of the
    profile walked out from the road-surface vertices; road-surface vertices are never dropped."""
    from pipeline.deck_pointcloud import drop_fallback_jumps

    s = [0.0, 9.0, 18.0, 27.0, 36.0, 45.0]
    h = [11.7, 10.7, 9.6, 2.6, 2.6, 21.5]  # vertex 3 (road surface) jumps: left for the test
    fb = {0, 4, 5}
    assert drop_fallback_jumps(s, h, fb, 0.13) == 1
    assert h == [11.7, 10.7, 9.6, 2.6, 2.6, None]
    h2 = [5.0, 9.0, 5.0]
    assert drop_fallback_jumps([0.0, 10.0, 20.0], h2, {0, 1, 2}, 0.13) == 0  # no road surface
    h3 = [9.0, 5.0, 5.2, 9.0]
    assert drop_fallback_jumps([0.0, 10.0, 20.0, 30.0], h3, {0, 3}, 0.13) == 2
    assert h3 == [None, 5.0, 5.2, None]


# --- Stage B: cutting profiles ----------------------------------------------------------------


def cutting_scene(depth: float = 6.0) -> dp.PointIndex:
    """Street level z = 10 everywhere, except a freeway along x (|y| <= 6) in a cutting: its
    floor at 10 - depth for |x| <= 30, ramping up at 5 % to street level."""
    n = 400_000
    x = RNG.uniform(-200, 200, n)
    y = RNG.uniform(-50, 50, n)
    z = 10.0 + RNG.normal(0, 0.03, n)
    road = np.abs(y) <= 6
    d = np.clip(depth - np.maximum(np.abs(x[road]) - 30.0, 0) * 0.05, 0, None)
    z[road] = 10.0 - d + RNG.normal(0, 0.03, road.sum())
    car = RNG.random(n) < 0.05
    z[car] += 1.5
    return dp.PointIndex.build(x, y, z.astype(np.float32))


def test_cutting_profile_follows_the_floor_and_skips_hidden_vertices():
    pts = cutting_scene()
    line = dp.Line([(-180.0, 0.0), (180.0, 0.0)])
    bridge = dp.Line([(0.0, -50.0), (0.0, 50.0)])  # a street bridge over the cutting at x = 0
    shadow = dp.Shadow({1: line, 2: bridge}, {1: 0, 2: 1}, {1: 3.3, 2: 5.0})
    vs = [float(s) for s in range(0, 361, 10)]
    rec = dp.measure_cutting(pts, line, 3.3, vs, 0, shadow, lambda x, y: [1, 2], lambda x, y: 10.0)
    assert rec["used"] and rec["max_depth_m"] == pytest.approx(6.0, abs=0.1)
    depths = dict(zip(vs, rec["depths_m"], strict=True))
    assert depths[180.0] == pytest.approx(6.0, abs=0.1)  # under the bridge: interpolated
    assert not rec["measured"][vs.index(180.0)]
    assert depths[180.0 + 80] == pytest.approx(6.0 - 50 * 0.05, abs=0.1)  # x = 80, on the ramp
    assert depths[0.0] == pytest.approx(0.0, abs=0.1)  # beyond the ramp: street level


def test_shallow_road_is_not_a_cutting():
    pts = cutting_scene(depth=1.0)
    line = dp.Line([(-180.0, 0.0), (180.0, 0.0)])
    shadow = dp.Shadow({1: line}, {1: 0}, {1: 3.3})
    vs = [float(s) for s in range(0, 361, 10)]
    rec = dp.measure_cutting(pts, line, 3.3, vs, 0, shadow, lambda x, y: [1], lambda x, y: 10.0)
    assert not rec["used"] and "not in a cutting" in rec["reason"]
