"""Grade-separation check: what goes over or under what in roads_3d.geojson (reports, never fixes).

Usage:
    uv run python -m pipeline.checks.grade_separation

Inputs: data/processed/osm_sim_area.osm.gz (topology: which OSM nodes each way uses, tags) and
data/processed/roads_3d.geojson (z). data/raw/bbbike_melbourne.osm.pbf is read too, when present,
only to explain bridge ends (was the way clipped at the extract edge? what non-drivable OSM ways
use a dangling end node?) and to give service roads their `service=*` subtype.

Outputs (not contract files, see data/SOURCES.md "Derived outputs"):
    data/processed/checks/grade_separation.json           summary + every record
    data/processed/checks/grade_separation_flags.geojson  a Point per flagged crossing / bridge end

1. Crossings: every place two ways' 2D lines cross, touch or overlap WITHOUT a shared OSM node.
   Points where the two ways share a node are junctions and are ignored. Kinds:
       cross           the lines cross between nodes
       touch           a way's end node lies on the other way, but is not a node of it
       coincident      two different OSM nodes at the same position (an unjoined duplicate)
       overlap         collinear segments without shared nodes (point = middle of the overlap)
   z for each way is interpolated along its roads_3d line at the crossing point.

2. Classes (effective layer: a bridge with layer <= 0 counts as 1, as in roads_3d; a real
   tunnel, i.e. not `tunnel=building_passage`, with layer >= 0 counts as -1):
       separated_ok            layers differ, upper way higher by >= CLEARANCE_M
       separated_low           layers differ, upper way higher by FLAT_EPS_M .. CLEARANCE_M
       order_mismatch          layers differ, upper way NOT higher by more than FLAT_EPS_M
                               (`detail` = "inverted" if the lower way is higher, else "flat")
       same_layer_no_junction  same effective layer, no shared node
       pending_tunnel_depth    (Stage A only, kept for the report schema; always 0 since Stage B
                               gives tunnels depth: a real tunnel under another way is classified
                               like any crossing, so one left at street level is order_mismatch)
       pending_trench_depth    layers differ, upper not CLEARANCE_M above, and the lower way runs
                               in a cutting here that roads_3d still lists in deck_pointcloud.json
                               `pending_trench_depth` (Stage B lowers the CityLink cutting to its
                               measured depth, so none are expected). `stage_a_class` = the class
                               it gets without that listing.
   Pairs of two `highway=service` ways are flagged `noise_candidate` (car park aisles, driveways);
   they stay in every list and the summary reports counts with and without them.

   Why CLEARANCE_M = 4.0 m: roads_3d puts each upper deck a measured separation (City of Melbourne
   point cloud, or OSM maxheight + deck) or the 6.5 m default above what is beneath it, so a
   separated crossing is normally >= ~5 m. 4.0 m flags anything clearly lower: below it a rendered
   car (~1.5 m) on the lower road would visibly graze the upper deck ribbon, and it is under the
   4.3 m Australian vehicle height limit, so a truck could not physically pass. Counts at other
   thresholds are in the summary (`clearance_sensitivity`) so the choice is visible.

   Cause of any order_mismatch / separated_low: roads_3d drops a crossing from its clearance rule
   only when the lower road joins the upper deck too closely for the grade limits (it lists them
   in its build summary); then the lower road is ramped up from a deck nearby. For these records
   `lifted_by_*` names the deck along the road graph whose 6 % cone is highest at the crossing,
   its height there (from roads_3d), the distance to it and the grade that would be needed to get
   CLEARANCE_M under the upper road (`grade_needed`).

3. Bridge ends: both end nodes of every bridge way must be used by another way in the extract,
   or be where the extract clipped the way (at its bbox edge). Otherwise the deck is dangling.
   For each dangling end, `raw_osm_ways_at_node` lists EVERY way in the regional extract that uses
   the node (buildings, car parks, barriers, railways, walls, not only roads) with its key tags,
   so the reason the deck stops there is visible.

4. Mid-structure junctions (extra, informational): a node in the middle of a bridge or real
   tunnel shared with a non-structure way on another layer, i.e. a ground road joined to the
   middle of a deck. Real access ramps do this, but so does a wrongly connected flyover.
"""

from __future__ import annotations

import gzip
import hashlib
import heapq
import json
import math
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from shapely.geometry import LineString, Point
from shapely.strtree import STRtree

from pipeline.config import REVIEW_AREAS, SIM_EXTRACT_BBOX, BBox

REPO_ROOT = Path(__file__).resolve().parents[2]
EXTRACT_PATH = REPO_ROOT / "data" / "processed" / "osm_sim_area.osm.gz"
ROADS_3D_PATH = REPO_ROOT / "data" / "processed" / "roads_3d.geojson"
RAW_PBF = REPO_ROOT / "data" / "raw" / "bbbike_melbourne.osm.pbf"
OUT_DIR = REPO_ROOT / "data" / "processed" / "checks"
OUT_JSON = OUT_DIR / "grade_separation.json"
OUT_FLAGS = OUT_DIR / "grade_separation_flags.geojson"

CLEARANCE_M = 4.0
LIFT_SEARCH_M = 500.0
RAMP_GRADE = 0.06  # roads_3d MAX_GRADE
FLAT_EPS_M = 0.5
SENSITIVITY_M = (2.0, 3.0, 4.0, 5.0, 5.4, 6.0)
POINT_TOL_M = 0.05  # an intersection point this close to a shared node is that node
MERGE_TOL_M = 1.0  # intersection points of one pair closer than this are one crossing
EDGE_FALLBACK_M = 100.0  # without the raw PBF: an end this close to the bbox edge counts as clipped
NEAR_MISS_M = 15.0  # dangling ends: report drivable ways passing within this distance

CLASSES = (
    "separated_ok",
    "separated_low",
    "order_mismatch",
    "same_layer_no_junction",
    "pending_tunnel_depth",
    "pending_trench_depth",
)
DECK_REPORT = OUT_DIR / "deck_pointcloud.json"
TRENCH_NEAR_M = 25.0  # a crossing this close to a listed cutting crossing of the same pair
NOT_A_STRUCTURE = frozenset({"no", "false", "0"})
_M_PER_DEG_LAT = 111_320.0


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def repo_key(path: Path) -> str:
    """Repo-relative path (as reports key their inputs), else the absolute path."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        try:
            return str(path.absolute().relative_to(REPO_ROOT))
        except ValueError:
            return str(path)


# --- tags ---------------------------------------------------------------------------------------


def parse_layer(value: str | None) -> int:
    """OSM `layer` as an int; 0 when missing or unparseable ("1;2" -> 1). Same rule as roads_3d."""
    if value is None:
        return 0
    try:
        return int(round(float(value.split(";")[0].strip())))
    except ValueError:
        return 0


def _structure(tags: dict[str, str], key: str) -> str | None:
    v = tags.get(key)
    if v is None or v.strip().lower() in NOT_A_STRUCTURE:
        return None
    return v.strip().lower()


def is_bridge(tags: dict[str, str]) -> bool:
    return _structure(tags, "bridge") is not None


def is_real_tunnel(tags: dict[str, str]) -> bool:
    """A tunnel Stage B will lower (any tunnel except `building_passage`)."""
    t = _structure(tags, "tunnel")
    return t is not None and t != "building_passage"


def effective_layer(tags: dict[str, str]) -> int:
    layer = parse_layer(tags.get("layer"))
    if is_bridge(tags):
        return max(layer, 1)
    if is_real_tunnel(tags):
        return min(layer, -1)
    return layer


def pending_tunnel(tags: dict[str, str]) -> bool:
    """A real tunnel at or below ground (roads_3d gives it depth in Stage B)."""
    return is_real_tunnel(tags) and parse_layer(tags.get("layer")) <= 0


# --- geometry -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Projection:
    """Local equirectangular metres around a centre point (accurate to <0.1 % over a few km)."""

    lon0: float
    lat0: float

    @classmethod
    def for_bbox(cls, bbox: BBox) -> Projection:
        return cls((bbox.west + bbox.east) / 2, (bbox.south + bbox.north) / 2)

    @property
    def kx(self) -> float:
        return _M_PER_DEG_LAT * math.cos(math.radians(self.lat0))

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * _M_PER_DEG_LAT)

    def lonlat(self, x: float, y: float) -> tuple[float, float]:
        return (self.lon0 + x / self.kx, self.lat0 + y / _M_PER_DEG_LAT)


@dataclass
class Way:
    id: int
    nodes: list[int]
    tags: dict[str, str]


@dataclass
class Network:
    ways: dict[int, Way]
    coords: dict[int, tuple[float, float]]  # OSM node id -> (lon, lat)
    lines3d: dict[int, list[list[float]]]  # way id -> roads_3d coordinates [[lon, lat, z], ...]


def read_extract(path: Path) -> tuple[dict[int, Way], dict[int, tuple[float, float]]]:
    """Ways and node coordinates from the OSM XML extract (standard library only)."""
    ways: dict[int, Way] = {}
    coords: dict[int, tuple[float, float]] = {}
    with gzip.open(path, "rb") as fh:
        for _, el in ET.iterparse(fh, events=("end",)):
            if el.tag == "node":
                coords[int(el.get("id"))] = (float(el.get("lon")), float(el.get("lat")))
                el.clear()
            elif el.tag == "way":
                nodes = [int(nd.get("ref")) for nd in el.iter("nd")]
                tags = {t.get("k"): t.get("v") for t in el.iter("tag")}
                ways[int(el.get("id"))] = Way(int(el.get("id")), nodes, tags)
                el.clear()
    return ways, coords


def read_roads_3d(path: Path) -> dict[int, list[list[float]]]:
    fc = json.loads(path.read_text())
    return {f["properties"]["way_id"]: f["geometry"]["coordinates"] for f in fc["features"]}


def z_along(line3d: list[list[float]], proj: Projection, x: float, y: float) -> float:
    """z of a roads_3d line at the point on it nearest (x, y), linearly interpolated."""
    xy = [proj.xy(c[0], c[1]) for c in line3d]
    zs = [c[2] for c in line3d]
    if len(xy) == 1:
        return float(zs[0])
    ls = LineString(xy)
    d = ls.project(Point(x, y))
    seg = np.hypot(*np.diff(np.asarray(xy), axis=0).T)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    return float(np.interp(d, cum, zs))


# --- crossings ----------------------------------------------------------------------------------


def _points_of(geom: Any) -> tuple[list[tuple[float, float]], list[LineString]]:
    pts: list[tuple[float, float]] = []
    lines: list[LineString] = []
    for part in shapely.get_parts(geom):
        if part.is_empty:
            continue
        if part.geom_type == "Point":
            pts.append((part.x, part.y))
        elif part.geom_type == "LineString":
            lines.append(part)
        else:  # GeometryCollection etc. (rare): recurse
            p, ln = _points_of(part)
            pts += p
            lines += ln
    return pts, lines


def _near(p: tuple[float, float], qs: list[tuple[float, float]], tol: float) -> bool:
    return any(math.hypot(p[0] - q[0], p[1] - q[1]) <= tol for q in qs)


def find_crossings(net: Network, proj: Projection) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Every crossing/touch/overlap of two ways without a shared OSM node (one dict each)."""
    ids = sorted(net.ways)
    xy_of = {n: proj.xy(*ll) for n, ll in net.coords.items()}
    geoms = [LineString([xy_of[n] for n in net.ways[w].nodes]) for w in ids]
    tree = STRtree(geoms)
    left, right = tree.query(geoms, predicate="intersects")
    stats = Counter()
    out: list[dict[str, Any]] = []
    for i, j in zip(left.tolist(), right.tolist(), strict=True):
        if i >= j:
            continue
        wa, wb = net.ways[ids[i]], net.ways[ids[j]]
        stats["intersecting_pairs"] += 1
        shared = set(wa.nodes) & set(wb.nodes)
        shared_xy = [xy_of[n] for n in shared]
        pts, lines = _points_of(geoms[i].intersection(geoms[j]))
        found: list[tuple[tuple[float, float], str]] = []
        for ln in lines:
            ends = [ln.coords[0], ln.coords[-1]]
            if all(_near(e, shared_xy, POINT_TOL_M) for e in ends):
                stats["shared_segment_overlaps_ignored"] += 1
                continue
            m = ln.interpolate(0.5, normalized=True)
            found.append(((m.x, m.y), "overlap"))
        ends_xy = [xy_of[w.nodes[k]] for w in (wa, wb) for k in (0, -1)]
        verts_a = [xy_of[n] for n in wa.nodes if n not in shared]
        verts_b = [xy_of[n] for n in wb.nodes if n not in shared]
        for p in pts:
            if _near(p, shared_xy, POINT_TOL_M):
                stats["shared_node_touches_ignored"] += 1
                continue
            if _near(p, verts_a, POINT_TOL_M) and _near(p, verts_b, POINT_TOL_M):
                kind = "coincident"
            elif _near(p, ends_xy, POINT_TOL_M):
                kind = "touch"
            else:
                kind = "cross"
            found.append((p, kind))
        kept: list[tuple[tuple[float, float], str]] = []
        for p, kind in found:
            if not _near(p, [q for q, _ in kept], MERGE_TOL_M):
                kept.append((p, kind))
        for (x, y), kind in kept:
            out.append({"a": wa.id, "b": wb.id, "x": x, "y": y, "kind": kind})
    return out, dict(stats)


def classify(
    tags_a: dict[str, str],
    za: float,
    tags_b: dict[str, str],
    zb: float,
    clearance: float = CLEARANCE_M,
) -> tuple[str, str, str]:
    """(class, stage_a_class, detail) for one crossing. See the module docstring."""
    la, lb = effective_layer(tags_a), effective_layer(tags_b)
    if la == lb:
        return "same_layer_no_junction", "same_layer_no_junction", ""
    (lu, zu, _), (_, zl, tl) = sorted(
        [(la, za, tags_a), (lb, zb, tags_b)], key=lambda t: t[0], reverse=True
    )
    dz = zu - zl
    if dz >= clearance:
        now, detail = "separated_ok", ""
    elif dz > FLAT_EPS_M:
        now, detail = "separated_low", ""
    else:
        now, detail = "order_mismatch", ("inverted" if dz < -FLAT_EPS_M else "flat")
    return now, now, detail


def _way_props(w: Way, suffix: str, z: float, service_types: dict[int, str]) -> dict[str, Any]:
    t = w.tags
    return {
        f"way_{suffix}": w.id,
        f"name_{suffix}": t.get("name"),
        f"highway_{suffix}": t.get("highway"),
        f"service_{suffix}": service_types.get(w.id),
        f"layer_{suffix}": parse_layer(t.get("layer")),
        f"effective_layer_{suffix}": effective_layer(t),
        f"bridge_{suffix}": t.get("bridge") if is_bridge(t) else None,
        f"tunnel_{suffix}": _structure(t, "tunnel"),
        f"z_{suffix}_m": round(z, 2),
    }


def review_area(lon: float, lat: float) -> str | None:
    for name, b in REVIEW_AREAS.items():
        if b.west <= lon <= b.east and b.south <= lat <= b.north:
            return name
    return None


def read_trenches(path: Path = DECK_REPORT) -> list[dict[str, Any]]:
    """Crossings over a road in a cutting, as roads_3d listed them (empty if no report)."""
    if not path.exists():
        return []
    return json.loads(path.read_text()).get("pending_trench_depth", [])


def _trench_at(
    trenches: list[dict[str, Any]], a: int, b: int, x: float, y: float, proj: Projection
) -> dict[str, Any] | None:
    for t in trenches:
        if {t["upper_way"], t["lower_way"]} == {a, b}:
            tx, ty = proj.xy(t["lon"], t["lat"])
            if math.hypot(tx - x, ty - y) <= TRENCH_NEAR_M:
                return t
    return None


def crossing_records(
    net: Network,
    proj: Projection,
    service_types: dict[int, str] | None = None,
    clearance: float = CLEARANCE_M,
    trenches: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    service_types = service_types or {}
    trenches = trenches or []
    raw, stats = find_crossings(net, proj)
    recs = []
    for c in raw:
        wa, wb = net.ways[c["a"]], net.ways[c["b"]]
        za = z_along(net.lines3d[wa.id], proj, c["x"], c["y"])
        zb = z_along(net.lines3d[wb.id], proj, c["x"], c["y"])
        cls, now, detail = classify(wa.tags, za, wb.tags, zb, clearance)
        trench = None
        if cls in ("separated_low", "order_mismatch"):
            trench = _trench_at(trenches, wa.id, wb.id, c["x"], c["y"], proj)
            if trench is not None:
                cls = "pending_trench_depth"
        la, lb = effective_layer(wa.tags), effective_layer(wb.tags)
        dz_upper = None if la == lb else round((za - zb) if la > lb else (zb - za), 2)
        lon, lat = proj.lonlat(c["x"], c["y"])
        rec = {
            "check": "crossing",
            "class": cls,
            "stage_a_class": now,
            "detail": detail,
            "kind": c["kind"],
            "lon": round(lon, 6),
            "lat": round(lat, 6),
            **_way_props(wa, "a", za, service_types),
            **_way_props(wb, "b", zb, service_types),
            "dz_m": round(abs(za - zb), 2),
            "dz_upper_minus_lower_m": dz_upper,
            "noise_candidate": wa.tags.get("highway") == "service"
            and wb.tags.get("highway") == "service",
            "review_area": review_area(lon, lat),
        }
        if trench is not None:
            rec["trench_depth_m"] = trench["trench_depth_m"]
        recs.append(rec)
    recs.sort(key=lambda r: (CLASSES.index(r["class"]), r["way_a"], r["way_b"]))
    return recs, stats


# --- bridge ends --------------------------------------------------------------------------------


@dataclass
class RawContext:
    """What the full regional extract says about specific ways/nodes (optional)."""

    way_nodes: dict[int, list[int]]  # raw node list of selected ways
    node_ways: dict[int, list[dict[str, Any]]]  # selected node -> raw highway ways using it


def _dist_to_bbox_edge_m(lon: float, lat: float, bbox: BBox, proj: Projection) -> float:
    x, y = proj.xy(lon, lat)
    w, s = proj.xy(bbox.west, bbox.south)
    e, n = proj.xy(bbox.east, bbox.north)
    return min(x - w, e - x, y - s, n - y)


def bridge_end_records(
    net: Network,
    proj: Projection,
    bbox: BBox = SIM_EXTRACT_BBOX,
    raw: RawContext | None = None,
) -> list[dict[str, Any]]:
    node_ways: dict[int, set[int]] = defaultdict(set)
    for w in net.ways.values():
        for n in w.nodes:
            node_ways[n].add(w.id)
    xy_of = {n: proj.xy(*ll) for n, ll in net.coords.items()}
    ids = sorted(net.ways)
    geoms = [LineString([xy_of[n] for n in net.ways[w].nodes]) for w in ids]
    tree = STRtree(geoms)
    recs = []
    for w in sorted(net.ways.values(), key=lambda w: w.id):
        if not is_bridge(w.tags):
            continue
        line3d = net.lines3d[w.id]
        for end, n in (("start", w.nodes[0]), ("end", w.nodes[-1])):
            others = sorted(node_ways[n] - {w.id})
            lon, lat = net.coords[n]
            z = line3d[0][2] if end == "start" else line3d[-1][2]
            if raw is not None and w.id in raw.way_nodes:
                rn = raw.way_nodes[w.id]
                clipped = (rn[0] != n) if end == "start" else (rn[-1] != n)
            else:
                clipped = _dist_to_bbox_edge_m(lon, lat, bbox, proj) <= EDGE_FALLBACK_M
            if others:
                status = "connected"
            elif clipped:
                status = "extract_edge"
            else:
                status = "dangling"
            rec: dict[str, Any] = {
                "check": "bridge_end",
                "class": "dangling_bridge_end" if status == "dangling" else status,
                "end": end,
                "node_id": n,
                "lon": round(lon, 6),
                "lat": round(lat, 6),
                "way_a": w.id,
                "name_a": w.tags.get("name"),
                "highway_a": w.tags.get("highway"),
                "layer_a": parse_layer(w.tags.get("layer")),
                "effective_layer_a": effective_layer(w.tags),
                "bridge_a": w.tags.get("bridge"),
                "tunnel_a": _structure(w.tags, "tunnel"),
                "z_a_m": z,
                "connected_ways": others,
                "review_area": review_area(lon, lat),
            }
            if status == "dangling":
                rec["way_connected_anywhere"] = any(len(node_ways[m]) > 1 for m in w.nodes)
                x, y = xy_of[n]
                near = []
                for k in tree.query(Point(x, y).buffer(NEAR_MISS_M)).tolist():
                    if ids[k] == w.id:
                        continue
                    d = geoms[k].distance(Point(x, y))
                    if d <= NEAR_MISS_M:
                        o = net.ways[ids[k]]
                        near.append(
                            {
                                "way_id": o.id,
                                "name": o.tags.get("name"),
                                "highway": o.tags.get("highway"),
                                "distance_m": round(d, 1),
                            }
                        )
                rec["drivable_ways_within_15m"] = sorted(near, key=lambda r: r["distance_m"])
                if raw is not None:
                    rec["raw_osm_ways_at_node"] = [
                        r for r in raw.node_ways.get(n, []) if r["way_id"] != w.id
                    ]
            recs.append(rec)
    return recs


def mid_structure_junction_records(net: Network) -> list[dict[str, Any]]:
    """Nodes inside a bridge or real tunnel (not at its ends) shared with a lower/higher way.

    A ground road joined to the middle of a deck (or of a tunnel) is either a real access ramp
    or a wrongly connected flyover; roads_3d gives the node the deck's height either way.
    """
    node_ways: dict[int, set[int]] = defaultdict(set)
    for w in net.ways.values():
        for n in w.nodes:
            node_ways[n].add(w.id)
    recs = []
    for w in sorted(net.ways.values(), key=lambda w: w.id):
        if not (is_bridge(w.tags) or pending_tunnel(w.tags)):
            continue
        lw = effective_layer(w.tags)
        for k, n in enumerate(w.nodes[1:-1], start=1):
            for oid in sorted(node_ways[n] - {w.id}):
                o = net.ways[oid]
                if effective_layer(o.tags) == lw or is_bridge(o.tags) or pending_tunnel(o.tags):
                    continue
                lon, lat = net.coords[n]
                line3d = net.lines3d[w.id]
                z = next(
                    (c[2] for c in line3d if abs(c[0] - lon) < 2e-6 and abs(c[1] - lat) < 2e-6),
                    None,
                )
                recs.append(
                    {
                        "check": "mid_structure_junction",
                        "class": "mid_structure_junction",
                        "node_id": n,
                        "node_index": k,
                        "lon": round(lon, 6),
                        "lat": round(lat, 6),
                        "way_a": w.id,
                        "name_a": w.tags.get("name"),
                        "highway_a": w.tags.get("highway"),
                        "layer_a": parse_layer(w.tags.get("layer")),
                        "effective_layer_a": lw,
                        "bridge_a": w.tags.get("bridge") if is_bridge(w.tags) else None,
                        "tunnel_a": _structure(w.tags, "tunnel"),
                        "z_a_m": z,
                        "way_b": o.id,
                        "name_b": o.tags.get("name"),
                        "highway_b": o.tags.get("highway"),
                        "layer_b": parse_layer(o.tags.get("layer")),
                        "effective_layer_b": effective_layer(o.tags),
                        "review_area": review_area(lon, lat),
                    }
                )
    return recs


CONTEXT_TAGS = (
    "highway",
    "name",
    "building",
    "building:part",
    "amenity",
    "parking",
    "barrier",
    "wall",
    "man_made",
    "railway",
    "service",
    "landuse",
    "area",
    "indoor",
    "layer",
    "level",
    "bridge",
    "tunnel",
    "access",
    "motor_vehicle",
    "motorcar",
    "construction",
    "disused",
)


def read_raw_context(pbf: Path, way_ids: set[int], node_ids: set[int]) -> RawContext:
    """One pass over every way in the regional PBF: node lists of `way_ids`, and every way (of
    any kind: road, building, barrier, railway, ...) using one of `node_ids`, with its
    CONTEXT_TAGS."""
    import osmium  # lazy: unit tests of the other functions don't need the C extension

    way_nodes: dict[int, list[int]] = {}
    node_ways: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for w in osmium.FileProcessor(str(pbf), osmium.osm.WAY):
        refs = [n.ref for n in w.nodes]
        if w.id in way_ids:
            way_nodes[w.id] = refs
        hits = node_ids.intersection(refs)
        if hits:
            info = {
                "way_id": w.id,
                "tags": {k: w.tags[k] for k in CONTEXT_TAGS if k in w.tags},
            }
            for n in sorted(hits):
                node_ways[n].append(info)
    return RawContext(way_nodes, dict(node_ways))


def read_service_types(pbf: Path, way_ids: set[int]) -> dict[int, str]:
    import osmium

    out: dict[int, str] = {}
    fp = osmium.FileProcessor(str(pbf), osmium.osm.WAY).with_filter(
        osmium.filter.KeyFilter("service")
    )
    for w in fp:
        if w.id in way_ids:
            out[w.id] = w.tags.get("service")
    return out


# --- report -------------------------------------------------------------------------------------


def add_lift_sources(net: Network, proj: Projection, crossings: list[dict[str, Any]]) -> None:
    """For order_mismatch / separated_low records: the higher deck that lifts the lower road.

    Dijkstra in metres over the OSM node graph from the crossing point (along the lower way) to
    nodes of bridge ways, other than the upper way, whose roads_3d height at that node is at
    least the lower road's z here. The lift source is the one whose 6 % ramp cone is highest at
    the crossing (deck z - 0.06 x distance), i.e. the deck the ramp comes from. Searched up to
    LIFT_SEARCH_M.
    """
    zpos = {
        (w, round(c[0], 6), round(c[1], 6)): c[2] for w, line in net.lines3d.items() for c in line
    }

    def node_z(w: int, n: int) -> float:
        lon, lat = net.coords[n]
        return zpos.get((w, round(lon, 6), round(lat, 6)), 0.0)

    adj: dict[int, list[tuple[int, float]]] = defaultdict(list)
    node_ways: dict[int, set[int]] = defaultdict(set)
    xy = {n: proj.xy(*ll) for n, ll in net.coords.items()}
    for w in net.ways.values():
        for a, b in zip(w.nodes, w.nodes[1:], strict=False):
            d = math.dist(xy[a], xy[b])
            adj[a].append((b, d))
            adj[b].append((a, d))
        for n in w.nodes:
            node_ways[n].add(w.id)
    for r in crossings:
        if r["class"] not in ("order_mismatch", "separated_low"):
            continue
        a_upper = r["effective_layer_a"] > r["effective_layer_b"]
        upper, lower = ("a", "b") if a_upper else ("b", "a")
        z_up = r[f"z_{upper}_m"]
        z_low = r[f"z_{lower}_m"]
        up_id, low = r[f"way_{upper}"], net.ways[r[f"way_{lower}"]]
        x, y = proj.xy(r["lon"], r["lat"])
        pts = [xy[n] for n in low.nodes]
        s0 = LineString(pts).project(Point(x, y))
        steps = [math.dist(p, q) for p, q in zip(pts, pts[1:], strict=False)]
        cum = np.concatenate([[0.0], np.cumsum(steps)])
        dist: dict[int, float] = {}
        heap = []
        for n, c in zip(low.nodes, cum, strict=True):
            d0 = abs(float(c) - s0)
            if d0 < dist.get(n, math.inf):
                dist[n] = d0
                heap.append((d0, n))
        heapq.heapify(heap)
        found = None
        while heap:
            d, n = heapq.heappop(heap)
            if d > dist[n]:
                continue
            if d > LIFT_SEARCH_M:
                break
            decks = [
                (net.ways[w], node_z(w, n))
                for w in node_ways[n]
                if w != up_id and is_bridge(net.ways[w].tags) and node_z(w, n) >= z_low - 0.01
            ]
            for deck, dz in decks:  # the deck whose 6 % cone is highest here is the lift source
                reach = dz - RAMP_GRADE * d
                if found is None or reach > found[2]:
                    found = (d, deck, reach, dz)
            for m, dm in adj[n]:
                if d + dm < dist.get(m, math.inf):
                    dist[m] = d + dm
                    heapq.heappush(heap, (d + dm, m))
        if found is None:
            continue
        d, deck, _, deck_z = found
        target = max(z_up - CLEARANCE_M, 0.0)
        r["lifted_by_way"] = deck.id
        r["lifted_by_name"] = deck.tags.get("name")
        r["lifted_by_layer"] = effective_layer(deck.tags)
        r["lifted_by_deck_z_m"] = deck_z
        r["lift_distance_m"] = round(d, 1)
        r["grade_needed"] = round((deck_z - target) / d, 3) if d > 0 else None


def summarise(
    crossings: list[dict[str, Any]],
    ends: list[dict[str, Any]],
    stats: dict[str, int],
    net: Network,
    net_proj: Projection,
) -> dict[str, Any]:
    def counts(rs: list[dict[str, Any]]) -> dict[str, int]:
        c = Counter(r["class"] for r in rs)
        return {k: c.get(k, 0) for k in CLASSES}

    without_noise = [r for r in crossings if not r["noise_candidate"]]
    sens = {}
    for t in SENSITIVITY_M:
        n_ok = sum(
            1
            for r in crossings
            if r["dz_upper_minus_lower_m"] is not None
            and r["class"] != "pending_tunnel_depth"
            and r["dz_upper_minus_lower_m"] >= t
        )
        sens[f"{t:g}"] = n_ok
    areas: dict[str, Any] = {}
    for name in REVIEW_AREAS:
        rs = [r for r in crossings if r["review_area"] == name]
        es = [e for e in ends if e["review_area"] == name]
        areas[name] = {
            "bbox": list(REVIEW_AREAS[name]),
            "crossings": counts(rs),
            "bridge_ends": dict(Counter(e["class"] for e in es)),
        }
    return {
        "ways": len(net.ways),
        "bridge_ways": sum(1 for w in net.ways.values() if is_bridge(w.tags)),
        "tunnel_ways": sum(1 for w in net.ways.values() if _structure(w.tags, "tunnel")),
        "geometry_stats": stats,
        "crossings_total": len(crossings),
        "crossings_by_class": counts(crossings),
        "crossings_by_class_excluding_service_service": counts(without_noise),
        "service_service_pairs": len(crossings) - len(without_noise),
        "crossings_by_kind": dict(Counter(r["kind"] for r in crossings)),
        "order_mismatch_detail": dict(
            Counter(r["detail"] for r in crossings if r["class"] == "order_mismatch")
        ),
        "pending_tunnel_depth_stage_a_class": dict(
            Counter(r["stage_a_class"] for r in crossings if r["class"] == "pending_tunnel_depth")
        ),
        "pending_trench_depth_stage_a_class": dict(
            Counter(r["stage_a_class"] for r in crossings if r["class"] == "pending_trench_depth")
        ),
        "clearance_threshold_m": CLEARANCE_M,
        "clearance_sensitivity": {
            "note": "crossings (different layers, no tunnel pending) whose upper road is at "
            "least this many metres above the lower one",
            "counts": sens,
        },
        "bridge_ends": dict(Counter(e["class"] for e in ends)),
        "review_areas": areas,
    }


MISMATCH_CAUSE = (
    "Clearance-based heights (roads_3d.py): each upper deck sits a measured or default separation "
    "above what is beneath it, so a mismatch can only come from a crossing roads_3d had to drop "
    "(the lower road joins the upper deck too close for the grade limits; listed in its build "
    "summary). See lifted_by_* / lift_distance_m / grade_needed on any order_mismatch and "
    "separated_low record."
)


def run(
    extract: Path = EXTRACT_PATH,
    roads3d: Path = ROADS_3D_PATH,
    pbf: Path | None = RAW_PBF,
    bbox: BBox = SIM_EXTRACT_BBOX,
    deck_report: Path = DECK_REPORT,
) -> dict[str, Any]:
    ways, coords = read_extract(extract)
    lines3d = read_roads_3d(roads3d)
    missing = set(ways) ^ set(lines3d)
    if missing:
        raise ValueError(f"extract and roads_3d disagree on {len(missing)} way ids")
    net = Network(ways, coords, lines3d)
    proj = Projection.for_bbox(bbox)
    raw = None
    service_types: dict[int, str] = {}
    if pbf is not None and pbf.exists():
        bridge_ids = {w.id for w in ways.values() if is_bridge(w.tags)}
        end_nodes = {w.nodes[k] for w in ways.values() if w.id in bridge_ids for k in (0, -1)}
        raw = read_raw_context(pbf, bridge_ids, end_nodes)
        svc = {w.id for w in ways.values() if w.tags.get("highway") == "service"}
        service_types = read_service_types(pbf, svc)
    crossings, stats = crossing_records(
        net, proj, service_types, trenches=read_trenches(deck_report)
    )
    add_lift_sources(net, proj, crossings)
    ends = bridge_end_records(net, proj, bbox, raw)
    mids = mid_structure_junction_records(net)
    summary = summarise(crossings, ends, stats, net, proj)
    summary["mid_structure_junctions"] = len(mids)
    summary["raw_pbf_used"] = raw is not None
    summary["order_mismatch_cause"] = MISMATCH_CAUSE
    return {
        "inputs_sha256": {
            repo_key(p): sha256_file(p) for p in (roads3d, extract, deck_report) if p.exists()
        },
        "summary": summary,
        "crossings": crossings,
        "bridge_ends": ends,
        "mid_structure_junctions": mids,
    }


def flags_geojson(report: dict[str, Any]) -> dict[str, Any]:
    feats = []
    flagged = [r for r in report["crossings"] if r["class"] != "separated_ok"]
    flagged += [e for e in report["bridge_ends"] if e["class"] == "dangling_bridge_end"]
    flagged += report.get("mid_structure_junctions", [])
    for r in flagged:
        props = {k: v for k, v in r.items() if k not in ("lon", "lat")}
        for k, v in props.items():  # keep properties flat for map styling
            if isinstance(v, list | dict):
                props[k] = json.dumps(v, ensure_ascii=False)
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                "properties": props,
            }
        )
    return {"type": "FeatureCollection", "features": feats}


def write(report: dict[str, Any], out_json: Path = OUT_JSON, out_flags: Path = OUT_FLAGS) -> None:
    out_json.parent.mkdir(parents=True, exist_ok=True)
    meta = {
        "about": "Grade-separation check on roads_3d.geojson (pipeline/checks/grade_separation.py)."
        " Reports only; nothing in the contract files is changed. Heights are visual: "
        "clearance-based, measured where roads_3d's z_source says so.",
        "inputs": [
            "data/processed/osm_sim_area.osm.gz",
            "data/processed/roads_3d.geojson",
            "data/raw/bbbike_melbourne.osm.pbf (context only)",
        ],
        "licence": "Derived from OpenStreetMap data, ODbL 1.0, (c) OpenStreetMap contributors",
    }
    out_json.write_text(json.dumps({**meta, **report}, indent=1, ensure_ascii=False) + "\n")
    out_flags.write_text(json.dumps(flags_geojson(report), ensure_ascii=False) + "\n")


def main() -> int:
    report = run()
    write(report)
    s = report["summary"]
    print(json.dumps({k: v for k, v in s.items() if k != "review_areas"}, indent=1))
    for name, a in s["review_areas"].items():
        print(name, a["crossings"], a["bridge_ends"])
    print(f"wrote {OUT_JSON} ({OUT_JSON.stat().st_size / 1e3:.0f} kB)")
    print(f"wrote {OUT_FLAGS} ({OUT_FLAGS.stat().st_size / 1e3:.0f} kB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
