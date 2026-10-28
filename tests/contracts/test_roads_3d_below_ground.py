"""qa contract tests for Phase 2 Stage B: roads below street level in roads_3d.geojson.

Written by qa-reviewer, independently of pipeline.roads_3d (imports nothing from it). Water areas
and `maxheight` tags are read here from the raw BBBike extract with osmium (data/raw/, read only);
those tests skip when the raw file is not in this checkout.

Rules checked (PLAN.md Decisions log 2026-10-26 "Visual road heights" and "Tunnels under water";
data/SOURCES.md "Below street level"):
- A real tunnel (any `tunnel` value except `building_passage` / `covered`, layer <= 0 or untagged)
  is never above street level; documented exceptions only (TUNNELS_ABOVE_GROUND). Building
  passages and positive-layer tunnels are never lowered.
- Every real tunnel with an interior vertex goes below ground (one too short to have one, e.g. a
  9 m laneway, can't); never deeper than the -10 m x level target of the deepest tunnel it joins.
- A tunnel end that the extract clipped (the OSM way continues past it) stays below ground; only
  a real portal (the OSM way ends there, or another road joins) may come up to 0.
- Under river-type water (the Yarra) a tunnel's road surface is at or below the documented
  river-bed limit, water surface - river depth - cover - tunnel roof = 0 - 6 - 5 - 6 = -17 m
  (assumptions, not measurements), everywhere along the stretch under the water (sampled every
  metre). Under shallow water (ponds, fountains) the tunnel keeps its normal depth and the report
  lists it (`under_shallow_water`).
- A road over a tunnel clears it: tunnel's numeric `maxheight` + 1.2 m (deck / roof thickness),
  else 6.5 m, and never less than the 4 m check clearance.
"""

from __future__ import annotations

import gzip
import json
import math
import re
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import pytest

from pipeline.config import BUILDINGS_BBOX

ROOT = Path(__file__).resolve().parents[2]
ROADS_3D = ROOT / "data" / "processed" / "roads_3d.geojson"
EXTRACT = ROOT / "data" / "processed" / "osm_sim_area.osm.gz"
REPORT = ROOT / "data" / "processed" / "checks" / "deck_pointcloud.json"
SOURCES = ROOT / "data" / "SOURCES.md"
RAW_PBF = ROOT / "data" / "raw" / "bbbike_melbourne.osm.pbf"

M_PER_DEG = 111_320.0
Z_TOL_M = 0.005  # z is stored to 0.01 m
TUNNEL_DEPTH_PER_LEVEL_M = 10.0  # Decisions log: tunnels ~ -10 m x level
# data/SOURCES.md: water surface 0 - river depth 6 - cover 5 - tunnel roof 6 (all assumptions)
RIVER_BED_LIMIT_M = 0.0 - 6.0 - 5.0 - 6.0
SHALLOW_WATER = {"pond", "fountain", "reflecting_pool", "basin", "wastewater", "moat", "lock"}
DECK_THICKNESS_M = 1.2  # numeric maxheight + roof (data/SOURCES.md)
DEFAULT_TUNNEL_SEPARATION_M = 6.5
CLEARANCE_M = 4.0
ROUNDING_SLACK_M = 0.03
NOT_A_STRUCTURE = {"no", "false", "0"}

# Documented exceptions: real tunnels drawn above street level.
#   203310289: OSM `indoor=yes`, `level=0` (the ground floor inside the Southern Cross station
#   buildings, tagged tunnel=yes layer=-1 because it is under a building), so not lowered; it is
#   held up at <= 5.7 m by the building passage 1278499862 it joins, which ramps down from the
#   unmeasured service bridge 1455122808 (8.2 m). Data-engineer, Stage B round 1; qa: accepted as
#   a known issue (not a tunnel in the X-ray sense).
TUNNELS_ABOVE_GROUND = {203310289}

pytestmark = pytest.mark.skipif(not ROADS_3D.exists(), reason="roads_3d.geojson not built yet")


def _metres(a: list[float], b: list[float]) -> float:
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * k, b[1] - a[1]) * M_PER_DEG


def _layer(tags: dict[str, str]) -> int:
    try:
        return int(round(float(tags.get("layer", "0").split(";")[0])))
    except ValueError:
        return 0


def _structure(tags: dict[str, str], key: str) -> bool:
    v = tags.get(key)
    return v is not None and v.strip().lower() not in NOT_A_STRUCTURE


def _lowered_tunnel(tags: dict[str, str]) -> bool:
    """A tunnel Stage B lowers: not a building passage / covered way, layer <= 0 or untagged."""
    return (
        _structure(tags, "tunnel")
        and tags.get("tunnel") not in ("building_passage", "covered")
        and _layer(tags) <= 0
    )


def _effective_layer(tags: dict[str, str]) -> int:
    layer = _layer(tags)
    if _structure(tags, "bridge") and layer <= 0:
        return 1
    if _structure(tags, "tunnel") and tags.get("tunnel") != "building_passage" and layer >= 0:
        return -1
    return layer


def _numeric_maxheight(tags: dict[str, str]) -> float | None:
    for key in ("maxheight:physical", "maxheight", "maxheight:signed"):
        m = re.fullmatch(r"\s*([0-9]+(?:\.[0-9]+)?)\s*(?:m)?\s*", tags.get(key, ""))
        if m:
            return float(m.group(1))
    return None


@pytest.fixture(scope="module")
def roads() -> dict[int, dict]:
    fc = json.loads(ROADS_3D.read_text(encoding="utf-8"))
    return {f["properties"]["way_id"]: f for f in fc["features"]}


@pytest.fixture(scope="module")
def tags() -> dict[int, dict[str, str]]:
    with gzip.open(EXTRACT) as fh:
        root = ET.parse(fh).getroot()
    return {
        int(el.get("id")): {c.get("k"): c.get("v") for c in el if c.tag == "tag"}
        for el in root.iter("way")
    }


@pytest.fixture(scope="module")
def report() -> dict:
    assert REPORT.exists(), "deck_pointcloud.json missing (reports are committed)"
    return json.loads(REPORT.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def tunnels(roads: dict, tags: dict) -> dict[int, list[list[float]]]:
    return {w: f["geometry"]["coordinates"] for w, f in roads.items() if _lowered_tunnel(tags[w])}


@pytest.fixture(scope="module")
def raw_ways(tunnels: dict) -> dict[int, tuple[list[tuple[float, float]], dict[str, str]]]:
    """Raw OSM node positions and full tags of the tunnel ways (osmium, data/raw/)."""
    if not RAW_PBF.exists():
        pytest.skip("data/raw/bbbike_melbourne.osm.pbf not available")
    osmium = pytest.importorskip("osmium")
    out = {}
    fp = osmium.FileProcessor(str(RAW_PBF), osmium.osm.NODE | osmium.osm.WAY).with_locations()
    for o in fp.with_filter(osmium.filter.KeyFilter("highway")):
        if o.is_way() and o.id in tunnels:
            pts = [(round(n.lon, 6), round(n.lat, 6)) for n in o.nodes if n.location.valid()]
            out[o.id] = (pts, dict(o.tags))
    return out


@pytest.fixture(scope="module")
def water() -> list[tuple[str, dict[str, str], object]]:
    """Water areas over the building ring (contains every tunnel), as shapely geometries."""
    if not RAW_PBF.exists():
        pytest.skip("data/raw/bbbike_melbourne.osm.pbf not available")
    osmium = pytest.importorskip("osmium")
    from shapely import wkb
    from shapely.geometry import box

    ring = box(BUILDINGS_BBOX.west, BUILDINGS_BBOX.south, BUILDINGS_BBOX.east, BUILDINGS_BBOX.north)
    keys = ("natural", "waterway", "water", "landuse")
    fab = osmium.geom.WKBFactory()
    out = []
    fp = (
        osmium.FileProcessor(str(RAW_PBF))
        .with_areas(osmium.filter.KeyFilter(*keys))
        .with_filter(osmium.filter.KeyFilter(*keys))
    )
    for o in fp:
        if not o.is_area():
            continue
        t = dict(o.tags)
        is_water = (
            t.get("natural") == "water"
            or "water" in t
            or t.get("waterway") in ("riverbank", "dock", "canal")
            or t.get("landuse") in ("reservoir", "basin")
        )
        if not is_water:
            continue
        try:
            g = wkb.loads(fab.create_multipolygon(o), hex=True)
        except RuntimeError:
            continue
        if g.intersects(ring):
            src = f"{'way' if o.from_way() else 'relation'}/{o.orig_id()}"
            out.append((src, t, g))
    assert out, "no water areas read"
    return out


def _shallow(t: dict[str, str]) -> bool:
    return t.get("water") in SHALLOW_WATER or t.get("landuse") == "basin"


def _samples(coords: list[list[float]], step_m: float = 1.0):
    """(lon, lat, z) every ~step_m along the line, z interpolated."""
    for a, b in zip(coords, coords[1:], strict=False):
        n = max(1, int(_metres(a, b) / step_m))
        for k in range(n):
            t = k / n
            yield (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]), a[2] + t * (b[2] - a[2]))
    yield tuple(coords[-1])


# --- documentation ---------------------------------------------------------------------------


def test_river_bed_limit_is_documented(report: dict):
    a = report["below_ground"]["assumptions"]
    assert a["under_water_max_z_m"] == pytest.approx(RIVER_BED_LIMIT_M)
    total = a["water_surface_z_m"] - a["river_depth_m"] - a["river_cover_m"] - a["tunnel_roof_m"]
    assert total == pytest.approx(a["under_water_max_z_m"])
    assert a["tunnel_depth_per_level_m"] == TUNNEL_DEPTH_PER_LEVEL_M
    text = " ".join(SOURCES.read_text(encoding="utf-8").split())
    assert "= **-17 m**" in text and "assumptions, not measurements" in text


# --- above / below street level --------------------------------------------------------------


def test_no_tunnel_above_ground_except_documented(tunnels: dict):
    above = {w for w, c in tunnels.items() if max(x[2] for x in c) > Z_TOL_M}
    new, fixed = sorted(above - TUNNELS_ABOVE_GROUND), sorted(TUNNELS_ABOVE_GROUND - above)
    assert above == TUNNELS_ABOVE_GROUND, f"new: {new}, fixed (drop the exception): {fixed}"


def test_building_passages_and_positive_layer_tunnels_not_lowered(roads: dict, tags: dict):
    low = [
        (w, min(x[2] for x in f["geometry"]["coordinates"]))
        for w, f in roads.items()
        if _structure(tags[w], "tunnel")
        and not _lowered_tunnel(tags[w])
        and min(x[2] for x in f["geometry"]["coordinates"]) < -Z_TOL_M
    ]
    assert not low, low


def test_tunnels_with_an_interior_vertex_go_below_ground(tunnels: dict):
    flat = [
        w
        for w, c in tunnels.items()
        if len(c) > 2 and min(x[2] for x in c) >= 0 and w not in TUNNELS_ABOVE_GROUND
    ]
    assert not flat, f"tunnels never below street level: {flat}"
    assert len([w for w, c in tunnels.items() if min(x[2] for x in c) < 0]) >= 30


def test_tunnel_depth_not_beyond_the_deepest_target_it_joins(tunnels: dict, tags: dict):
    """-10 m x max(-layer, 1); a link tunnel may come down to the deeper tunnel it joins (taken
    over the tunnels connected through shared positions)."""
    parent = {w: w for w in tunnels}

    def find(w: int) -> int:
        while parent[w] != w:
            parent[w] = parent[parent[w]]
            w = parent[w]
        return w

    at: dict[tuple[float, float], list[int]] = defaultdict(list)
    for w, c in tunnels.items():
        for x in c:
            at[(x[0], x[1])].append(w)
    for ws in at.values():
        for w in ws[1:]:
            parent[find(w)] = find(ws[0])
    target: dict[int, float] = {}
    for w in tunnels:
        t = -TUNNEL_DEPTH_PER_LEVEL_M * max(-_layer(tags[w]), 1)
        target[find(w)] = min(target.get(find(w), 0.0), t)
    deep = [
        (w, min(x[2] for x in c), target[find(w)])
        for w, c in tunnels.items()
        if min(x[2] for x in c) < target[find(w)] - Z_TOL_M
    ]
    assert not deep, deep
    # Burnley reaches its -30 m target (layer -3)
    assert min(x[2] for x in tunnels[8066124]) == pytest.approx(-30.0, abs=Z_TOL_M)


def test_clipped_tunnel_ends_stay_below_ground(roads: dict, tunnels: dict, raw_ways: dict):
    """Only a real portal comes up to street level: a tunnel end that no other road shares and
    that is not the OSM way's own end is a clip end, so it must be below ground (Burnley's east
    end at the TUNNEL_EXTRACT_BBOX edge). The Domain's east end is the OSM way's first node (its
    real portal; the road outside is beyond the extract), so 0 there is right."""
    shared: dict[tuple[float, float], int] = defaultdict(int)
    for f in roads.values():
        for x in f["geometry"]["coordinates"]:
            shared[(x[0], x[1])] += 1
    bad, clip_ends = [], []
    for w, c in tunnels.items():
        pts, _ = raw_ways[w]
        for end in (c[0], c[-1]):
            key = (end[0], end[1])
            if shared[key] > 1 or key in (pts[0], pts[-1]):
                continue  # a junction or the OSM way's own end (portal / dead end)
            clip_ends.append((w, end[2]))
            if end[2] >= 0:
                bad.append((w, end))
    assert not bad, f"clipped tunnel ends at street level: {bad}"
    assert (8066124, -30.0) in clip_ends, clip_ends
    domain_east = tunnels[8066126][0]
    assert (domain_east[0], domain_east[1]) == raw_ways[8066126][0][0]
    assert domain_east[2] == 0.0


# --- under water -----------------------------------------------------------------------------


def test_tunnels_under_the_river_are_below_the_river_bed(tunnels: dict, water, report: dict):
    from shapely.geometry import Point
    from shapely.prepared import prep

    deep = [(src, t, prep(g)) for src, t, g in water if not _shallow(t)]
    shallow = [(src, t, prep(g)) for src, t, g in water if _shallow(t)]
    listed = {
        r["way"]: {s["osm"] for s in r["under_shallow_water"]}
        for r in report["below_ground"]["tunnels"]
    }
    wet: dict[int, list[float]] = defaultdict(list)
    above_limit, unlisted = [], []
    for w, c in tunnels.items():
        for lon, lat, z in _samples(c):
            p = Point(lon, lat)
            for src, _, g in deep:
                if g.contains(p):
                    wet[w].append(z)
                    if z > RIVER_BED_LIMIT_M + Z_TOL_M:
                        above_limit.append((w, src, round(lon, 6), round(lat, 6), round(z, 2)))
            for src, _, g in shallow:
                if g.contains(p) and src not in listed.get(w, set()):
                    unlisted.append((w, src))
    assert not above_limit, f"{len(above_limit)} samples above the river bed: {above_limit[:5]}"
    assert not unlisted, f"tunnels under shallow water not in the report: {sorted(set(unlisted))}"
    # not vacuous: both CityLink tunnels cross the Yarra inside the tunnel clip
    assert {8066124, 8066126} <= wet.keys(), sorted(wet)
    assert max(wet[8066124]) == pytest.approx(-30.0, abs=Z_TOL_M)  # Burnley: -30 m all the way
    assert max(wet[8066126]) <= RIVER_BED_LIMIT_M


# --- roads over tunnels ----------------------------------------------------------------------


def test_roads_over_tunnels_clear_them(roads: dict, tags: dict, tunnels: dict, raw_ways: dict):
    """Every 2D crossing (no shared position) of a road over a lowered tunnel: upper minus tunnel
    >= numeric maxheight + 1.2 m, else 6.5 m (never < 4 m)."""
    from shapely import STRtree
    from shapely.geometry import LineString

    kx = M_PER_DEG * math.cos(math.radians(-37.82))

    def xy(lon: float, lat: float) -> tuple[float, float]:
        return (lon * kx, lat * M_PER_DEG)

    def z_at(c: list[list[float]], p: tuple[float, float]) -> float:
        best = (math.inf, 0.0)
        for a, b in zip(c, c[1:], strict=False):
            (ax, ay), (bx, by) = xy(a[0], a[1]), xy(b[0], b[1])
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
            d = math.hypot(ax + t * dx - p[0], ay + t * dy - p[1])
            if d < best[0]:
                best = (d, a[2] + t * (b[2] - a[2]))
        return best[1]

    ids = list(roads)
    coords = [roads[w]["geometry"]["coordinates"] for w in ids]
    lines = [LineString([xy(x[0], x[1]) for x in c]) for c in coords]
    tree = STRtree(lines)
    short, n = [], 0
    for w, c in tunnels.items():
        tl = LineString([xy(x[0], x[1]) for x in c])
        mh = _numeric_maxheight(raw_ways[w][1])
        need = max(mh + DECK_THICKNESS_M, CLEARANCE_M) if mh is not None else 6.5
        tv = {(x[0], x[1]) for x in c}
        for j in tree.query(tl, predicate="intersects").tolist():
            u = ids[j]
            if u == w or _effective_layer(tags[u]) <= _effective_layer(tags[w]):
                continue
            shared = [xy(*v) for v in tv & {(x[0], x[1]) for x in coords[j]}]
            inter = tl.intersection(lines[j])
            for g in getattr(inter, "geoms", [inter]):
                if g.is_empty:
                    continue
                q = g if g.geom_type == "Point" else g.interpolate(0.5, normalized=True)
                if any(math.hypot(q.x - s[0], q.y - s[1]) <= 0.1 for s in shared):
                    continue  # a junction, not a crossing
                n += 1
                dz = z_at(coords[j], (q.x, q.y)) - z_at(c, (q.x, q.y))
                if dz < need - ROUNDING_SLACK_M:
                    short.append((u, w, round(dz, 2), need))
    assert n >= 40, n  # 45 road-over-tunnel crossings in Stage B
    assert not short, f"{len(short)} roads too close above a tunnel: {short[:5]}"
