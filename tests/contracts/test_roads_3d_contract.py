"""Contract test for data/processed/roads_3d.geojson (CLAUDE.md "Data contracts", PLAN.md Phase 2).

Written by qa-reviewer, independently of pipeline.roads_3d (it imports neither the pipeline's
validator nor its constants, only the extract bbox from pipeline/config.py, the single source of
truth for bboxes). The extract is read with the standard library, not osmium; crossings are found
with shapely on the stored lines. Skips when the file is absent.

What it checks:
- RFC 7946 FeatureCollection of LineStrings, positions `[lon, lat, z_m]`, all finite numbers.
- Every line starts and ends inside SIM_EXTRACT_BBOX (TUNNEL_EXTRACT_BBOX for motorway tunnel ways,
  Stage B); interior points may loop out only slightly.
- Properties exactly `way_id` (int, unique), `highway` (str), `layer` (int), `bridge` (bool),
  `tunnel` (bool), `name` (str or null), `z_source` (one of ground / estimated / measured), and
  they agree with the tags in osm_sim_area.osm.gz.
- One feature per way in osm_sim_area.osm.gz, following that way's OSM nodes in order.
- Smoothness (PLAN.md Decisions log): no step > 1 m between consecutive vertices, one z per shared
  position across features, ramp grades within the documented 6 % (8 % on *_link ways <= 150 m;
  12 % on measured-profile segments of highway=service ramps, George 2026-10-26).
- Clearance-based heights (PLAN.md Decisions log 2026-10-26, "clearance-based road heights";
  OSM layer gives only the ORDER at a crossing):
  * `z_source` agrees with the geometry (Stage B, data/SOURCES.md): `ground` <=> every z is 0;
    `estimated` has some z != 0; `measured` only on ways that are raised (some z > 0) or on the
    CityLink cutting ways with a used point-cloud cutting profile; a tunnel below ground is never
    `measured` (the point cloud can't see underground) and neither is a lowered approach road.
  * Order: at every crossing without a shared node between ways on different effective layers
    (bridge => layer >= 1; a real tunnel, i.e. not `building_passage`, => layer <= -1, the same
    rule as roads_3d and the grade-separation check), the higher layer is higher in z by at least
    the 4 m clearance threshold. Stage B: no tunnel or trench exception any more (tunnels have
    depth, the cutting is lowered, `pending_trench_depth` must be empty). Only exception: the
    spots listed in deck_pointcloud.json `precedence.conflicts_below_ground` (two measurements
    leave less room than 4 m), which may be at most 0.35 m short.
  * Nothing is raised without a deck under it: every vertex of a non-deck way is no higher than
    the ramp cone (6 %, 8 % on short links, along the road graph) from the stored heights of the
    deck vertices (bridges and ways on layer >= 1).
  * Below-ground z (Stage B) only on tunnels, on the documented CityLink cutting ways, and on the
    approach roads listed in deck_pointcloud.json `below_ground.approach_ways_lowered`, each within
    a 6 % cone from a tunnel or cutting vertex; the list matches the file exactly.
The point-cloud measurements themselves (measured precedence, calibration) are checked in
test_roads_3d_pointcloud.py.
"""

from __future__ import annotations

import gzip
import heapq
import json
import math
import xml.etree.ElementTree as ET
from collections import defaultdict
from itertools import pairwise
from pathlib import Path

import pytest

from pipeline.config import SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX, BBox

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "data" / "processed" / "roads_3d.geojson"
EXTRACT = ROOT / "data" / "processed" / "osm_sim_area.osm.gz"

EXPECTED_KEYS = {"way_id", "highway", "layer", "bridge", "tunnel", "name", "z_source"}
Z_SOURCES = {"ground", "estimated", "measured"}
M_PER_DEG = 111_320.0

# Decisions log: no step > 1 m; same z at shared nodes.
MAX_STEP_M = 1.0
SHARED_Z_TOL_M = 0.01
# data/SOURCES.md: ramps at <= 6 % (8 % on short links).
GRADE = 0.06
SHORT_LINK_GRADE = 0.08
SHORT_LINK_MAX_M = 150.0
SERVICE_RAMP_GRADE = 0.12  # measured service / car-park ramps (George, PLAN.md 5c06a21)
# Rounding of the stored file: z to 0.01 m, lon/lat to 1e-6 deg (~0.1 m of distance).
ROUNDING_SLACK_M = 0.03
# Allowance on the deck-support cone: storage rounding (z 0.01 m, lon/lat 1e-6 deg) only.
CONE_SLACK_M = 0.05
# The grade-separation check's clearance threshold (pipeline/checks/grade_separation.py
# CLEARANCE_M, data/SOURCES.md); a crossing below it is `separated_low`.
CLEARANCE_M = 4.0
# Orchestrator brief (Stage A heights review): a crossing at a listed measured-precedence conflict
# may be at most this much short of CLEARANCE_M.
CONFLICT_SHORT_M = 0.35
# A crossing belongs to a listed conflict if its lower way is among the conflict's ways and the
# crossing lies within this distance of the conflict vertex (the vertices are <= 10 m apart).
CONFLICT_NEAR_M = 25.0
# Intersection points this close to a position both lines share are that junction, not a crossing;
# intersection points of one pair closer than MERGE_M are one crossing.
JUNCTION_TOL_M = 0.1
MERGE_M = 1.0
CHECKS = ROOT / "data" / "processed" / "checks" / "deck_pointcloud.json"
# Interior points of a clipped way may loop slightly outside the extract bbox (seen: 4.8 m).
LOOP_OUT_DEG = 0.001  # ~100 m
NOT_A_STRUCTURE = {"no", "false", "0"}
# Stage B extended clip: motorway tunnel ways are clipped at TUNNEL_EXTRACT_BBOX (data/SOURCES.md).
EXTENDED_CLIP_HIGHWAYS = {"motorway", "motorway_link"}
# The CityLink cutting under Power St, lowered to its measured depth (data/SOURCES.md, Stage B).
CUTTING_WAYS = {5234624, 5234646, 5234953, 5234957, 19792505, 980110339, 1175874270}

pytestmark = pytest.mark.skipif(not PATH.exists(), reason="roads_3d.geojson not built yet")


def _metres(a: list[float], b: list[float]) -> float:
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * k, b[1] - a[1]) * M_PER_DEG


def _is_num(x: object) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


def _structure(tags: dict[str, str], key: str) -> bool:
    v = tags.get(key)
    return v is not None and v.strip().lower() not in NOT_A_STRUCTURE


def _layer(tags: dict[str, str]) -> int:
    try:
        return int(round(float(tags.get("layer", "0").split(";")[0])))
    except ValueError:
        return 0


def _real_tunnel(tags: dict[str, str]) -> bool:
    return _structure(tags, "tunnel") and tags.get("tunnel") != "building_passage"


def _effective_layer(tags: dict[str, str]) -> int:
    """A bridge counts as layer >= 1, a real tunnel as <= -1 (roads_3d / grade_separation rule)."""
    layer = _layer(tags)
    if _structure(tags, "bridge") and layer <= 0:
        return 1
    if _real_tunnel(tags) and layer >= 0:
        return -1
    return layer


def _clip_box(tags: dict[str, str]) -> BBox:
    """Stage B: motorway tunnel ways are clipped at TUNNEL_EXTRACT_BBOX, the rest at
    SIM_EXTRACT_BBOX."""
    if tags.get("tunnel") == "yes" and tags.get("highway") in EXTENDED_CLIP_HIGHWAYS:
        return TUNNEL_EXTRACT_BBOX
    return SIM_EXTRACT_BBOX


def _tunnels_lowered(features: list[dict]) -> bool:
    return any(
        c[2] < 0
        for f in features
        if f["properties"]["tunnel"]
        for c in f["geometry"]["coordinates"]
    )


@pytest.fixture(scope="module")
def fc() -> dict:
    return json.loads(PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def features(fc: dict) -> list[dict]:
    return fc["features"]


@pytest.fixture(scope="module")
def extract() -> tuple[dict[int, tuple[float, float]], dict[int, tuple[list[int], dict]]]:
    if not EXTRACT.exists():
        pytest.skip("osm_sim_area.osm.gz not built yet")
    with gzip.open(EXTRACT) as fh:
        root = ET.parse(fh).getroot()
    nodes: dict[int, tuple[float, float]] = {}
    ways: dict[int, tuple[list[int], dict]] = {}
    for el in root:
        if el.tag == "node":
            nodes[int(el.get("id"))] = (float(el.get("lon")), float(el.get("lat")))
        elif el.tag == "way":
            refs = [int(c.get("ref")) for c in el if c.tag == "nd"]
            tags = {c.get("k"): c.get("v") for c in el if c.tag == "tag"}
            ways[int(el.get("id"))] = (refs, tags)
    return nodes, ways


@pytest.fixture(scope="module")
def graph(features: list[dict]) -> dict[tuple[float, float], list[tuple[tuple, float]]]:
    """Road graph: positions are vertices (shared positions join features), edges are segments."""
    adj: dict[tuple[float, float], list[tuple[tuple, float]]] = defaultdict(list)
    for f in features:
        c = f["geometry"]["coordinates"]
        for a, b in zip(c, c[1:], strict=False):
            d = _metres(a, b)
            adj[(a[0], a[1])].append(((b[0], b[1]), d))
            adj[(b[0], b[1])].append(((a[0], a[1]), d))
    return adj


@pytest.fixture(scope="module")
def deck_support(features: list[dict], extract) -> dict[tuple[float, float], float]:
    """max over deck vertices d of (stored z(d) - grade x road-graph distance), cut off at -10 m.

    Decks are bridges and ways on layer >= 1 (e.g. Kings Way's building_passage at layer 2). The
    cone falls at the gentlest documented grade (6 %), so it bounds every ramp from above (a short
    link steepened to 8 % comes down faster, i.e. lower). A non-deck vertex above this cone is
    raised without any deck holding it up.
    """
    _, ways = extract
    adj: dict[tuple[float, float], list[tuple[tuple, float]]] = defaultdict(list)
    cone: dict[tuple[float, float], float] = {}
    for f in features:
        c = f["geometry"]["coordinates"]
        for a, b in zip(c, c[1:], strict=False):
            d = _metres(a, b) * GRADE
            adj[(a[0], a[1])].append(((b[0], b[1]), d))
            adj[(b[0], b[1])].append(((a[0], a[1]), d))
        tags = ways[f["properties"]["way_id"]][1]
        if f["properties"]["bridge"] or _effective_layer(tags) >= 1:
            for x in c:
                key = (x[0], x[1])
                cone[key] = max(cone.get(key, -math.inf), x[2])
    heap = [(-h, v) for v, h in cone.items()]
    heapq.heapify(heap)
    while heap:
        neg, u = heapq.heappop(heap)
        hu = -neg
        if hu < cone[u] or hu < -10.0:
            continue
        for v, drop in adj[u]:
            cand = hu - drop
            if cand > cone.get(v, -math.inf):
                cone[v] = cand
                heapq.heappush(heap, (-cand, v))
    return cone


@pytest.fixture(scope="module")
def crossings(features: list[dict], extract) -> list[dict]:
    """Every point where two stored lines cross without a shared position (qa's own search)."""
    from shapely import STRtree
    from shapely.geometry import LineString

    _, ways = extract
    lat0 = (SIM_EXTRACT_BBOX.south + SIM_EXTRACT_BBOX.north) / 2
    lon0 = (SIM_EXTRACT_BBOX.west + SIM_EXTRACT_BBOX.east) / 2
    kx = M_PER_DEG * math.cos(math.radians(lat0))

    def xy(lon: float, lat: float) -> tuple[float, float]:
        return ((lon - lon0) * kx, (lat - lat0) * M_PER_DEG)

    coords = [f["geometry"]["coordinates"] for f in features]
    lines = [LineString([xy(c[0], c[1]) for c in cs]) for cs in coords]
    vsets = [{(c[0], c[1]) for c in cs} for cs in coords]

    def z_at(i: int, p: tuple[float, float]) -> float:
        best = (math.inf, 0.0)
        for a, b in zip(coords[i], coords[i][1:], strict=False):
            ax, ay = xy(a[0], a[1])
            bx, by = xy(b[0], b[1])
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
            d = math.hypot(ax + t * dx - p[0], ay + t * dy - p[1])
            if d < best[0]:
                best = (d, a[2] + t * (b[2] - a[2]))
        return best[1]

    out = []
    left, right = STRtree(lines).query(lines, predicate="intersects")
    for i, j in zip(left.tolist(), right.tolist(), strict=True):
        if i >= j:
            continue
        shared = [xy(*v) for v in vsets[i] & vsets[j]]
        inter = lines[i].intersection(lines[j])
        pts = []
        for g in getattr(inter, "geoms", [inter]):
            if g.is_empty:
                continue
            q = g if g.geom_type == "Point" else g.interpolate(0.5, normalized=True)
            if all(math.hypot(q.x - s[0], q.y - s[1]) > JUNCTION_TOL_M for s in shared):
                pts.append((q.x, q.y))
        merged: list[tuple[float, float]] = []
        for p in pts:
            if all(math.hypot(p[0] - m[0], p[1] - m[1]) > MERGE_M for m in merged):
                merged.append(p)
        for p in merged:
            wa = features[i]["properties"]["way_id"]
            wb = features[j]["properties"]["way_id"]
            out.append(
                {
                    "way_a": wa,
                    "way_b": wb,
                    "layer_a": _effective_layer(ways[wa][1]),
                    "layer_b": _effective_layer(ways[wb][1]),
                    "z_a": z_at(i, p),
                    "z_b": z_at(j, p),
                    "lon": lon0 + p[0] / kx,
                    "lat": lat0 + p[1] / M_PER_DEG,
                }
            )
    return out


# --- structure --------------------------------------------------------------------------------


def test_is_rfc7946_feature_collection_of_linestrings(fc: dict, features: list[dict]):
    assert fc.get("type") == "FeatureCollection"
    assert "crs" not in fc, "RFC 7946 has no crs member (WGS84 is implied)"
    assert isinstance(features, list) and features
    for f in features:
        assert f.get("type") == "Feature"
        assert f["geometry"]["type"] == "LineString", f["properties"]
        assert len(f["geometry"]["coordinates"]) >= 2, f["properties"]


def test_positions_are_lon_lat_z_finite_numbers(features: list[dict]):
    for f in features:
        for pos in f["geometry"]["coordinates"]:
            assert isinstance(pos, list) and len(pos) == 3, (f["properties"]["way_id"], pos)
            assert all(_is_num(x) for x in pos), (f["properties"]["way_id"], pos)


def test_lines_start_and_end_inside_buffered_sim_bbox(features: list[dict], extract):
    _, ways = extract
    eps = 1e-6  # stored lon/lat are rounded to 6 decimals
    for f in features:
        b = _clip_box(ways[f["properties"]["way_id"]][1])
        for lon, lat, _ in (f["geometry"]["coordinates"][0], f["geometry"]["coordinates"][-1]):
            assert b.west - eps <= lon <= b.east + eps, (f["properties"]["way_id"], lon)
            assert b.south - eps <= lat <= b.north + eps, (f["properties"]["way_id"], lat)


def test_all_positions_near_buffered_sim_bbox(features: list[dict], extract):
    # lon first: lon ~144.9, lat ~-37.8. A swapped [lat, lon] fails here.
    _, ways = extract
    tol = LOOP_OUT_DEG
    for f in features:
        b = _clip_box(ways[f["properties"]["way_id"]][1])
        for lon, lat, _ in f["geometry"]["coordinates"]:
            assert b.west - tol <= lon <= b.east + tol, (f["properties"]["way_id"], lon)
            assert b.south - tol <= lat <= b.north + tol, (f["properties"]["way_id"], lat)


def test_properties_exact_keys_and_types(features: list[dict]):
    seen: set[int] = set()
    for f in features:
        p = f["properties"]
        assert set(p) == EXPECTED_KEYS, sorted(p)
        wid = p["way_id"]
        assert isinstance(wid, int) and not isinstance(wid, bool) and wid > 0, wid
        assert wid not in seen, f"duplicate way_id {wid}"
        seen.add(wid)
        assert isinstance(p["highway"], str) and p["highway"], wid
        assert isinstance(p["layer"], int) and not isinstance(p["layer"], bool), wid
        assert isinstance(p["bridge"], bool), wid
        assert isinstance(p["tunnel"], bool), wid
        assert p["name"] is None or (isinstance(p["name"], str) and p["name"]), wid
        assert p["z_source"] in Z_SOURCES, (wid, p["z_source"])


# --- agreement with osm_sim_area.osm.gz -------------------------------------------------------


def test_one_feature_per_extract_way(features: list[dict], extract):
    _, ways = extract
    ids = {f["properties"]["way_id"] for f in features}
    missing_in_extract = ids - ways.keys()
    assert not missing_in_extract, f"way_ids not in osm_sim_area: {sorted(missing_in_extract)[:10]}"
    no_road = ways.keys() - ids
    assert not no_road, f"extract ways with no roads_3d feature: {sorted(no_road)[:10]}"


def test_properties_match_extract_tags(features: list[dict], extract):
    _, ways = extract
    for f in features:
        p = f["properties"]
        tags = ways[p["way_id"]][1]
        assert p["highway"] == tags.get("highway"), p
        assert p["name"] == tags.get("name"), p
        assert p["bridge"] == _structure(tags, "bridge"), (p, tags.get("bridge"))
        assert p["tunnel"] == _structure(tags, "tunnel"), (p, tags.get("tunnel"))
        if "layer" not in tags:
            assert p["layer"] == 0, p


def test_geometry_follows_the_ways_osm_nodes(features: list[dict], extract):
    nodes, ways = extract
    tol = 1.5e-6
    for f in features:
        refs = ways[f["properties"]["way_id"]][0]
        coords = f["geometry"]["coordinates"]
        want = [nodes[r] for r in refs]

        def close(c: list[float], w: tuple[float, float]) -> bool:
            return abs(c[0] - w[0]) < tol and abs(c[1] - w[1]) < tol

        assert close(coords[0], want[0]) and close(coords[-1], want[-1]), f["properties"]
        it = iter(coords)
        assert all(any(close(c, w) for c in it) for w in want), (
            f"way {f['properties']['way_id']}: OSM nodes not all on the line, in order"
        )


# --- smoothness -------------------------------------------------------------------------------


def test_no_step_over_1_m(features: list[dict]):
    worst = max(
        (abs(b[2] - a[2]), f["properties"]["way_id"])
        for f in features
        for a, b in pairwise(f["geometry"]["coordinates"])
    )
    assert worst[0] <= MAX_STEP_M + 1e-9, f"step {worst[0]:.2f} m on way {worst[1]}"


def test_same_z_at_shared_positions(features: list[dict]):
    zs: dict[tuple[float, float], list[float]] = defaultdict(list)
    for f in features:
        for lon, lat, z in f["geometry"]["coordinates"]:
            zs[(lon, lat)].append(z)
    bad = {k: v for k, v in zs.items() if max(v) - min(v) > SHARED_Z_TOL_M + 1e-9}
    assert not bad, f"{len(bad)} shared positions with different z, e.g. {next(iter(bad.items()))}"


@pytest.fixture(scope="module")
def measured_profile_vertices() -> dict[int, set[int]]:
    """way id -> vertex indices with a point-cloud profile height (used deck_profile / open_deck
    records in deck_pointcloud.json)."""
    if not CHECKS.exists():
        pytest.fail("data/processed/checks/deck_pointcloud.json missing (it is committed)")
    out: dict[int, set[int]] = defaultdict(set)
    for r in json.loads(CHECKS.read_text())["records"]:
        if r["kind"] in ("deck_profile", "open_deck") and r.get("used") and r.get("heights_m"):
            out[r["upper_way"]].update(i for i, h in enumerate(r["heights_m"]) if h is not None)
    return out


def test_ramp_grades_within_documented_limits(features: list[dict], measured_profile_vertices):
    """6 %, 8 % on short links; George (PLAN.md 2026-10-26): a measured service / car-park ramp
    may be up to 12 % (SERVICE_RAMP_GRADE): a `highway=service` way with a used point-cloud profile
    (its raised vertices are measured or interpolated between measured ones; the Crown ramp is
    ~11 % in the cloud). Everything else keeps 6 % / 8 %."""
    over = []
    for f in features:
        c = f["geometry"]["coordinates"]
        hw = f["properties"]["highway"]
        length = sum(_metres(a, b) for a, b in zip(c, c[1:], strict=False))
        g0 = SHORT_LINK_GRADE if hw.endswith("_link") and length <= SHORT_LINK_MAX_M else GRADE
        prof = measured_profile_vertices.get(f["properties"]["way_id"], set())
        for a, b in zip(c, c[1:], strict=False):
            g = SERVICE_RAMP_GRADE if hw == "service" and prof else g0
            d = _metres(a, b)
            if abs(b[2] - a[2]) > g * d + ROUNDING_SLACK_M:
                over.append((f["properties"]["way_id"], hw, round(d, 2), a[2], b[2]))
    assert not over, f"{len(over)} segments steeper than documented, e.g. {over[:3]}"


# --- heights: clearance-based, labelled by z_source -------------------------------------------


@pytest.fixture(scope="module")
def below_ground_report() -> dict:
    if not CHECKS.exists():
        pytest.fail("data/processed/checks/deck_pointcloud.json missing (it is committed)")
    return json.loads(CHECKS.read_text())["below_ground"]


@pytest.fixture(scope="module")
def cutting_profile_ways() -> set[int]:
    """Ways with a USED point-cloud cutting profile (deck_pointcloud.json `cutting_profiles`)."""
    if not CHECKS.exists():
        pytest.fail("data/processed/checks/deck_pointcloud.json missing (it is committed)")
    rep = json.loads(CHECKS.read_text())
    return {r["way"] for r in rep["cutting_profiles"] if r["used"]}


def test_tunnels_are_lowered_in_stage_b(features: list[dict]):
    assert _tunnels_lowered(features), "Stage B: no tunnel vertex below ground"


def test_z_source_agrees_with_geometry(features: list[dict], cutting_profile_ways: set[int]):
    """Stage B (data/SOURCES.md): `ground` = every z is 0; tunnels and lowered approach roads are
    `estimated`; `measured` = raised and held by measurements, or a CityLink cutting way lowered
    to its point-cloud depth."""
    bad = []
    for f in features:
        p = f["properties"]
        zs = [c[2] for c in f["geometry"]["coordinates"]]
        if (p["z_source"] == "ground") != all(z == 0 for z in zs):
            bad.append((p["way_id"], p["z_source"], min(zs), max(zs)))
        if p["z_source"] == "estimated" and all(z == 0 for z in zs):
            bad.append((p["way_id"], "estimated but flat"))
        if p["z_source"] == "measured":
            cutting = p["way_id"] in CUTTING_WAYS and p["way_id"] in cutting_profile_ways
            if not (max(zs) > 0 or (cutting and min(zs) < 0)):
                bad.append((p["way_id"], "measured but neither raised nor a measured cutting"))
            if min(zs) < 0 and not cutting:
                bad.append((p["way_id"], "measured below ground off the measured cutting"))
    assert not bad, f"{len(bad)} ways whose z_source does not fit their heights: {bad[:5]}"


def test_z_source_ground_iff_all_zero(features: list[dict]):
    """`ground` is exactly the set of ways with every z == 0 (Stage A and Stage B alike)."""
    ground = {
        f["properties"]["way_id"] for f in features if f["properties"]["z_source"] == "ground"
    }
    flat = {
        f["properties"]["way_id"]
        for f in features
        if all(c[2] == 0 for c in f["geometry"]["coordinates"])
    }
    assert ground == flat, (sorted(ground - flat)[:5], sorted(flat - ground)[:5])


def test_every_z_source_is_used(features: list[dict]):
    """Stage A has measured decks (point cloud / maxheight), estimated decks and ground roads."""
    used = {f["properties"]["z_source"] for f in features}
    assert used == Z_SOURCES, used


@pytest.fixture(scope="module")
def conflicts() -> list[dict]:
    if not CHECKS.exists():
        pytest.fail("data/processed/checks/deck_pointcloud.json missing (it is committed)")
    return json.loads(CHECKS.read_text())["precedence"]["conflicts_below_ground"]


def test_no_crossing_left_pending_trench_depth():
    """Stage B lowered the CityLink cutting: nothing is pending (data/SOURCES.md)."""
    if not CHECKS.exists():
        pytest.fail("data/processed/checks/deck_pointcloud.json missing (it is committed)")
    assert json.loads(CHECKS.read_text())["pending_trench_depth"] == []


def _in_conflict(lower: int, lon: float, lat: float, conflicts: list[dict]) -> bool:
    return any(
        lower in c["ways"] and _metres([lon, lat], [c["lon"], c["lat"]]) <= CONFLICT_NEAR_M
        for c in conflicts
    )


def test_crossings_found(crossings: list[dict]):
    """Sanity: the search finds the extract's grade separations (349 on 2026-10-26)."""
    assert len(crossings) > 100


def test_higher_layer_is_higher_by_the_clearance(crossings: list[dict], conflicts):
    """Stage B: crossings over tunnels and over the cutting get no exception."""
    bad, allowed = [], []
    for c in crossings:
        if c["layer_a"] == c["layer_b"]:
            continue
        up, lo = ("a", "b") if c["layer_a"] > c["layer_b"] else ("b", "a")
        lower_way = c[f"way_{lo}"]
        dz = c[f"z_{up}"] - c[f"z_{lo}"]
        if dz >= CLEARANCE_M - ROUNDING_SLACK_M:
            continue
        rec = (c[f"way_{up}"], lower_way, round(dz, 2), round(c["lon"], 6), round(c["lat"], 6))
        if _in_conflict(lower_way, c["lon"], c["lat"], conflicts):
            allowed.append(rec)
            if dz < CLEARANCE_M - CONFLICT_SHORT_M - ROUNDING_SLACK_M:
                bad.append(("conflict more than 0.35 m short", *rec))
        else:
            bad.append(rec)
    assert not bad, f"{len(bad)} crossings with the wrong order or < {CLEARANCE_M} m: {bad[:5]}"


def test_listed_conflicts_are_at_most_0_35_m_short(conflicts: list[dict]):
    """Each conflict's bound is how far below ground the lower road would have to go."""
    for c in conflicts:
        assert -CONFLICT_SHORT_M <= c["bound_m"] < 0, c


def test_nothing_raised_without_a_deck_under_it(features: list[dict], deck_support):
    over = []
    for f in features:
        for lon, lat, z in f["geometry"]["coordinates"]:
            if z > 0:
                cap = max(deck_support.get((lon, lat), -math.inf), 0.0) + CONE_SLACK_M
                if z > cap:
                    over.append((f["properties"]["way_id"], lon, lat, z, round(cap, 2)))
    assert not over, f"{len(over)} vertices higher than any deck nearby holds up: {over[:3]}"


def test_below_ground_only_on_tunnels_cutting_and_listed_approaches(
    features: list[dict], graph: dict, below_ground_report: dict, cutting_profile_ways: set[int]
):
    """Negative z (Stage B) only on tunnel ways, on the documented CityLink cutting ways (each
    with a used point-cloud profile) and on the approach roads the report lists; the non-tunnel,
    non-cutting ways below ground are exactly `below_ground.approach_ways_lowered`, all
    `estimated`, and every such vertex lies inside a 6 % cone (along the road graph) from the
    tunnel and cutting vertices, i.e. it is a ramp into one of them."""
    assert set(below_ground_report["cutting_ways"]) == CUTTING_WAYS
    assert cutting_profile_ways <= CUTTING_WAYS, sorted(cutting_profile_ways - CUTTING_WAYS)
    seeds: dict[tuple[float, float], float] = {}
    below_ways: dict[int, dict] = {}
    for f in features:
        w = f["properties"]["way_id"]
        source = f["properties"]["tunnel"] or w in CUTTING_WAYS
        for lon, lat, z in f["geometry"]["coordinates"]:
            if source and z < 0:
                seeds[(lon, lat)] = max(seeds.get((lon, lat), 0.0), -z)
            if not source and z < 0:
                below_ways[w] = f["properties"]
    # every documented cutting way that is lowered has a used profile
    lowered_cutting = {
        f["properties"]["way_id"]
        for f in features
        if f["properties"]["way_id"] in CUTTING_WAYS
        and min(c[2] for c in f["geometry"]["coordinates"]) < 0
    }
    assert lowered_cutting <= cutting_profile_ways, sorted(lowered_cutting - cutting_profile_ways)
    listed = set(below_ground_report["approach_ways_lowered"])
    assert set(below_ways) == listed, (
        f"below ground but not listed: {sorted(set(below_ways) - listed)}; "
        f"listed but not below ground: {sorted(listed - set(below_ways))}"
    )
    not_est = [w for w, p in below_ways.items() if p["z_source"] != "estimated"]
    assert not not_est, f"lowered approach roads not labelled estimated: {not_est}"
    # Downward cone from tunnel / cutting vertices, mirrored from the bridge rule.
    depth = dict(seeds)
    heap = [(-d, v) for v, d in depth.items()]
    heapq.heapify(heap)
    while heap:
        neg, u = heapq.heappop(heap)
        du = -neg
        if du < depth[u] or du <= 0:
            continue
        for v, d in graph[u]:
            cand = du - GRADE * d
            if cand > depth.get(v, -math.inf):
                depth[v] = cand
                heapq.heappush(heap, (-cand, v))
    bad = [
        (f["properties"]["way_id"], lon, lat, z)
        for f in features
        if f["properties"]["way_id"] in below_ways
        for lon, lat, z in f["geometry"]["coordinates"]
        if -z > max(depth.get((lon, lat), 0.0), 0.0) + CONE_SLACK_M
    ]
    assert not bad, f"{len(bad)} approach vertices deeper than a ramp from a tunnel: {bad[:3]}"
