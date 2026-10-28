"""Connectivity check on sim/net/roads.net.xml: what connects to what, and does it match OSM?

Usage (from the repo root):
    uv run python -m sim.checks.connectivity

Writes sim/out/checks/connectivity.json with five sections:

reachability   share of edges in the largest strongly connected component (SCC); every edge outside
               it, classed as an entry (reaches the SCC but cannot be reached), exit, or isolated,
               and whether it lies in the 300 m buffer ring around the simulated area (expected: the
               extract is clipped there, so roads end) or inside the simulated area (a problem).
               Dead ends (edges with no way on) and trap loops (one-way cycles you cannot leave).
grade_separation every pair of public car ways in the extract that cross without sharing a node
               while on different levels (OSM layer / bridge / tunnel), and any junction in the
               network that wrongly joins them. Each network connection is also traced back to an
               OSM path (sim/checks/movements.py); untraced connections are listed.
ramps          for every motorway_link / trunk_link: each movement OSM allows at the nodes it
               shares with other roads exists in the network, and every connection on a ramp is
               backed by an OSM path.
car_free       ways cars may not use (pedestrian, footway, cycleway, busway, motor_vehicle=no,
               private/permit, ...) in the raw OSM data have no edge; named checks for Swanston St
               through the CBD, Bourke St Mall and the CBD laneways, including a geometric test that
               no car edge runs along those corridors under another tag.
routing        fastest free-flow routes for sample trips: roads used, length, detour ratio,
               U-turns.
"""

from __future__ import annotations

import argparse
import heapq
import json
import math
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path

from pipeline.config import SIM_BBOX, SIM_EXTRACT_BBOX
from sim.checks import restrictions
from sim.checks.movements import Movements, Seg, oneway_dir, turn_angle
from sim.checks.netindex import NetIndex, OsmData, read_joined, read_osm
from sim.config import CHECKS_DIR, JOINED_PATH, NET_PATH, OSM_EXTRACT, REPO_ROOT
from sim.generated_patches import PRIVATE_VALUES, motor_access

RAW_PBF = REPO_ROOT / "data" / "raw" / "bbbike_melbourne.osm.pbf"

# ---------------------------------------------------------------------------------------------
# reachability


def _scc(edges, succ) -> list[list]:
    """Tarjan's strongly connected components, iterative."""
    index: dict = {}
    low: dict = {}
    on_stack: set = set()
    stack: list = []
    comps: list[list] = []
    counter = 0
    for root in edges:
        if root in index:
            continue
        work = [(root, iter(succ(root)))]
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        while work:
            v, it = work[-1]
            advanced = False
            for w in it:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(succ(w))))
                    advanced = True
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            if advanced:
                continue
            work.pop()
            if work:
                low[work[-1][0]] = min(low[work[-1][0]], low[v])
            if low[v] == index[v]:
                comp = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w is v:
                        break
                comps.append(comp)
    return comps


def _reach(starts, step) -> set:
    seen = set(starts)
    todo = list(starts)
    while todo:
        e = todo.pop()
        for n in step(e):
            if n not in seen:
                seen.add(n)
                todo.append(n)
    return seen


def _inside(bbox, lon: float, lat: float) -> bool:
    return bbox.west <= lon <= bbox.east and bbox.south <= lat <= bbox.north


def _edge_info(e, idx: NetIndex, osm: OsmData) -> dict:
    ways = idx.ways_by_edge.get(e.getID(), [])
    return {
        "edge": e.getID(),
        "name": e.getName() or None,
        "type": e.getType(),
        "osm_ways": ways,
        "length_m": round(e.getLength(), 1),
    }


def reachability(net, idx: NetIndex, osm: OsmData) -> dict:
    edges = net.getEdges()
    succ = lambda e: e.getOutgoing().keys()  # noqa: E731
    pred = lambda e: e.getIncoming().keys()  # noqa: E731
    comps = _scc(edges, succ)
    main = set(max(comps, key=len))
    total_len = sum(e.getLength() for e in edges)
    from_main = _reach(main, succ)
    to_main = _reach(main, pred)

    def where(e) -> str:
        # an entry starts where the road begins, an exit ends where it stops
        x, y = e.getShape()[0] if e not in from_main else e.getShape()[-1]
        lon, lat = net.convertXY2LonLat(x, y)
        return "sim_area" if _inside(SIM_BBOX, lon, lat) else "buffer_ring"

    def in_ring(e, at_end: bool) -> bool:
        x, y = e.getShape()[-1] if at_end else e.getShape()[0]
        lon, lat = net.convertXY2LonLat(x, y)
        return not _inside(SIM_BBOX, lon, lat)

    # Entries fed from a road that starts in the buffer ring (where the extract is clipped), and
    # exits that drain to one ending there, are expected: traffic enters and leaves the model there.
    sources = [e for e in edges if not e.getIncoming() and in_ring(e, False)]
    sinks = [e for e in edges if not e.getOutgoing() and in_ring(e, True)]
    fed = _reach(sources, succ)
    drained = _reach(sinks, pred)

    outside = []
    for e in edges:
        if e in main:
            continue
        cls = "exit" if e in from_main else "entry" if e in to_main else "isolated"
        expected = (cls == "entry" and e in fed) or (cls == "exit" and e in drained)
        outside.append(
            {
                **_edge_info(e, idx, osm),
                "class": cls,
                "where": where(e),
                "boundary_through_route": expected,
            }
        )

    dead_ends = []
    for e in edges:
        if not e.getOutgoing():
            x, y = e.getShape()[-1]
            lon, lat = net.convertXY2LonLat(x, y)
            dead_ends.append(
                {
                    **_edge_info(e, idx, osm),
                    "lon": round(lon, 6),
                    "lat": round(lat, 6),
                    "where": "sim_area" if _inside(SIM_BBOX, lon, lat) else "buffer_ring",
                    "turnaround_possible": False,
                }
            )

    traps = []
    comp_of = {e: i for i, c in enumerate(comps) for e in c}
    for i, comp in enumerate(comps):
        if len(comp) < 2 or set(comp) == main:
            continue
        leaves = any(comp_of[n] != i for e in comp for n in e.getOutgoing())
        if not leaves:
            x, y = comp[0].getShape()[0]
            lon, lat = net.convertXY2LonLat(x, y)
            traps.append(
                {
                    "where": "sim_area" if _inside(SIM_BBOX, lon, lat) else "buffer_ring",
                    "edges": [_edge_info(e, idx, osm) for e in comp],
                }
            )

    outside_sim = [o for o in outside if o["where"] == "sim_area"]
    problem = [o for o in outside_sim if not o["boundary_through_route"]]
    return {
        "edges": len(edges),
        "largest_scc_edges": len(main),
        "largest_scc_share": round(len(main) / len(edges), 4),
        "largest_scc_length_share": round(sum(e.getLength() for e in main) / total_len, 4),
        "components": len(comps),
        "outside_largest_scc": len(outside),
        "outside_by_class_and_place": dict(
            sorted(Counter(f"{o['class']}:{o['where']}" for o in outside).items())
        ),
        "outside_in_sim_area_not_boundary_routes": problem,
        "outside_in_sim_area": outside_sim,
        "outside_in_buffer_ring": [o for o in outside if o["where"] != "sim_area"],
        "dead_ends": len(dead_ends),
        "dead_ends_by_place": dict(Counter(d["where"] for d in dead_ends)),
        "dead_end_edges": dead_ends,
        "trap_loops": traps,
    }


# ---------------------------------------------------------------------------------------------
# grade separation


def _level(tags: dict[str, str]) -> tuple[int, str]:
    """(OSM layer, structure) with OSM's implied layers: bridge defaults to 1, tunnel to -1."""
    structure = (
        "bridge"
        if tags.get("bridge") not in (None, "no")
        else "tunnel"
        if tags.get("tunnel") not in (None, "no", "building_passage")
        else ""
    )
    try:
        layer = int(float(tags.get("layer", "")))
    except ValueError:
        layer = 1 if structure == "bridge" else -1 if structure == "tunnel" else 0
    return layer, structure


def _car_way(tags: dict[str, str]) -> bool:
    return (
        tags.get("highway") in restrictions.PASSENGER_HIGHWAYS
        and motor_access(tags) not in PRIVATE_VALUES
    )


@dataclass
class Crossing:
    way_a: int
    way_b: int
    x: float
    y: float
    level_a: tuple[int, str]
    level_b: tuple[int, str]


def find_crossings(osm: OsmData, mv: Movements, ways: list[int]) -> list[Crossing]:
    """Pairs of the given ways whose lines cross away from any shared node."""
    from shapely.geometry import LineString
    from shapely.strtree import STRtree

    lines, ids = [], []
    for w in ways:
        refs = osm.ways[w][0]
        if len(refs) >= 2:
            lines.append(LineString([mv.xy[n] for n in refs]))
            ids.append(w)
    tree = STRtree(lines)
    out = []
    for i, j in zip(*tree.query(lines, predicate="intersects"), strict=False):
        if i >= j:
            continue
        a, b = ids[i], ids[j]
        shared = set(osm.ways[a][0]) & set(osm.ways[b][0])
        shared_xy = [mv.xy[n] for n in shared]
        inter = lines[i].intersection(lines[j])
        pts = [inter] if inter.geom_type == "Point" else list(getattr(inter, "geoms", []))
        for p in pts:
            if p.geom_type != "Point":
                continue
            if any(math.dist((p.x, p.y), s) < 1.0 for s in shared_xy):
                continue
            out.append(
                Crossing(
                    a,
                    b,
                    p.x,
                    p.y,
                    _level(osm.ways[a][1]),
                    _level(osm.ways[b][1]),
                )
            )
    return out


def _connected_within(mv: Movements, members: set[int], a_nodes, b_nodes) -> bool:
    """Is there an OSM path (either direction) between the two node sets inside the junction?"""
    allowed = members | {n for n in mv.adj if mv.idx.junction(n) is None}
    und = defaultdict(set)
    for n in allowed:
        for m, _, _ in mv.adj.get(n, []):
            if m in allowed:
                und[n].add(m)
                und[m].add(n)
    seen = set(a_nodes)
    todo = list(a_nodes)
    while todo:
        n = todo.pop()
        if n in b_nodes:
            return True
        for m in und[n]:
            if m not in seen:
                seen.add(m)
                todo.append(m)
    return False


def grade_separation(net, idx: NetIndex, osm: OsmData, mv: Movements) -> dict:
    car_ways = [w for w, (_, t) in osm.ways.items() if _car_way(t) and w in idx.edges_by_way]
    crossings = find_crossings(osm, mv, car_ways)
    separated = [c for c in crossings if c.level_a != c.level_b]
    same_level = [c for c in crossings if c.level_a == c.level_b]

    junction_ways: dict[str, set[int]] = defaultdict(set)
    for e in net.getEdges():
        for w in idx.ways_by_edge.get(e.getID(), []):
            junction_ways[e.getFromNode().getID()].add(w)
            junction_ways[e.getToNode().getID()].add(w)

    joined_wrongly = []
    for c in separated:
        for jid, ways in junction_ways.items():
            if c.way_a in ways and c.way_b in ways:
                members = mv.members.get(jid, set())
                a_nodes = set(osm.ways[c.way_a][0]) & (members or set(osm.ways[c.way_a][0]))
                b_nodes = set(osm.ways[c.way_b][0]) & (members or set(osm.ways[c.way_b][0]))
                if members and not _connected_within(mv, members, a_nodes, b_nodes):
                    joined_wrongly.append(_crossing_row(c, osm, net) | {"junction": jid})

    # every connection, traced back to OSM
    untraced = []
    total = 0
    for e in net.getEdges():
        for t in e.getOutgoing():
            total += 1
            if e.getToNode().getID().endswith(("-AddedOnRampNode", "-AddedOffRampNode")):
                continue  # netconvert split one OSM way here to add a ramp lane: no OSM junction
            if not mv.movements(e, t, limit=50):
                fa = {_level(osm.ways[w][1]) for w in idx.ways_by_edge[e.getID()] if w in osm.ways}
                ta = {_level(osm.ways[w][1]) for w in idx.ways_by_edge[t.getID()] if w in osm.ways}
                untraced.append(
                    {
                        "from": e.getID(),
                        "to": t.getID(),
                        "junction": e.getToNode().getID(),
                        "from_name": e.getName() or None,
                        "to_name": t.getName() or None,
                        "levels_differ": not (fa & ta),
                    }
                )
    return {
        "car_way_crossings_without_shared_node": len(crossings),
        "grade_separated_crossings": len(separated),
        "same_level_crossings_without_junction": [_crossing_row(c, osm, net) for c in same_level],
        "grade_separated_joined_by_a_junction": joined_wrongly,
        "connections": total,
        "connections_traced_to_osm": total - len(untraced),
        "untraced_connections": untraced,
        "untraced_with_level_mismatch": [u for u in untraced if u["levels_differ"]],
        "grade_separated_examples": [_crossing_row(c, osm, net) for c in separated[:15]],
    }


def _crossing_row(c: Crossing, osm: OsmData, net) -> dict:
    lon, lat = net.convertXY2LonLat(c.x, c.y)
    return {
        "lon": round(lon, 6),
        "lat": round(lat, 6),
        "way_a": c.way_a,
        "name_a": osm.way_name(c.way_a),
        "level_a": list(c.level_a),
        "way_b": c.way_b,
        "name_b": osm.way_name(c.way_b),
        "level_b": list(c.level_b),
    }


# ---------------------------------------------------------------------------------------------
# ramps


def ramps(net, idx: NetIndex, osm: OsmData, mv: Movements) -> dict:
    links = [
        w
        for w, (_, t) in osm.ways.items()
        if t.get("highway") in ("motorway_link", "trunk_link") and w in idx.edges_by_way
    ]
    node_ways: dict[int, list[int]] = defaultdict(list)
    for w, (refs, t) in osm.ways.items():
        if _car_way(t) and w in idx.edges_by_way:
            for n in refs:
                node_ways[n].append(w)

    # transitions (p, v, q) covered by some connection, and by edge interiors
    covered: set[tuple[int, int, int]] = set()
    link_junctions = {
        n.getID()
        for w in links
        for e in idx.edges_by_way[w]
        for n in (e.getFromNode(), e.getToNode())
    }
    untraced_on_links = []
    for jid in link_junctions:
        node = net.getNode(jid)
        for f in node.getIncoming():
            for t in f.getOutgoing():
                paths = mv.movements(f, t, limit=50)
                if jid.endswith(("-AddedOnRampNode", "-AddedOffRampNode")):
                    continue
                if not paths and (
                    set(idx.ways_by_edge[f.getID()]) & set(links)
                    or set(idx.ways_by_edge[t.getID()]) & set(links)
                ):
                    untraced_on_links.append({"from": f.getID(), "to": t.getID(), "junction": jid})
                for p in paths:
                    for s1, s2 in zip(p, p[1:], strict=False):
                        covered.add((s1.a, s1.b, s2.b))

    def interior(x: int, y: int, v: int) -> bool:
        if idx.junction(v) is not None:
            return False
        return any(set(idx.ways_by_edge[e.getID()]) >= {x, y} for e in idx.edges_by_way[x])

    # OSM turn restrictions in force, by via node: a banned movement is not expected
    rels, _ = restrictions.parse_restrictions(osm)
    by_node: dict[int, list] = defaultdict(list)
    for m in restrictions.local_matchers(rels, osm, idx, mv).values():
        for mt in m:
            by_node[mt.entry].append(mt)

    def banned(x: int, p: int, v: int, y: int, q: int) -> list[int]:
        path = [Seg(x, p, v), Seg(y, v, q)]
        out = []
        for mt in by_node.get(v, []):
            if mt.r.via_nodes:
                if mt.violates(path):
                    out.append(mt.r.rel_id)
            elif mt.enters(path) is not None:  # via way: the first step must enter the via way
                first_ok = y == mt.chain[0]
                if (mt.r.kind.startswith("only_") and not first_ok) or (
                    mt.r.kind.startswith("no_") and first_ok and len(mt.chain) == 1 and mt.exit == v
                ):
                    out.append(mt.r.rel_id)
        return out

    expected, missing, seen = 0, [], set()
    banned_count = 0
    for w in links:
        refs = osm.ways[w][0]
        for v in refs:
            others = [o for o in node_ways.get(v, []) if o != w]
            if not others:
                continue
            for o in others:
                for x, y in ((w, o), (o, w)):
                    for p in _arrivals(osm, x, v):
                        for q in _departures(osm, y, v):
                            if p == q or (p, v, q) in seen:
                                continue
                            seen.add((p, v, q))
                            if banned(x, p, v, y, q):
                                banned_count += 1
                                continue
                            expected += 1
                            if (p, v, q) in covered or interior(x, y, v):
                                continue
                            angle = turn_angle(mv.xy[p], mv.xy[v], mv.xy[q])
                            missing.append(
                                {
                                    "turn_deg": round(angle),
                                    "sharp": abs(angle) > 120,
                                    "node": v,
                                    "from_way": x,
                                    "from_name": osm.way_name(x),
                                    "to_way": y,
                                    "to_name": osm.way_name(y),
                                    "lon": osm.nodes[v][0],
                                    "lat": osm.nodes[v][1],
                                }
                            )
    return {
        "ramp_ways": len(links),
        "expected_movements": expected,
        "movements_banned_by_osm_restrictions": banned_count,
        "missing_sharp_turns": sum(1 for m in missing if m["sharp"]),
        "missing_movements": missing,
        "untraced_connections_on_ramps": untraced_on_links,
    }


def _arrivals(osm: OsmData, way: int, v: int) -> list[int]:
    refs, tags = osm.ways[way]
    d = oneway_dir(tags)
    out = []
    for i, n in enumerate(refs):
        if n != v:
            continue
        if i > 0 and d >= 0:
            out.append(refs[i - 1])
        if i + 1 < len(refs) and d <= 0:
            out.append(refs[i + 1])
    return out


def _departures(osm: OsmData, way: int, v: int) -> list[int]:
    refs, tags = osm.ways[way]
    d = oneway_dir(tags)
    out = []
    for i, n in enumerate(refs):
        if n != v:
            continue
        if i + 1 < len(refs) and d >= 0:
            out.append(refs[i + 1])
        if i > 0 and d <= 0:
            out.append(refs[i - 1])
    return out


# ---------------------------------------------------------------------------------------------
# car-free streets

DRIVABLE = restrictions.PASSENGER_HIGHWAYS | {"service"}
LANE_SUFFIXES = ("Lane", "Place", "Alley", "Walk", "Arcade", "Way", "Court")
NAMED_LANEWAYS = (
    "Degraves Street",
    "Centre Place",
    "Hosier Lane",
    "AC/DC Lane",
    "Hardware Lane",
    "Block Place",
    "Howey Place",
    "Royal Arcade",
    "Duckboard Place",
    "Rutledge Lane",
)


def read_raw_ways(pbf: Path, bbox) -> dict[int, tuple[list[tuple[float, float]], dict[str, str]]]:
    import osmium

    out = {}
    fp = (
        osmium.FileProcessor(str(pbf))
        .with_locations()
        .with_filter(osmium.filter.KeyFilter("highway"))
    )
    for obj in fp:
        if not obj.is_way():
            continue
        pts = [(n.lon, n.lat) for n in obj.nodes if n.location.valid()]
        if any(_inside(bbox, lon, lat) for lon, lat in pts):
            out[obj.id] = (pts, dict(obj.tags))
    return out


def _no_cars(tags: dict[str, str]) -> str | None:
    """Why cars may not use this way (None if they may)."""
    hw = tags.get("highway", "")
    if hw not in DRIVABLE:
        return f"highway={hw}"
    acc = motor_access(tags)
    if acc in PRIVATE_VALUES:
        return f"motor access={acc}"
    if hw == "service":
        return "highway=service (not a passenger road in SUMO's typemap)"
    return None


def car_free(net, idx: NetIndex, raw: dict | None) -> dict:
    from shapely.geometry import LineString
    from shapely.ops import unary_union

    if raw is None:
        return {"skipped": f"{RAW_PBF} not found (raw data lives only in the main checkout)"}
    in_net: set[int] = set()
    for ways in idx.ways_by_edge.values():
        in_net.update(ways)

    no_car = {w: _no_cars(t) for w, (_, t) in raw.items()}
    no_car = {w: why for w, why in no_car.items() if why}
    leaked = [
        {"way": w, "name": raw[w][1].get("name"), "why": no_car[w]} for w in no_car if w in in_net
    ]

    def xy_line(pts):
        return LineString([net.convertLonLat2XY(lon, lat) for lon, lat in pts])

    def corridor(ways: list[int], buffer_m: float):
        return unary_union([xy_line(raw[w][0]) for w in ways]).buffer(buffer_m)

    def along(poly, label: str) -> list[dict]:
        """Car edges running along a corridor (> 30 m inside it); crossing streets are shorter."""
        hits = []
        for e in net.getEdges():
            line = LineString(e.getShape())
            if not line.intersects(poly):
                continue
            inside = line.intersection(poly).length
            if inside > 30.0:
                hits.append({"edge": e.getID(), "name": e.getName(), "inside_m": round(inside, 1)})
        return hits

    checks = []
    # Swanston Street through the CBD: OSM's own car-free Swanston ways, Flinders St to La Trobe St
    sw = [w for w, (_, t) in raw.items() if t.get("name") == "Swanston Street" and w in no_car]
    sw_cbd = [w for w in sw if all(-37.8185 <= lat <= -37.8095 for _, lat in raw[w][0])]
    poly = corridor(sw_cbd, 8.0)
    checks.append(
        {
            "check": "Swanston Street, Flinders St to La Trobe St",
            "osm_car_free_ways": len(sw_cbd),
            "why": sorted(Counter(no_car[w] for w in sw_cbd).items()),
            "car_ways_with_edges": [w for w in sw_cbd if w in in_net],
            "car_edges_along_corridor": along(poly, "swanston"),
        }
    )
    mall = [
        w
        for w, (_, t) in raw.items()
        if t.get("name") in ("Bourke Street Mall", "Bourke Street")
        and w in no_car
        and all(144.9625 <= lon <= 144.9665 for lon, _ in raw[w][0])
    ]
    poly = corridor(mall, 8.0)
    checks.append(
        {
            "check": "Bourke Street Mall, Elizabeth St to Swanston St",
            "osm_car_free_ways": len(mall),
            "why": sorted(Counter(no_car[w] for w in mall).items()),
            "car_ways_with_edges": [w for w in mall if w in in_net],
            "car_edges_along_corridor": along(poly, "mall"),
        }
    )
    lanes = [
        w
        for w, (pts, t) in raw.items()
        if (t.get("name") or "").endswith(LANE_SUFFIXES)
        and t.get("highway") in ("service", "pedestrian", "footway", "living_street", "steps")
        and all(_inside(SIM_BBOX, lon, lat) for lon, lat in pts)
    ]
    lane_names = sorted({raw[w][1]["name"] for w in lanes})
    named = {}
    for name in NAMED_LANEWAYS:
        ws = [w for w, (_, t) in raw.items() if t.get("name") == name]
        named[name] = {
            "osm_ways": len(ws),
            "highway": sorted({raw[w][1].get("highway") for w in ws}),
            "with_car_edges": [w for w in ws if w in in_net],
        }
    checks.append(
        {
            "check": "Laneways in the simulated area (service/pedestrian/footway/living_street "
            "ways named ...Lane/Place/Alley/Walk/Arcade/Way/Court)",
            "osm_ways": len(lanes),
            "names": len(lane_names),
            "car_free_with_car_edges": [
                {"way": w, "name": raw[w][1]["name"], "highway": raw[w][1]["highway"]}
                for w in lanes
                if w in in_net and raw[w][1]["highway"] != "living_street"
            ],
            "shared_zones_kept": sorted(
                {
                    raw[w][1]["name"]
                    for w in lanes
                    if w in in_net and raw[w][1]["highway"] == "living_street"
                }
            ),
            "shared_zones_note": "highway=living_street laneways are legal for cars (shared zone, "
            "10 km/h), so they stay in the network; listed for review",
            "named_examples": named,
        }
    )
    overrides = [
        {
            "edge": e.getID(),
            "overrides_osm_way": int(e.getID().removeprefix("override_")),
            "osm_says": no_car.get(int(e.getID().removeprefix("override_"))),
            "name": e.getName(),
            "why": "opened by decision (PLAN.md Decisions log), see sim/patches/osm/",
        }
        for e in net.getEdges()
        if e.getID().startswith("override_") and e.getID().removeprefix("override_").isdigit()
    ]
    return {
        "raw_ways_in_extract_bbox": len(raw),
        "overridden_by_decision": overrides,
        "ways_cars_may_not_use": len(no_car),
        "by_reason": dict(Counter(no_car.values()).most_common()),
        "of_which_have_car_edges": leaked,
        "named_checks": checks,
    }


# ---------------------------------------------------------------------------------------------
# routing sanity


@dataclass
class Trip:
    label: str
    origin: tuple[float, float, str, float | None]  # lon, lat, road name, heading (deg) or None
    dest: tuple[float, float, str, float | None]


SAMPLE_TRIPS = [
    Trip(
        "CBD (Collins St at William St) -> West Gate Fwy westbound",
        (144.9590, -37.8170, "Collins Street", None),
        (144.9400, -37.8257, "West Gate Freeway", 270),
    ),
    Trip(
        "Docklands (Docklands Dr) -> St Kilda Rd southbound at Domain",
        (144.9411, -37.8136, "Docklands Drive", None),
        (144.9715, -37.8314, "St Kilda Road", 170),
    ),
    Trip(
        "Southbank (Southbank Blvd) -> Docklands (Harbour Esplanade)",
        (144.9669, -37.8233, "Southbank Boulevard", None),
        (144.9452, -37.8156, "Harbour Esplanade", None),
    ),
    Trip(
        "Flinders St (at Elizabeth St) -> La Trobe St (at Elizabeth St)",
        (144.9645, -37.8180, "Flinders Street", None),
        (144.9620, -37.8103, "La Trobe Street", None),
    ),
    Trip(
        "Kings Way (northbound at Sturt St) -> CityLink eastbound (Domain/Burnley)",
        (144.9641, -37.8298, "Kings Way", None),
        (144.9721, -37.8246, "CityLink", 80),
    ),
    Trip(
        "Spencer St (north, at Dudley St) -> Flinders St (at Spring St)",
        (144.9492, -37.8090, "Spencer Street", 140),
        (144.9720, -37.8160, "Flinders Street", None),
    ),
    Trip(
        "Exhibition St (at La Trobe St) -> Sturt St Southbank",
        (144.9695, -37.8095, "Exhibition Street", 160),
        (144.9665, -37.8264, "Sturt Street", None),
    ),
    Trip(
        "St Kilda Rd northbound (Domain) -> Docklands (Harbour Esplanade)",
        (144.9711, -37.8299, "St Kilda Road", 0),
        (144.9452, -37.8156, "Harbour Esplanade", None),
    ),
    Trip(
        "West Gate Fwy eastbound -> CBD (Collins St at Queen St)",
        (144.9387, -37.8256, "", 90),
        (144.9612, -37.8165, "Collins Street", None),
    ),
    Trip(
        "Footscray Rd eastbound -> Spring St",
        (144.9356, -37.8095, "Footscray Road", 98),
        (144.9741, -37.8138, "Spring Street", None),
    ),
    Trip(
        "Wurundjeri Way southbound -> Montague St (South Melbourne)",
        (144.9494, -37.8161, "Wurundjeri Way", 150),
        (144.9500, -37.8305, "Montague Street", None),
    ),
    Trip(
        "Batman Ave westbound -> Southern Cross Station (Spencer St)",
        (144.9761, -37.8208, "Batman Avenue", None),
        (144.9533, -37.8173, "Spencer Street", None),
    ),
]


def _heading(e) -> float:
    (x1, y1), (x2, y2) = e.getShape()[0], e.getShape()[-1]
    return math.degrees(math.atan2(x2 - x1, y2 - y1)) % 360


def find_edges(net, lon, lat, name, heading, radius: float = 120) -> list:
    """The nearest matching edge in each travel direction (up to two candidates)."""
    x, y = net.convertLonLat2XY(lon, lat)
    best: dict[int, tuple[float, object]] = {}
    for e, d in net.getNeighboringEdges(x, y, radius):
        if (name and e.getName() != name) or not e.allows("passenger"):
            continue
        if heading is not None and abs((_heading(e) - heading + 180) % 360 - 180) > 60:
            continue
        key = int(((_heading(e) + 90) % 360) // 180)  # two opposite half-planes
        if key not in best or d < best[key][0]:
            best[key] = (d, e)
    return [e for _, e in sorted(best.values(), key=lambda t: t[0])]


def find_edge(net, lon: float, lat: float, name: str, heading: float | None, radius: float = 120):
    x, y = net.convertLonLat2XY(lon, lat)
    best, best_score = None, math.inf
    for e, d in net.getNeighboringEdges(x, y, radius):
        if name and e.getName() != name:
            continue
        if not e.allows("passenger"):
            continue
        score = d
        if heading is not None:
            diff = abs((_heading(e) - heading + 180) % 360 - 180)
            if diff > 60:
                continue
            score += diff
        if score < best_score:
            best, best_score = e, score
    return best


def fastest_path(net, src, dst) -> tuple[list, float] | tuple[None, float]:
    """Free-flow fastest path (edge length / speed limit) over the network's connections."""
    dist = {src: src.getLength() / src.getSpeed()}
    prev: dict = {}
    heap = [(dist[src], 0, src)]
    tie = 1
    while heap:
        d, _, e = heapq.heappop(heap)
        if e is dst:
            path = [e]
            while path[-1] in prev:
                path.append(prev[path[-1]])
            return path[::-1], d
        if d > dist.get(e, math.inf):
            continue
        for n in e.getOutgoing():
            nd = d + n.getLength() / n.getSpeed()
            if nd < dist.get(n, math.inf):
                dist[n] = nd
                prev[n] = e
                heapq.heappush(heap, (nd, tie, n))
                tie += 1
    return None, math.inf


def routing(net) -> list[dict]:
    rows = []
    for trip in SAMPLE_TRIPS:
        srcs, dsts = find_edges(net, *trip.origin), find_edges(net, *trip.dest)
        row = {"trip": trip.label}
        if not srcs or not dsts:
            row["flags"] = ["origin or destination edge not found"]
            rows.append(row)
            continue
        # the fastest of the direction combinations, so a one-way block does not force a loop
        options = [(fastest_path(net, a, b), a, b) for a in srcs for b in dsts]
        (path, cost), src, dst = min(options, key=lambda o: o[0][1])
        row.update(from_edge=src.getID(), to_edge=dst.getID())
        if path is None:
            row["flags"] = ["no route"]
            rows.append(row)
            continue
        names = []
        for e in path:
            n = e.getName() or f"({e.getType().removeprefix('highway.')})"
            if not names or names[-1] != n:
                names.append(n)
        length = sum(e.getLength() for e in path)
        (ax, ay), (bx, by) = src.getShape()[0], dst.getShape()[-1]
        crow = math.dist((ax, ay), (bx, by))
        uturns = [
            [a.getID(), b.getID()]
            for a, b in zip(path, path[1:], strict=False)
            if {c.getDirection() for c in a.getOutgoing()[b]} & {"t", "T"}
        ]
        flags = []
        ratio = length / crow if crow > 0 else 1.0
        if ratio > 1.8:
            flags.append(f"detour: {ratio:.2f} x the straight-line distance")
        if uturns:
            flags.append(f"{len(uturns)} U-turn(s)")
        row.update(
            roads=names,
            edges=len(path),
            length_km=round(length / 1000, 2),
            straight_line_km=round(crow / 1000, 2),
            detour_ratio=round(ratio, 2),
            free_flow_min=round(cost / 60, 1),
            uturns=uturns,
            flags=flags,
        )
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------------------------


def run(net_path: Path = NET_PATH, osm_path: Path = OSM_EXTRACT, pbf: Path = RAW_PBF) -> dict:
    import sumolib

    osm = read_osm(osm_path)
    net = sumolib.net.readNet(str(net_path), withInternal=False)
    idx = NetIndex(net, read_joined(JOINED_PATH))
    mv = Movements(osm, idx)
    raw = read_raw_ways(pbf, SIM_EXTRACT_BBOX) if pbf.exists() else None
    return {
        "network": net_path.name,
        "reachability": reachability(net, idx, osm),
        "grade_separation": grade_separation(net, idx, osm, mv),
        "ramps": ramps(net, idx, osm, mv),
        "car_free": car_free(net, idx, raw),
        "routing": routing(net),
    }


def summary(report: dict) -> dict:
    r, g, rp, cf = (
        report["reachability"],
        report["grade_separation"],
        report["ramps"],
        report["car_free"],
    )
    return {
        "edges": r["edges"],
        "largest_scc_share": r["largest_scc_share"],
        "largest_scc_length_share": r["largest_scc_length_share"],
        "outside_by_class_and_place": r["outside_by_class_and_place"],
        "dead_ends_by_place": r["dead_ends_by_place"],
        "trap_loops": len(r["trap_loops"]),
        "grade_separated_crossings": g["grade_separated_crossings"],
        "grade_separated_joined": len(g["grade_separated_joined_by_a_junction"]),
        "same_level_crossings_without_junction": len(g["same_level_crossings_without_junction"]),
        "connections": g["connections"],
        "untraced_connections": len(g["untraced_connections"]),
        "untraced_with_level_mismatch": len(g["untraced_with_level_mismatch"]),
        "ramp_ways": rp["ramp_ways"],
        "ramp_expected_movements": rp["expected_movements"],
        "ramp_missing_movements": len(rp["missing_movements"]),
        "ramp_missing_sharp_turns": rp["missing_sharp_turns"],
        "outside_scc_in_sim_area_not_boundary_routes": len(
            r["outside_in_sim_area_not_boundary_routes"]
        ),
        "trap_loops_in_sim_area": sum(1 for t in r["trap_loops"] if t["where"] == "sim_area"),
        "ramp_untraced_connections": len(rp["untraced_connections_on_ramps"]),
        "car_free_leaks": None if "skipped" in cf else len(cf["of_which_have_car_edges"]),
        "routes_flagged": [x["trip"] for x in report["routing"] if x.get("flags")],
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--net", type=Path, default=NET_PATH)
    ap.add_argument("-o", "--output", type=Path, default=CHECKS_DIR / "connectivity.json")
    args = ap.parse_args(argv)
    report = run(args.net)
    report = {"summary": summary(report), **report}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
