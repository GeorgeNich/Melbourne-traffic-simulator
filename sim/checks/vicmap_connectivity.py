"""Junction connectivity of sim/net/roads.net.xml cross-checked against Vicmap Transport (report).

Usage (from the repo root):
    uv run python -m sim.checks.vicmap_connectivity

Vicmap Transport (CC BY 4.0, data/SOURCES.md) is an independent, planar road network: every 2D
crossing is a node, typed as an at-grade intersection (int_signal, int_nosignal, int_attribute,
int_locality, roundabout) or as a structure (bridge / tunnel, whose conpfi1/conpfi2 name the road
the structure carries). The readers and the line matching are reused, unchanged, from
pipeline/checks/vicmap_crosscheck.py; Vicmap is read in the SUMO network's own coordinates.

For every Vicmap node inside the simulated area where two or more vehicle roads meet:
    at-grade node   each road meeting there is matched to the SUMO edges along it (a point 12 m
                    out from the node, direction-consistent, same name preferred). The roads are
                    `connected` if edges of two different roads share a SUMO junction within
                    NODE_RADIUS_M, else `not_connected` (Vicmap has a junction, SUMO has none).
    structure node  the carried road and the crossing road are matched the same way; they must
                    NOT share a SUMO junction near the node: `separated`, else `joined`.
Roads that match no SUMO edge (e.g. car-free in OSM but vehicular in Vicmap) are listed as
`unmatched`, never as errors. Writes sim/out/checks/vicmap_connectivity.json.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from pathlib import Path

from shapely.geometry import LineString, Point

from pipeline.checks.vicmap_crosscheck import (
    JUNCTION_TYPES,
    STRUCTURE_NODE_TYPES,
    VICMAP_INFRA,
    VICMAP_ROAD,
    SegmentIndex,
    _angle_diff,
    build_nodes,
    read_vicmap,
    same_name,
)
from pipeline.config import SIM_BBOX, SIM_EXTRACT_BBOX
from sim.config import CHECKS_DIR, NET_PATH

PROBE_M = 12.0  # how far out along each Vicmap road from the node to match it to SUMO
NODE_RADIUS_M = 40.0  # a SUMO junction this close to the Vicmap node counts as the same place
FAR_RADIUS_M = 150.0  # second look for at-grade nodes not joined within NODE_RADIUS_M
MATCH_DIST_M = 20.0
MATCH_ANGLE_DEG = 30.0


class NetProjection:
    """Vicmap lon/lat into the SUMO network's x/y (what pipeline's readers expect)."""

    def __init__(self, net):
        self.net = net

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        return self.net.convertLonLat2XY(lon, lat)

    def lonlat(self, x: float, y: float) -> tuple[float, float]:
        return self.net.convertXY2LonLat(x, y)


def _probe(xy: list[tuple[float, float]], node: tuple[float, float]):
    """(x, y, angle) PROBE_M along the line from its end nearest `node`."""
    line = LineString(xy)
    from_start = math.dist(xy[0], node) <= math.dist(xy[-1], node)
    d = min(PROBE_M, line.length / 2)
    p = line.interpolate(d if from_start else line.length - d)
    q = line.interpolate(max(0.0, (d if from_start else line.length - d) - 1.0))
    r = line.interpolate(min(line.length, (d if from_start else line.length - d) + 1.0))
    ang = math.degrees(math.atan2(r.y - q.y, r.x - q.x)) % 180.0
    return p.x, p.y, ang


def _local_components(net, node_xy, radius: float = NODE_RADIUS_M) -> dict[str, str]:
    """SUMO junctions within NODE_RADIUS_M of the Vicmap node, grouped by the edges that run
    entirely inside that radius (a roundabout ring, the short links of a split intersection):
    junction id -> group id. Roads whose junctions fall in one group meet at the node."""
    x, y = node_xy
    near = (
        [n for n in net.getNeighboringNodes(x, y, NODE_RADIUS_M)]
        if hasattr(net, "getNeighboringNodes")
        else [n for n in net.getNodes() if math.dist(n.getCoord()[:2], node_xy) <= NODE_RADIUS_M]
    )
    ids = {n.getID() for n in near}
    parent = {i: i for i in ids}

    def find(i: str) -> str:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for n in near:
        for e in n.getOutgoing():
            t = e.getToNode().getID()
            if t in ids:
                parent[find(n.getID())] = find(t)
    return {i: find(i) for i in ids}


def run(net_path: Path = NET_PATH) -> dict:
    import sumolib

    net = sumolib.net.readNet(str(net_path), withInternal=False)
    proj = NetProjection(net)
    vlines, stats = read_vicmap(VICMAP_ROAD, proj, SIM_EXTRACT_BBOX)
    infra = json.loads(VICMAP_INFRA.read_text())["features"]
    nodes = build_nodes(vlines, infra, proj)

    edges = net.getEdges()
    index = SegmentIndex([list(e.getShape()) for e in edges])

    def match_nearest(v, node_xy) -> list:
        """The single nearest direction-consistent SUMO edge (structures: at an interchange the
        roads around a bridge are tied by ramps, so only the road itself may be matched)."""
        x, y, ang = _probe(v.xy, node_xy)
        hit = index.match(
            x,
            y,
            ang,
            MATCH_DIST_M,
            MATCH_ANGLE_DEG,
            prefer=lambda i: same_name(edges[i].getName(), v.name),
        )
        return [edges[hit[0]]] if hit else []

    def match(v, node_xy) -> list:
        """Every SUMO edge along the Vicmap road at the probe point: both carriageways of a
        divided road, both directions of a two-way one. Same-named edges win when there are any
        (so a parallel ramp or service road is not taken instead)."""
        x, y, ang = _probe(v.xy, node_xy)
        p = Point(x, y)
        hits = set()
        for k in index.tree.query(p.buffer(MATCH_DIST_M)).tolist():
            if _angle_diff(float(index.ang[k]), ang) > MATCH_ANGLE_DEG:
                continue
            if index.segs[k].distance(p) <= MATCH_DIST_M:
                hits.add(int(index.owner[k]))
        named = {i for i in hits if same_name(edges[i].getName(), v.name)}
        return [edges[i] for i in sorted(named or hits)]

    def near_junctions(e, node_xy) -> set[str]:
        out = set()
        for n in (e.getFromNode(), e.getToNode()):
            if math.dist(n.getCoord()[:2], node_xy) <= NODE_RADIUS_M:
                out.add(n.getID())
        return out

    rows = []
    for node in nodes.values():
        lon, lat = proj.lonlat(*node.xy)
        if not (SIM_BBOX.west <= lon <= SIM_BBOX.east and SIM_BBOX.south <= lat <= SIM_BBOX.north):
            continue
        segs = [vlines[i] for i in node.segments]
        structure = node.ftype in STRUCTURE_NODE_TYPES and node.carried
        if structure:
            groups = {
                "carried": [s for s in segs if s.pfi in node.carried],
                "crossing": [s for s in segs if s.pfi not in node.carried],
            }
        else:
            if node.ftype not in JUNCTION_TYPES and node.ftype is not None:
                continue
            groups: dict[str, list] = {}
            for s in segs:
                groups.setdefault(s.name or f"pfi {s.pfi}", []).append(s)
        if len(groups) < 2 or not all(groups.values()):
            continue
        matched = {}
        for road, ss in groups.items():
            es = [e for s in ss for e in (match_nearest if structure else match)(s, node.xy)]
            matched[road] = {
                "edges": sorted({e.getID() for e in es}),
                "junctions": set().union(*(near_junctions(e, node.xy) for e in es))
                if es
                else set(),
            }
        roads = list(matched)
        local = _local_components(net, node.xy)
        comp = {r: {local.get(j, j) for j in matched[r]["junctions"]} for r in roads}
        met = sorted(
            {c for i, a in enumerate(roads) for b in roads[i + 1 :] for c in comp[a] & comp[b]}
        )
        with_edges = [r for r in roads if matched[r]["edges"]]
        if len(with_edges) < 2:
            status = "unmatched"
        elif structure:
            carried = {net.getEdge(e).getName() for e in matched["carried"]["edges"]}
            crossing = {net.getEdge(e).getName() for e in matched["crossing"]["edges"]}
            if carried & crossing - {""}:
                # both probes landed on one road: Vicmap's crossing road has no distinct SUMO edge
                status = "ambiguous_match"
            else:
                status = "joined" if met else "separated"
        elif met:
            status = "connected"
        else:
            # Vicmap often puts a freeway merge or a split intersection's node some way from where
            # OSM / SUMO join the roads: look again over FAR_RADIUS_M along short SUMO links.
            wide = _local_components(net, node.xy, FAR_RADIUS_M)
            far_edges = {
                r: {
                    wide.get(n, n)
                    for e in matched[r]["edges"]
                    for n in (
                        net.getEdge(e).getFromNode().getID(),
                        net.getEdge(e).getToNode().getID(),
                    )
                    if n in wide
                }
                for r in roads
            }
            joined_far = any(
                far_edges[a] & far_edges[b] for i, a in enumerate(roads) for b in roads[i + 1 :]
            )
            status = "connected_within_150m" if joined_far else "not_connected"
        rows.append(
            {
                "vicmap_ufi": node.ufi,
                "vicmap_type": node.ftype,
                "kind": "structure" if structure else "at_grade",
                "status": status,
                "lon": round(lon, 6),
                "lat": round(lat, 6),
                "roads": {
                    r: {"sumo_edges": m["edges"], "sumo_junctions": sorted(m["junctions"])}
                    for r, m in matched.items()
                },
                "shared_sumo_junction_groups": met,
                "unmatched_roads": [r for r in roads if not matched[r]["edges"]],
            }
        )
    by = Counter(f"{r['kind']}:{r['status']}" for r in rows)
    return {
        "about": "Vicmap Transport node topology vs SUMO junctions in the simulated area. "
        "Reports only; Vicmap is a cross-check, not an input.",
        "licence": "Vicmap Transport (c) State of Victoria, CC BY 4.0",
        "vicmap_read": stats,
        "nodes_checked": len(rows),
        "by_kind_and_status": dict(sorted(by.items())),
        "structures_joined": [r for r in rows if r["status"] == "joined"],
        "at_grade_not_connected": [r for r in rows if r["status"] == "not_connected"],
        "connected_within_150m": [r for r in rows if r["status"] == "connected_within_150m"],
        "ambiguous_match": [r for r in rows if r["status"] == "ambiguous_match"],
        "unmatched": [r for r in rows if r["status"] == "unmatched"],
        "connected_or_separated": [r for r in rows if r["status"] in ("connected", "separated")],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--net", type=Path, default=NET_PATH)
    ap.add_argument("-o", "--output", type=Path, default=CHECKS_DIR / "vicmap_connectivity.json")
    args = ap.parse_args(argv)
    if not VICMAP_ROAD.exists():
        print(f"{VICMAP_ROAD} not found (raw data lives only in the main checkout)")
        return 1
    report = run(args.net)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("nodes_checked", "by_kind_and_status")}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
