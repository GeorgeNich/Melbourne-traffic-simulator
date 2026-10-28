"""Independent cross-check of the OSM road model against Vicmap Transport (reports, never fixes).

Usage:
    uv run python -m pipeline.download --vicmap-only   # once: two WFS layers into data/raw/
    uv run python -m pipeline.checks.vicmap_crosscheck

Inputs: data/processed/osm_sim_area.osm.gz + roads_3d.geojson (the OSM model) and
data/raw/vicmap_tr_road.geojson + vicmap_tr_road_infrastructure.geojson (Vicmap Transport,
CC BY 4.0, see data/SOURCES.md). Vicmap is used for verification only; nothing here feeds a
contract file.

What Vicmap Transport carries (metadata record 61f633ae-c18c-5967-a546-84ceb44273f6):
    tr_road             line segments; feature_type_code road, roundabout, connector, ford,
                        bridge, foot_bridge, tunnel, trail; construction_type for bridges;
                        from_ufi / to_ufi node topology. No level / z field.
    tr_road_infrastructure  the nodes, typed: int_signal, int_nosignal, roundabout, road_end,
                        bridge, tunnel, ... The network is planar (every 2D crossing is a node).
                        A grade-separated crossing is a `bridge` or `tunnel` node, and its
                        conpfi1 / conpfi2 are the two segments of the road CARRIED by the
                        structure (checked on this extract: at all 4-way structure nodes the two
                        conpfi segments continue one road and the others are the crossing road).
                        So at a `bridge` node the conpfi road is on top; at a `tunnel` node it is
                        underneath. That is Vicmap's (pairwise) vertical order.

Vehicle roads only: class_code 0-6 (freeway .. minor road), feature_type_code not trail /
foot_bridge, restrictions != 4 (permanently closed).

Matching: a point on one dataset's line matches the other dataset's nearest line within
MATCH_DIST_M whose local direction is within MATCH_ANGLE_DEG (undirected).

Reported:
    (a) structures: every OSM bridge / tunnel way, is there a Vicmap bridge / tunnel along it
        (sampled every SAMPLE_M), and every Vicmap bridge / tunnel, is there an OSM one.
    (b) order: for each grade_separation crossing, the Vicmap node where the two matched Vicmap
        roads meet; if it is a bridge/tunnel node, which road it puts on top, compared with
        OSM's layer order and roads_3d's z.
    (c) topology: (c1) OSM crossings without a junction where Vicmap's node is an at-grade
        intersection (int_* / roundabout): Vicmap says the roads connect, OSM says they do not;
        (c2) every Vicmap bridge/tunnel node where two vehicle roads cross, matched to OSM: does
        OSM join the two roads with a shared node there (a flyover wrongly connected in OSM, or
        a real junction Vicmap models as a structure)?
"""

from __future__ import annotations

import json
import math
import sys
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon, box
from shapely.strtree import STRtree

from pipeline.checks import grade_separation as gs
from pipeline.config import SIM_EXTRACT_BBOX, BBox

REPO_ROOT = Path(__file__).resolve().parents[2]
VICMAP_ROAD = REPO_ROOT / "data" / "raw" / "vicmap_tr_road.geojson"
VICMAP_INFRA = REPO_ROOT / "data" / "raw" / "vicmap_tr_road_infrastructure.geojson"
OUT_DIR = REPO_ROOT / "data" / "processed" / "checks"
OUT_JSON = OUT_DIR / "vicmap_crosscheck.json"
OUT_FLAGS = OUT_DIR / "vicmap_crosscheck_flags.geojson"

MATCH_DIST_M = 20.0
MATCH_ANGLE_DEG = 30.0
SAMPLE_M = 10.0
END_SKIP_M = 5.0  # samples this close to a line end are skipped (junction ambiguity)
JUNCTION_DIST_M = 30.0
CUT_END_NEAR_M = MATCH_DIST_M  # an OSM way's cut end this close to an unmatched structure
VEHICLE_CLASSES = frozenset(range(0, 7))
NON_VEHICLE_TYPES = frozenset({"trail", "foot_bridge"})
CLOSED_RESTRICTION = "4"
STRUCTURE_RANK = {"bridge": 1, "tunnel": -1}


# --- data ---------------------------------------------------------------------------------------


@dataclass
class VLine:
    pfi: int
    ufi: int
    ftype: str
    name: str | None
    class_code: int | None
    from_ufi: int | None
    to_ufi: int | None
    construction_type: str | None
    structure_name: str | None
    xy: list[tuple[float, float]]  # clipped to the extract bbox
    from_xy: tuple[float, float] | None = None  # original end points (node positions), which
    to_xy: tuple[float, float] | None = None  # may lie outside the bbox for clipped lines
    clipped: bool = False  # the line was cut by the extract bbox edge

    def __post_init__(self) -> None:
        if self.from_xy is None:
            self.from_xy = self.xy[0]
        if self.to_xy is None:
            self.to_xy = self.xy[-1]

    @property
    def rank(self) -> int:
        return STRUCTURE_RANK.get(self.ftype, 0)


def is_vehicle_road(p: dict[str, Any]) -> bool:
    return (
        p.get("class_code") in VEHICLE_CLASSES
        and p.get("feature_type_code") not in NON_VEHICLE_TYPES
        and p.get("restrictions") != CLOSED_RESTRICTION
    )


def _vicmap_name(p: dict[str, Any]) -> str | None:
    name = p.get("ezi_road_name_label") or p.get("road_name")
    return None if name is None or name.strip().lower() == "unnamed" else name


def same_name(a: str | None, b: str | None) -> bool:
    """Loose road-name match: one normalised name contains the other ("West Gate Tunnel" and
    "West Gate Tunnel Tollway")."""
    if not a or not b:
        return False
    na, nb = (" ".join(x.lower().replace("-", " ").split()) for x in (a, b))
    return na in nb or nb in na


def osm_name_is(net: gs.Network, osm_ids: list[int], name: str | None) -> Callable[[int], bool]:
    """`prefer` predicate for the OSM index: the OSM way has (loosely) this name."""
    return lambda i: same_name(net.ways[osm_ids[i]].tags.get("name"), name)


def bbox_polygon(bbox: BBox, proj: gs.Projection) -> Polygon:
    w, so = proj.xy(bbox.west, bbox.south)
    e, n = proj.xy(bbox.east, bbox.north)
    return box(w, so, e, n)


def read_vicmap(
    path: Path, proj: gs.Projection, bbox: BBox = SIM_EXTRACT_BBOX
) -> tuple[list[VLine], dict[str, int]]:
    """Vehicle road lines clipped to `bbox`, and read/clip counts.

    The WFS returns every feature that touches the bbox, so lines are cut at its edge (a line
    that leaves and re-enters becomes several VLines with the same pfi, `clipped=True`). The
    original end points are kept as node positions for the topology.
    """
    fc = json.loads(path.read_text())
    clip = bbox_polygon(bbox, proj)
    out = []
    stats = Counter(features_read=len(fc["features"]))
    for f in fc["features"]:
        p, g = f["properties"], f["geometry"]
        if not is_vehicle_road(p):
            continue
        stats["vehicle_features"] += 1
        parts = [g["coordinates"]] if g["type"] == "LineString" else g["coordinates"]
        for coords in parts:
            xy = [proj.xy(c[0], c[1]) for c in coords]
            full = LineString(xy)
            inside = full.intersection(clip)
            pieces = [q for q in shapely.get_parts(inside) if q.geom_type == "LineString"]
            pieces = [q for q in pieces if q.length > 0.01]
            if not pieces:
                stats["outside_bbox_dropped"] += 1
                continue
            clipped = full.length - sum(q.length for q in pieces) > 0.01
            stats["clipped_at_bbox_edge"] += clipped
            for q in pieces:
                out.append(
                    VLine(
                        pfi=p.get("pfi"),
                        ufi=p.get("ufi"),
                        ftype=p.get("feature_type_code"),
                        name=_vicmap_name(p),
                        class_code=p.get("class_code"),
                        from_ufi=p.get("from_ufi"),
                        to_ufi=p.get("to_ufi"),
                        construction_type=p.get("construction_type"),
                        structure_name=p.get("structure_name"),
                        xy=[tuple(c) for c in q.coords],
                        from_xy=xy[0],
                        to_xy=xy[-1],
                        clipped=clipped,
                    )
                )
    return out, dict(stats)


# --- matching -----------------------------------------------------------------------------------


def _angle(a: tuple[float, float], b: tuple[float, float]) -> float:
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0])) % 180.0


def _angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 180.0
    return min(d, 180.0 - d)


class SegmentIndex:
    """STRtree over every 2-point segment of a set of lines, for nearest + direction matching."""

    def __init__(self, lines: list[list[tuple[float, float]]]):
        segs, owner, ang = [], [], []
        for i, xy in enumerate(lines):
            for a, b in zip(xy, xy[1:], strict=False):
                if a == b:
                    continue
                segs.append(LineString([a, b]))
                owner.append(i)
                ang.append(_angle(a, b))
        self.segs = segs
        self.owner = np.asarray(owner, dtype=int)
        self.ang = np.asarray(ang)
        self.tree = STRtree(segs)

    def match(
        self,
        x: float,
        y: float,
        angle: float,
        dist: float = MATCH_DIST_M,
        dang: float = MATCH_ANGLE_DEG,
        prefer: Callable[[int], bool] | None = None,
    ) -> tuple[int, float] | None:
        """(line index, distance) of the nearest direction-consistent line, or None.

        With `prefer`, the nearest candidate it accepts wins over a nearer one it rejects (used
        to prefer the same road name, so a parallel carriageway or ramp is not picked instead).
        """
        p = Point(x, y)
        best: tuple[int, float] | None = None
        best_pref: tuple[int, float] | None = None
        for k in self.tree.query(p.buffer(dist)).tolist():
            o = int(self.owner[k])
            if _angle_diff(float(self.ang[k]), angle) > dang:
                continue
            d = self.segs[k].distance(p)
            if d > dist:
                continue
            if best is None or d < best[1]:
                best = (o, d)
            if prefer is not None and prefer(o) and (best_pref is None or d < best_pref[1]):
                best_pref = (o, d)
        return best_pref or best


def samples(
    xy: list[tuple[float, float]], step: float = SAMPLE_M
) -> list[tuple[float, float, float]]:
    """(x, y, angle) every `step` m along a line, skipping END_SKIP_M at each end (>= 1 sample)."""
    ls = LineString(xy)
    n = ls.length
    if n <= 2 * END_SKIP_M:
        ds = [n / 2]
    else:
        ds = list(np.arange(END_SKIP_M, n - END_SKIP_M + 1e-9, step)) or [n / 2]
    out = []
    for d in ds:
        p = ls.interpolate(d)
        out.append((p.x, p.y, angle_at(xy, p.x, p.y)))
    return out


def angle_at(xy: list[tuple[float, float]], x: float, y: float) -> float:
    """Direction of the segment of `xy` nearest (x, y)."""
    p = Point(x, y)
    best, ang = math.inf, 0.0
    for a, b in zip(xy, xy[1:], strict=False):
        if a == b:
            continue
        d = LineString([a, b]).distance(p)
        if d < best:
            best, ang = d, _angle(a, b)
    return ang


# --- (a) structures -----------------------------------------------------------------------------


def osm_structure_records(
    net: gs.Network,
    osm_xy: dict[int, list[tuple[float, float]]],
    vlines: list[VLine],
    vindex: SegmentIndex,
    proj: gs.Projection,
) -> list[dict[str, Any]]:
    recs = []
    for w in sorted(net.ways.values(), key=lambda w: w.id):
        kind = (
            "bridge"
            if gs.is_bridge(w.tags)
            else ("tunnel" if gs._structure(w.tags, "tunnel") else None)
        )
        if kind is None:
            continue
        ss = samples(osm_xy[w.id])
        name = w.tags.get("name")
        hits = [
            vindex.match(x, y, a, prefer=lambda i, n=name: same_name(vlines[i].name, n))
            for x, y, a in ss
        ]
        matched = [vlines[h[0]] for h in hits if h is not None]
        same = [v for v in matched if v.ftype == kind]
        if same:
            status = "matched"
        elif matched:
            status = f"vicmap_not_{kind}"
        else:
            status = "no_vicmap_counterpart"
        mid = LineString(osm_xy[w.id]).interpolate(0.5, normalized=True)
        lon, lat = proj.lonlat(mid.x, mid.y)
        recs.append(
            {
                "check": f"osm_{kind}",
                "class": status,
                "lon": round(lon, 6),
                "lat": round(lat, 6),
                "way_id": w.id,
                "name": w.tags.get("name"),
                "highway": w.tags.get("highway"),
                "layer": gs.parse_layer(w.tags.get("layer")),
                "osm_value": w.tags.get(kind),
                "samples": len(ss),
                "samples_matched": len(matched),
                f"samples_on_vicmap_{kind}": len(same),
                "vicmap_types_seen": dict(Counter(v.ftype for v in matched)),
                "vicmap_structure_names": sorted(
                    {v.structure_name for v in same if v.structure_name}
                ),
                "review_area": gs.review_area(lon, lat),
            }
        )
    return recs


def osm_cut_ends(
    net: gs.Network, raw_way_nodes: dict[int, list[int]], proj: gs.Projection
) -> dict[int, list[tuple[float, float]]]:
    """way id -> positions (projected) of its extract end nodes where the extract cut the way
    (the regional OSM way continues past that node)."""
    out: dict[int, list[tuple[float, float]]] = {}
    for wid, w in net.ways.items():
        raw = raw_way_nodes.get(wid)
        if raw is None:
            continue
        cut = []
        if raw[0] != w.nodes[0]:
            cut.append(proj.xy(*net.coords[w.nodes[0]]))
        if raw[-1] != w.nodes[-1]:
            cut.append(proj.xy(*net.coords[w.nodes[-1]]))
        if cut:
            out[wid] = cut
    return out


def vicmap_structure_records(
    net: gs.Network,
    osm_ids: list[int],
    oindex: SegmentIndex,
    vlines: list[VLine],
    proj: gs.Projection,
    bbox: BBox = SIM_EXTRACT_BBOX,
    cut_ends: dict[int, list[tuple[float, float]]] | None = None,
) -> list[dict[str, Any]]:
    """`extract_edge` = the Vicmap line was clipped by the extract bbox, or the OSM way that
    would be its counterpart was cut at the extract edge: a matched way with a cut end, or (if
    nothing matched) any way with a cut end within CUT_END_NEAR_M of the structure. Distance to
    the bbox edge alone never makes a structure `extract_edge` (qa follow-up, Swan St Bridge).
    `cut_ends` comes from the regional PBF (`osm_cut_ends`); without it only clipping counts."""
    cut_ends = cut_ends or {}
    recs = []
    edge = bbox_polygon(bbox, proj).exterior
    for v in vlines:
        if v.ftype not in STRUCTURE_RANK:
            continue
        edge_m = LineString(v.xy).distance(edge)
        ss = samples(v.xy)
        hits = [
            oindex.match(
                x,
                y,
                a,
                prefer=osm_name_is(net, osm_ids, v.name),
            )
            for x, y, a in ss
        ]
        ways = [net.ways[osm_ids[h[0]]] for h in hits if h is not None]
        if v.ftype == "bridge":
            same = [w for w in ways if gs.is_bridge(w.tags)]
        else:
            same = [w for w in ways if gs._structure(w.tags, "tunnel")]
        vl = LineString(v.xy)
        matched_cut = sorted({w.id for w in ways if w.id in cut_ends})
        near_cut = (
            []
            if ways
            else sorted(
                wid
                for wid, pts in cut_ends.items()
                if any(vl.distance(Point(p)) <= CUT_END_NEAR_M for p in pts)
            )
        )
        if same:
            status = "matched"
        elif v.clipped or matched_cut or near_cut:
            # the counterpart may lie outside the extract
            status = "extract_edge"
        elif ways:
            status = f"osm_not_{v.ftype}"
        else:
            status = "no_osm_counterpart"
        mid = LineString(v.xy).interpolate(0.5, normalized=True)
        lon, lat = proj.lonlat(mid.x, mid.y)
        recs.append(
            {
                "check": f"vicmap_{v.ftype}",
                "class": status,
                "lon": round(lon, 6),
                "lat": round(lat, 6),
                "vicmap_pfi": v.pfi,
                "vicmap_ufi": v.ufi,
                "name": v.name,
                "vicmap_class_code": v.class_code,
                "structure_name": v.structure_name,
                "construction_type": v.construction_type,
                "length_m": round(LineString(v.xy).length, 1),
                "clipped_at_extract_edge": v.clipped,
                "distance_to_extract_edge_m": round(edge_m, 1),
                "osm_ways_cut_at_extract_edge": matched_cut or near_cut,
                "samples": len(ss),
                "samples_matched": len(ways),
                "osm_ways_matched": sorted({w.id for w in ways}),
                "osm_structure_ways": sorted({w.id for w in same}),
                "review_area": gs.review_area(lon, lat),
            }
        )
    return recs


# --- Vicmap nodes -------------------------------------------------------------------------------


@dataclass
class VNode:
    ufi: int
    xy: tuple[float, float]
    ftype: str | None  # tr_road_infrastructure feature_type_code; None if no point feature
    segments: list[int]  # indices into the vehicle VLine list
    carried: set[int]  # pfis of the road carried by a bridge / tunnel node (conpfi1, conpfi2)


JUNCTION_TYPES = frozenset({"int_signal", "int_nosignal", "int_attribute", "int_locality"})
JUNCTION_TYPES |= {"roundabout"}
STRUCTURE_NODE_TYPES = frozenset({"bridge", "tunnel"})


def build_nodes(vlines: list[VLine], infra: list[dict[str, Any]], proj: gs.Projection):
    """Vicmap's node topology: every segment runs from_ufi -> to_ufi (a planar graph)."""
    pts = {f["properties"]["ufi"]: f for f in infra}
    nodes: dict[int, VNode] = {}
    for i, v in enumerate(vlines):
        for u, xy in ((v.from_ufi, v.from_xy), (v.to_ufi, v.to_xy)):
            if u is None:
                continue
            if u not in nodes:
                p = pts.get(u)
                props = p["properties"] if p else {}
                carried = {props.get("conpfi1"), props.get("conpfi2")} - {None}
                nodes[u] = VNode(u, xy, props.get("feature_type_code"), [], carried)
            nodes[u].segments.append(i)
    return nodes


def shared_node_near(
    va: VLine, vb: VLine, nodes: dict[int, VNode], x: float, y: float
) -> VNode | None:
    """A node of both va and vb within JUNCTION_DIST_M of (x, y) (nearest first)."""
    shared = ({va.from_ufi, va.to_ufi} & {vb.from_ufi, vb.to_ufi}) - {None}
    best = None
    for u in shared:
        n = nodes[u]
        d = math.hypot(n.xy[0] - x, n.xy[1] - y)
        if d <= JUNCTION_DIST_M and (best is None or d < best[0]):
            best = (d, n)
    return best[1] if best else None


def named_node_near(
    va: VLine, vb: VLine, nodes: dict[int, VNode], vlines: list[VLine], x: float, y: float
) -> tuple[VNode, VLine, VLine] | None:
    """Fallback when the two matched segments share no node: the nearest node within
    JUNCTION_DIST_M where a segment of va's road and a segment of vb's road (same name) meet.
    Returns the node and those two segments."""

    def is_road(s: VLine, v: VLine) -> bool:
        return s.pfi == v.pfi or same_name(s.name, v.name)

    best = None
    for n in nodes.values():
        d = math.hypot(n.xy[0] - x, n.xy[1] - y)
        if d > JUNCTION_DIST_M or (best is not None and d >= best[0]):
            continue
        segs = [vlines[i] for i in n.segments]
        sa = next((s for s in segs if is_road(s, va)), None)
        sb = next((s for s in segs if is_road(s, vb) and s is not sa), None)
        if sa is not None and sb is not None and not same_name(sa.name, sb.name):
            best = (d, n, sa, sb)
    return best[1:] if best else None


def vicmap_upper(node: VNode, va: VLine, vb: VLine) -> str | None:
    """'a' / 'b' if the structure node says which road is on top, else None.

    At a `bridge` node the road named by conpfi1/conpfi2 is on the bridge (above the others);
    at a `tunnel` node it is in the tunnel (below the others).
    """
    if node.ftype not in STRUCTURE_NODE_TYPES:
        return None
    a_in, b_in = va.pfi in node.carried, vb.pfi in node.carried
    if a_in == b_in:
        return None
    carried = "a" if a_in else "b"
    if node.ftype == "bridge":
        return carried
    return "b" if carried == "a" else "a"


# --- (b) + (c1) OSM crossings -------------------------------------------------------------------


def crossing_order_records(
    crossings: list[dict[str, Any]],
    osm_xy: dict[int, list[tuple[float, float]]],
    vlines: list[VLine],
    vindex: SegmentIndex,
    nodes: dict[int, VNode],
    proj: gs.Projection,
) -> list[dict[str, Any]]:
    recs = []
    for c in crossings:
        x, y = proj.xy(c["lon"], c["lat"])
        ha = vindex.match(
            x,
            y,
            angle_at(osm_xy[c["way_a"]], x, y),
            prefer=lambda i, n=c["name_a"]: same_name(vlines[i].name, n),
        )
        hb = vindex.match(
            x,
            y,
            angle_at(osm_xy[c["way_b"]], x, y),
            prefer=lambda i, n=c["name_b"]: same_name(vlines[i].name, n),
        )
        la, lb = c["effective_layer_a"], c["effective_layer_b"]
        osm_upper = None if la == lb else ("a" if la > lb else "b")
        z_upper = None
        if abs(c["z_a_m"] - c["z_b_m"]) > gs.FLAT_EPS_M:
            z_upper = "a" if c["z_a_m"] > c["z_b_m"] else "b"
        rec: dict[str, Any] = {
            "check": "crossing_order",
            "lon": c["lon"],
            "lat": c["lat"],
            "grade_separation_class": c["class"],
            "way_a": c["way_a"],
            "name_a": c["name_a"],
            "way_b": c["way_b"],
            "name_b": c["name_b"],
            "effective_layer_a": la,
            "effective_layer_b": lb,
            "z_a_m": c["z_a_m"],
            "z_b_m": c["z_b_m"],
            "osm_layer_upper": osm_upper,
            "roads_3d_upper": z_upper,
            "vicmap_upper": None,
            "vicmap_pfi_a": None,
            "vicmap_name_a": None,
            "vicmap_type_a": None,
            "vicmap_pfi_b": None,
            "vicmap_name_b": None,
            "vicmap_type_b": None,
            "vicmap_node_ufi": None,
            "vicmap_node_type": None,
            "roads_3d_agrees_with_vicmap": None,
            "names_match": None,
            "review_area": c["review_area"],
        }
        if ha is None or hb is None:
            rec["class"] = "no_vicmap_match"
            recs.append(rec)
            continue
        va, vb = vlines[ha[0]], vlines[hb[0]]
        rec.update(
            vicmap_pfi_a=va.pfi,
            vicmap_name_a=va.name,
            vicmap_type_a=va.ftype,
            vicmap_pfi_b=vb.pfi,
            vicmap_name_b=vb.name,
            vicmap_type_b=vb.ftype,
        )
        if ha[0] == hb[0]:
            rec["class"] = "same_vicmap_line"
            recs.append(rec)
            continue
        node = shared_node_near(va, vb, nodes, x, y)
        rec["vicmap_node_found_by"] = "shared_node"
        if node is None:
            found = named_node_near(va, vb, nodes, vlines, x, y)
            if found is None:
                rec["class"] = "no_vicmap_crossing_node"
                rec["vicmap_node_found_by"] = None
                recs.append(rec)
                continue
            node, va, vb = found
            rec["vicmap_node_found_by"] = "road_names"
        rec["vicmap_node_ufi"], rec["vicmap_node_type"] = node.ufi, node.ftype
        if node.ftype in JUNCTION_TYPES:
            rec["class"] = "vicmap_at_grade_junction"  # (c1) OSM has no junction here
            recs.append(rec)
            continue
        up = vicmap_upper(node, va, vb)
        by_type = None if va.rank == vb.rank else ("a" if va.rank > vb.rank else "b")
        rec["vicmap_upper"] = up
        rec["vicmap_upper_by_segment_type"] = by_type
        if up is not None and by_type is not None and up != by_type:
            rec["class"] = "vicmap_inconsistent"  # node's carried road vs segment types
            rec["vicmap_upper"] = None
            up = None
        elif up is None:
            up = rec["vicmap_upper"] = by_type
        rec["names_match"] = same_name(c["name_a"], va.name) and same_name(c["name_b"], vb.name)
        if rec.get("class") == "vicmap_inconsistent":
            pass
        elif up is None:
            rec["class"] = "vicmap_no_order_info"
        elif osm_upper is None:
            rec["class"] = "osm_same_layer"
        else:
            rec["class"] = "agree" if up == osm_upper else "disagree"
        if up is not None:
            rec["roads_3d_agrees_with_vicmap"] = up == z_upper
        recs.append(rec)
    return recs


# --- (c2) Vicmap grade-separated crossings vs OSM junctions -------------------------------------


def _point_near_node(xy: list[tuple[float, float]], node_xy: tuple[float, float], d: float):
    """(x, y, angle) about `d` m into the line from its end nearest `node_xy`."""
    ls = LineString(xy)
    s0 = ls.project(Point(node_xy))
    d = min(d, ls.length / 2)
    t = s0 + d if s0 <= ls.length / 2 else s0 - d
    p = ls.interpolate(t)
    return p.x, p.y, angle_at(xy, p.x, p.y)


def structure_node_records(
    net: gs.Network,
    osm_ids: list[int],
    oindex: SegmentIndex,
    vlines: list[VLine],
    nodes: dict[int, VNode],
    osm_crossings: list[dict[str, Any]],
    proj: gs.Projection,
    bbox: BBox = SIM_EXTRACT_BBOX,
) -> list[dict[str, Any]]:
    """Every Vicmap bridge/tunnel node inside `bbox` where two vehicle roads cross, vs OSM."""
    clip = bbox_polygon(bbox, proj)
    osm_node_xy = {n: proj.xy(*ll) for n, ll in net.coords.items()}
    osm_cross_xy = defaultdict(list)
    for c in osm_crossings:
        osm_cross_xy[frozenset((c["way_a"], c["way_b"]))].append(proj.xy(c["lon"], c["lat"]))
    recs = []
    for node in sorted(nodes.values(), key=lambda n: n.ufi):
        if node.ftype not in STRUCTURE_NODE_TYPES:
            continue
        carried = [i for i in node.segments if vlines[i].pfi in node.carried]
        crossing = [i for i in node.segments if vlines[i].pfi not in node.carried]
        if not carried or not crossing:
            continue  # a structure end, or the crossing road is a trail / footbridge
        if not clip.covers(Point(node.xy)):
            continue  # outside the extract (a node of a line clipped at the edge)
        x, y = node.xy
        lon, lat = proj.lonlat(x, y)

        def match_road(segs: list[int], nxy: tuple[float, float] = node.xy):
            for i in segs:
                v = vlines[i]
                px, py, ang = _point_near_node(v.xy, nxy, 8.0)
                h = oindex.match(
                    px,
                    py,
                    ang,
                    prefer=osm_name_is(net, osm_ids, v.name),
                )
                if h is not None:
                    return v, net.ways[osm_ids[h[0]]]
            return vlines[segs[0]], None

        vs, ws = match_road(carried)
        vx, wx = match_road(crossing)
        rec: dict[str, Any] = {
            "check": "vicmap_structure_node",
            "lon": round(lon, 6),
            "lat": round(lat, 6),
            "vicmap_node_ufi": node.ufi,
            "vicmap_node_type": node.ftype,
            "vicmap_carried_road": vs.name,
            "vicmap_carried_pfi": vs.pfi,
            "vicmap_crossing_road": vx.name,
            "vicmap_crossing_pfi": vx.pfi,
            "osm_carried_way": ws.id if ws else None,
            "osm_carried_name": ws.tags.get("name") if ws else None,
            "osm_crossing_way": wx.id if wx else None,
            "osm_crossing_name": wx.tags.get("name") if wx else None,
            "osm_junction_node": None,
            "review_area": gs.review_area(lon, lat),
        }
        if ws is None or wx is None:
            rec["class"] = "no_osm_match"
        elif ws.id == wx.id:
            rec["class"] = "same_osm_way"
        else:
            near = [
                n
                for n in set(ws.nodes) & set(wx.nodes)
                if math.hypot(osm_node_xy[n][0] - x, osm_node_xy[n][1] - y) <= JUNCTION_DIST_M
            ]
            if near:
                rec["osm_junction_node"] = near[0]
                rec["class"] = "osm_junction_vicmap_grade_separated"
            elif any(
                math.hypot(px - x, py - y) <= JUNCTION_DIST_M
                for px, py in osm_cross_xy.get(frozenset((ws.id, wx.id)), [])
            ):
                rec["class"] = "both_grade_separated"
            else:
                rec["class"] = "osm_no_crossing_here"
        recs.append(rec)
    return recs


# --- run ----------------------------------------------------------------------------------------


def run(
    extract: Path = gs.EXTRACT_PATH,
    roads3d: Path = gs.ROADS_3D_PATH,
    vicmap_road: Path = VICMAP_ROAD,
    vicmap_infra: Path = VICMAP_INFRA,
    bbox: BBox = SIM_EXTRACT_BBOX,
    pbf: Path | None = gs.RAW_PBF,
) -> dict[str, Any]:
    for p in (vicmap_road, vicmap_infra):
        if not p.exists():
            raise FileNotFoundError(f"{p} missing; run `uv run python -m pipeline.download`")
    proj = gs.Projection.for_bbox(bbox)
    ways, coords = gs.read_extract(extract)
    net = gs.Network(ways, coords, gs.read_roads_3d(roads3d))
    osm_ids = sorted(ways)
    osm_xy = {w: [proj.xy(*coords[n]) for n in ways[w].nodes] for w in osm_ids}
    oindex = SegmentIndex([osm_xy[w] for w in osm_ids])

    vlines, read_stats = read_vicmap(vicmap_road, proj, bbox)
    vindex = SegmentIndex([v.xy for v in vlines])
    infra = json.loads(vicmap_infra.read_text())["features"]
    nodes = build_nodes(vlines, infra, proj)

    osm_struct = osm_structure_records(net, osm_xy, vlines, vindex, proj)
    cut_ends = None
    if pbf is not None and pbf.exists():
        raw = gs.read_raw_context(pbf, set(ways), set())
        cut_ends = osm_cut_ends(net, raw.way_nodes, proj)
    vic_struct = vicmap_structure_records(net, osm_ids, oindex, vlines, proj, bbox, cut_ends)
    crossings, _ = gs.crossing_records(net, proj)
    order = crossing_order_records(crossings, osm_xy, vlines, vindex, nodes, proj)
    snodes = structure_node_records(net, osm_ids, oindex, vlines, nodes, crossings, proj, bbox)

    def by(rs: list[dict[str, Any]], check: str | None = None) -> dict[str, int]:
        return dict(Counter(r["class"] for r in rs if check is None or r["check"] == check))

    resolved = [r for r in order if r["roads_3d_agrees_with_vicmap"] is not None]
    summary = {
        "vicmap_features_read": read_stats.get("features_read", 0),
        "vicmap_vehicle_features": read_stats.get("vehicle_features", 0),
        "vicmap_vehicle_features_clipped_at_bbox_edge": read_stats.get("clipped_at_bbox_edge", 0),
        "vicmap_vehicle_features_outside_bbox_dropped": read_stats.get("outside_bbox_dropped", 0),
        "vicmap_vehicle_lines": len(vlines),
        "vicmap_vehicle_lines_by_type": dict(Counter(v.ftype for v in vlines)),
        "vicmap_infrastructure_points_by_type": dict(
            Counter(f["properties"]["feature_type_code"] for f in infra)
        ),
        "vicmap_model": "planar graph: every crossing is a node; grade separations are nodes of "
        "type bridge/tunnel whose conpfi1/conpfi2 name the road carried by the structure. "
        "No level or z field.",
        "match_rule": {
            "distance_m": MATCH_DIST_M,
            "angle_deg": MATCH_ANGLE_DEG,
            "sample_m": SAMPLE_M,
            "junction_radius_m": JUNCTION_DIST_M,
        },
        "a_osm_bridges": by(osm_struct, "osm_bridge"),
        "a_osm_tunnels": by(osm_struct, "osm_tunnel"),
        "a_vicmap_bridges": by(vic_struct, "vicmap_bridge"),
        "a_vicmap_tunnels": by(vic_struct, "vicmap_tunnel"),
        "b_crossing_order": by(order),
        "b_crossing_order_excluding_pending_tunnels": by(
            [r for r in order if r["grade_separation_class"] != "pending_tunnel_depth"]
        ),
        "b_disagree_with_both_names_matching": sum(
            1 for r in order if r["class"] == "disagree" and r["names_match"]
        ),
        "b_roads_3d_z_agrees_with_vicmap": dict(
            Counter(r["roads_3d_agrees_with_vicmap"] for r in resolved)
        ),
        "c2_vicmap_structure_nodes": by(snodes),
        "raw_pbf_used_for_extract_edge": cut_ends is not None,
    }
    inputs = (roads3d, extract, vicmap_road, vicmap_infra)
    return {
        "inputs_sha256": {gs.repo_key(p): gs.sha256_file(p) for p in inputs},
        "summary": summary,
        "osm_structures": osm_struct,
        "vicmap_structures": vic_struct,
        "crossing_order": order,
        "vicmap_structure_nodes": snodes,
    }


FLAG_CLASSES = {
    "osm_structures": {"no_vicmap_counterpart", "vicmap_not_bridge", "vicmap_not_tunnel"},
    "vicmap_structures": {"no_osm_counterpart", "osm_not_bridge", "osm_not_tunnel"},
    "crossing_order": {"disagree", "vicmap_at_grade_junction", "vicmap_inconsistent"},
    "vicmap_structure_nodes": {"osm_junction_vicmap_grade_separated"},
}


def flags_geojson(report: dict[str, Any]) -> dict[str, Any]:
    feats = []
    for key, classes in FLAG_CLASSES.items():
        for r in report[key]:
            if r["class"] not in classes:
                continue
            props = {k: v for k, v in r.items() if k not in ("lon", "lat")}
            for k, v in props.items():
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
        "about": "OSM road model vs Vicmap Transport (pipeline/checks/vicmap_crosscheck.py). "
        "Reports only. Vicmap has no level or z field. 'vicmap_upper' comes mainly from the "
        "crossing node: at a bridge/tunnel node, conpfi1/conpfi2 name the road the structure "
        "carries (on top at a bridge, underneath at a tunnel). Only where the node does not "
        "decide does it fall back to the segment types (bridge > road > tunnel); where the two "
        "disagree the crossing is 'vicmap_inconsistent'. Vicmap lines are clipped to "
        "SIM_EXTRACT_BBOX; an unmatched structure is 'extract_edge' only if its Vicmap line was "
        "clipped or its OSM counterpart was cut at the extract edge (never by distance alone). "
        "Heights: clearance-based (roads_3d.py), see grade_separation.json.",
        "licence": "Derived from OpenStreetMap (ODbL 1.0, (c) OpenStreetMap contributors) and "
        "Vicmap Transport ((c) State of Victoria, Department of Transport and Planning, CC BY 4.0)",
    }
    out_json.write_text(json.dumps({**meta, **report}, indent=1, ensure_ascii=False) + "\n")
    out_flags.write_text(json.dumps(flags_geojson(report), ensure_ascii=False) + "\n")


def main() -> int:
    report = run()
    write(report)
    print(json.dumps(report["summary"], indent=1))
    print(f"wrote {OUT_JSON} ({OUT_JSON.stat().st_size / 1e3:.0f} kB)")
    print(f"wrote {OUT_FLAGS} ({OUT_FLAGS.stat().st_size / 1e3:.0f} kB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
