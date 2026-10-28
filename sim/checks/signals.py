"""Signal audit: every OSM highway=traffic_signals node mapped to a traffic light or explained.

Usage (from the repo root):
    uv run python -m sim.checks.signals

Writes sim/out/checks/signals.json. Each signal node gets one class:
    intersection    the node is (part of) a signalised intersection in the network
    mid_block       the node is its own traffic light on a plain stretch of road (no crossing road
                    for cars: pedestrian, tram or cycle signals); SUMO's default program for it is
                    a 90 s cycle with a short red
    stop_line       the node sits on an approach a few metres before a signalised junction; with
                    --tls.guess-signals netconvert moves the signal onto that junction
    dead_end        the node is the end of a road whose continuation is not a car road (or lies
                    beyond the extract); netconvert builds no program for it
    not_in_network the node is only on ways that are not passenger roads (e.g. highway=service)
    unexplained     none of the above (should be empty)

Signal timings are SUMO's defaults (netconvert's static programs). There is no open signal-timing
data for Melbourne, so every program is a placeholder; the report says so.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from sim.checks.netindex import NetIndex, read_joined, read_osm
from sim.config import CHECKS_DIR, JOINED_PATH, NET_PATH, OSM_EXTRACT, PATCH_DIR

# How far (m) from the edge of a signalised junction a signal node still counts as its stop line.
# netconvert's --tls.guess-signals.dist default is 25 m; a little slack for junction shapes.
STOP_LINE_M = 30.0


def _dist_to_shape(p: tuple[float, float], shape: list[tuple[float, float]]) -> float:
    if not shape:
        return math.inf
    if len(shape) == 1:
        return math.dist(p, shape[0])
    best = math.inf
    pts = [*shape, shape[0]]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
        dx, dy = x2 - x1, y2 - y1
        L2 = dx * dx + dy * dy
        t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((p[0] - x1) * dx + (p[1] - y1) * dy) / L2))
        best = min(best, math.dist(p, (x1 + t * dx, y1 + t * dy)))
    return best


def audit(net_path: Path = NET_PATH, osm_path: Path = OSM_EXTRACT, joined: Path = JOINED_PATH):
    import sumolib

    osm = read_osm(osm_path)
    net = sumolib.net.readNet(str(net_path), withInternal=False, withPrograms=True)
    idx = NetIndex(net, read_joined(joined))
    node_ways: dict[int, list[int]] = {}
    for wid, (refs, _) in osm.ways.items():
        for n in refs:
            node_ways.setdefault(n, []).append(wid)

    tls_nodes = [n for n in net.getNodes() if n.getType().startswith("traffic_light")]
    tls_of_node = {}
    for tl in net.getTrafficLights():
        for conn in tl.getConnections():
            tls_of_node[conn[0].getEdge().getToNode().getID()] = tl.getID()

    stop_line_patch = PATCH_DIR / "osm" / "stop_line_signals.nod.xml"
    patched_stop_lines = (
        {int(n.get("id")) for n in ET.parse(stop_line_patch).getroot().iter("node")}
        if stop_line_patch.exists()
        else set()
    )
    rows = []
    for nid, tags in sorted(osm.node_tags.items()):
        if tags.get("highway") != "traffic_signals":
            continue
        lon, lat = osm.nodes[nid]
        xy = idx.xy(lon, lat)
        ways = node_ways.get(nid, [])
        in_net = [w for w in ways if w in idx.edges_by_way]
        names = sorted({osm.way_name(w) for w in ways})
        row = {"node": nid, "lon": lon, "lat": lat, "roads": names}
        jid = idx.junction(nid)
        node = net.getNode(jid) if jid and net.hasNode(jid) else None
        if node is not None and node.getType().startswith("traffic_light"):
            cls = "mid_block" if _plain_junction(node) else "intersection"
            row.update(cls=cls, junction=jid, tls=tls_of_node.get(jid))
            if cls == "mid_block":
                near = _nearest_other_tls(node, tls_nodes)
                row["nearest_other_tls_m"] = round(near, 1)
        elif nid in patched_stop_lines:
            # a stop line the import had made its own light; stop_line_signals.nod.xml folds it
            # into the intersection ahead (sim/generated_patches.py)
            row.update(cls="stop_line", junction=jid, via="stop_line_signals.nod.xml")
        elif node is not None and node.getType() == "dead_end":
            row.update(cls="dead_end", junction=jid)
        elif not in_net and node is None:
            hw = sorted({osm.ways[w][1].get("highway", "?") for w in ways})
            row.update(cls="not_in_network", highway=hw)
        else:
            # Prefer a signalised junction at the end of an edge that carries this node's road.
            on_road = {
                n
                for w in in_net
                for e in idx.edges_by_way[w]
                for n in (e.getToNode(), e.getFromNode())
                if n.getType().startswith("traffic_light")
            }
            near = min(
                ((_dist_to_shape(xy, t.getShape() or [t.getCoord()[:2]]), t) for t in on_road),
                key=lambda x: x[0],
                default=(math.inf, None),
            )
            if near[0] > STOP_LINE_M:
                near = min(
                    (
                        (_dist_to_shape(xy, t.getShape() or [t.getCoord()[:2]]), t)
                        for t in tls_nodes
                    ),
                    key=lambda x: x[0],
                    default=(math.inf, None),
                )
            if near[1] is not None and near[0] <= STOP_LINE_M:
                row.update(
                    cls="stop_line",
                    junction=near[1].getID(),
                    tls=tls_of_node.get(near[1].getID()),
                    distance_m=round(near[0], 1),
                )
            else:
                row.update(
                    cls="unexplained",
                    junction=jid,
                    junction_type=node.getType() if node is not None else None,
                    nearest_tls_m=round(near[0], 1),
                )
        rows.append(row)

    signals_per_tls = Counter(r.get("tls") for r in rows if r.get("tls"))
    signal_xy = [idx.xy(r["lon"], r["lat"]) for r in rows]
    programs = []
    for tl in net.getTrafficLights():
        junctions = {c[0].getEdge().getToNode() for c in tl.getConnections()}
        nearby = sum(
            1
            for p in signal_xy
            if any(_dist_to_shape(p, j.getShape() or [j.getCoord()[:2]]) <= 40 for j in junctions)
        )
        progs = tl.getPrograms()
        prog = next(iter(progs.values())) if progs else None
        phases = prog.getPhases() if prog else []
        programs.append(
            {
                "tls": tl.getID(),
                "links": len(tl.getConnections()),
                "osm_signal_nodes": signals_per_tls.get(tl.getID(), 0),
                "osm_signal_nodes_within_40m": nearby,
                "cycle_s": round(sum(p.duration for p in phases), 1),
                "phases": len(phases),
                "timing": "sumo_default",
            }
        )
    return rows, programs


def _plain_junction(node) -> bool:
    """A junction with no crossing road: one way in and out, or a two-way road passing through."""
    inc, out = node.getIncoming(), node.getOutgoing()
    if len(inc) <= 1 and len(out) <= 1:
        return True
    if len(inc) == 2 and len(out) == 2:
        ends = {e.getFromNode().getID() for e in inc}
        return ends == {e.getToNode().getID() for e in out}
    return False


def _nearest_other_tls(node, tls_nodes) -> float:
    x, y = node.getCoord()[:2]
    return min(
        (math.dist((x, y), t.getCoord()[:2]) for t in tls_nodes if t is not node),
        default=math.inf,
    )


def write_report(rows, programs, path: Path) -> dict:
    by_class = Counter(r["cls"] for r in rows)
    report = {
        "network": NET_PATH.name,
        "osm_signal_nodes": len(rows),
        "by_class": dict(sorted(by_class.items())),
        "traffic_lights": len(programs),
        "traffic_lights_without_links": sum(1 for p in programs if p["links"] == 0),
        # A traffic light with no OSM signal node within 40 m of its junctions would be invented.
        "traffic_lights_without_osm_signal": [
            p["tls"] for p in programs if not p["osm_signal_nodes_within_40m"]
        ],
        "timings": (
            "SUMO defaults (netconvert static programs). No open signal-timing data exists for "
            "Melbourne, so cycle and phase lengths are placeholders, not measurements."
        ),
        "cycle_s_counts": dict(sorted(Counter(p["cycle_s"] for p in programs).items())),
        "programs": programs,
        "signals": rows,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--net", type=Path, default=NET_PATH)
    ap.add_argument("-o", "--output", type=Path, default=CHECKS_DIR / "signals.json")
    args = ap.parse_args(argv)
    rows, programs = audit(args.net)
    report = write_report(rows, programs, args.output)
    keys = (
        "osm_signal_nodes",
        "by_class",
        "traffic_lights",
        "traffic_lights_without_links",
        "traffic_lights_without_osm_signal",
        "cycle_s_counts",
    )
    print(json.dumps({k: report[k] for k in keys}))
    for r in rows:
        if r["cls"] in ("unexplained", "dead_end"):
            print(r)
    return 0


if __name__ == "__main__":
    sys.exit(main())
