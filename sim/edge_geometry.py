"""SUMO network -> sim/out/edge_geometry.json: one 3D line per car edge, for colouring roads by
sim/out/edge_stats_<scenario>.parquet in the browser.

Contract: an array of `{edge_id, way_id, path: [[lon, lat, z_m], ...]}`, one entry per
non-internal edge of sim/net/roads.net.xml that passenger cars may use, sorted by edge_id.

- edge_id: exactly the id in roads.net.xml (and in edge_stats_*.parquet).
- path: the edge's carriageway centre in driving direction: the mean of its lane centrelines
  (vertex by vertex; netconvert gives the lanes of an edge the same vertices). On the 29 edges
  whose lanes have different vertex counts, the middle lane (index n // 2) is used. A two-way
  road therefore gives two separate lines, one on each side of the road (left-hand network), not
  two copies of the shared road centreline. lon/lat from the network's own projection
  (sumolib convertXY2LonLat, vectorised), 6 decimals (~0.1 m).
- z_m: height above ground from data/processed/roads_3d.geojson through the trips export's own
  height model (sim/export_trips.py: load_height_model, assign_heights, fill_and_smooth), so a
  car and the road under it get the same height. The path is sampled every DENSIFY_M (plus every
  shape vertex), each sample is projected onto the nearest of the edge's OSM ways (lane param
  origId) and z interpolated along that way's 3D line; then steps over 1 m between samples are
  smoothed exactly as in the trips export. Densified samples are dropped again where the height
  between the kept points is linear within SIMPLIFY_Z_M, and consecutive kept points never differ
  by more than 1 m in height (as in trips_*.json). Every shape vertex is kept. 2 decimals.
- way_id: the OSM way of the edge (from netconvert --output.original-names, lane param origId).
  When an edge carries several ways (828 edges, joined by netconvert), it is the way the largest
  share of the edge's length was mapped to by the height model (each sample weighted by half the
  length of its neighbouring segments), ties going to the way listed first in origId; if no
  sample reached a way in roads_3d, the first way in origId. Deterministic for a given network.

Coordinates stay inside SIM_EXTRACT_BBOX except lane offsets of a few metres past its edge and
the CityLink motorway tunnels, which run to TUNNEL_EXTRACT_BBOX (same rule as the trips export).

Run (from the repo root; needs only the network and data/processed):
    uv run python -m sim.edge_geometry
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from sim.export_trips import (
    SIMPLIFY_Z_M,
    STEP_TARGET_M,
    Sample,
    assign_heights,
    fill_and_smooth,
    load_height_model,
    to_lonlat,
)

DENSIFY_M = 5.0
VEHICLE_CLASS = "passenger"


def car_edges(net) -> list:
    """Non-internal edges with at least one lane passenger cars may use, sorted by id."""
    return sorted(
        (e for e in net.getEdges(withInternal=False) if e.allows(VEHICLE_CLASS)),
        key=lambda e: e.getID(),
    )


def carriageway_centre(edge) -> list[tuple[float, float]]:
    """Mean of the lane centrelines (same vertex count), else the middle lane's shape."""
    shapes = [ln.getShape() for ln in edge.getLanes()]
    if len({len(s) for s in shapes}) == 1:
        arr = np.array([[p[:2] for p in s] for s in shapes], dtype=float)
        return [(float(x), float(y)) for x, y in arr.mean(axis=0)]
    return [(float(p[0]), float(p[1])) for p in shapes[len(shapes) // 2]]


def densify(xy: list[tuple[float, float]], step: float = DENSIFY_M):
    """Points every <= `step` metres along the line; returns (points, is_vertex flags)."""
    pts: list[tuple[float, float]] = [xy[0]]
    vertex = [True]
    for (x0, y0), (x1, y1) in zip(xy, xy[1:], strict=False):
        seg = float(np.hypot(x1 - x0, y1 - y0))
        n = max(1, int(np.ceil(seg / step)))
        for k in range(1, n + 1):
            f = k / n
            pts.append((x0 + f * (x1 - x0), y0 + f * (y1 - y0)))
            vertex.append(k == n)
    return pts, vertex


def keep_points(d: np.ndarray, z: np.ndarray, vertex: list[bool]) -> list[int]:
    """Indices to keep: every vertex, plus densified points needed for the height profile (linear
    in distance within SIMPLIFY_Z_M between kept points, steps <= STEP_TARGET_M)."""
    n = len(d)
    keep = [0]
    a = 0
    while a < n - 1:
        b = a + 1
        while not vertex[b] and b + 1 < n:
            c = b + 1
            span = d[c] - d[a]
            f = (d[a + 1 : c] - d[a]) / span if span > 0 else np.zeros(c - a - 1)
            err = z[a] + f * (z[c] - z[a]) - z[a + 1 : c]
            if np.max(np.abs(err)) > SIMPLIFY_Z_M or abs(z[c] - z[a]) > STEP_TARGET_M:
                break
            b = c
        keep.append(b)
        a = b
    return keep


def edge_profile(edge, heights) -> tuple[list[tuple[float, float, float]], int | None]:
    """(kept [(x, y, z)], way id) for one edge."""
    eid = edge.getID()
    pts, vertex = densify(carriageway_centre(edge))
    xs = np.array([p[0] for p in pts])
    ys = np.array([p[1] for p in pts])
    seg = np.hypot(np.diff(xs), np.diff(ys))
    d = np.concatenate([[0.0], np.cumsum(seg)])
    samples = [
        Sample(0.0, float(x), float(y), f"{eid}_0", 0.0, float(s))
        for x, y, s in zip(xs, ys, d, strict=True)
    ]
    assign_heights(samples, heights)
    fill_and_smooth(samples)
    z = np.array([s.z for s in samples])
    keep = keep_points(d, z, vertex)

    # way covering most of the length: sample weight = half of each neighbouring segment
    weight = np.zeros(len(samples))
    weight[:-1] += seg / 2
    weight[1:] += seg / 2
    ways = heights.ways_by_edge.get(eid, [])
    share: dict[int, float] = {}
    for s, w in zip(samples, weight, strict=True):
        if s.way is not None:
            share[s.way] = share.get(s.way, 0.0) + float(w)
    if share:
        order = {w: i for i, w in enumerate(ways)}
        way = max(share, key=lambda w: (round(share[w], 2), -order.get(w, len(order))))
    else:
        way = ways[0] if ways else None
    return [(samples[i].x, samples[i].y, samples[i].z) for i in keep], way


def geometry_json(net, heights) -> tuple[str, dict]:
    """The contract JSON (one edge per line, fixed rounding: deterministic bytes) and a summary."""
    lines = []
    multi = no_way = points = 0
    tunnel_edges = 0
    for e in car_edges(net):
        pts, way = edge_profile(e, heights)
        lon, lat = to_lonlat(net, np.array([p[0] for p in pts]), np.array([p[1] for p in pts]))
        path = ",".join(
            f"[{lo:.6f},{la:.6f},{_z(p[2])}]" for lo, la, p in zip(lon, lat, pts, strict=True)
        )
        lines.append(
            f'{{"edge_id":{json.dumps(e.getID())},"way_id":{json.dumps(way)},"path":[{path}]}}'
        )
        multi += len(heights.ways_by_edge.get(e.getID(), [])) > 1
        no_way += way is None
        points += len(pts)
        tunnel_edges += way in heights.tunnel_ways
    summary = {
        "edges": len(lines),
        "points": points,
        "edges_with_several_ways": multi,
        "edges_without_way": no_way,
        "tunnel_edges": tunnel_edges,
    }
    return "[\n" + ",\n".join(lines) + "\n]\n", summary


def _z(v: float) -> str:
    r = round(v, 2)
    return "0" if r == 0 else f"{r:.2f}".rstrip("0").rstrip(".")


def export(net_path: Path, out_path: Path, inputs: dict[str, Path] | None = None) -> dict:
    """Write edge_geometry.json for `net_path`. `inputs` overrides osm, roads_3d, joined (tests)."""
    import sumolib

    from sim.checks.netindex import read_osm
    from sim.config import JOINED_PATH, OSM_EXTRACT, REPO_ROOT

    paths = {
        "osm": OSM_EXTRACT,
        "roads_3d": REPO_ROOT / "data" / "processed" / "roads_3d.geojson",
        "joined": JOINED_PATH,
        **(inputs or {}),
    }
    net = sumolib.net.readNet(str(net_path))
    heights = load_height_model(net, read_osm(paths["osm"]), paths["roads_3d"], paths["joined"])
    text, summary = geometry_json(net, heights)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text, encoding="utf-8")
    summary["bytes"] = out_path.stat().st_size
    return summary


def main() -> int:
    from sim.config import NET_PATH, OUT_DIR

    print(json.dumps(export(NET_PATH, OUT_DIR / "edge_geometry.json"), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
