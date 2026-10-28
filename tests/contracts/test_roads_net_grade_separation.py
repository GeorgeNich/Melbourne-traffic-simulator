"""Flyover-bug guard for sim/net/roads.net.xml (qa-reviewer, independent of sim/ code).

Roads that only cross at different heights (a bridge over a street, a tunnel under it) must never
share a SUMO junction, or cars could turn from the deck onto the road below. Levels come from
data/processed/roads_3d.geojson, the single source of road heights (CLAUDE.md).

1. Every pair of car ways in the network whose geometries cross without a shared OSM node, and
   that sit on different levels (OSM layer / bridge / tunnel, or z more than 2.5 m apart at the
   crossing), must not have edges meeting at a junction near the crossing point.
2. No SUMO connection joins two edges whose heights at the junction differ by more than a deck's
   clearance (6 m): a car cannot climb or drop a level inside a junction.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from functools import cache

import pytest

from tests.contracts.roads_net_helpers import NET_PATH, OSM_PATH, ROADS_3D_PATH, load_net, load_osm

pytestmark = pytest.mark.skipif(
    not (NET_PATH.exists() and ROADS_3D_PATH.exists() and OSM_PATH.exists()),
    reason="network, roads_3d.geojson or the OSM extract not built yet",
)

LEVEL_DZ_M = 2.5  # z gap at a crossing that counts as grade-separated
NEAR_CROSSING_M = 60.0  # a junction this close to the crossing would be the one joining them
MAX_CONNECTION_DZ_M = 6.0  # one deck's clearance


@cache
def roads_3d() -> dict[int, dict]:
    feats = json.loads(ROADS_3D_PATH.read_text(encoding="utf-8"))["features"]
    return {f["properties"]["way_id"]: f for f in feats}


def _z_near(way: int, lon: float, lat: float) -> tuple[float, float]:
    """z (m) of the roads_3d vertex of `way` nearest to (lon, lat), and its distance (m)."""
    kx = 111_320.0 * math.cos(math.radians(lat))
    best, best_d = 0.0, math.inf
    for x, y, z in roads_3d()[way]["geometry"]["coordinates"]:
        d = math.hypot((x - lon) * kx, (y - lat) * 111_320.0)
        if d < best_d:
            best, best_d = z, d
    return best, best_d


def _level(way: int) -> tuple:
    p = roads_3d()[way]["properties"]
    return (p.get("layer") or 0, bool(p.get("bridge")), bool(p.get("tunnel")))


def test_no_junction_joins_a_grade_separated_crossing():
    from shapely.geometry import LineString
    from shapely.strtree import STRtree

    net, osm, r3 = load_net(), load_osm(), roads_3d()
    junctions_of_way: dict[int, set[str]] = defaultdict(set)
    for e in net.edges.values():
        for w in e.ways:
            junctions_of_way[w] |= {e.frm, e.to}
    ids = sorted(w for w in junctions_of_way if w in r3)
    geoms = [LineString([c[:2] for c in r3[w]["geometry"]["coordinates"]]) for w in ids]
    tree = STRtree(geoms)

    crossings, joined = 0, []
    for i, g in enumerate(geoms):
        for j in tree.query(g):
            if j <= i:
                continue
            a, b = ids[i], ids[int(j)]
            if set(osm.ways[a][0]) & set(osm.ways[b][0]):
                continue  # they meet at a shared OSM node: a real connection
            inter = g.intersection(geoms[j])
            points = [inter] if inter.geom_type == "Point" else getattr(inter, "geoms", [])
            for p in points:
                if p.geom_type != "Point":
                    continue
                dz = abs(_z_near(a, p.x, p.y)[0] - _z_near(b, p.x, p.y)[0])
                if _level(a) == _level(b) and dz <= LEVEL_DZ_M:
                    continue
                crossings += 1
                x, y = net.xy(p.x, p.y)
                for jid in junctions_of_way[a] & junctions_of_way[b]:
                    jx, jy = float(net.junctions[jid]["x"]), float(net.junctions[jid]["y"])
                    if math.hypot(jx - x, jy - y) < NEAR_CROSSING_M:
                        joined.append((a, b, jid))
    assert crossings > 100, f"only {crossings} grade-separated crossings found: check is vacuous"
    assert not joined, f"junctions joining roads on different levels: {joined[:10]}"


def _end_z(net, e, at_end: bool) -> float | None:
    x, y = e.shape[-1] if at_end else e.shape[0]
    lon, lat = net.lonlat(x, y)
    best = None
    for w in e.ways:
        if w in roads_3d():
            z, d = _z_near(w, lon, lat)
            if best is None or d < best[1]:
                best = (z, d)
    return None if best is None else best[0]


def test_no_connection_changes_level():
    net = load_net()
    jumps = []
    for e in net.edges.values():
        zf = _end_z(net, e, True)
        for t in e.out:
            zt = _end_z(net, net.edges[t], False)
            if zf is not None and zt is not None and abs(zf - zt) > MAX_CONNECTION_DZ_M:
                jumps.append((e.id, t, round(zf, 1), round(zt, 1)))
    assert not jumps, f"connections that jump between levels: {jumps[:10]}"
