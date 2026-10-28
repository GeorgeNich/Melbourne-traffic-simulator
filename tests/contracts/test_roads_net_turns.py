"""Turn movements in sim/net/roads.net.xml against OSM (qa-reviewer, independent of sim/ code).

1. no_right_turn relations (incl. via-way ones absorbed into one joined junction, and conditional
   ones decided as in force for the weekday AM peak): the edge that carries the from way into the
   junction must have no right-turn connection ('r'/'R') onto the edge carrying the to way (or the
   way that continues it out of the junction). Relations this simple lookup cannot place (the from
   way absorbed into a joined junction, a to way running through the via node) are skipped; at
   least 40 must be checkable, so the test is never vacuous.
   A joined junction is one SUMO movement: a right-turn connection there is the banned turn, even if
   some longer OSM path through the cluster (a laneway, a U-turn at the lights) ends on the same
   edge. Victorian Road Rule 39 forbids U-turns at traffic lights unless signed.
2. The conditional restrictions that only apply in the PM peak or to tall vehicles are not applied:
   those right/left turns still exist for cars.
3. An approach whose OSM turn:lanes are all one side (e.g. right|right) must offer that turn: it
   must not dead-end, and must not offer only the opposite turn.
"""

from __future__ import annotations

import pytest

from tests.contracts.roads_net_helpers import (
    NET_PATH,
    OSM_PATH,
    junction_of_osm_node,
    load_net,
    load_osm,
)

pytestmark = pytest.mark.skipif(
    not (NET_PATH.exists() and OSM_PATH.exists()), reason="network or OSM extract not built yet"
)

# Conditional restrictions that are not in force for cars in the weekday AM peak.
NOT_IN_FORCE = {
    "no_right_turn @ (Mo-Fr 16:00-18:30)",
    "no_right_turn @ (height > 4)",
    "no_left_turn @ (height > 3.9)",
}
RIGHT, LEFT = {"r", "R"}, {"l", "L"}
RIGHT_LANES = {"right", "slight_right", "sharp_right"}
LEFT_LANES = {"left", "slight_left", "sharp_left"}


def _shared_end(osm, a: int, b: int) -> int | None:
    ra, rb = osm.ways[a][0], osm.ways[b][0]
    for n in (ra[0], ra[-1]):
        if n in (rb[0], rb[-1]):
            return n
    return None


def _locate(rel_id: int):
    """(from edges, to edges, junction) for a single-junction restriction, or None."""
    net, osm, j_of = load_net(), load_osm(), junction_of_osm_node()
    members, _ = osm.rels[rel_id]
    frm = [r for t, r, ro in members if ro == "from" and t == "way"]
    to = [r for t, r, ro in members if ro == "to" and t == "way"]
    via_n = [r for t, r, ro in members if ro == "via" and t == "node"]
    via_w = [r for t, r, ro in members if ro == "via" and t == "way"]
    if len(frm) != 1 or len(to) != 1:
        return None
    f, t = frm[0], to[0]
    if via_n:
        entry = exit_ = via_n[0]
    elif len(via_w) == 1:
        entry, exit_ = _shared_end(osm, f, via_w[0]), _shared_end(osm, via_w[0], t)
    else:
        return None
    if entry is None or exit_ is None:
        return None
    jid = j_of.get(entry)
    if jid is None or j_of.get(exit_) != jid:
        return None
    t_refs = osm.ways[t][0]
    if exit_ not in (t_refs[0], t_refs[-1]):
        return None
    far = t_refs[-1] if exit_ == t_refs[0] else t_refs[0]
    to_ways = {t}
    if j_of.get(far) == jid:  # the to way lies inside the joined junction: follow its continuation
        to_ways |= osm.ways_at()[far] - {t}
    from_edges = [e for e in net.edges.values() if f in e.ways and e.to == jid]
    to_edges = {e.id for e in net.edges.values() if e.frm == jid and to_ways & set(e.ways)}
    if not from_edges or not to_edges:
        return None
    return from_edges, to_edges, jid


def _restrictions(kind: str, in_force: bool):
    for rid, (_, tags) in sorted(load_osm().rels.items()):
        if tags.get("type") != "restriction":
            continue
        cond = tags.get("restriction:conditional")
        k = tags.get("restriction") or (cond.split("@")[0].strip() if cond else None)
        if k != kind:
            continue
        if cond and (cond in NOT_IN_FORCE) == in_force:
            continue
        if not cond and not in_force:
            continue
        yield rid


def test_no_right_turn_restrictions_are_enforced():
    checked, violated = 0, []
    for rid in _restrictions("no_right_turn", in_force=True):
        found = _locate(rid)
        if found is None:
            continue
        from_edges, to_edges, jid = found
        checked += 1
        for e in from_edges:
            for t, dirs in e.out.items():
                if t in to_edges and dirs & RIGHT:
                    violated.append((rid, e.id, t, e.name, jid[:40]))
    assert checked >= 40, f"only {checked} no_right_turn relations could be placed"
    assert not violated, f"banned right turns still in the network: {violated}"


@pytest.mark.parametrize(
    ("kind", "dirs"), [("no_right_turn", RIGHT), ("no_left_turn", LEFT)], ids=["right", "left"]
)
def test_conditional_restrictions_not_in_force_are_not_applied(kind: str, dirs: set[str]):
    checked, missing = 0, []
    for rid in _restrictions(kind, in_force=False):
        found = _locate(rid)
        if found is None:
            continue
        from_edges, to_edges, _ = found
        checked += 1
        if not any(t in to_edges and d & dirs for e in from_edges for t, d in e.out.items()):
            missing.append(rid)
    assert checked >= 1, f"no {kind} conditional relation could be placed"
    assert not missing, f"PM-peak / tall-vehicle restrictions applied to cars: {missing}"


def test_one_sided_turn_lanes_offer_their_turn():
    net, osm, j_of = load_net(), load_osm(), junction_of_osm_node()
    checked, bad = 0, []
    for w, (refs, tags) in osm.ways.items():
        tl = tags.get("turn:lanes")
        if not tl or tags.get("oneway") not in ("yes", "true", "1"):
            continue
        lanes = [set(x.split(";")) for x in tl.split("|")]
        for side, want, other in ((RIGHT_LANES, RIGHT, LEFT), (LEFT_LANES, LEFT, RIGHT)):
            if not all(lane and lane <= side for lane in lanes):
                continue
            jid = j_of.get(refs[-1])
            for e in net.edges.values():
                if w not in e.ways or e.to != jid:
                    continue
                checked += 1
                got = set().union(*e.out.values()) if e.out else set()
                if not got or (got <= other and not got & want):
                    bad.append((w, tags.get("name"), tl, e.id, sorted(got)))
    assert checked >= 20, f"only {checked} one-sided approaches found"
    assert not bad, f"approaches missing the turn their lane arrows show: {bad}"
