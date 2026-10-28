"""Which OSM movement does each SUMO connection stand for?

A SUMO connection `f -> t` at junction J is mapped back to an OSM node path:

    last OSM segment of f into J  +  shortest directed OSM path inside J  +  first segment of t

"Inside J" means between OSM nodes that netconvert merged into J (a joined cluster), or nothing for
a plain one-node junction. The path is a list of `Seg(way, a, b)` (OSM way id, from node, to node).
Turn restrictions are then matched against these segment lists, and the connectivity check uses
them to confirm that every junction movement follows OSM ways that really share nodes.
"""

from __future__ import annotations

import heapq
import math
from dataclasses import dataclass
from functools import cached_property

from sim.checks.netindex import NetIndex, OsmData

# Ways SUMO's typemap treats as one-way even without an oneway tag.
IMPLIED_ONEWAY = {"motorway", "motorway_link"}
# Longest OSM path traced through one junction (a joined cluster is a few tens of metres across).
INNER_MAX_M = 150.0


@dataclass(frozen=True)
class Seg:
    way: int
    a: int
    b: int


def oneway_dir(tags: dict[str, str]) -> int:
    """1 forward only, -1 backward only, 0 both directions."""
    v = (tags.get("oneway") or "").lower()
    if v in ("yes", "true", "1"):
        return 1
    if v == "-1":
        return -1
    if v == "no":
        return 0
    if tags.get("junction") in ("roundabout", "circular"):
        return 1
    return 1 if tags.get("highway") in IMPLIED_ONEWAY else 0


def bearing(p: tuple[float, float], q: tuple[float, float]) -> float:
    return math.degrees(math.atan2(q[0] - p[0], q[1] - p[1]))  # clockwise from north


def turn_angle(p: tuple[float, float], v: tuple[float, float], q: tuple[float, float]) -> float:
    """Signed heading change at v for p -> v -> q, in (-180, 180]; positive = right turn."""
    d = bearing(v, q) - bearing(p, v)
    while d <= -180:
        d += 360
    while d > 180:
        d -= 360
    return d


def classify_turn(angle: float) -> set[str]:
    """Turn kinds compatible with a heading change (bands overlap: borderline turns fit both)."""
    kinds = set()
    a = abs(angle)
    if a <= 50:
        kinds.add("straight_on")
    if 25 <= angle <= 160:
        kinds.add("right_turn")
    if -160 <= angle <= -25:
        kinds.add("left_turn")
    if a >= 140:
        kinds.add("u_turn")
    return kinds


class Movements:
    def __init__(self, osm: OsmData, idx: NetIndex):
        self.osm = osm
        self.idx = idx
        self.net = idx.net
        # directed adjacency: node -> [(next_node, way, length)]
        self.adj: dict[int, list[tuple[int, int, float]]] = {}
        self.node_ways: dict[int, set[int]] = {}
        for wid, (refs, tags) in osm.ways.items():
            d = oneway_dir(tags)
            for n in refs:
                self.node_ways.setdefault(n, set()).add(wid)
            for a, b in zip(refs, refs[1:], strict=False):
                length = self._dist(a, b)
                if d >= 0:
                    self.adj.setdefault(a, []).append((b, wid, length))
                if d <= 0:
                    self.adj.setdefault(b, []).append((a, wid, length))
        self.members: dict[str, set[int]] = {}
        for nid, jid in idx.junction_of_node.items():
            if nid.lstrip("-").isdigit():
                self.members.setdefault(jid, set()).add(int(nid))

    @cached_property
    def xy(self) -> dict[int, tuple[float, float]]:
        return {n: self.idx.xy(lon, lat) for n, (lon, lat) in self.osm.nodes.items()}

    def _dist(self, a: int, b: int) -> float:
        (x1, y1), (x2, y2) = self.osm.nodes[a], self.osm.nodes[b]
        kx = 111_320.0 * math.cos(math.radians((y1 + y2) / 2))
        return math.hypot((x2 - x1) * kx, (y2 - y1) * 111_320.0)

    # -- edge ends ---------------------------------------------------------------------------
    def _end_seg(self, edge, at_end: bool) -> Seg | None:
        """The OSM segment by which `edge` enters (at_end) or leaves (not at_end) its junction."""
        junction = edge.getToNode() if at_end else edge.getFromNode()
        members = self.members.get(junction.getID(), set())
        ways = self.idx.ways_by_edge.get(edge.getID(), [])
        shape = edge.getShape()
        tip = shape[-1] if at_end else shape[0]
        near = shape[-2] if at_end else shape[1]
        best, best_d = None, math.inf
        for w in ways:
            if w not in self.osm.ways:
                continue
            refs, tags = self.osm.ways[w]
            d_way = oneway_dir(tags)
            # Neighbours on the side a car comes from (at_end) or goes to, by the way's direction.
            sides = {1: (-1,), -1: (1,), 0: (-1, 1)}[d_way if at_end else -d_way]
            for i, n in enumerate(refs):
                if members and n not in members:
                    continue
                d = math.dist(self.xy[n], tip)
                for j in (i + k for k in sides):
                    if 0 <= j < len(refs) and refs[j] not in members:
                        nb = refs[j]
                        score = d + 0.5 * math.dist(self.xy[nb], near)
                        if score < best_d:
                            best = Seg(w, nb, n) if at_end else Seg(w, n, nb)
                            best_d = score
        if best is None and not members:
            return None
        return best

    @cached_property
    def _in_seg(self) -> dict[str, Seg | None]:
        return {e.getID(): self._end_seg(e, True) for e in self.net.getEdges()}

    @cached_property
    def _out_seg(self) -> dict[str, Seg | None]:
        return {e.getID(): self._end_seg(e, False) for e in self.net.getEdges()}

    def in_seg(self, edge) -> Seg | None:
        return self._in_seg[edge.getID()]

    def out_seg(self, edge) -> Seg | None:
        return self._out_seg[edge.getID()]

    # -- inside a junction -------------------------------------------------------------------
    def inner_path(self, a: int, b: int, members: set[int]) -> list[Seg] | None:
        """Shortest directed OSM path a -> b inside the junction ([] if a == b).

        The path may use the junction's own OSM nodes and shape-only nodes that are no junction
        at all (netconvert drops them), never another junction, and is at most INNER_MAX_M long.
        """
        if a == b:
            return []
        dist = {a: 0.0}
        prev: dict[int, tuple[int, int]] = {}
        heap = [(0.0, a)]
        while heap:
            d, n = heapq.heappop(heap)
            if n == b:
                break
            if d > dist.get(n, math.inf):
                continue
            for m, w, length in self.adj.get(n, []):
                if m not in members and self.idx.junction(m) is not None:
                    continue
                nd = d + length
                if nd > INNER_MAX_M:
                    continue
                if nd < dist.get(m, math.inf):
                    dist[m] = nd
                    prev[m] = (n, w)
                    heapq.heappush(heap, (nd, m))
        if b not in prev:
            return None
        segs, n = [], b
        while n != a:
            p, w = prev[n]
            segs.append(Seg(w, p, n))
            n = p
        return segs[::-1]

    def inner_paths(self, a: int, b: int, members: set[int], limit: int = 400):
        """Every simple directed OSM path a -> b inside the junction (same node rules as
        `inner_path`), up to `limit` paths; yields lists of Seg."""
        if a == b:
            yield []
            return
        found = 0
        stack = [(a, 0.0, [], {a})]
        while stack and found < limit:
            n, d, segs, seen = stack.pop()
            for m, w, length in self.adj.get(n, []):
                if m in seen or d + length > INNER_MAX_M:
                    continue
                if m == b:
                    found += 1
                    yield [*segs, Seg(w, n, m)]
                    continue
                if m not in members and self.idx.junction(m) is not None:
                    continue
                stack.append((m, d + length, [*segs, Seg(w, n, m)], seen | {m}))

    def movements(self, f, t, limit: int = 400):
        """All OSM segment paths the connection f -> t can stand for (shortest first)."""
        s_in, s_out = self.in_seg(f), self.out_seg(t)
        if s_in is None or s_out is None:
            return []
        members = self.members.get(f.getToNode().getID(), set())
        paths = [[s_in, *p, s_out] for p in self.inner_paths(s_in.b, s_out.a, members, limit)]
        paths.sort(key=lambda segs: sum(self._dist(s.a, s.b) for s in segs))
        return paths

    def movement(self, f, t) -> list[Seg] | None:
        """OSM segments for the connection f -> t (None if it can't be traced back to OSM)."""
        s_in, s_out = self.in_seg(f), self.out_seg(t)
        if s_in is None or s_out is None:
            return None
        members = self.members.get(f.getToNode().getID(), set())
        inner = self.inner_path(s_in.b, s_out.a, members)
        if inner is None:
            return None
        return [s_in, *inner, s_out]

    def turn_kinds(self, s1: Seg, s2: Seg) -> set[str]:
        return classify_turn(turn_angle(self.xy[s1.a], self.xy[s1.b], self.xy[s2.b]))
