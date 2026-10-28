"""Contract test for sim/out/edge_geometry.json (Phase 3A, contract added 2026-10-27).

Written by qa-reviewer, independently of sim/ (it imports nothing from sim/: the network is read by
tests/contracts/roads_net_helpers.py with ElementTree + pyproj, the bboxes come from
pipeline/config.py).

CLAUDE.md "Data contracts": an array of `{edge_id, way_id, path: [[lon, lat, z_m], ...]}` for every
non-internal car edge in sim/net/roads.net.xml; z from data/processed/roads_3d.geojson (same height
model as the trips export).

What it checks:
- Exact keys; `edge_id` a unique string, `way_id` an int, `path` >= 2 finite `[lon, lat, z]`.
- Coverage: the edge ids are EXACTLY the non-internal edges with a lane passenger cars may use
  (independent reading of each lane's allow / disallow), and every edge in any
  edge_stats_<scenario>.parquet has a geometry (the web needs a shape for every coloured edge).
- `way_id` is one of the edge's OSM ways (lane param origId), and on edges carrying several ways it
  is the way nearest to most of the path (independent estimate) on at least WAY_AGREE of them (the
  rest are near-ties: two ways covering about the same length).
- The path follows the network in driving direction: its ends lie within END_TOL_M of the
  carriageway centre's ends (mean of the lane shapes' ends; the middle lane where lanes have
  different vertex counts, as documented), and its plan length matches that centreline.
- Bbox rule (the same as the trips contract, test_trips_baseline_contract.py): every point inside
  SIM_EXTRACT_BBOX give or take BBOX_SLACK_M (lane offsets past the clip), or within TUNNEL_SNAP_M
  of a CityLink motorway tunnel line in roads_3d and inside TUNNEL_EXTRACT_BBOX. Swapped lon/lat
  would be off by ~180 km.
- Heights: z equals roads_3d's height of the nearest of the edge's ways at that spot (interpolated
  along the way's 3D line) within Z_ROADS_TOL_M on >= 99% of points and within Z_ROADS_MAX_M on all;
  consecutive points never step more than 1 m (Decisions log). Agreement with the trips export
  (where a car is, the road drawn under it has its height): for the trips points within
  TRIPS_NEAR_M of an edge line, >= 99% have an edge line nearby within 0.5 m in height (the rest are
  cars on junction-internal lanes under an elevated edge, which have no geometry of their own).
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from functools import cache
from pathlib import Path

import pytest

from pipeline.config import SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX

from .roads_net_helpers import NET_PATH, load_net

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "sim" / "out"
GEOMETRY = OUT / "edge_geometry.json"
ROADS_3D = ROOT / "data" / "processed" / "roads_3d.geojson"

KEYS = {"edge_id", "way_id", "path"}
M_PER_DEG = 111_320.0
BBOX_SLACK_M = 5.0  # same as test_trips_baseline_contract.py
TUNNEL_SNAP_M = 10.0  # same as test_trips_contract.py
END_TOL_M = 0.5  # 6 decimals of a degree are ~0.1 m
LENGTH_TOL = 0.01  # 1 % (+ END_TOL_M) of the centreline length
Z_ROUND_M = 0.01
MAX_Z_STEP_M = 1.0
Z_ROADS_TOL_M = 0.1
Z_ROADS_MAX_M = 2.0
TRIPS_NEAR_M = 6.0
TRIPS_Z_TOL_M = 0.5
WAY_AGREE = 0.9

pytestmark = pytest.mark.skipif(
    not GEOMETRY.exists(), reason="sim/out/edge_geometry.json not exported"
)

LAT0 = (SIM_EXTRACT_BBOX.south + SIM_EXTRACT_BBOX.north) / 2
MX = M_PER_DEG * math.cos(math.radians(LAT0))


def _m(lon: float, lat: float) -> tuple[float, float]:
    """Local metres (equirectangular at the extract's mid latitude): plenty for metre tolerances."""
    return ((lon - 144.95) * MX, (lat - LAT0) * M_PER_DEG)


@cache
def _geometry() -> list:
    return json.loads(GEOMETRY.read_text(encoding="utf-8"))


@cache
def _roads_3d() -> list:
    return json.loads(ROADS_3D.read_text(encoding="utf-8"))["features"]


def _is_num(x: object) -> bool:
    return isinstance(x, int | float) and not isinstance(x, bool) and math.isfinite(x)


def _lane_allows_cars(lane: dict[str, str]) -> bool:
    if "allow" in lane:
        allow = lane["allow"].split()
        return "passenger" in allow or "all" in allow
    if "disallow" in lane:
        dis = lane["disallow"].split()
        return "passenger" not in dis and "all" not in dis
    return True


def _lane_shape(lane: dict[str, str]) -> list[tuple[float, float]]:
    return [tuple(float(v) for v in p.split(",")[:2]) for p in lane["shape"].split()]


def _centre(edge) -> list[tuple[float, float]]:
    """Carriageway centre in net x/y: the documented rule (mean of lane shapes, or middle lane)."""
    shapes = [_lane_shape(ln) for ln in edge.lanes]
    if len({len(s) for s in shapes}) == 1:
        return [
            (sum(s[k][0] for s in shapes) / len(shapes), sum(s[k][1] for s in shapes) / len(shapes))
            for k in range(len(shapes[0]))
        ]
    return shapes[len(shapes) // 2]


def _length(pts: list[tuple[float, float]]) -> float:
    return sum(math.dist(a, b) for a, b in zip(pts, pts[1:], strict=False))


def test_entries_have_the_contract_shape():
    g = _geometry()
    assert isinstance(g, list) and g, "edge_geometry.json must be a non-empty array"
    bad_keys, bad_ids, bad_way, bad_path = [], [], [], []
    for k, e in enumerate(g):
        if not isinstance(e, dict) or set(e) != KEYS:
            bad_keys.append((k, sorted(e) if isinstance(e, dict) else type(e).__name__))
            continue
        if not isinstance(e["edge_id"], str) or not e["edge_id"]:
            bad_ids.append((k, e["edge_id"]))
        if not isinstance(e["way_id"], int) or isinstance(e["way_id"], bool):
            bad_way.append((e["edge_id"], e["way_id"]))
        p = e["path"]
        if not (
            isinstance(p, list)
            and len(p) >= 2
            and all(isinstance(q, list) and len(q) == 3 and all(_is_num(v) for v in q) for q in p)
        ):
            bad_path.append(e["edge_id"])
    assert not bad_keys, f"entries without exactly {sorted(KEYS)}: {bad_keys[:10]}"
    assert not bad_ids, f"edge_id not a non-empty string: {bad_ids[:10]}"
    assert not bad_way, f"way_id not an int: {bad_way[:10]}"
    assert not bad_path, f"path not >= 2 [lon, lat, z] numbers: {bad_path[:10]}"
    ids = [e["edge_id"] for e in g]
    dupes = [i for i, n in Counter(ids).items() if n > 1]
    assert not dupes, f"duplicate edge ids: {dupes[:10]}"


def test_edge_ids_are_exactly_the_car_edges_of_the_network():
    net = load_net(NET_PATH)
    car = {e.id for e in net.edges.values() if any(_lane_allows_cars(ln) for ln in e.lanes)}
    ids = {e["edge_id"] for e in _geometry()}
    assert not ids - car, f"edge ids not car edges of roads.net.xml: {sorted(ids - car)[:10]}"
    assert not car - ids, f"car edges with no geometry: {sorted(car - ids)[:10]}"


def test_every_edge_with_stats_has_a_geometry():
    files = sorted(OUT.glob("edge_stats_*.parquet"))
    if not files:
        pytest.skip("no edge_stats_*.parquet yet")
    import pyarrow.parquet as pq

    ids = {e["edge_id"] for e in _geometry()}
    for f in files:
        with f.open("rb") as fh:  # a file object: see test_edge_stats_contract._table (libsumo)
            missing = set(pq.read_table(fh, columns=["edge_id"]).column(0).to_pylist()) - ids
        assert not missing, f"{f.name}: edges with stats but no geometry: {sorted(missing)[:10]}"


def test_way_id_is_an_osm_way_of_the_edge_and_covers_most_of_it():
    from shapely.geometry import LineString

    net = load_net(NET_PATH)
    not_origin = [
        (e["edge_id"], e["way_id"])
        for e in _geometry()
        if e["way_id"] not in net.edges[e["edge_id"]].ways
    ]
    assert not not_origin, f"way_id not among the edge's origId ways: {not_origin[:10]}"
    if not ROADS_3D.exists():
        pytest.skip("roads_3d.geojson not built")
    lines: dict[int, list] = defaultdict(list)
    for f in _roads_3d():
        c = f["geometry"]["coordinates"]
        if len(c) >= 2:
            lines[f["properties"]["way_id"]].append(LineString([_m(p[0], p[1]) for p in c]))
    agree = total = 0
    disagree = []
    for e in _geometry():
        ways = [w for w in net.edges[e["edge_id"]].ways if w in lines]
        if len(ways) < 2:
            continue
        path = LineString([_m(p[0], p[1]) for p in e["path"]])
        n = max(2, int(path.length) + 1)  # a sample per metre
        share: Counter = Counter()
        for k in range(n):
            pt = path.interpolate(k / (n - 1), normalized=True)
            share[min(ways, key=lambda w: min(ln.distance(pt) for ln in lines[w]))] += 1
        total += 1
        if share.most_common(1)[0][0] == e["way_id"]:
            agree += 1
        else:
            disagree.append((e["edge_id"], e["way_id"], dict(share.most_common(3))))
    assert total, "no multi-way edges to check"
    assert agree / total >= WAY_AGREE, (
        f"way_id is the majority way on only {agree}/{total} multi-way edges; e.g. {disagree[:10]}"
    )


def test_paths_follow_the_network_in_driving_direction():
    net = load_net(NET_PATH)
    bad_ends, bad_len = [], []
    for e in _geometry():
        edge = net.edges[e["edge_id"]]
        centre = [_m(*net.lonlat(x, y)) for x, y in _centre(edge)]
        path = [_m(p[0], p[1]) for p in e["path"]]
        d0, d1 = math.dist(path[0], centre[0]), math.dist(path[-1], centre[-1])
        if d0 > END_TOL_M or d1 > END_TOL_M:
            bad_ends.append((e["edge_id"], round(d0, 2), round(d1, 2)))
        lc, lp = _length(centre), _length(path)
        if abs(lp - lc) > LENGTH_TOL * lc + END_TOL_M:
            bad_len.append((e["edge_id"], round(lp, 1), round(lc, 1)))
    assert not bad_ends, (
        f"{len(bad_ends)} paths whose ends are > {END_TOL_M} m from the carriageway centre's ends "
        f"(wrong direction or shape): {bad_ends[:10]}"
    )
    assert not bad_len, f"path length differs from the carriageway centre: {bad_len[:10]}"


def test_points_are_lon_lat_inside_the_extract_or_on_a_citylink_tunnel():
    from shapely import STRtree
    from shapely.geometry import LineString, Point

    b, t = SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX
    dlon, dlat = BBOX_SLACK_M / MX, BBOX_SLACK_M / M_PER_DEG

    def inside(bb, lon, lat):
        return bb.west - dlon <= lon <= bb.east + dlon and bb.south - dlat <= lat <= bb.north + dlat

    tunnels = [
        LineString([_m(p[0], p[1]) for p in f["geometry"]["coordinates"]])
        for f in (_roads_3d() if ROADS_3D.exists() else [])
        if f["properties"].get("tunnel") is True
        and f["properties"].get("highway") in ("motorway", "motorway_link")
        and len(f["geometry"]["coordinates"]) >= 2
    ]
    tree = STRtree(tunnels) if tunnels else None
    outside = []
    for e in _geometry():
        for lon, lat, _ in e["path"]:
            if inside(b, lon, lat):
                continue
            on_tunnel = tree is not None and len(
                tree.query(Point(_m(lon, lat)), predicate="dwithin", distance=TUNNEL_SNAP_M)
            )
            if not (on_tunnel and inside(t, lon, lat)):
                outside.append((e["edge_id"], lon, lat))
    assert not outside, (
        f"{len(outside)} points more than {BBOX_SLACK_M} m outside SIM_EXTRACT_BBOX and not on a "
        f"CityLink tunnel inside TUNNEL_EXTRACT_BBOX (or lat/lon swapped): {outside[:10]}"
    )


def _z_on(line: list[tuple[float, float, float]], s: float) -> float:
    acc = 0.0
    for a, b in zip(line, line[1:], strict=False):
        seg = math.dist(a[:2], b[:2])
        if acc + seg >= s:
            f = (s - acc) / seg if seg else 0.0
            return a[2] + f * (b[2] - a[2])
        acc += seg
    return line[-1][2]


def test_heights_are_roads_3d_heights_of_the_edge_ways_and_ramp_smoothly():
    if not ROADS_3D.exists():
        pytest.skip("roads_3d.geojson not built")
    from shapely.geometry import LineString, Point

    net = load_net(NET_PATH)
    ways: dict[int, list] = defaultdict(list)
    for f in _roads_3d():
        c = f["geometry"]["coordinates"]
        if len(c) >= 2:
            pts = [(*_m(p[0], p[1]), p[2]) for p in c]
            ways[f["properties"]["way_id"]].append((LineString([p[:2] for p in pts]), pts))
    diffs, worst, steps, no_way = [], [], [], []
    for e in _geometry():
        cand = [ln for w in net.edges[e["edge_id"]].ways for ln in ways.get(w, [])]
        if not cand:
            no_way.append(e["edge_id"])
        path = e["path"]
        for k, (lon, lat, z) in enumerate(path):
            if k and abs(z - path[k - 1][2]) > MAX_Z_STEP_M + Z_ROUND_M:
                steps.append((e["edge_id"], k, path[k - 1][2], z))
            if not cand:
                continue
            pt = Point(_m(lon, lat))
            geom, pts = min(cand, key=lambda c: c[0].distance(pt))
            d = abs(z - _z_on(pts, geom.project(pt)))
            diffs.append(d)
            if d > Z_ROADS_MAX_M:
                worst.append((e["edge_id"], k, z, round(z - d, 2)))
    assert not steps, f"height steps over {MAX_Z_STEP_M} m between points: {steps[:10]}"
    assert len(no_way) <= 0.01 * len(_geometry()), f"edges with no roads_3d way: {no_way[:10]}"
    within = sum(d <= Z_ROADS_TOL_M for d in diffs) / len(diffs)
    assert within >= 0.99, f"only {within:.3%} of points within {Z_ROADS_TOL_M} m of roads_3d"
    assert not worst, f"points more than {Z_ROADS_MAX_M} m off roads_3d: {worst[:10]}"


def test_heights_agree_with_the_trips_export():
    trips_path = OUT / "trips_baseline.json"
    if not trips_path.exists():
        pytest.skip("trips_baseline.json not exported")
    from shapely import STRtree
    from shapely.geometry import LineString, Point

    segs, ends = [], []
    for e in _geometry():
        p = e["path"]
        for a, b in zip(p, p[1:], strict=False):
            segs.append(LineString([_m(a[0], a[1]), _m(b[0], b[1])]))
            ends.append((a[2], b[2]))
    tree = STRtree(segs)
    trips = json.loads(trips_path.read_text(encoding="utf-8"))
    pts = [q for tr in trips for q in tr["path"]][::5]  # every 5th point: deterministic sample
    near = ok = 0
    off = []
    for lon, lat, z in pts:
        pt = Point(_m(lon, lat))
        idx = tree.query(pt, predicate="dwithin", distance=TRIPS_NEAR_M)
        if not len(idx):
            continue  # junction-internal: no edge geometry there
        near += 1
        best = min(
            abs(ends[i][0] + segs[i].project(pt, normalized=True) * (ends[i][1] - ends[i][0]) - z)
            for i in idx
        )
        if best <= TRIPS_Z_TOL_M:
            ok += 1
        else:
            off.append((lon, lat, z, round(best, 2)))
    assert near > 1000, f"too few trips points near the edge lines ({near}): wrong coordinates?"
    assert ok / near >= 0.99, (
        f"only {ok}/{near} trips points have an edge line within {TRIPS_Z_TOL_M} m in height; "
        f"e.g. {off[:10]}"
    )
