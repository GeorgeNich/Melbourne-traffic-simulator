"""SCATS approach -> SUMO edge mapping.

Inputs:
- `pipeline/reference/scats_detector_map.json`: per site, the vehicle stop-line detectors of each
  approach (street + heading), read from the DTP configuration sheets (cited per row).
- `sim/net/roads.net.xml` (sim-engineer's network, `--output.original-names`).
- the site coordinates from the Victorian Traffic Signals register.

Method (per site):
1. The site's junction: the traffic-light node nearest the site coordinate (within
   `MAX_SITE_NODE_M`), every traffic-light node within `NEAR_NODE_M` of the site coordinate, and
   every other node of the same SUMO traffic light within `TLS_GROUP_M` of the nearest one (a
   divided-road intersection can be several nodes, under one program or several; netconvert's
   joined clusters, `sim/net/joined_junctions.xml`, are already single nodes).
2. Its approach edges: car edges that end at one of those nodes and start outside them.
3. Each approach (street, heading) is matched to the approach edges whose travel bearing (last
   `BEARING_TAIL_M` of the shape) is within `HEADING_TOL_DEG` of the heading AND whose street
   name matches. If the name does not match any edge (OSM vs DTP naming), a single edge in that
   heading is accepted and flagged `name_match: false`.
4. An approach that matches more than one edge is resolved only if all of them are fed by one
   common upstream edge (a slip lane splitting off): that upstream edge carries the whole
   approach. Otherwise it is dropped (reason recorded); counts are never split by guesswork.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
NET_PATH = REPO_ROOT / "sim" / "net" / "roads.net.xml"
DETECTOR_MAP = Path(__file__).resolve().parent / "reference" / "scats_detector_map.json"

MAX_SITE_NODE_M = 60.0
NEAR_NODE_M = 35.0
TLS_GROUP_M = 45.0
HEADING_TOL_DEG = 50.0
BEARING_TAIL_M = 25.0

HEADINGS = {"NB": 0, "NE": 45, "EB": 90, "SE": 135, "SB": 180, "SW": 225, "WB": 270, "NW": 315}
TL_TYPES = ("traffic_light", "traffic_light_unregulated", "traffic_light_right_on_red")

_ABBREV = {
    "st": "street",
    "rd": "road",
    "pde": "parade",
    "ave": "avenue",
    "av": "avenue",
    "hwy": "highway",
    "fwy": "freeway",
    "ln": "lane",
    "pl": "place",
    "bvd": "boulevard",
    "blvd": "boulevard",
    "dr": "drive",
    "cres": "crescent",
    "cr": "crescent",
    "esp": "esplanade",
    "prom": "promenade",
    "tce": "terrace",
    "sth": "south",
    "nth": "north",
}


def norm_street(name: str | None) -> str:
    """'Kings Way' / 'KINGSWAY' / 'Kingsway' -> 'kingsway'; 'Flinders St' -> 'flindersstreet'."""
    if not name:
        return ""
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    words = [_ABBREV.get(w, w) for w in words]
    return "".join(words)


def street_core(name: str | None) -> str:
    """The distinctive part only: 'Flinders Street' -> 'flinders', 'Kings Way' -> 'kings'."""
    n = norm_street(name)
    for suffix in (
        "street",
        "road",
        "parade",
        "avenue",
        "highway",
        "freeway",
        "lane",
        "place",
        "boulevard",
        "drive",
        "crescent",
        "esplanade",
        "promenade",
        "terrace",
        "way",
    ):
        if n.endswith(suffix) and len(n) > len(suffix):
            return n[: -len(suffix)]
    return n


def names_match(a: str | None, b: str | None) -> bool:
    ca, cb = street_core(a), street_core(b)
    if not ca or not cb:
        return False
    return ca == cb or ca.startswith(cb) or cb.startswith(ca)


def bearing_deg(x0: float, y0: float, x1: float, y1: float) -> float:
    """Compass bearing (0 = north, 90 = east) from (x0, y0) to (x1, y1) in projected metres."""
    return math.degrees(math.atan2(x1 - x0, y1 - y0)) % 360.0


def angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def heading_of(bearing: float) -> str:
    """Nearest of the 8 compass headings."""
    return min(HEADINGS, key=lambda h: angle_diff(HEADINGS[h], bearing))


def tail_bearing(shape: list[tuple[float, float]], tail_m: float = BEARING_TAIL_M) -> float:
    """Bearing of travel over the last `tail_m` metres of a polyline (into the junction)."""
    x1, y1 = shape[-1]
    acc = 0.0
    x0, y0 = shape[-1]
    for i in range(len(shape) - 1, 0, -1):
        ax, ay = shape[i - 1]
        bx, by = shape[i]
        acc += math.hypot(bx - ax, by - ay)
        x0, y0 = ax, ay
        if acc >= tail_m:
            break
    return bearing_deg(x0, y0, x1, y1)


@dataclass
class ApproachEdge:
    edge_id: str
    name: str
    bearing: float
    lanes: int
    way_ids: list[str]
    from_node: str
    to_node: str


@dataclass
class SiteJunction:
    site_no: int
    nodes: list[str]
    tls_ids: list[str]
    dist_m: float
    approaches: list[ApproachEdge] = field(default_factory=list)


class NetIndex:
    """The parts of the SUMO network the mapping needs (loaded once with sumolib)."""

    def __init__(self, net_path: Path = NET_PATH):
        import sumolib

        self.net = sumolib.net.readNet(
            str(net_path), withInternal=False, withPedestrianConnections=False
        )
        self.tl_nodes = [n for n in self.net.getNodes() if n.getType() in TL_TYPES]
        self.tls_of: dict[str, set[str]] = {}
        for tls in self.net.getTrafficLights():
            for conn in tls.getConnections():
                node = conn[0].getEdge().getToNode().getID()
                self.tls_of.setdefault(node, set()).add(tls.getID())

    def xy(self, lon: float, lat: float) -> tuple[float, float]:
        return self.net.convertLonLat2XY(lon, lat)

    def lonlat(self, x: float, y: float) -> tuple[float, float]:
        return self.net.convertXY2LonLat(x, y)

    def site_junction(self, site_no: int, lon: float, lat: float) -> SiteJunction | None:
        x, y = self.xy(lon, lat)
        best = None
        for n in self.tl_nodes:
            nx, ny = n.getCoord()
            d = math.hypot(nx - x, ny - y)
            if best is None or d < best[0]:
                best = (d, n)
        if best is None or best[0] > MAX_SITE_NODE_M:
            return None
        d0, n0 = best
        tls = self.tls_of.get(n0.getID(), set())
        nodes = {n0.getID()}
        for n in self.tl_nodes:
            nx, ny = n.getCoord()
            if math.hypot(nx - x, ny - y) <= NEAR_NODE_M:
                nodes.add(n.getID())
        if tls:
            cx, cy = n0.getCoord()
            for n in self.tl_nodes:
                if self.tls_of.get(n.getID(), set()) & tls:
                    nx, ny = n.getCoord()
                    if math.hypot(nx - cx, ny - cy) <= TLS_GROUP_M:
                        nodes.add(n.getID())
        sj = SiteJunction(site_no, sorted(nodes), sorted(tls), round(d0, 1))
        for nid in sj.nodes:
            node = self.net.getNode(nid)
            for e in node.getIncoming():
                if e.getFromNode().getID() in nodes:
                    continue
                if not e.allows("passenger"):
                    continue
                sj.approaches.append(self.approach_edge(e))
        return sj

    def approach_edge(self, e) -> ApproachEdge:
        return ApproachEdge(
            edge_id=e.getID(),
            name=e.getName() or "",
            bearing=round(tail_bearing(e.getShape()), 1),
            lanes=sum(1 for ln in e.getLanes() if ln.allows("passenger")),
            way_ids=way_ids(e),
            from_node=e.getFromNode().getID(),
            to_node=e.getToNode().getID(),
        )

    def common_upstream(self, edge_ids: list[str]) -> str | None:
        """The single car edge that feeds every edge in `edge_ids`, if there is one."""
        feeders = None
        for eid in edge_ids:
            e = self.net.getEdge(eid)
            inc = {i.getID() for i in e.getIncoming() if i.allows("passenger")}
            feeders = inc if feeders is None else feeders & inc
        if feeders and len(feeders) == 1:
            return next(iter(feeders))
        return None


def way_ids(edge) -> list[str]:
    """OSM way IDs of a SUMO edge (`origId` lane params from --output.original-names)."""
    ids: list[str] = []
    for ln in edge.getLanes():
        v = ln.getParam("origId") if hasattr(ln, "getParam") else None
        for w in (v or "").split():
            if w not in ids:
                ids.append(w)
    return ids


@dataclass
class EdgeMatch:
    edge_id: str | None
    candidates: list[str]
    name_match: bool
    bearing: float | None
    reason: str


def match_approach(
    sj: SiteJunction,
    street: str,
    heading: str,
    index: NetIndex | None = None,
    n_lanes: int | None = None,
) -> EdgeMatch:
    """The SUMO edge that carries the (street, heading) approach into this junction.

    `n_lanes` (the number of lane loops of a full-coverage lane group) is used only to choose
    between several candidate edges: exactly one candidate with that many car lanes is accepted
    (an approach drawn as two lane groups either side of an island)."""
    target = HEADINGS[heading]
    in_dir = [a for a in sj.approaches if angle_diff(a.bearing, target) <= HEADING_TOL_DEG]
    named = [a for a in in_dir if names_match(a.name, street)]
    cands = named or in_dir
    ids = [a.edge_id for a in cands]
    if not in_dir:
        return EdgeMatch(None, [], False, None, f"no approach edge heading {heading}")
    if not named and len(in_dir) > 1:
        return EdgeMatch(
            None, ids, False, None, f"{len(in_dir)} edges heading {heading}, none named {street}"
        )
    if len(cands) == 1:
        a = cands[0]
        return EdgeMatch(a.edge_id, ids, bool(named), a.bearing, "ok")
    if n_lanes is not None:
        same = [a for a in cands if a.lanes == n_lanes]
        if len(same) == 1:
            a = same[0]
            return EdgeMatch(a.edge_id, ids, bool(named), a.bearing, "lane group matched by lanes")
    if index is not None:
        up = index.common_upstream(ids)
        if up is not None:
            return EdgeMatch(up, ids, bool(named), None, "split approach: common upstream edge")
    return EdgeMatch(None, ids, bool(named), None, f"approach splits over {len(ids)} edges")


def load_detector_map(path: Path = DETECTOR_MAP) -> dict:
    return json.loads(path.read_text())
