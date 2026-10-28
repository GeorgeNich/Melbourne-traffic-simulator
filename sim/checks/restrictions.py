"""Turn-restriction audit: every OSM type=restriction relation in the extract, on the network.

Usage (from the repo root):
    uv run python -m sim.checks.restrictions                 # audit sim/net/roads.net.xml
    uv run python -m sim.checks.restrictions --net sim/work/roads_osm.net.xml \\
        --write-patch sim/patches/net/turn_restrictions.con.xml   # (re)write the patch file

Writes sim/out/checks/turn_restrictions.json, one row per relation, with a status:
    enforced        the network does not allow what the relation forbids
    violated        the network still allows it (the row lists the offending connections)
    not_applied     deliberately not applied: a conditional restriction outside the weekday AM
                    peak, or one that only applies to vehicles taller than a car
    not_in_network  its from/to/via way is not a passenger road (e.g. highway=service)
    unresolved      the relation could not be located on the network (the row says why)

Each SUMO connection is traced back to the OSM node path it stands for (sim/checks/movements.py),
so restrictions are matched exactly, including ones whose via way netconvert absorbed into a
joined junction.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

from sim.checks.movements import Movements, Seg, turn_angle
from sim.checks.netindex import NetIndex, OsmData, read_joined, read_osm
from sim.config import CHECKS_DIR, JOINED_PATH, NET_PATH, OSM_EXTRACT
from sim.generated_patches import PRIVATE_VALUES, motor_access

# Conditional (time- or vehicle-based) restrictions, decided for the weekday AM peak (7-9 am).
# True = applied as a plain restriction, False = not applied.
CONDITIONAL_DECISIONS: dict[str, tuple[bool, str]] = {
    "no_right_turn @ (Mo-Fr 07:00-09:30,16:00-18:30)": (True, "in force 7-9 am weekdays"),
    "no_right_turn @ (Mo-Fr 06:00-10:00,15:00-19:00)": (True, "in force 7-9 am weekdays"),
    "no_right_turn @ (Mo-Fr 07:00 - 09:30)": (True, "in force 7-9 am weekdays"),
    "no_left_turn @ (Mo-Fr 7:30-9:30)": (True, "in force 7:30-9:00, most of the 7-9 am peak"),
    "no_right_turn @ (Su-Tu 04:00-20:00; Fr-Sa 00:00-20:00)": (
        True,
        "in force 7-9 am on Mon, Tue and Fri (3 of 5 weekdays), not Wed/Thu",
    ),
    "no_right_turn @ (Mo-Fr 16:00-18:30)": (False, "PM peak only (4-6:30 pm)"),
    "no_right_turn @ (height > 4)": (False, "vehicles over 4 m tall only; cars are not"),
    "no_left_turn @ (height > 3.9)": (False, "vehicles over 3.9 m tall only; cars are not"),
}

# `except=*` values that do not exempt private cars, so the restriction still applies to them.
NON_CAR_EXCEPTIONS = {"bicycle", "hgv", "taxi", "bus", "psv", "emergency", "delivery", "goods"}

# OSM highway values SUMO's typemap opens to passenger cars (highway=service is not one).
PASSENGER_HIGHWAYS = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
    "motorway_link",
    "trunk_link",
    "primary_link",
    "secondary_link",
    "tertiary_link",
}


@dataclass
class Restriction:
    rel_id: int
    kind: str  # e.g. no_right_turn
    conditional: str | None
    from_way: int
    via_nodes: list[int]
    via_ways: list[int]
    to_way: int
    except_: str | None

    @property
    def turn(self) -> str:
        return self.kind.split("_", 1)[1]  # right_turn, u_turn, straight_on, ...


@dataclass
class AuditRow:
    rel_id: int
    kind: str
    via: str  # "node" / "way"
    from_way: int
    to_way: int
    from_name: str
    to_name: str
    status: str
    detail: str = ""
    junctions: list[str] = field(default_factory=list)
    # connections [from_edge, to_edge] that break the restriction (the patch deletes them)
    offending: list[list[str]] = field(default_factory=list)
    # connections [from_edge, to_edge] the relation requires but the network lacks (only_*)
    missing: list[list[str]] = field(default_factory=list)


def parse_restrictions(osm: OsmData) -> tuple[list[Restriction], list[tuple[int, str]]]:
    out, bad = [], []
    for rid, (members, tags) in sorted(osm.relations.items()):
        if tags.get("type") != "restriction":
            continue
        kind = tags.get("restriction")
        cond = tags.get("restriction:conditional")
        if not kind and cond:
            kind = cond.split("@")[0].strip()
        frm = [ref for t, ref, role in members if role == "from" and t == "way"]
        to = [ref for t, ref, role in members if role == "to" and t == "way"]
        vn = [ref for t, ref, role in members if role == "via" and t == "node"]
        vw = [ref for t, ref, role in members if role == "via" and t == "way"]
        if not kind or len(frm) != 1 or len(to) != 1 or not (vn or vw):
            bad.append((rid, "malformed relation (needs one from way, one to way and a via)"))
            continue
        out.append(Restriction(rid, kind, cond, frm[0], vn, vw, to[0], tags.get("except")))
    return out, bad


def _shared_node(osm: OsmData, a: int, b: int) -> int | None:
    na, nb = osm.ways[a][0], osm.ways[b][0]
    ends_b = {nb[0], nb[-1]}
    for n in (na[0], na[-1]):
        if n in ends_b:
            return n
    common = set(na) & set(nb)
    return min(common) if common else None


def _order_via_ways(osm: OsmData, r: Restriction) -> list[int] | None:
    remaining, chain, prev = list(r.via_ways), [], r.from_way
    while remaining:
        nxt = next((w for w in remaining if _shared_node(osm, prev, w) is not None), None)
        if nxt is None:
            return None
        chain.append(nxt)
        remaining.remove(nxt)
        prev = nxt
    return chain


def _neighbours(osm: OsmData, way: int, node: int) -> list[int]:
    refs = osm.ways[way][0]
    out = []
    for i, n in enumerate(refs):
        if n == node:
            out += [refs[j] for j in (i - 1, i + 1) if 0 <= j < len(refs)]
    return out


class Matcher:
    """Restriction semantics on OSM nodes: which (p -> via -> q) turns a relation refers to."""

    def __init__(self, r: Restriction, osm: OsmData, mv: Movements):
        self.r, self.osm, self.mv = r, osm, mv
        self.chain: list[int] = []
        if r.via_nodes:
            self.entry = self.exit = r.via_nodes[0]
        else:
            chain = _order_via_ways(osm, r)
            if chain is None:
                raise LookupError("via ways do not connect the from and to ways")
            self.chain = chain
            self.entry = _shared_node(osm, r.from_way, chain[0])
            self.exit = _shared_node(osm, chain[-1], r.to_way)
            if self.entry is None or self.exit is None:
                raise LookupError("via ways do not touch the from/to ways")
        self.approach = set(_neighbours(osm, r.from_way, self.entry))
        self.depart = set(_neighbours(osm, r.to_way, self.exit))
        if r.via_nodes:
            pairs = {(p, q) for p in self.approach for q in self.depart}
            if len(pairs) > 1:  # a from/to way that runs through the via node: use the turn kind
                v = self.entry
                fit = {
                    (p, q)
                    for p, q in pairs
                    if r.turn in mv.turn_kinds(Seg(0, p, v), Seg(0, v, q))
                    and not (p == q and r.turn != "u_turn")
                }
                if fit:
                    pairs = fit
            self.pairs = pairs
            self.approach = {p for p, _ in pairs}
            self.depart = {q for _, q in pairs}

    def _runs(self, segs: list[Seg]) -> list[tuple[int, Seg, Seg]]:
        """Consecutive same-way segments merged: (way, first seg, last seg)."""
        runs: list[tuple[int, Seg, Seg]] = []
        for s in segs:
            if runs and runs[-1][0] == s.way and runs[-1][2].b == s.a:
                runs[-1] = (s.way, runs[-1][1], s)
            else:
                runs.append((s.way, s, s))
        return runs

    def enters(self, segs: list[Seg]) -> int | None:
        """Index of the segment arriving at the restriction's entry node from its from way."""
        for i, s in enumerate(segs[:-1]):
            if s.way == self.r.from_way and s.b == self.entry and s.a in self.approach:
                return i
        return None

    def performs(self, segs: list[Seg]) -> bool:
        """Does this OSM path make the restricted movement?"""
        i = self.enters(segs)
        if i is None:
            return False
        if self.r.via_nodes:
            nxt = segs[i + 1]
            ok = nxt.way == self.r.to_way and nxt.a == self.entry
            if ok and hasattr(self, "pairs"):
                ok = (segs[i].a, nxt.b) in self.pairs
            return ok
        runs = self._runs(segs[i + 1 :])
        want = [*self.chain, self.r.to_way]
        if len(runs) < len(want):
            return False
        if [w for w, _, _ in runs[: len(want)]] != want:
            return False
        return runs[len(want) - 1][1].a == self.exit and runs[len(want) - 1][1].b in self.depart

    def violates(self, segs: list[Seg]) -> bool:
        if self.r.kind.startswith("no_"):
            return self.performs(segs)
        return self.enters(segs) is not None and not self.performs(segs)


def _applies(r: Restriction, osm: OsmData) -> bool:
    """Whether the restriction is in force for cars in the AM peak scenario."""
    if r.conditional and not CONDITIONAL_DECISIONS.get(r.conditional, (False, ""))[0]:
        return False
    if r.except_ and set(r.except_.replace(",", ";").split(";")) - NON_CAR_EXCEPTIONS:
        return False
    return all(_car_way(osm, w) for w in (r.from_way, r.to_way, *r.via_ways))


def _car_way(osm: OsmData, wid: int) -> bool:
    """A way that is a public passenger road in the network (not service, not private/permit)."""
    tags = osm.ways.get(wid, ([], {}))[1]
    return tags.get("highway") in PASSENGER_HIGHWAYS and motor_access(tags) not in PRIVATE_VALUES


def _ways_at(osm: OsmData, node: int, idx: NetIndex) -> set[int]:
    """Passenger ways in the network that touch an OSM node."""
    return {w for w, (refs, _) in osm.ways.items() if node in refs and w in idx.edges_by_way}


def _junction_connections(net, junction_id: str):
    node = net.getNode(junction_id)
    for f in node.getIncoming():
        for t in f.getOutgoing():
            yield f, t


@dataclass
class Judged:
    f: object
    t: object
    paths: list[list[Seg]]
    legal: bool
    blame: set[int]


# A path through a joined junction can only excuse a connection if a driver could really make the
# connection's movement that way: one steady turn or a slip lane (*_link ways), never a U-turn
# (Victorian Road Rule 39 bans them at traffic lights) or a detour of several turns in different
# directions.
MAX_BEND_DEG = 140.0  # any single bend above this is a U-turn
MAX_ZIGZAG_DEG = 60.0  # total turning beyond the net turn (turns that cancel each other out)
MAX_NET_TURN_DEG = 150.0


def _plausible(path: list[Seg], mv: Movements, slip_lane: bool = False) -> bool:
    angles = [
        turn_angle(mv.xy[a.a], mv.xy[a.b], mv.xy[b.b]) for a, b in zip(path, path[1:], strict=False)
    ]
    if any(abs(a) > MAX_BEND_DEG for a in angles):
        return False
    net_turn = abs(sum(angles))
    if net_turn > MAX_NET_TURN_DEG:
        return False
    # A slip lane veers out of the road and then curves into the other one, so its bends partly
    # cancel; that is still one movement. Elsewhere cancelling bends mean a detour.
    return slip_lane or sum(abs(a) for a in angles) - net_turn <= MAX_ZIGZAG_DEG


def _is_slip_lane(path: list[Seg], osm: OsmData) -> bool:
    """Every segment off the entry and exit roads is on a *_link way (and there is one)."""
    ends = {path[0].way, path[-1].way}
    inner = [s for s in path if s.way not in ends]
    return bool(inner) and all(
        osm.ways.get(s.way, ([], {}))[1].get("highway", "").endswith("_link") for s in inner
    )


def _can_excuse(path: list[Seg], osm: OsmData, mv: Movements) -> bool:
    """Only paths over public car roads, driven as one plausible movement, excuse a connection."""
    return all(_car_way(osm, s.way) for s in path) and _plausible(
        path, mv, slip_lane=_is_slip_lane(path, osm)
    )


def _judge_junction(jid: str, matchers: list[Matcher], idx: NetIndex, mv: Movements, osm: OsmData):
    """Each connection at the junction: legal if at least one OSM path it can stand for is a
    plausible car movement (`_can_excuse`) that breaks none of the junction's restrictions;
    otherwise blamed on the restrictions broken by its shortest path, or by any of its paths."""
    out = []
    for f, t in _junction_connections(idx.net, jid):
        paths = mv.movements(f, t)
        if not paths:
            continue
        legal = any(
            _can_excuse(p, osm, mv) and not any(m.violates(p) for m in matchers) for p in paths
        )
        blame: set[int] = set()
        if not legal:
            blame = {m.r.rel_id for m in matchers if m.violates(paths[0])}
            if not blame:
                blame = {m.r.rel_id for m in matchers for p in paths if m.violates(p)}
        out.append(Judged(f, t, paths, legal, blame))
    return out


def audit_one(
    r: Restriction,
    osm: OsmData,
    idx: NetIndex,
    mv: Movements,
    local: dict[str, list[Matcher]],
    junction_results: dict,
) -> AuditRow:
    row = AuditRow(
        r.rel_id,
        r.kind if not r.conditional else f"{r.kind} (conditional: {r.conditional})",
        "node" if r.via_nodes else "way",
        r.from_way,
        r.to_way,
        osm.way_name(r.from_way),
        osm.way_name(r.to_way),
        status="unresolved",
    )
    if r.conditional:
        decision = CONDITIONAL_DECISIONS.get(r.conditional)
        if decision is None:
            row.detail = "conditional restriction without a decision"
            return row
        if not decision[0]:
            row.status, row.detail = "not_applied", decision[1]
            return row
        row.detail = f"conditional, applied: {decision[1]}. "
    if r.except_:
        exempt = set(r.except_.replace(",", ";").split(";")) - NON_CAR_EXCEPTIONS
        if exempt:
            row.status, row.detail = "not_applied", f"except={r.except_} exempts cars"
            return row
        row.detail += f"except={r.except_} does not exempt cars. "
    not_car = [w for w in (r.from_way, r.to_way, *r.via_ways) if not _car_way(osm, w)]
    if not_car:
        kinds = sorted(
            {
                osm.ways[w][1].get("highway", "?")
                + (
                    f" access={motor_access(osm.ways[w][1])}"
                    if motor_access(osm.ways[w][1])
                    else ""
                )
                for w in not_car
                if w in osm.ways
            }
        )
        row.status = "not_in_network"
        row.detail += f"way(s) {not_car} are not public car roads ({', '.join(kinds)})"
        return row
    try:
        m = Matcher(r, osm, mv)
    except LookupError as exc:
        row.detail += str(exc)
        return row
    j_start, j_end = idx.junction(m.entry), idx.junction(m.exit)
    row.junctions = sorted({j for j in (j_start, j_end) if j})
    if j_start is None or j_end is None:
        gone = [w for w in (r.from_way, r.to_way) if w not in idx.edges_by_way]
        if gone:
            row.status = "enforced"
            row.detail += (
                f"movement does not exist: way(s) {gone} have no edge (absorbed by junction "
                "joining) and the via node is no junction"
            )
        elif r.kind.startswith("only_") and _ways_at(osm, m.entry, idx) <= {
            r.from_way,
            r.to_way,
        }:
            row.status = "enforced"
            row.detail += "only the from and to ways meet at the via node: nothing else to do"
        else:
            row.detail += "via node is not a junction in the network"
        return row

    if j_start == j_end:
        if j_start not in junction_results:
            junction_results[j_start] = _judge_junction(j_start, local[j_start], idx, mv, osm)
        judged = junction_results[j_start]
        entering = [c for c in judged if any(m.enters(p) is not None for p in c.paths)]
        if not entering:
            untraced = sum(1 for _ in _junction_connections(idx.net, j_start)) - len(judged)
            if r.kind.startswith("no_") and untraced == 0:
                row.status = "enforced"
                row.detail += "no connection at the junction comes from the from way at the via"
            else:
                row.detail += (
                    f"no connection comes from the from way ({untraced} connections untraced)"
                )
            return row
        if r.kind.startswith("only_") and not any(
            any(m.performs(p) for p in c.paths) for c in entering
        ):
            row.status = "violated"
            row.detail += "the only allowed movement does not exist on the network"
            row.missing = _missing_only_links(m, entering, j_start, idx, mv)
            row.offending = sorted([c.f.getID(), c.t.getID()] for c in entering)
            if row.missing:
                row.detail += "; add " + ", ".join(f"{x[0]} > {x[1]}" for x in row.missing)
            return row
        offending = [(c.f, c.t) for c in entering if not c.legal and r.rel_id in c.blame]
    else:
        offending = _multi_junction(r, m, idx, mv, j_start)
        if offending is None:
            row.detail += "via ways span junctions in a way the audit cannot trace"
            return row
    row.offending = sorted({(f.getID(), t.getID()) for f, t in offending})
    row.offending = [list(p) for p in row.offending]
    row.status = "violated" if offending else "enforced"
    if offending:
        row.detail += "allowed: " + ", ".join(f"{a} > {b}" for a, b in row.offending)
    return row


def _missing_only_links(m: Matcher, entering, jid: str, idx: NetIndex, mv: Movements):
    """For an only_* relation whose allowed movement has no connection: the (from, to) edge
    pairs at the junction whose OSM paths would perform it."""
    members = mv.members.get(jid, set())
    froms = {c.f.getID(): c.f for c in entering}
    out = []
    for f in froms.values():
        for t in idx.net.getNode(jid).getOutgoing():
            s_in, s_out = mv.in_seg(f), mv.out_seg(t)
            if s_in is None or s_out is None:
                continue
            for inner in mv.inner_paths(s_in.b, s_out.a, members):
                if m.performs([s_in, *inner, s_out]):
                    # Left-hand traffic: lane 0 is at the kerb, the highest index by the centre
                    # line. Right turns and U-turns use the centre lanes, the rest the kerb lanes.
                    inner_side = m.r.turn in ("right_turn", "u_turn")
                    fl = f.getLaneNumber() - 1 if inner_side else 0
                    tl = t.getLaneNumber() - 1 if inner_side else 0
                    out.append([f.getID(), t.getID(), str(fl), str(tl)])
                    break
    return sorted(out)


def _multi_junction(r: Restriction, m: Matcher, idx: NetIndex, mv: Movements, j_start: str):
    """Restricted paths over several network junctions (via ways that kept their own edges).

    Returns the connections to delete: the first link of each offending path if its via edge
    leads nowhere but the forbidden target, else the last link if only the from edge feeds it.
    None if neither cut is clean.
    """
    via = set(m.chain)
    froms = [f for f in idx.net.getNode(j_start).getIncoming() if mv.in_seg(f)]
    froms = [f for f in froms if mv.in_seg(f).way == r.from_way and mv.in_seg(f).b == m.entry]
    paths = []
    for f in froms:
        queue = deque([[f]])
        while queue:
            path = queue.popleft()
            if len(path) > len(via) + 4:
                continue
            for n in path[-1].getOutgoing():
                ways = set(idx.ways_by_edge[n.getID()])
                if r.to_way in ways and len(path) > 1:
                    segs = []
                    for a, b in zip(path, [*path[1:], n], strict=False):
                        s = mv.movement(a, b) or []
                        segs = segs[:-1] + s if segs else s
                    if r.kind.startswith("no_") and m.performs(segs):
                        paths.append([*path, n])
                elif ways and ways <= via and n not in path:
                    queue.append([*path, n])
    if r.kind.startswith("only_"):
        return None
    cuts = []
    for p in paths:
        first_via, last_via = p[1], p[-2]
        reach = _forward_exits(first_via, via, idx)
        if reach and all(r.to_way in idx.ways_by_edge[e.getID()] for e in reach):
            cuts.append((p[0], first_via))
        elif all(e is p[-3] or e in p for e in _feeders(last_via, via, idx)):
            cuts.append((last_via, p[-1]))
        else:
            return None
    return cuts


def _forward_exits(edge, via: set[int], idx: NetIndex) -> list:
    out, seen, queue = [], {edge}, deque([edge])
    while queue:
        e = queue.popleft()
        for n in e.getOutgoing():
            if n in seen:
                continue
            seen.add(n)
            if set(idx.ways_by_edge[n.getID()]) <= via:
                queue.append(n)
            else:
                out.append(n)
    return out


def _feeders(edge, via: set[int], idx: NetIndex) -> list:
    out, seen, queue = [], {edge}, deque([edge])
    while queue:
        e = queue.popleft()
        for n in e.getIncoming():
            if n in seen:
                continue
            seen.add(n)
            out.append(n)
            if set(idx.ways_by_edge[n.getID()]) <= via:
                queue.append(n)
    return out


def local_matchers(
    rels: list[Restriction], osm: OsmData, idx: NetIndex, mv: Movements
) -> dict[str, list[Matcher]]:
    """Restrictions in force, by the (single) junction they act at."""
    local: dict[str, list[Matcher]] = {}
    for r in rels:
        if not _applies(r, osm):
            continue
        try:
            m = Matcher(r, osm, mv)
        except LookupError:
            continue
        j_start, j_end = idx.junction(m.entry), idx.junction(m.exit)
        if j_start is not None and j_start == j_end:
            local.setdefault(j_start, []).append(m)
    return local


def audit(net_path: Path = NET_PATH, osm_path: Path = OSM_EXTRACT, joined: Path = JOINED_PATH):
    import sumolib

    osm = read_osm(osm_path)
    net = sumolib.net.readNet(str(net_path), withInternal=False)
    idx = NetIndex(net, read_joined(joined))
    mv = Movements(osm, idx)
    rels, bad = parse_restrictions(osm)
    local = local_matchers(rels, osm, idx, mv)
    junction_results: dict = {}
    rows = [audit_one(r, osm, idx, mv, local, junction_results) for r in rels]
    rows += [AuditRow(rid, "?", "?", 0, 0, "", "", "unresolved", why) for rid, why in bad]
    return sorted(rows, key=lambda r: r.rel_id)


def write_report(rows: list[AuditRow], path: Path, net_path: Path = NET_PATH) -> dict:
    report = {
        "network": str(net_path.name),
        "relations": len(rows),
        "by_status": dict(sorted(Counter(r.status for r in rows).items())),
        "by_via_and_status": dict(sorted(Counter(f"{r.via}:{r.status}" for r in rows).items())),
        "conditional_decisions": {
            k: {"applied": v[0], "reason": v[1]} for k, v in CONDITIONAL_DECISIONS.items()
        },
        "rows": [asdict(r) for r in rows],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    return report


PATCH_HEADER = """<?xml version="1.0" encoding="UTF-8"?>
<!--
  PATCH: OSM turn restrictions that netconvert's own import does not apply.

  Generated from the turn-restriction audit (sim/checks/restrictions.py) on the unpatched import
  (sim/work/roads_osm.net.xml) by the network build's refresh-patches option, then reviewed.
  Each <delete> removes one connection (all its lanes) that performs a movement the OSM relation
  forbids; a <connection> adds the one movement an only_* relation requires when the import has
  no connection for it (a U-turn bay merged into the main carriageway). netconvert misses these
  because the via is a way (often one that junction joining absorbed), because the relation is
  conditional (decided for the weekday AM peak, see CONDITIONAL_DECISIONS in
  sim/checks/restrictions.py), or because the from/to way runs through the via node so it cannot
  tell the direction.

  Edge ids are those of the imported network; if the OSM extract or the import options change,
  regenerate (uv run python -m sim.build_net with the refresh-patches option) and check the
  audit (sim/out/checks/turn_restrictions.json) before committing.
-->
<connections>
"""


def write_patch(rows: list[AuditRow], path: Path, skip_edges: set[str] | None = None) -> int:
    lines = [PATCH_HEADER]
    n = 0
    skip = skip_edges or set()
    first_rel: dict[tuple[str, str], int] = {}  # each connection is deleted once
    for r in rows:
        if r.status != "violated" or not (r.offending or r.missing):
            continue
        note = f"relation {r.rel_id}: {r.kind}, {r.from_name} to {r.to_name} (via {r.via})"
        if r.missing:
            note += ". Its only allowed movement had no connection, so it is added"
        dup = [first_rel[(a, b)] for a, b in r.offending if (a, b) in first_rel]
        if dup:
            note += f". Also covers deletions listed under relation(s) {sorted(set(dup))}"
        body = []
        for a, b in r.offending:
            if a in skip or b in skip or (a, b) in first_rel:
                continue  # edge deleted as a private road, or connection already deleted above
            first_rel[(a, b)] = r.rel_id
            body.append(f'    <delete from="{a}" to="{b}"/>\n')
        for a, b, fl, tl in r.missing:
            body.append(f'    <connection from="{a}" to="{b}" fromLane="{fl}" toLane="{tl}"/>\n')
        if not body and not dup:
            continue  # nothing left to change for this relation
        lines.append(f"    <!-- {escape(note).replace('-->', '>').replace('--', '-')} -->\n")
        lines += body
        n += len(body)
    lines.append("</connections>\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8")
    return n


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--net", type=Path, default=NET_PATH)
    ap.add_argument("-o", "--output", type=Path, default=CHECKS_DIR / "turn_restrictions.json")
    ap.add_argument("--write-patch", type=Path, help="write <delete> lines for violations here")
    args = ap.parse_args(argv)
    rows = audit(args.net)
    report = write_report(rows, args.output, args.net)
    print(json.dumps({k: report[k] for k in ("relations", "by_status", "by_via_and_status")}))
    for r in rows:
        if r.status in ("violated", "unresolved"):
            print(f"{r.status:10} {r.rel_id} {r.kind} via {r.via}: {r.from_name} -> {r.to_name}")
            print(f"           {r.detail}")
    if args.write_patch:
        print(f"wrote {write_patch(rows, args.write_patch)} deletions to {args.write_patch}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
