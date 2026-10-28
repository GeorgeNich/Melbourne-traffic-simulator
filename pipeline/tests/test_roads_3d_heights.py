"""Clearance-based height rules of pipeline.roads_3d on small synthetic networks (no raw data).

Networks are laid out in metres on a local grid near the CBD and converted to lon/lat.
"""

import math

import pytest

from pipeline.roads_3d import (
    DECK_THICKNESS_M,
    DEFAULT_SEPARATION_M,
    MAX_GRADE,
    MAX_SPACING_M,
    MAX_STEP_M,
    OPEN_DECK_M,
    SERVICE_RAMP_GRADE,
    SERVICE_RAMP_SPACING_M,
    SHORT_LINK_GRADE,
    Way,
    build_model,
    effective_layer,
    is_bridge,
    is_tunnel,
    measured_clearance,
    metres,
    parse_layer,
    parse_maxheight,
    smoothness,
    validate_feature_collection,
)

LON0, LAT0 = 144.955, -37.82
K = 111_320.0
EPS = 0.03  # z rounding (0.01), 6-decimal lon/lat rounding, interpolation at the crossing


def ll(x: float, y: float = 0.0) -> tuple[float, float]:
    return (LON0 + x / (K * math.cos(math.radians(LAT0))), LAT0 + y / K)


class Net:
    def __init__(self) -> None:
        self.coords: dict[int, tuple[float, float]] = {}
        self.ways: list[Way] = []
        self.rails: list[Way] = []
        self._next = 1

    def node(self, x: float, y: float = 0.0) -> int:
        nid = self._next
        self._next += 1
        self.coords[nid] = ll(x, y)
        return nid

    def way(self, wid: int, nodes: list[int], **tags: str) -> None:
        tags.setdefault("highway", "primary")
        self.ways.append(Way(wid, nodes, tags))

    def rail(self, wid: int, nodes: list[int], **tags: str) -> None:
        tags.setdefault("railway", "rail")
        self.rails.append(Way(wid, nodes, tags, rail=True))

    def build(self, measure=None, water=None, **stage_b) -> dict[int, dict]:
        feats, self.info = build_model(
            self.ways, self.coords, self.rails, {}, measure, water, **stage_b
        )
        fc = {"type": "FeatureCollection", "features": feats}
        assert validate_feature_collection(fc, bbox=_box()) == []
        sm = smoothness(fc)
        assert sm["max_step_m"] <= MAX_STEP_M
        assert sm["shared_position_mismatches"] == 0
        self.fc = fc
        return {f["properties"]["way_id"]: f for f in feats}


def _box():
    from pipeline.config import BBox

    return BBox(144.9, -37.9, 145.0, -37.7)


def zs(f: dict) -> list[float]:
    return [c[2] for c in f["geometry"]["coordinates"]]


def z_at(f: dict, x: float, y: float) -> float:
    """z of a feature at the point of its line nearest local (x, y), interpolated."""
    lon, lat = ll(x, y)
    c = f["geometry"]["coordinates"]
    best = (math.inf, 0.0)
    for a, b in zip(c, c[1:], strict=False):
        kx = math.cos(math.radians(LAT0)) * K
        ax, ay = (a[0] - lon) * kx, (a[1] - lat) * K
        bx, by = (b[0] - lon) * kx, (b[1] - lat) * K
        dx, dy = bx - ax, by - ay
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / L2))
        d = math.hypot(ax + t * dx, ay + t * dy)
        if d < best[0]:
            best = (d, a[2] + t * (b[2] - a[2]))
    return best[1]


def max_grade(f: dict) -> float:
    c = f["geometry"]["coordinates"]
    g = 0.0
    for a, b in zip(c, c[1:], strict=False):
        d = metres(a[0], a[1], b[0], b[1])
        if d >= 5:
            g = max(g, abs(b[2] - a[2]) / d)
    return g


def deck_over(lower_tags: dict | None = None, *, rail: bool = False) -> tuple[Net, dict]:
    """Way 2: a 100 m layer-1 bridge (x 0..100) with ground approaches 1 and 3; it crosses way
    10 (a ground road, or railway) along y at x = 55 without a shared node (vertices offset so
    no two lines have a vertex at the same position)."""
    n = Net()
    a, b, c, d = n.node(-300), n.node(0), n.node(100), n.node(400)
    n.way(1, [a, b])
    n.way(2, [b, c], bridge="yes", layer="1")
    n.way(3, [c, d])
    s, t = n.node(55, -301), n.node(55, 299)
    if rail:
        n.rail(10, [s, t])
    else:
        n.way(10, [s, t], **(lower_tags or {}))
    return n, n.build()


# --- tags -------------------------------------------------------------------------------------


def test_parse_layer():
    assert parse_layer(None) == 0
    assert parse_layer("2") == 2
    assert parse_layer("-1") == -1
    assert parse_layer("1;2") == 1
    assert parse_layer("high") == 0


def test_bridge_and_tunnel_values():
    assert is_bridge({"bridge": "yes"}) and is_bridge({"bridge": "viaduct"})
    assert not is_bridge({"bridge": "no"}) and not is_bridge({})
    assert is_tunnel({"tunnel": "yes"}) and is_tunnel({"tunnel": "building_passage"})
    assert not is_tunnel({"tunnel": "no"})


def test_effective_layer_matches_the_grade_separation_rule():
    assert effective_layer({"bridge": "yes"}) == 1
    assert effective_layer({"bridge": "yes", "layer": "-1"}) == 1
    assert effective_layer({"tunnel": "yes"}) == -1
    assert effective_layer({"tunnel": "building_passage", "layer": "2"}) == 2
    assert effective_layer({"layer": "3"}) == 3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("4.3", 4.3),
        ("4.3 m", 4.3),
        ("4.3m", 4.3),
        ("4,3", 4.3),
        ("14'6\"", 14 * 0.3048 + 6 * 0.0254),
        ("14 ft", 14 * 0.3048),
        ("default", None),
        ("below_default", None),
        ("none", None),
        ("no", None),
        ("unsigned", None),
        ("4.3;4.5", None),
        ("0", None),
        (None, None),
    ],
)
def test_parse_maxheight(raw, expected):
    got = parse_maxheight(raw)
    assert got == (pytest.approx(expected) if expected is not None else None)


def test_measured_clearance_prefers_physical_then_maxheight_then_signed():
    assert measured_clearance({"maxheight": "4.0", "maxheight:physical": "4.4"}) == (
        4.4,
        "maxheight:physical",
    )
    assert measured_clearance({"maxheight": "default", "maxheight:signed": "4.1"}) == (
        4.1,
        "maxheight:signed",
    )
    assert measured_clearance({"maxheight": "default"}) is None


# --- rule 1: crossings -------------------------------------------------------------------------


def test_deck_over_a_ground_road_is_default_separation():
    n, f = deck_over()
    assert z_at(f[2], 55, 0) == pytest.approx(DEFAULT_SEPARATION_M, abs=EPS)
    assert set(zs(f[10])) == {0.0}
    # no higher than needed: the deck peaks at the separation, and ramps fall at <= 6 %
    assert max(zs(f[2])) == pytest.approx(DEFAULT_SEPARATION_M, abs=EPS)
    for w in (1, 2, 3):
        assert max_grade(f[w]) <= MAX_GRADE + 0.003
    assert min(zs(f[1])) == 0.0 and min(zs(f[3])) == 0.0
    assert f[2]["properties"]["z_source"] == "estimated"
    assert f[10]["properties"]["z_source"] == "ground"
    (c,) = [c for c in n.info["crossings"] if c.dropped is None]
    assert (c.upper, c.lower, c.source) == (2, 10, "default")


def short_deck_over(lower_tags: dict | None = None, measure=None) -> tuple[Net, dict]:
    """Way 2: a 20 m bridge (x 45..65) over way 10 at x = 55: one crossing holds most of it."""
    n = Net()
    a, b, c, d = n.node(-300), n.node(45), n.node(65), n.node(400)
    n.way(1, [a, b])
    n.way(2, [b, c], bridge="yes", layer="1")
    n.way(3, [c, d])
    n.way(10, [n.node(55, -301), n.node(55, 299)], **(lower_tags or {}))
    return n, n.build(measure)


def test_measured_maxheight_anchor_sets_the_deck_and_z_source():
    _, f = short_deck_over({"maxheight": "4.3 m"})
    assert z_at(f[2], 55, 0) == pytest.approx(4.3 + DECK_THICKNESS_M, abs=EPS)
    assert f[2]["properties"]["z_source"] == "measured"
    assert f[10]["properties"]["z_source"] == "ground"
    assert f[1]["properties"]["z_source"] == "estimated"  # the ramp is the grade rule


def test_z_source_measured_needs_half_the_raised_vertices_held():
    """Orchestrator decision: one measured point on a long deck does not make it `measured`."""
    _, f = deck_over({"maxheight": "4.3 m"})  # 100 m deck, one crossing
    assert z_at(f[2], 55, 0) == pytest.approx(4.3 + DECK_THICKNESS_M, abs=EPS)
    assert f[2]["properties"]["z_source"] == "estimated"


def test_non_numeric_maxheight_falls_back_to_the_default():
    _, f = deck_over({"maxheight": "default"})
    assert z_at(f[2], 55, 0) == pytest.approx(DEFAULT_SEPARATION_M, abs=EPS)
    assert f[2]["properties"]["z_source"] == "estimated"


def test_railway_beneath_counts_and_is_not_written_out():
    n, f = deck_over(rail=True)
    assert 10 not in f
    assert z_at(f[2], 55, 0) == pytest.approx(DEFAULT_SEPARATION_M, abs=EPS)


def test_deck_over_a_deck_is_stacked():
    """Way 1 ground (along x), way 3 a layer-1 deck over it (along y), way 4 a layer-2 deck over
    both at the same point (diagonal)."""
    n = Net()
    n.way(1, [n.node(-401), n.node(399)])
    p0, p1, p2, p3 = (n.node(3, y) for y in (-300, -60, 60, 300))
    n.way(2, [p0, p1])
    n.way(3, [p1, p2], bridge="yes", layer="1")
    n.way(5, [p2, p3])
    n.way(4, [n.node(-117, -125), n.node(123, 115)], bridge="yes", layer="2")
    f = n.build()
    assert set(zs(f[1])) == {0.0}
    z3 = z_at(f[3], 3, 0)
    assert z3 == pytest.approx(DEFAULT_SEPARATION_M, abs=EPS)
    assert z_at(f[4], 3, 0) >= z3 + DEFAULT_SEPARATION_M - EPS
    assert z_at(f[4], 3, 0) == pytest.approx(2 * DEFAULT_SEPARATION_M, abs=0.7)


def test_kings_way_style_flyover_then_under_a_lower_layer_deck():
    """A layer-2 flyover (way 2) over a ground road ends; its road (way 3, layer 0) carries on and
    70 m later passes UNDER a layer-1 deck (way 20). The layer-1 deck must end up above it."""
    n = Net()
    a, b, c, d = n.node(-300), n.node(-100), n.node(0), n.node(400)
    n.way(1, [a, b])
    n.way(2, [b, c], bridge="yes", layer="2")
    n.way(3, [c, d])  # Kings Way at ground layer, still ramping down from the flyover
    n.way(10, [n.node(-23, -201), n.node(-23, 199)])  # City Road under the flyover
    s0, s1, s2, s3 = (n.node(73, y) for y in (-401, -61, 59, 399))
    n.way(19, [s0, s1])
    n.way(20, [s1, s2], bridge="yes", layer="1", highway="motorway")  # West Gate deck
    n.way(21, [s2, s3])
    f = n.build()
    z_fly = z_at(f[2], -23, 0)
    z_kw = z_at(f[3], 73, 0)
    z_wg = z_at(f[20], 73, 0)
    assert z_fly == pytest.approx(DEFAULT_SEPARATION_M, abs=EPS)
    assert z_kw > 0  # still on the flyover's ramp
    assert z_wg >= z_kw + DEFAULT_SEPARATION_M - EPS  # the lower-layer deck is on top
    assert not [c for c in n.info["crossings"] if c.dropped]


def test_no_sag_between_two_crossings_on_one_deck():
    n = Net()
    a, b, c, d = n.node(-300), n.node(0), n.node(300), n.node(600)
    n.way(1, [a, b])
    n.way(2, [b, c], bridge="yes", layer="1")
    n.way(3, [c, d])
    n.way(10, [n.node(33, -101), n.node(33, 99)])
    n.way(11, [n.node(267, -101), n.node(267, 99)])
    f = n.build()
    z = zs(f[2])
    i0 = len(z) // 10
    middle = z[i0 + 1 : len(z) - i0 - 1]
    assert min(middle) >= DEFAULT_SEPARATION_M - EPS  # the deck spans flat, no dip to 1.5 m


def open_bridge(layer: str = "1", highway: str = "motorway") -> Net:
    n = Net()
    a, b, c, d = n.node(-300), n.node(0), n.node(100), n.node(400)
    n.way(1, [a, b], highway=highway)
    n.way(2, [b, c], bridge="yes", layer=layer, highway=highway)  # nothing beneath it
    n.way(3, [c, d], highway=highway)
    return n


def test_open_deck_over_water_gets_the_open_deck_height():
    n = open_bridge()
    f = n.build(water=[[ll(55, -200), ll(55, 200)]])  # a river centreline under the deck
    assert set(zs(f[2])) == {OPEN_DECK_M}
    assert max_grade(f[1]) <= MAX_GRADE + 0.003
    assert zs(f[1])[0] == 0.0
    assert f[2]["properties"]["z_source"] == "estimated"
    assert n.info["layer_floors"]["not_floored"]["over_water"] == [2]


@pytest.mark.parametrize("layer", ["1", "2"])
def test_bare_freeway_deck_over_nothing_gets_the_layer_floor(layer):
    """Rule 7 (post-2018 viaducts): freeway class, no measurement, nothing beneath, no water."""
    n = open_bridge(layer)
    f = n.build()
    floor = int(layer) * DEFAULT_SEPARATION_M  # no point cloud here: constant separation
    assert max(zs(f[2])) == pytest.approx(floor, abs=EPS)
    assert f[2]["properties"]["z_source"] == "estimated"
    assert n.info["layer_floors"]["full"] == {"2": round(floor, 2)}
    assert max_grade(f[1]) <= MAX_GRADE + 0.003


@pytest.mark.parametrize("highway", ["primary", "residential", "service"])
def test_layer_floor_only_on_freeway_class_decks(highway):
    n = open_bridge("1", highway)
    f = n.build()
    assert set(zs(f[2])) == {OPEN_DECK_M}
    assert n.info["layer_floors"]["not_floored"]["not_freeway"] == [2]


def test_layer_floor_cannot_lift_a_measured_deck():
    """A bare freeway deck (way 2) under a measured layer-2 deck (way 20, 8 m over it): its
    floor is an estimate, so the measured deck caps it and it stays at the open-deck height."""

    def measure(g, crossings):
        for c in crossings:
            if c.upper == 20:
                c.separation, c.source = 8.0, "pointcloud"
        return {}, []

    n = open_bridge()
    n.way(19, [n.node(55, -401), n.node(55, -61)])
    n.way(20, [n.ways[-1].nodes[1], n.node(55, 59)], bridge="yes", layer="2")
    n.way(21, [n.ways[-1].nodes[1], n.node(55, 399)])
    f = n.build(measure)
    assert z_at(f[2], 55, 0) == pytest.approx(OPEN_DECK_M, abs=EPS)
    assert z_at(f[20], 55, 0) == pytest.approx(OPEN_DECK_M + 8.0, abs=EPS)
    assert n.info["precedence"]["anchors_moved"] == 0


def test_layer_floor_skips_decks_already_held_up_by_the_rules():
    n = Net()
    a, b, c, d, e = n.node(-300), n.node(0), n.node(100), n.node(200), n.node(500)
    n.way(1, [a, b], highway="motorway")
    n.way(2, [b, c], bridge="yes", layer="2", highway="motorway")  # over a road: rule 1
    n.way(3, [c, d], bridge="yes", layer="2", highway="motorway")  # nothing beneath, held up
    n.way(4, [d, e], highway="motorway")
    n.way(10, [n.node(55, -301), n.node(55, 299)])
    n.build()
    assert 3 in n.info["layer_floors"]["not_floored"]["held_up_by_rules"]


def test_bridge_over_a_tunnel_is_not_raised_for_the_tunnel():
    n = Net()
    n.way(1, [n.node(-300), n.node(0)])
    n.way(2, [n.node(0), n.node(100)], bridge="yes", layer="1")
    n.ways[-1].nodes[0] = n.ways[0].nodes[1]
    n.way(10, [n.node(55, -301), n.node(55, 299)], tunnel="yes", layer="-1")
    f = n.build()
    assert not n.info["crossings"]  # Stage B lowers the tunnel instead
    assert set(zs(f[2])) == {OPEN_DECK_M}  # a primary road: no layer floor
    assert min(zs(f[10])) == -10.0 and max(zs(f[10])) < 0  # dangling ends: no portal


# --- measured precedence and calibration --------------------------------------------------------


def kings_way_stack(
    measured: dict, profile_21: float | None = None, with_41: bool = False
) -> tuple[Net, dict]:
    """Way 30 (layer 3, the Kings Way flyover, along x) passes over ground roads 10 (x = -45) and
    12 (x = 135), and between them over the service deck 21 (layer 2, along y at x = 45), which
    crosses ground road 11 (along x at y = 40) or, `with_41`, an estimated layer-1 deck 41 there.
    `measured` maps (upper, lower) -> point-cloud separation; `profile_21` = a measured deck
    profile (height) for way 21."""

    def measure(g, crossings):
        for c in crossings:
            if (c.upper, c.lower) in measured:
                c.separation, c.source = measured[(c.upper, c.lower)], "pointcloud"
        prof = {v: profile_21 for v in g.lines[21]} if profile_21 is not None else {}
        return prof, []

    n = Net()
    a, b, c, d = n.node(-300), n.node(-100), n.node(200), n.node(400)
    n.way(29, [a, b], highway="trunk")
    n.way(30, [b, c], bridge="yes", layer="3", highway="trunk")
    n.way(31, [c, d], highway="trunk")
    n.way(10, [n.node(-45, -301), n.node(-45, 299)])
    n.way(12, [n.node(135, -301), n.node(135, 299)])
    s0, s1, s2, s3 = (n.node(45, y) for y in (-201, -61, 99, 201))
    n.way(20, [s0, s1], highway="service")
    n.way(21, [s1, s2], bridge="yes", layer="2", highway="service")
    n.way(22, [s2, s3], highway="service")
    if with_41:
        t0, t1, t2, t3 = (n.node(x, 40) for x in (-151, -21, 111, 249))
        n.way(40, [t0, t1], highway="service")
        n.way(41, [t1, t2], bridge="yes", layer="1", highway="service")
        n.way(42, [t2, t3], highway="service")
        n.way(11, [n.node(23, -301), n.node(23, 299)])
    else:
        n.way(11, [n.node(-151, 40), n.node(249, 40)])
    return n, n.build(measure)


def test_measured_precedence_follows_the_measured_decks():
    """Like the Crown service deck under the Kings Way flyover (qa finding 4): the flyover is
    measured 11.0 m over two ground roads and 4.4 m over the service deck, whose own profile is
    measured at 6.6 m: the flyover holds 11.0 m between them; nothing estimated lifts it."""
    measured = {(30, 10): 11.0, (30, 12): 11.0, (30, 21): 4.4}
    n, f = kings_way_stack(measured, profile_21=6.6)
    for x in (-45, 45, 135):
        assert z_at(f[30], x, 0) == pytest.approx(11.0, abs=0.05)
    assert z_at(f[21], 45, 0) == pytest.approx(6.6, abs=EPS)
    assert z_at(f[21], 45, 40) - z_at(f[11], 45, 40) >= 4.0
    assert f[30]["properties"]["z_source"] == "measured"
    assert f[21]["properties"]["z_source"] == "measured"
    assert n.info["precedence"]["anchors_moved"] == 0
    # without any ground measurement the 4.4 m is only relative to the deck beneath: no anchor
    n2, f2 = kings_way_stack({(30, 21): 4.4})
    assert z_at(f2[30], 45, 0) == pytest.approx(z_at(f2[21], 45, 0) + 4.4, abs=EPS)
    assert n2.info["precedence"]["anchors"] == 0


def test_estimated_deck_under_a_measured_one_bends_and_conflicts_are_reported():
    """An estimated deck 41 (over road 11) under the measured service deck 21 (profile 6.6 m)
    must come down to <= 6.6 - 4 m, yet stay >= 4 m over road 11: impossible here, so the
    measurements win and the spot is listed in `conflicts_below_ground`."""
    measured = {(30, 10): 11.0, (30, 12): 11.0, (30, 21): 4.4}
    n, f = kings_way_stack(measured, profile_21=6.6, with_41=True)
    assert z_at(f[21], 45, 0) == pytest.approx(6.6, abs=EPS)
    assert z_at(f[30], 45, 0) == pytest.approx(11.0, abs=0.05)
    pr = n.info["precedence"]
    assert pr["anchors_moved"] == 0
    assert pr["conflicts_below_ground"]
    assert all(c["bound_m"] < 0 for c in pr["conflicts_below_ground"])
    assert pr["vertices_bounded_below_zero"] == len(pr["conflicts_below_ground"])


def test_per_deck_calibration_uses_the_decks_own_measurement():
    """La Trobe St style: a deck measured 7.35 m over a road keeps 7.35 m over the railway it
    also crosses (rail crossings are never point-cloud measured), not the class default."""

    def measure(g, crossings):
        for c in crossings:
            if c.lower == 10:
                c.separation, c.source = 7.35, "pointcloud"
        return {}, []

    n = Net()
    a, b, c, d = n.node(-300), n.node(0), n.node(200), n.node(500)
    n.way(1, [a, b], highway="tertiary")
    n.way(2, [b, c], bridge="yes", layer="2", highway="tertiary")
    n.way(3, [c, d], highway="tertiary")
    n.way(10, [n.node(45, -301), n.node(45, 299)])
    n.rail(50, [n.node(155, -301), n.node(155, 299)])
    f = n.build(measure)
    rail = [c for c in n.info["crossings"] if c.lower == 50]
    assert [(c.source, c.basis, c.separation) for c in rail] == [("default", "deck", 7.35)]
    assert z_at(f[2], 155, 0) == pytest.approx(7.35, abs=EPS)
    assert max(zs(f[2])) == pytest.approx(7.35, abs=EPS)


def _crossing(source: str, sep: float, clearance: float | None = None, upper: int = 1):
    from pipeline.roads_3d import Crossing

    return Crossing(
        upper=upper,
        lower=2,
        up_a=0,
        up_b=1,
        low_a=2,
        low_b=3,
        low_f=0.5,
        separation=sep,
        clearance_m=clearance,
        clearance_key="maxheight" if clearance else None,
        lon=LON0,
        lat=LAT0,
        kind="cross",
        source=source,
    )


def _graph():
    from pipeline.roads_3d import Graph

    g = Graph()
    g.ways = {
        1: Way(1, [], {"highway": "motorway"}),
        3: Way(3, [], {"highway": "trunk_link"}),
        4: Way(4, [], {"highway": "primary"}),
        5: Way(5, [], {"highway": "service"}),
        2: Way(2, [], {"highway": "primary"}),
    }
    return g


def test_calibration_per_deck_then_class_then_global():
    from pipeline.roads_3d import calibrate

    g = _graph()
    fw = [_crossing("pointcloud", s, upper=1) for s in (9.0, 10.0, 11.0)]
    fw += [_crossing("pointcloud", s, upper=3) for s in (12.0, 13.0)]
    ot = [_crossing("pointcloud", s, upper=4) for s in (7.0, 8.0)]
    targets = [
        _crossing("default", 0.0, upper=1),  # deck 1 has its own measurements
        _crossing("default", 0.0, upper=5),  # 'other' class has n = 2 < 5: global
        _crossing("maxheight", 0.0, clearance=4.3, upper=5),
    ]
    cal = calibrate(g, fw + ot + targets)
    assert (cal["separation_freeway_m"], cal["separation_freeway_n"]) == (11.0, 5)
    assert (cal["separation_other_m"], cal["separation_other_n"]) == (None, 2)
    assert (cal["separation_global_m"], cal["separation_global_n"]) == (10.0, 7)
    assert (targets[0].basis, targets[0].separation) == ("deck", 10.0)
    assert (targets[1].basis, targets[1].separation) == ("global", 10.0)
    assert (targets[2].basis, targets[2].separation) == ("constant", 4.3 + DECK_THICKNESS_M)
    assert [c.separation for c in fw + ot] == [9.0, 10.0, 11.0, 12.0, 13.0, 7.0, 8.0]


def test_calibration_class_median_and_deck_thickness():
    from pipeline.roads_3d import calibrate

    g = _graph()
    fw = [_crossing("pointcloud", s, clearance=5.0, upper=1) for s in (8.0, 8.5, 9.0, 9.5, 10.0)]
    other = _crossing("default", 0.0, upper=3)  # a freeway link with no measurement: class
    mh = _crossing("maxheight", 0.0, clearance=4.6, upper=3)
    cal = calibrate(g, [*fw, other, mh])
    assert (cal["deck_thickness_freeway_m"], cal["deck_thickness_freeway_n"]) == (4.0, 5)
    assert (other.basis, other.separation) == ("class", 9.0)
    assert (mh.basis, mh.separation) == ("class", pytest.approx(8.6))


def test_calibration_falls_back_below_five_measurements():
    from pipeline.roads_3d import calibrate

    g = _graph()
    cs = [_crossing("pointcloud", 12.0, 5.0, upper=4) for _ in range(4)]
    cs += [_crossing("default", 0.0, upper=5)]
    cal = calibrate(g, cs)
    assert cal["separation_global_m"] == DEFAULT_SEPARATION_M
    assert cal["deck_thickness_global_m"] == DECK_THICKNESS_M
    assert (cs[-1].basis, cs[-1].separation) == ("constant", DEFAULT_SEPARATION_M)


def test_ground_road_over_a_cutting_is_not_raised():
    n = Net()
    n.way(1, [n.node(-301), n.node(299)])  # layer 0
    n.way(10, [n.node(3, -301), n.node(3, 299)], layer="-1")  # a cutting, no tunnel tag
    f = n.build()
    assert set(zs(f[1])) == {0.0} and set(zs(f[10])) == {0.0}


def test_tunnels_are_lowered_in_stage_b_and_labelled_estimated():
    n = Net()
    a, b, c, d = n.node(-200), n.node(0), n.node(400), n.node(600)
    n.way(1, [a, b])
    n.way(2, [b, c], tunnel="yes", layer="-1")
    n.way(3, [c, d])
    f = n.build()
    assert set(zs(f[1])) == {0.0} and set(zs(f[3])) == {0.0}  # nothing needs the approaches
    assert min(zs(f[2])) == -10.0 and zs(f[2])[0] == zs(f[2])[-1] == 0.0
    assert f[2]["properties"]["z_source"] == "estimated"
    assert [f[w]["properties"]["z_source"] for w in (1, 3)] == ["ground", "ground"]


# --- rules 4 and 6: grades and infeasible crossings -------------------------------------------


def loop_under(highway: str) -> tuple[Net, dict]:
    """A deck (way 2, x 0..100) whose end ramp (way 5) loops back and passes under the deck
    itself at x = 63, ~70 m of road from the deck end: 6 % gives 4.2 m < 6.5 m."""
    n = Net()
    a, b, c = n.node(-300), n.node(0), n.node(100)
    n.way(1, [a, b])
    n.way(2, [b, c], bridge="yes", layer="1")
    p1, p2, p3 = n.node(100, 15), n.node(63, 15), n.node(63, -5)
    n.way(5, [c, p1, p2, p3], highway=highway)
    n.way(6, [p3, n.node(63, -301)])
    return n, n.build()


def test_short_link_is_steepened_only_when_a_crossing_needs_it():
    n, f = loop_under("motorway_link")
    assert n.info["steepened_links"] == [5]
    assert not [c for c in n.info["crossings"] if c.dropped]
    assert MAX_GRADE + 0.005 < max_grade(f[5]) <= SHORT_LINK_GRADE + 0.003
    assert z_at(f[2], 63, 0) >= z_at(f[5], 63, 0) + DEFAULT_SEPARATION_M - EPS
    # a deck with nothing looping under it keeps 6 % links
    n2 = Net()
    a, b, c, d = n2.node(-100), n2.node(0), n2.node(100), n2.node(200)
    n2.way(1, [a, b], highway="motorway_link")
    n2.way(2, [b, c], highway="motorway", bridge="yes", layer="1")
    n2.way(3, [c, d])
    n2.way(10, [n2.node(55, -101), n2.node(55, 99)])
    f2 = n2.build()
    assert n2.info["steepened_links"] == []
    assert max_grade(f2[1]) <= MAX_GRADE + 0.003


def test_crossing_that_cannot_be_separated_is_dropped_and_reported():
    n, _ = loop_under("primary")  # not a link: stays at 6 %
    dropped = [c for c in n.info["crossings"] if c.dropped]
    assert [(c.upper, c.lower) for c in dropped] == [(2, 5)]
    assert n.info["steepened_links"] == []


# --- measured inputs through the `measure` hook (the point cloud in the real build) -------------


def test_pointcloud_separation_replaces_the_rule_and_is_measured():
    def measure(g, crossings):
        for c in crossings:
            c.separation, c.source = 8.4, "pointcloud"
        return {}, [{"kind": "crossing", "used": True}]

    n, f = short_deck_over({"maxheight": "4.3"}, measure)
    assert z_at(f[2], 55, 0) == pytest.approx(8.4, abs=EPS)
    assert f[2]["properties"]["z_source"] == "measured"


def test_pointcloud_separation_is_an_anchor_not_a_lower_bound():
    """qa review finding 1: a deck measured 8.0 m over one road and 6.0 m over another 60 m
    away really sags; neither the no-sag rule nor anything else lifts the 6.0 m point."""

    def measure(g, crossings):
        for c in crossings:
            c.separation, c.source = {10: 8.0, 11: 6.0, 12: 8.0}[c.lower], "pointcloud"
        return {}, []

    n = Net()
    a, b, c, d = n.node(-300), n.node(0), n.node(200), n.node(500)
    n.way(1, [a, b])
    n.way(2, [b, c], bridge="yes", layer="1", highway="motorway")
    n.way(3, [c, d])
    for wid, x in ((10, 35), (11, 95), (12, 155)):
        n.way(wid, [n.node(x, -301), n.node(x, 299)])
    f = n.build(measure)
    for x, sep in ((35, 8.0), (95, 6.0), (155, 8.0)):
        assert z_at(f[2], x, 0) == pytest.approx(sep, abs=0.05)
    assert n.info["precedence"]["anchors_moved"] == 0


def test_pointcloud_open_deck_profile_replaces_the_default():
    def measure(g, crossings):
        line = g.lines[2]
        prof = [0.3 + 0.5 * math.sin(math.pi * i / (len(line) - 1)) for i in range(len(line))]
        return dict(zip(line, prof, strict=True)), []

    n = Net()
    a, b, c, d = n.node(-300), n.node(0), n.node(100), n.node(400)
    n.way(1, [a, b])
    n.way(2, [b, c], bridge="yes", layer="1")
    n.way(3, [c, d])
    f = n.build(measure)
    z = zs(f[2])
    assert max(z) == pytest.approx(0.8, abs=EPS) and z[0] == pytest.approx(0.3, abs=EPS)
    assert f[2]["properties"]["z_source"] == "measured"


# --- guarantees -------------------------------------------------------------------------------


def test_steps_spacing_and_shared_nodes():
    n, f = deck_over()
    sm = smoothness(n.fc)
    assert sm["max_spacing_m"] <= MAX_SPACING_M + 0.05
    assert sm["max_step_m"] <= MAX_STEP_M
    assert sm["shared_position_mismatches"] == 0
    # the bridge ends are shared nodes: one z each across ways 1/2 and 2/3
    assert zs(f[1])[-1] == zs(f[2])[0] and zs(f[2])[-1] == zs(f[3])[0]


def test_ground_far_from_structures_is_exactly_zero():
    _, f = deck_over()
    z = zs(f[1])
    assert z[0] == 0.0 and z.count(0.0) > len(z) // 2


def test_z_source_values_are_validated():
    n, _ = deck_over()
    fc = n.fc
    f0 = fc["features"][0]
    f0["properties"]["z_source"] = "surveyed"
    assert any("z_source" in e for e in validate_feature_collection(fc, bbox=_box()))
    f0["properties"]["z_source"] = (
        "ground" if any(c[2] for c in f0["geometry"]["coordinates"]) else "estimated"
    )
    assert any("z_source" in e for e in validate_feature_collection(fc, bbox=_box()))


def test_river_bridge_pieces_are_never_floored():
    """Finding 3 (Swan St Bridge 985383991 / 985383992): a freeway-class bridge split in two,
    one piece over the river centreline and one over the bank 40 m on; neither is floored."""
    n = Net()
    a, b, c, d, e = n.node(-300), n.node(0), n.node(60), n.node(120), n.node(400)
    n.way(1, [a, b], highway="trunk")
    n.way(2, [b, c], bridge="yes", layer="1", highway="trunk")  # over the river at x = 30
    n.way(3, [c, d], bridge="yes", layer="1", highway="trunk")  # the bank span
    n.way(4, [d, e], highway="trunk")
    f = n.build(water=[[ll(30, -300), ll(30, 300)]])
    assert n.info["layer_floors"]["not_floored"]["over_water"] == [2, 3]
    assert max(zs(f[2])) == pytest.approx(OPEN_DECK_M) and max(zs(f[3])) == pytest.approx(
        OPEN_DECK_M
    )


def test_tunnel_under_a_river_is_untouched_by_the_water_rules():
    """George: tunnels run UNDER water (Burnley, Domain): never a river bridge, no water-based
    height. A tunnel way passing beneath a river polygon stays exactly as without water."""
    river = [ll(-60, 60), ll(60, 60), ll(60, -60), ll(-60, -60), ll(-60, 60)]

    def tunnel_net() -> Net:
        n = Net()
        a, b, c, d = n.node(-300), n.node(-100), n.node(100), n.node(300)
        n.way(1, [a, b], highway="motorway")
        n.way(2, [b, c], highway="motorway", tunnel="yes", layer="-1")
        n.way(3, [c, d], highway="motorway")
        return n

    n0 = tunnel_net()
    f0 = n0.build()
    n1 = tunnel_net()
    f1 = n1.build(water=[river])
    for w in (1, 2, 3):
        assert zs(f1[w]) == zs(f0[w])
    assert min(zs(f1[2])) < 0  # lowered by Stage B, which has its own river-bed rule
    lf = n1.info["layer_floors"]
    assert 2 not in lf["not_floored"]["over_water"]
    assert 2 not in n1.info["layer_floors"]["river_bridges"]["river_bridge_ways"]


def test_contradicting_pin_is_demoted_not_replaced_by_maxheight():
    """qa round 2 finding 2: a point-cloud separation that cannot hold exactly stays a lower
    bound at its measured value; `maxheight` + deck never takes over."""

    def measure(g, crossings):
        for c in crossings:
            c.separation, c.source = {10: 8.0, 11: 9.5}[c.lower], "pointcloud"
        return {}, []

    n = Net()
    a, b, c, d = n.node(-300), n.node(0), n.node(40), n.node(300)
    n.way(1, [a, b])
    n.way(2, [b, c], bridge="yes", layer="1", highway="motorway")
    n.way(3, [c, d])
    # 8.0 m and 9.5 m measured 20 m apart: 1.5 m in 20 m is beyond the 6 % grade
    n.way(10, [n.node(9, -301), n.node(9, 299)], maxheight="4.0")
    n.way(11, [n.node(31, -301), n.node(31, 299)])
    f = n.build(measure)
    soft = [c for c in n.info["crossings"] if c.source == "pointcloud_soft"]
    assert len(soft) == 1
    c = soft[0]
    assert c.separation in (8.0, 9.5)  # its measured value, not maxheight 4.0 + deck
    drawn = z_at(f[2], 9 if c.lower == 10 else 31, 0)
    assert abs(drawn - c.separation) < 1.0
    assert n.info["precedence"]["pins_dropped"]


def test_river_bridges_take_the_measured_river_deck_median():
    """George 2026-10-26: an unmeasured street bridge over water gets the median of the measured
    river-bridge decks, not a fixed 1.5 m."""
    river = [[ll(x, -300), ll(x, 300)] for x in (50, 250, 450, 650)]

    def measure(g, crossings):
        recs, prof = [], {}
        for wid, h in ((2, 0.2), (12, 1.4), (22, 0.5)):
            line = g.lines[wid]
            for v in line:
                prof[v] = h
            recs.append(
                {
                    "kind": "open_deck",
                    "upper_way": wid,
                    "used": True,
                    "max_m": h,
                    "heights_m": [h] * len(line),
                }
            )
        return prof, recs

    n = Net()
    for k, base in enumerate((0, 200, 400, 600)):
        a, b, c, d = n.node(base - 60), n.node(base), n.node(base + 100), n.node(base + 160)
        n.way(10 * k + 1, [a, b])
        n.way(10 * k + 2, [b, c], bridge="yes", layer="1")
        n.way(10 * k + 3, [c, d])
    f = n.build(measure, water=river)
    assert n.info["layer_floors"]["river_bridges"]["river_deck_m"] == pytest.approx(0.5)
    assert set(zs(f[32])) == {0.5}  # the unmeasured river bridge


def _profile(wid: int, heights):
    """A `measure` returning one open-deck profile on way `wid`: heights(i, n) per vertex."""

    def measure(g, crossings):
        line = g.lines[wid]
        hs = [round(heights(i, len(line)), 2) for i in range(len(line))]
        rec = {"kind": "open_deck", "upper_way": wid, "used": True, "max_m": max(hs)}
        rec["heights_m"] = hs
        return dict(zip(line, hs, strict=True)), [rec]

    return measure


def test_measured_service_ramp_may_be_twelve_percent():
    """George 2026-10-26: a measured car-park ramp (the Crown ramp, ~11-13 %) may be steeper
    than the 6 % road grade, up to SERVICE_RAMP_GRADE; service bridges are densified finely
    enough that the 1 m step rule still holds."""
    assert SERVICE_RAMP_GRADE * SERVICE_RAMP_SPACING_M <= MAX_STEP_M
    n = Net()
    a, b, c = n.node(-200), n.node(0), n.node(80)
    n.way(1, [a, b], highway="service")
    n.way(2, [b, c], bridge="yes", layer="1", highway="service")  # dead-ends at the car park
    f = n.build(_profile(2, lambda i, k: 0.115 * 80 * i / (k - 1)))
    z = zs(f[2])
    assert z[-1] == pytest.approx(9.2, abs=EPS)  # the measured top, not cut to a 6 % ramp
    assert 0.11 <= max_grade(f[2]) <= SERVICE_RAMP_GRADE + 0.005
    c2 = f[2]["geometry"]["coordinates"]
    assert max(metres(p[0], p[1], q[0], q[1]) for p, q in zip(c2, c2[1:], strict=False)) <= 8.05
    assert f[2]["properties"]["z_source"] == "measured"


def test_unpinned_profile_vertex_stays_a_lower_bound():
    """A profile vertex its neighbours make impossible (a 2 m jump in 10 m) is unpinned but
    keeps its measurement as a lower bound: drawn as high as the grade from its neighbours
    allows, not dropped to the neighbours' height."""
    n = Net()
    a, b, c, d = n.node(-300), n.node(0), n.node(100), n.node(400)
    n.way(1, [a, b])
    n.way(2, [b, c], bridge="yes", layer="1")
    n.way(3, [c, d])
    f = n.build(_profile(2, lambda i, k: 7.0 if i == k // 2 else 5.0))
    z = zs(f[2])
    mid = len(z) // 2
    assert z[mid] == pytest.approx(5.0 + MAX_GRADE * 10.0, abs=EPS)
    assert z[mid - 2] == pytest.approx(5.0, abs=EPS) and z[mid + 2] == pytest.approx(5.0, abs=EPS)
    assert n.info["precedence"]["profile_vertices_soft"] == 1
