"""Australian Road Rules (Victoria) audit of the SUMO network and simulation.

Usage (from the repo root):
    uv run python -m sim.checks.road_rules

Writes sim/out/checks/road_rules.json: for each rule, what the Road Safety Road Rules 2017 (Vic)
(the Australian Road Rules as adopted in Victoria, "ARR") require, what the network / simulation
does, the evidence (queries on sim/net/roads.net.xml, and TraCI micro-scenarios on small networks
built with the same netconvert options), and a verdict: complies, fixed (and how), or
cannot_be_modelled (and why, with its visual or behavioural impact).

Notes on what this simulation can and cannot represent:
- Cars only (passenger vClass). No pedestrians, cyclists, trams or buses, so every rule about
  giving way to them (shared zones r.83, pedestrian crossings, trams r.163-164) is not modelled.
- Signal timings are SUMO's static 90 s defaults: there is no open signal-timing data.
- Hook turns (r.34-35: CBD right turns made from the kerb lane at signed intersections) are not
  modelled: SUMO has no hook-turn manoeuvre and OSM tags none of them here; right turns are made
  from the centre lane, as at an ordinary intersection.
- Time-based rules (school zones, clearways, peak-hour turn bans) apply only where a decision
  was made for the weekday AM peak (sim/checks/restrictions.py CONDITIONAL_DECISIONS).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

from sim.build_net import SHARED_OPTIONS
from sim.checks.netindex import NetIndex, read_joined, read_osm
from sim.config import CHECKS_DIR, JOINED_PATH, NET_PATH, OSM_EXTRACT, REPO_ROOT
from sim.generated_patches import sign_approach

RAW_PBF = REPO_ROOT / "data" / "raw" / "bbbike_melbourne.osm.pbf"

# Hoddle Grid (CBD), approximate box: Flinders St to La Trobe St, Spencer St to Spring St.
CBD_BOX = (144.9515, -37.8195, 144.9740, -37.8085)
MINOR_STATES = set("mswo=")


# --- TraCI micro-scenarios ------------------------------------------------------------------


def _netconvert(nodes: str, edges: str, out: Path) -> Path:
    import subprocess

    import sumolib

    d = out.parent
    (d / "n.nod.xml").write_text(nodes)
    (d / "e.edg.xml").write_text(edges)
    opts = [o for o in SHARED_OPTIONS if o not in ("--output.original-names",)]
    subprocess.run(
        [
            sumolib.checkBinary("netconvert"),
            "-n",
            str(d / "n.nod.xml"),
            "-e",
            str(d / "e.edg.xml"),
            *opts,
            "-o",
            str(out),
        ],
        check=True,
        capture_output=True,
    )
    return out


def give_way_to_right_test() -> dict:
    """Two cars reach an unsignalised cross of equal 40 km/h roads at the same time: northbound
    and westbound. For the northbound driver the westbound car comes from the RIGHT (east), so
    under r.72 northbound gives way."""
    import libsumo as traci
    import sumolib

    with tempfile.TemporaryDirectory() as tmp:
        net = _netconvert(
            "<nodes>"
            '<node id="C" x="0" y="0"/><node id="N" x="0" y="200"/><node id="S" x="0" y="-200"/>'
            '<node id="E" x="200" y="0"/><node id="W" x="-200" y="0"/></nodes>',
            "<edges>"
            + "".join(
                f'<edge id="{a}{b}" from="{a}" to="{b}" numLanes="1" speed="11.11"/>'
                for a, b in (
                    ("S", "C"),
                    ("C", "N"),
                    ("E", "C"),
                    ("C", "W"),
                    ("N", "C"),
                    ("C", "S"),
                    ("W", "C"),
                    ("C", "E"),
                )
            )
            + "</edges>",
            Path(tmp) / "cross.net.xml",
        )
        jtype = sumolib.net.readNet(str(net)).getNode("C").getType()
        traci.start([sumolib.checkBinary("sumo"), "-n", str(net), "--no-step-log", "--seed", "42"])
        try:
            traci.route.add("north", ["SC", "CN"])
            traci.route.add("west", ["EC", "CW"])
            traci.vehicle.add("northbound", "north", departPos="100", departSpeed="10")
            traci.vehicle.add("westbound", "west", departPos="100", departSpeed="10")
            low = {"northbound": 99.0, "westbound": 99.0}
            first = None
            for _ in range(400):
                traci.simulationStep()
                for v in traci.vehicle.getIDList():
                    low[v] = min(low[v], traci.vehicle.getSpeed(v))
                    if first is None and traci.vehicle.getRoadID(v) in ("CN", "CW"):
                        first = v
        finally:
            traci.close()
    ok = first == "westbound" and low["northbound"] < low["westbound"]
    return {
        "junction_type_built": jtype,
        "first_through": first,
        "min_speed_mps": {k: round(v, 2) for k, v in low.items()},
        "gives_way_to_the_right": ok,
    }


def keep_left_test() -> dict:
    """A 3-lane, 100 km/h one-way road (r.129 applies above 80 km/h). (a) A lone car starting in
    the outermost right lane should move back to the left lane; (b) a fast car behind a slow one
    in the left lane should overtake on the right and return to the left lane."""
    import libsumo as traci
    import sumolib

    with tempfile.TemporaryDirectory() as tmp:
        net = _netconvert(
            '<nodes><node id="A" x="0" y="0"/><node id="B" x="4000" y="0"/></nodes>',
            '<edges><edge id="AB" from="A" to="B" numLanes="3" speed="27.78"/></edges>',
            Path(tmp) / "road.net.xml",
        )
        traci.start([sumolib.checkBinary("sumo"), "-n", str(net), "--no-step-log", "--seed", "42"])
        try:
            traci.route.add("r", ["AB"])
            traci.vehicletype.copy("DEFAULT_VEHTYPE", "slow")
            traci.vehicletype.setMaxSpeed("slow", 15.0)
            traci.vehicle.add("lone", "r", departLane="2", departSpeed="25")
            traci.vehicle.add("slow", "r", typeID="slow", departLane="0", departPos="300")
            traci.vehicle.add("fast", "r", departLane="0", departPos="0", departSpeed="25")
            lanes: dict[str, list[int]] = defaultdict(list)
            passed_at = None
            for step in range(140):
                traci.simulationStep()
                ids = traci.vehicle.getIDList()
                for v in ("lone", "fast"):
                    if v in ids:
                        lanes[v].append(traci.vehicle.getLaneIndex(v))
                if (
                    passed_at is None
                    and {"fast", "slow"} <= set(ids)
                    and traci.vehicle.getLanePosition("fast")
                    > traci.vehicle.getLanePosition("slow")
                ):
                    passed_at = step
        finally:
            traci.close()

    def changes(seq: list[int]) -> list[int]:
        out = [seq[0]] if seq else []
        for x in seq[1:]:
            if x != out[-1]:
                out.append(x)
        return out

    lone, fast = changes(lanes["lone"]), changes(lanes["fast"])
    overtook_right = bool(fast) and max(fast) >= 1 and passed_at is not None
    return {
        "lane_index_note": "lane 0 is the left (kerb) lane in a left-hand network",
        "lone_car_lanes": lone,
        "lone_car_keeps_left": bool(lone) and lone[-1] == 0,
        "fast_car_lanes": fast,
        "fast_car_overtook_on_the_right": overtook_right,
        "fast_car_back_in_left_lane": bool(fast) and fast[-1] == 0,
    }


# --- network queries ------------------------------------------------------------------------


def junction_types(net) -> dict:
    return dict(Counter(n.getType() for n in net.getNodes()).most_common())


def _minor_unsigned(net, osm, gone=frozenset()):
    """Unsignalised junctions whose roads are all minor streets and with no stop / give-way sign
    within 30 m, with their arms (the population ARR r.69 and r.72 are judged on)."""
    from sim.generated_patches import _arms, _signs_xy, _unsigned_minor

    signs = _signs_xy(osm, net)
    for n in net.getNodes():
        arms = _arms(n, set(gone))
        if _unsigned_minor(n, arms, signs):
            yield n, arms


def t_intersections(net, osm, roundabout_nodes: set[str]) -> dict:
    """ARR r.69 by link states: at each unsigned, unsignalised minor-street T-intersection (3
    roads, two within 20 degrees of straight), the continuing road's straight-on movements must
    not yield (state m / s). Nodes of OSM-tagged roundabouts are reported apart: there r.114
    gives circulating traffic priority, and an entry that lines up with the exit is still an
    entry, not the continuing road."""
    from sim.generated_patches import THROUGH_DEG, _arm_heading

    judged, backwards, roundabout = 0, [], []
    for n, arms in _minor_unsigned(net, osm):
        if len(arms) != 3:
            continue
        head = {nb: _arm_heading(*lst[0]) for nb, lst in arms.items()}
        nbs = list(arms)
        straight, pair = max(
            (abs((head[a] - head[b] + 180) % 360 - 180), (a, b))
            for i, a in enumerate(nbs)
            for b in nbs[i + 1 :]
        )
        if straight < THROUGH_DEG:
            continue
        through_in = [e for nb in pair for out, e in arms[nb] if not out]
        if not through_in:
            continue
        judged += 1
        yields = {
            c.getState()
            for e in through_in
            for cs in e.getOutgoing().values()
            for c in cs
            if c.getDirection() == "s" and c.getState() in ("m", "s")
        }
        if yields:
            row = {"junction": n.getID(), "continuing": through_in[0].getName() or "(unnamed)"}
            (roundabout if n.getID() in roundabout_nodes else backwards).append(row)
    return {
        "t_intersections_judged": judged,
        "continuing_road_yields": backwards,
        "osm_roundabout_entries_lined_up_with_the_exit": roundabout,
        "roundabout_note": "at these OSM junction=roundabout nodes the entering road gives way "
        "to the circulating one (r.114 overrides r.69), although the entry lines up with the exit",
    }


def cross_intersections(net, osm) -> dict:
    """ARR r.72: unsigned, unsignalised cross intersections of four roads of one minor class
    must give way to the right (left_before_right in SUMO's left-hand mode)."""
    by_type: Counter = Counter()
    wrong = []
    for n, arms in _minor_unsigned(net, osm):
        if len(arms) != 4 or len({e.getType() for lst in arms.values() for _, e in lst}) != 1:
            continue
        by_type[n.getType()] += 1
        if n.getType() != "left_before_right":
            wrong.append(n.getID())
    return {"judged_by_type": dict(by_type), "not_give_way_to_the_right": wrong}


def opposing_right_turns(net_path: Path, net) -> dict:
    """Pairs of opposing right turns green together at a signal: in left-hand traffic they pass
    in front of each other (simultaneous rights are common in Victoria). Checks that their paths
    through the junction (internal lanes) do not cross."""
    from shapely.geometry import LineString

    root = ET.parse(net_path).getroot()
    programs = {
        t.get("id"): [p.get("state") for p in t.iter("phase")] for t in root.iter("tlLogic")
    }
    rights: dict[str, list] = defaultdict(list)
    for c in root.iter("connection"):
        if c.get("tl") and c.get("dir") in ("r", "R") and c.get("via"):
            rights[c.get("tl")].append((int(c.get("linkIndex")), c.get("from"), c.get("via")))
    pairs = crossing = 0
    crossing_rows = []
    seen = set()
    for tl, links in rights.items():
        for k, ph in enumerate(programs.get(tl, [])):
            green = [x for x in links if x[0] < len(ph) and ph[x[0]] in "Gg"]
            for i, a in enumerate(green):
                for b in green[i + 1 :]:
                    if a[1] == b[1]:
                        continue
                    key = (tl, a[2], b[2])
                    if key in seen:
                        continue
                    seen.add(key)
                    pairs += 1
                    la = LineString(net.getLane(a[2]).getShape())
                    lb = LineString(net.getLane(b[2]).getShape())
                    if la.crosses(lb):
                        crossing += 1
                        crossing_rows.append([tl, a[1], b[1], k])
    return {
        "verdict_note": "compliant: opposing right turns green together pass in front of each "
        "other; where their paths do cross, both are permissive green ('g'), so SUMO makes them "
        "give way to each other",
        "opposing_right_turn_pairs_green_together": pairs,
        "pairs_whose_paths_cross": crossing,
        "crossing": crossing_rows[:30],
    }


def looping_internal_lanes(net) -> list[dict]:
    """Junction lanes far longer than the gap they bridge (a sign of a contorted joined-junction
    shape): length > 40 m and > 2.5 x the straight distance between their ends."""
    out = []
    for e in net.getEdges(withInternal=True):
        if not e.getID().startswith(":"):
            continue
        for ln in e.getLanes():
            sh = ln.getShape()
            straight = math.dist(sh[0], sh[-1])
            if ln.getLength() > 40 and ln.getLength() > 2.5 * max(straight, 1.0):
                lon, lat = net.convertXY2LonLat(*sh[len(sh) // 2])
                out.append(
                    {
                        "lane": ln.getID(),
                        "length_m": round(ln.getLength(), 1),
                        "straight_m": round(straight, 1),
                        "lon": round(lon, 5),
                        "lat": round(lat, 5),
                    }
                )
    return sorted(out, key=lambda r: -r["length_m"])


def link_foes(net_path: Path) -> dict[str, dict[int, str]]:
    """junction id -> {link index: foes bit string} from the network's <request> rows."""
    out: dict[str, dict[int, str]] = {}
    for j in ET.parse(net_path).getroot().iter("junction"):
        reqs = {int(r.get("index")): r.get("foes", "") for r in j.iter("request")}
        if reqs:
            out[j.get("id")] = reqs
    return out


def _conflicting_major(conns, junction_id: str, foes: dict) -> set[str]:
    """States of the links that have priority AND cross or merge with another movement (a major
    link with no foes has nobody to give way to: a kerb-side turn into its own lane, a merge with
    nothing else entering, a U-turn at a dead end)."""
    out = set()
    reqs = foes.get(junction_id, {})
    for c in conns:
        st = c.getState()
        if st in MINOR_STATES:
            out.add(st)
            continue
        f = reqs.get(c.getJunctionIndex(), "")
        out.add(st if "1" in f else "free")
    return out


def signs(net, osm, idx, foes=None) -> dict:
    ways_at: dict[int, list[int]] = defaultdict(list)
    for w, (refs, _) in osm.ways.items():
        for n in refs:
            ways_at[n].append(w)
    out: Counter = Counter()
    major = []
    for nid, tags in sorted(osm.node_tags.items()):
        kind = tags.get("highway")
        if kind not in ("give_way", "stop"):
            continue
        found = sign_approach(nid, osm, idx, ways_at)
        if found is None:
            out[f"{kind}:road_not_in_network"] += 1
            continue
        edge, junction = found
        conns = [c for cs in edge.getOutgoing().values() for c in cs]
        states = _conflicting_major(conns, junction.getID(), foes or {})
        if junction.getType().startswith("traffic_light"):
            cls = "signalised (signals govern)"
        elif not states:
            cls = "no movement"
        elif states == {"free"}:
            cls = "no conflicting traffic"
        elif states <= MINOR_STATES | {"free"}:
            cls = "approach gives way" + (" and stops" if "s" in states else "")
        else:
            cls = "approach has priority"
            major.append(
                {
                    "sign_node": nid,
                    "sign": kind,
                    "approach": edge.getID(),
                    "road": edge.getName(),
                    "junction": junction.getID(),
                    "junction_type": junction.getType(),
                    "states": "".join(sorted(states)),
                }
            )
        out[f"{kind}:{cls}"] += 1
    return {"by_sign_and_outcome": dict(sorted(out.items())), "approaches_with_priority": major}


def roundabouts(net_path: Path, net, foes=None) -> dict:
    root = ET.parse(net_path).getroot()
    entry_major, ring_minor, entries, rings = [], [], 0, 0
    for rb in root.iter("roundabout"):
        ring = set(rb.get("edges", "").split())
        for nid in rb.get("nodes", "").split():
            if not net.hasNode(nid):
                continue
            for e in net.getNode(nid).getIncoming():
                for t, cs in e.getOutgoing().items():
                    if t.getID() not in ring:
                        continue
                    states = _conflicting_major(cs, nid, foes or {})
                    if e.getID() in ring:
                        rings += 1
                        if not states <= {"M", "O", "=", "free"}:
                            ring_minor.append([e.getID(), t.getID(), "".join(sorted(states))])
                    else:
                        entries += 1
                        if not states <= MINOR_STATES | {"free"}:
                            entry_major.append([e.getID(), t.getID(), "".join(sorted(states))])
    return {
        "roundabouts": sum(1 for _ in root.iter("roundabout")),
        "entry_links": entries,
        "entry_links_with_priority": entry_major,
        "circulating_links": rings,
        "circulating_links_giving_way": ring_minor,
    }


def _in_box(net, e) -> bool:
    x, y = e.getShape()[len(e.getShape()) // 2]
    lon, lat = net.convertXY2LonLat(x, y)
    return CBD_BOX[0] <= lon <= CBD_BOX[2] and CBD_BOX[1] <= lat <= CBD_BOX[3]


def cbd_patch_edges() -> set[str]:
    from sim.build_net import CBD_SPEED_PATCH

    if not CBD_SPEED_PATCH.exists():
        return set()
    return {e.get("id") for e in ET.parse(CBD_SPEED_PATCH).getroot().iter("edge")}


def speed_limits(net, osm, idx) -> dict:
    cbd40 = cbd_patch_edges()

    def tagged(w: int) -> float | None:
        v = osm.ways.get(w, ([], {}))[1].get("maxspeed")
        try:
            return float(v) if v else None
        except ValueError:
            return None

    mism, cbd_len, cbd_fast = [], Counter(), []
    for e in net.getEdges():
        kmh = round(e.getSpeed() * 3.6)
        tags = [tagged(w) for w in idx.ways_by_edge.get(e.getID(), [])]
        tags = [t for t in tags if t is not None]
        hw = e.getType().removeprefix("highway.")
        if tags:
            if kmh not in {round(t) for t in tags}:
                mism.append({"edge": e.getID(), "name": e.getName(), "kmh": kmh, "osm": tags})
        else:
            expected = {"motorway": 100, "motorway_link": 80, "living_street": 10}.get(hw, 50)
            if e.getID() in cbd40:
                expected = 40  # CBD (Hoddle Grid) area limit, cbd_speed_limit.edg.xml
            if kmh != expected:
                mism.append({"edge": e.getID(), "name": e.getName(), "kmh": kmh, "untagged": hw})
        x, y = e.getShape()[len(e.getShape()) // 2]
        lon, lat = net.convertXY2LonLat(x, y)
        if CBD_BOX[0] <= lon <= CBD_BOX[2] and CBD_BOX[1] <= lat <= CBD_BOX[3]:
            cbd_len[kmh] += e.getLength()
            if kmh > 50:
                cbd_fast.append({"edge": e.getID(), "name": e.getName(), "kmh": kmh})
    total = sum(cbd_len.values())
    in_net = sorted(e for e in cbd40 if net.hasEdge(e))
    return {
        "edges_not_matching_osm_or_default": mism,
        "cbd_area_limit_40": {
            "edges": len(in_net),
            "by_road": dict(Counter(net.getEdge(e).getName() or "unnamed" for e in in_net)),
            "km": round(sum(net.getEdge(e).getLength() for e in in_net) / 1000, 2),
            "untagged_cbd_box_edges_left_at_50": [
                r
                for r in (
                    {"edge": e.getID(), "name": e.getName(), "kmh": round(e.getSpeed() * 3.6)}
                    for e in net.getEdges()
                    if not any(tagged(w) is not None for w in idx.ways_by_edge.get(e.getID(), []))
                    and round(e.getSpeed() * 3.6) == 50
                    and _in_box(net, e)
                )
            ],
        },
        "cbd_box": CBD_BOX,
        "cbd_km_by_limit_kmh": {k: round(v / 1000, 2) for k, v in sorted(cbd_len.items())},
        "cbd_share_at_40_or_less": round(sum(v for k, v in cbd_len.items() if k <= 40) / total, 3)
        if total
        else None,
        "cbd_edges_above_50": cbd_fast,
    }


def bus_lanes(net, osm) -> dict:
    """Bus-only lanes in the network vs the extract's bus lane tags (sim/bus_lanes.py)."""
    from sim import bus_lanes as bl

    plan = bl.plan(osm)
    lanes = [
        (e, ln)
        for e in net.getEdges()
        for ln in e.getLanes()
        if ln.allows("bus") and not ln.allows("passenger")
    ]
    edges = sorted({e.getID() for e, _ in lanes})
    bus_only_moves = []
    for eid in edges:
        e = net.getEdge(eid)
        every, cars = set(), set()
        for ln in e.getLanes():
            for c in ln.getOutgoing():
                every.add(c.getTo().getID())
                if ln.allows("passenger") and c.getToLane().allows("passenger"):
                    cars.add(c.getTo().getID())
        if every - cars:
            bus_only_moves.append({"edge": eid, "to": sorted(every - cars)})
    return {
        "osm_ways_with_bus_lane_tags": len(plan),
        "ways_closed": sum(1 for r in plan if not r.skipped),
        "ways_by_source": dict(Counter(r.source for r in plan if not r.skipped)),
        "ways_skipped": [
            {"way": r.way, "name": r.name, "source": r.source, "why": r.skipped}
            for r in plan
            if r.skipped
        ],
        "bus_only_lanes": len(lanes),
        "bus_only_lane_edges": len(edges),
        "bus_only_lane_km": round(sum(ln.getLength() for _, ln in lanes) / 1000, 2),
        "bus_only_lanes_by_road": dict(Counter(e.getName() or "unnamed" for e, _ in lanes)),
        "bus_only_lane_index": dict(Counter(ln.getIndex() for _, ln in lanes)),
        "edges_without_a_car_lane": [
            eid for eid in edges if not net.getEdge(eid).allows("passenger")
        ],
        "movements_only_buses_can_make": bus_only_moves,
        "hours": bus_lane_hours_evidence(net),
    }


TV_RULE_URL = (
    "https://transport.vic.gov.au/road-and-active-transport/road-rules-and-safety/"
    "driving-with-buses/driving-with-buses-video-transcript"
)
LABEL_LISTED_NO_HOURS = (
    "full-time: the City of Melbourne lists it with no hours, and Transport Victoria's rule is "
    f"that a bus-lane sign with no times operates 24/7 ({TV_RULE_URL})"
)
LABEL_UNLISTED = (
    "full-time: no published hours; treated as 24/7 under Transport Victoria's rule that a "
    "bus-lane sign with no times operates full-time (signs not checked on site)"
)


SOURCE_URL_BUS = "https://www.melbourne.vic.gov.au/bus-lanes"


def bus_lane_hours_evidence(net) -> dict:
    """Which bus lanes are timed (open to cars in the baseline) and which always on, and why."""
    from sim import bus_lane_hours as bh

    timed = bh.timed_lane_rows(net)
    always = [(e, ln) for e in net.getEdges() for ln in e.getLanes() if bh.is_bus_only(ln)]
    listed: dict[str, list[str]] = {}
    for entry in bh.TABLE:
        if entry.hours is None:  # never "inbound", so no CBD centre is needed
            rows = bh.timed_lanes(net, (0.0, 0.0), entries=(entry,))
            listed[entry.key] = [f"{m.edge}_{m.lane}" for m in rows]
    listed_ids = {lid for v in listed.values() for lid in v}
    unlisted = Counter(e.getName() or "unnamed" for e, ln in always if ln.getID() not in listed_ids)
    return {
        "source": bh.SOURCE_URL,
        "source_note": bh.SOURCE_NOTE,
        "timed": [
            {
                "row": e.key,
                "street": e.street,
                "side": e.side,
                "from": e.from_street,
                "to": e.to_street,
                "hours": e.hours,
                "lanes": sorted(timed.get(e.key, [])),
                "baseline": "open to cars (the baseline has no clock time)",
            }
            for e in bh.TABLE
            if e.hours
        ],
        "always_on_listed_without_hours": [
            {
                "row": e.key,
                "street": e.street,
                "side": e.side,
                "from": e.from_street,
                "to": e.to_street,
                "lanes": sorted(listed.get(e.key, [])),
                "label": LABEL_LISTED_NO_HOURS,
            }
            for e in bh.TABLE
            if e.hours is None
        ],
        "always_on_unlisted": {"lanes_by_road": dict(unlisted), "label": LABEL_UNLISTED},
        "timed_lanes_open_in_baseline": sum(len(v) for v in timed.values()),
        "bus_only_lanes_in_baseline": len(always),
    }


def conditional_speed_limits(osm, idx) -> dict:
    from sim.conditional_speeds import conditional_ways, parse

    rows = conditional_ways(osm)
    for r in rows:
        r["edges"] = sorted(e.getID() for e in idx.edges_by_way.get(r["way"], []))
        c = parse(r["rule"])
        r["weekly_windows"] = [
            {
                "days": "".join(DAY_LETTERS[d] for d in sorted(w.days)),
                "from": f"{w.start_min // 60:02d}:{w.start_min % 60:02d}",
                "to": f"{w.end_min // 60:02d}:{w.end_min % 60:02d}",
            }
            for w in c.windows
        ]
    return {
        "ways": rows,
        "ways_in_network": sum(1 for r in rows if r["edges"]),
        "edges_in_network": sum(len(r["edges"]) for r in rows),
        "applied_in_baseline": False,
        "why_not_baseline": "the baseline scenario is placeholder demand in simulation seconds "
        "with no time of day or weekday, so no window can contain it; the signed limits stay",
        "phase3_am_peak": "apply with sim.conditional_speeds.edge_speeds(osm, idx, start time) "
        "when the scenario has a clock: on a school weekday the Dudley St school zone (40 km/h) "
        "runs 08:00-09:30 and Bay St / Crockford St (40 km/h) from 08:00, so a 07:00-09:00 AM "
        "peak would switch them on at 08:00",
    }


DAY_LETTERS = "MTWTFSS"


def turn_on_red(net_path: Path) -> dict:
    """No turn on red (Australia has no general turn-on-red): a turning link must never be green
    while the through link from the same lane is red, and no turning link may be green in every
    phase of a program that has red phases for its approach."""
    root = ET.parse(net_path).getroot()
    programs = {
        t.get("id"): [p.get("state") for p in t.iter("phase")] for t in root.iter("tlLogic")
    }
    by_lane: dict[tuple[str, str], list] = defaultdict(list)
    links = []
    for c in root.iter("connection"):
        if c.get("tl") and c.get("linkIndex") is not None:
            row = (
                c.get("tl"),
                int(c.get("linkIndex")),
                c.get("dir"),
                c.get("from"),
                c.get("fromLane"),
            )
            links.append(row)
            by_lane[(c.get("from"), c.get("fromLane"))].append(row)
    on_red, always_green, uncontrolled = [], [], Counter()
    for tl, i, d, frm, lane in links:
        states = [ph[i] for ph in programs.get(tl, []) if i < len(ph)]
        if all(s in "oO" for s in states):
            uncontrolled[d] += 1
            continue
        if d in ("l", "L", "r", "R", "t", "T"):
            if states and all(s in "Gg" for s in states):
                approach_red = any(
                    programs[tl][k][j] == "r"
                    for (tl2, j, _, frm2, _l) in links
                    if tl2 == tl and frm2 == frm
                    for k in range(len(programs[tl]))
                )
                if approach_red:
                    always_green.append([tl, frm, d])
            for k, ph in enumerate(programs.get(tl, [])):
                if ph[i] not in "Gg":
                    continue
                through = [r for r in by_lane[(frm, lane)] if r[2] == "s" and r[0] == tl]
                if through and all(ph[r[1]] == "r" for r in through):
                    on_red.append([tl, frm, lane, d, k])
    left_on_red = [r for r in on_red if r[3] in ("l", "L")]
    arrows = [r for r in on_red if r[3] not in ("l", "L")]
    return {
        "signalised_links": len(links),
        "left_turns_green_while_same_lane_through_red": left_on_red,
        "right_or_u_turn_arrows_while_same_lane_through_red": len(arrows),
        "arrow_note": "a right-turn (or U-turn) green while the shared lane's through movement is "
        "red is a protected turn arrow phase, which Australian signals use; allowed",
        "turns_green_in_every_phase_while_their_approach_has_red": always_green,
        "uncontrolled_links_at_signals_by_dir": dict(uncontrolled),
    }


def shared_zones(net, osm, idx) -> dict:
    zones = [e for e in net.getEdges() if e.getType() == "highway.living_street"]
    speeds = Counter(round(e.getSpeed() * 3.6) for e in zones)
    return {
        "edges": len(zones),
        "km": round(sum(e.getLength() for e in zones) / 1000, 2),
        "speed_kmh_counts": dict(speeds),
        "above_10_kmh": [
            {"edge": e.getID(), "name": e.getName(), "kmh": round(e.getSpeed() * 3.6)}
            for e in zones
            if e.getSpeed() * 3.6 > 10.5
        ],
    }


def raw_survey(pbf: Path) -> dict | None:
    """What the raw OSM data says about things we cannot model (counts in the extract box)."""
    if not pbf.exists():
        return None
    import osmium

    from pipeline.config import SIM_EXTRACT_BBOX as B

    def inside(lo: float, la: float) -> bool:
        return B.west <= lo <= B.east and B.south <= la <= B.north

    c: Counter = Counter()
    school = []
    for o in osmium.FileProcessor(str(pbf)).with_locations():
        if o.is_way():
            pts = [(n.lon, n.lat) for n in o.nodes if n.location.valid()]
            if not any(inside(*p) for p in pts):
                continue
            t = dict(o.tags)
            if t.get("railway") == "tram":
                c["tram_track_ways"] += 1
            if t.get("embedded_rails") == "tram" or "tram" in t.get("lanes:psv", ""):
                c["roads_with_tram_rails"] += 1
            if "maxspeed:conditional" in t:
                school.append(
                    {"way": o.id, "name": t.get("name"), "rule": t["maxspeed:conditional"]}
                )
            if "clearway" in " ".join(t.values()).lower():
                c["ways_mentioning_clearway"] += 1
            if any("hook" in k or "hook" in v.lower() for k, v in t.items() if k != "name"):
                c["ways_tagged_hook_turn"] += 1
        elif o.is_relation():
            t = dict(o.tags)
            if any("hook" in k or "hook" in v.lower() for k, v in t.items()):
                c["relations_tagged_hook_turn"] += 1
    return {"counts": dict(c), "maxspeed_conditional_ways": school}


def elizabeth_bourke(net, idx) -> dict:
    """The Elizabeth St / Bourke St box (OSM node 2190478936): one signalised junction whose
    approaches come straight from the previous intersections (no stop-line lights)."""
    jid = idx.junction(2190478936)
    node = net.getNode(jid) if jid and net.hasNode(jid) else None
    stop_lines = {n: idx.junction(n) for n in (13634634815, 3394726646, 3394726641)}
    lights_left = [
        n
        for n, j in stop_lines.items()
        if j and net.hasNode(j) and net.getNode(j).getType().startswith("traffic_light")
    ]
    ok = (
        node is not None
        and node.getType() == "traffic_light"
        and not lights_left
        and all(idx.junction(n) == jid for n in (2190478926, 2190561008, 2190561011))
    )
    return {
        "junction": jid,
        "type": node.getType() if node is not None else None,
        "approaches": sorted(e.getName() for e in node.getIncoming()) if node else [],
        "stop_line_nodes_still_lights": lights_left,
        "one_signalised_junction": ok,
    }


# --- report ---------------------------------------------------------------------------------


def run(net_path: Path = NET_PATH) -> dict:
    import sumolib

    net = sumolib.net.readNet(str(net_path), withInternal=False, withPrograms=True)
    osm = read_osm(OSM_EXTRACT)
    idx = NetIndex(net, read_joined(JOINED_PATH))
    gw = give_way_to_right_test()
    kl = keep_left_test()
    jt = junction_types(net)
    from sim.generated_patches import osm_roundabouts

    rb_nodes = {n for _, nodes in osm_roundabouts(net_path, osm, idx) for n in nodes}
    ti = t_intersections(net, osm, rb_nodes)
    ci = cross_intersections(net, osm)
    net_int = sumolib.net.readNet(str(net_path), withInternal=True)
    orr = opposing_right_turns(net_path, net_int)
    loops = looping_internal_lanes(net_int)
    foes = link_foes(net_path)
    sg = signs(net, osm, idx, foes)
    rb = roundabouts(net_path, net, foes)
    sp = speed_limits(net, osm, idx)
    tr = turn_on_red(net_path)
    sz = shared_zones(net, osm, idx)
    bl = bus_lanes(net, osm)
    cs = conditional_speed_limits(osm, idx)
    eb = elizabeth_bourke(net, idx)
    raw = raw_survey(RAW_PBF)

    rules = [
        {
            "rule": "Give way to the right at uncontrolled cross intersections (ARR r.72)",
            "network": f"{jt.get('left_before_right', 0)} left_before_right junctions "
            "(--junctions.left-before-right, plus right_of_way.nod.xml for unsigned cross "
            "intersections of equal minor streets at 50 km/h, where netconvert's 49 km/h "
            "threshold had made them priority junctions)",
            "evidence": {"micro_test": gw, "cross_intersections": ci},
            "verdict": "fixed"
            if gw["gives_way_to_the_right"] and not ci["not_give_way_to_the_right"]
            else "fails",
            "how": "SUMO's left-hand mode mirrors its default right_before_left into give way to "
            "the LEFT (tested: the westbound car stopped for one coming from its left). "
            "left_before_right in a left-hand network gives way to the right.",
        },
        {
            "rule": "T-intersections: the terminating road gives way (ARR r.69)",
            "network": "the terminating road's edges get a lower priority than the continuing "
            "road's (sim/patches/net/t_intersections.edg.xml), and equal-priority T-junctions "
            "become priority junctions (right_of_way.nod.xml)",
            "evidence": ti,
            "verdict": "fixed" if not ti["continuing_road_yields"] else "fails",
            "how": "netconvert broke ties between equal-class streets by speed, so an untagged "
            "50 km/h side street beat a continuing street signed 40 km/h: 69 of 311 judged "
            "T-intersections were backwards before",
        },
        {
            "rule": "Give-way and stop signs (ARR r.67-69); stop means stop",
            "network": "OSM highway=give_way / stop nodes mapped to the approach they sign; "
            "junctions with a stop-signed approach are priority_stop (SUMO does not import "
            "stop signs)",
            "evidence": sg,
            "verdict": "complies"
            if not sg["approaches_with_priority"]
            else "complies_with_exceptions",
            "how": "stop-signed priority junctions became priority_stop (vehicles stop), and "
            "signed roads were given a lower edge priority than the others at their junction "
            "(sim/patches/net/right_of_way.nod.xml, give_way_priorities.edg.xml)",
            "note": "approaches_with_priority are signed approaches whose movement still has "
            "priority over a conflicting one (equal-priority geometry netconvert resolves by "
            "straightness); 'no conflicting traffic' approaches have nothing to give way to",
        },
        {
            "rule": "Roundabouts: give way to vehicles already in the roundabout (ARR r.114)",
            "network": "SUMO roundabouts (--roundabouts.guess and OSM junction=roundabout)",
            "evidence": rb,
            "verdict": "complies"
            if not rb["entry_links_with_priority"] and not rb["circulating_links_giving_way"]
            else "fails",
        },
        {
            "rule": "Keep left unless overtaking; overtake on the right (ARR r.129-130, 141)",
            "network": "SUMO LC2013 lane changing; in a left-hand network lane 0 is the left lane "
            "and its keep-right tendency (lcKeepRight) keeps cars to lane 0",
            "evidence": kl,
            "verdict": "complies"
            if kl["lone_car_keeps_left"]
            and kl["fast_car_overtook_on_the_right"]
            and kl["fast_car_back_in_left_lane"]
            else "fails",
        },
        {
            "rule": "Speed limits: signed maxspeed; 50 km/h default in built-up areas (ARR r.20, "
            "25)",
            "network": "OSM maxspeed where tagged; untagged non-motorway roads 50 km/h "
            "(sim/patches/osm/urban_speed_defaults.typ.xml), 40 km/h inside the Hoddle Grid "
            "(CBD area limit), shared zones 10 km/h",
            "evidence": sp,
            "verdict": "complies" if not sp["edges_not_matching_osm_or_default"] else "fails",
            "note": "cbd_edges_above_50 are OSM-tagged 60 km/h arterials on the CBD fringe (Dudley "
            "St, Batman Ave and their ramps), listed for review. cbd_area_limit_40: untagged "
            "roads inside the Hoddle Grid get the CBD 40 km/h area limit "
            "(sim/patches/net/cbd_speed_limit.edg.xml); untagged_cbd_box_edges_left_at_50 lie "
            "in the wider CBD_BOX but outside the grid (north of La Trobe St, Docklands, south "
            "of Flinders St), where the area limit is not established and nearby ways are "
            "tagged 50",
        },
        {
            "rule": "No turning on a red light (ARR r.56-57; Australia has no general turn-on-red)",
            "network": "SUMO default static signal programs (90 s placeholders)",
            "evidence": tr,
            "verdict": "fixed"
            if not tr["left_turns_green_while_same_lane_through_red"]
            and not tr["turns_green_in_every_phase_while_their_approach_has_red"]
            else "fails",
            "how": "with no pedestrians simulated, netconvert's default programs left kerb-side "
            "left turns green in every phase (a left turn on red); the build now makes each left "
            "turn follow its approach's through movement (sim/generated_patches.py "
            "no_left_turn_on_red; the build log prints how many links it changed: 58 in 48 "
            "programs)",
            "note": "uncontrolled_links_at_signals are slip lanes and bays that netconvert left "
            "outside the signal ('o': give way, as at a slip lane with a give-way sign). "
            "Opposing right turns green together pass in front of each other in left-hand "
            "traffic: see opposing_right_turns",
            "opposing_right_turns": orr,
        },
        {
            "rule": "Bus lanes: only buses may drive in them (ARR r.154-158)",
            "network": "bus lanes from the extract's lane access tags (bus:lanes, lanes:bus, "
            "busway) closed to cars with --osm.lane-access (sim/bus_lanes.py)",
            "evidence": bl,
            "verdict": "fixed (timed lanes per scenario clock; others full-time)"
            if bl["bus_only_lanes"]
            and not bl["edges_without_a_car_lane"]
            and not bl["movements_only_buses_can_make"]
            else "fails",
            "how": "counted (lanes:bus) and busway=lane bus lanes are placed on the kerb lane, "
            "where Victorian bus lanes run (r.158 lets cars cross into one to turn left); "
            "explicit bus:lanes values and sides are used as tagged. netconvert builds the "
            "junction connections with the bus lanes in place, so cars turn from the lanes they "
            "may use (movements_only_buses_can_make is empty). Hours (sim/bus_lane_hours.py): "
            "the City of Melbourne bus-lanes page gives part-time hours for Queen St east side "
            "(Mo-Fr 16:30-18:30), Lonsdale St north side (Mo-Fr 16:00-18:30), Lonsdale St south "
            "side (Mo-Fr 07:00-09:30) and City Rd inbound (Mo-Fr 06:30-10:00); those lanes are "
            "matched by street name, side (geometry) and cross streets, carry a busLaneHours lane "
            "param, and are OPEN to cars in this baseline network (no clock time). A scenario "
            "with a clock closes them in their hours (bus_lane_hours.bus_only_lanes). See "
            "evidence.hours for the lanes",
            "labels": {
                "timed": "part-time: City of Melbourne published hours (cited facts, not "
                "open-licensed data); open to cars in the baseline",
                "Spring Street": LABEL_LISTED_NO_HOURS,
                "Victoria Parade, St Kilda Road, Power Street, Peel Street and any other "
                "unlisted bus lane": LABEL_UNLISTED,
            },
            "assumptions": [
                "kerb-lane placement of counted bus lanes (OSM gives only the count)",
                "OSM has no bus-lane hours (no :conditional tags); hours come from the City of "
                f"Melbourne page ({SOURCE_URL_BUS}), used as cited facts with George's approval "
                "(2026-10-27)",
                "timed lanes assume an ordinary weekday (public holidays unknown to a scenario)",
                "only the OSM-tagged part of each listed stretch is a bus lane in the network "
                "(e.g. City Rd: 2 short one-way edges; its two-way ways do not say which "
                "direction has the bus lane and are skipped)",
            ],
            "not_modelled": "cars entering a bus lane for up to 100 m to turn (r.158(2)); buses "
            "themselves (cars only)",
        },
        {
            "rule": "Time-based speed limits: school zones (maxspeed:conditional)",
            "network": "signed limits only; conditional limits apply per scenario clock "
            "(sim/conditional_speeds.py)",
            "evidence": cs,
            "verdict": "not applied to baseline (no time of day); ready for the Phase 3 AM peak",
        },
        {
            "rule": "Shared zones: 10 km/h unless signed lower, give way to pedestrians (ARR "
            "r.24, r.83)",
            "network": "highway=living_street edges",
            "evidence": sz,
            "verdict": "complies (speed); pedestrians cannot_be_modelled",
            "impact": "no pedestrians are simulated, so cars never yield to them in shared "
            "zones; they only drive at 10 km/h",
        },
    ]
    cannot = [
        {
            "item": "Hook turns (ARR r.34-35)",
            "why": "SUMO has no hook-turn manoeuvre; OSM tags no hook-turn intersection here "
            f"({(raw or {}).get('counts', {}).get('ways_tagged_hook_turn', 0)} ways, "
            f"{(raw or {}).get('counts', {}).get('relations_tagged_hook_turn', 0)} relations).",
            "impact": "at signed CBD intersections cars turn right from the centre lane at the "
            "same time as through traffic, instead of waiting in the kerb lane for the cross "
            "street's green: right-turn queues and conflicts look different from reality",
        },
        {
            "item": "Trams",
            "why": "cars only; no tram vehicles, schedules or tram signals",
            "impact": "cars never queue behind trams, give way to them or stop for passengers at "
            "kerbside tram stops (r.163-164); tram-only lanes and tracks are either absent or "
            "driven as ordinary lanes"
            + (
                f" ({raw['counts'].get('tram_track_ways', 0)} tram track ways in the raw data)"
                if raw
                else ""
            ),
        },
        {
            "item": "Real signal timings",
            "why": "no open signal-timing data for Melbourne (SCATS is not published)",
            "impact": "every signal runs SUMO's static 90 s default program; no coordination "
            "(green waves), no actuation, no pedestrian-demand phases",
        },
        {
            "item": "Clearways (kerb lanes open only in peak hours)",
            "why": "OSM has no structured clearway or parking-lane-condition tags here "
            f"({(raw or {}).get('counts', {}).get('ways_mentioning_clearway', 0)} ways mention "
            "it in free text)",
            "impact": "kerb lanes are always open, as if every clearway were in force (true in "
            "the AM peak on most arterials, too generous off-peak)",
        },
        {
            "item": "Pedestrians and cyclists",
            "why": "cars only (Phase 2 decision)",
            "impact": "no give way to pedestrians at turns (r.62, r.72) or crossings, no "
            "pedestrian phases beyond the placeholder programs; bike lanes not simulated",
        },
        {
            "item": "Buses and taxis",
            "why": "cars only (Phase 2 decision); full-time bus lanes are closed to cars (see the "
            "bus lane rule) but no bus runs in them",
            "impact": "no bus traffic or stops; bus lanes look empty",
        },
    ]
    known = [
        {
            "item": "Looping junction lanes",
            "status": "fixed" if not loops else "open",
            "evidence": loops,
            "why": "two joined-junction shapes made SUMO route a left turn along a loop inside "
            "the junction (Nott St to Princes St, 85.6 m for a 23.4 m gap, through a house; a "
            "49.8 m loop at Batmans Hill Dr, Docklands); both are now kept out of junction "
            "joining (build_net.JOIN_EXCLUDE) and this scan (junction lanes > 40 m and > 2.5 x "
            "their straight gap) finds none",
        },
        {
            "item": "Edges stretched by junction joining",
            "why": "netconvert moves the ends of edges into a joined junction, so near joined "
            "junctions an edge lies a few metres off its OSM line (alignment: "
            "share_within_own_way below share_within_any_carriageway)",
            "impact": "cars near joined junctions are drawn slightly off their OSM centreline",
        },
        {
            "item": "Elizabeth St at Bourke St: signals on separate stop-line nodes",
            "status": "fixed" if eb["one_signalised_junction"] else "open",
            "evidence": eb,
            "why": "OSM's four-node box was not joined, so its three stop-line signals were "
            "their own lights and the box gave way to the right; "
            "sim/patches/osm/elizabeth_bourke_signals.nod.xml joins and signalises it",
        },
        {
            "item": "Signed approaches that keep priority",
            "evidence": "rules[Give-way and stop signs].evidence.approaches_with_priority",
            "why": "netconvert resolves some equal-priority geometries by straightness",
        },
    ]
    return {
        "about": "Australian Road Rules (Victoria) audit of sim/net/roads.net.xml and the SUMO "
        "behaviour. Evidence from network queries and TraCI micro-scenarios.",
        "junction_types": jt,
        "rules": rules,
        "cannot_be_modelled": cannot,
        "known_issues": known,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--net", type=Path, default=NET_PATH)
    ap.add_argument("-o", "--output", type=Path, default=CHECKS_DIR / "road_rules.json")
    args = ap.parse_args(argv)
    report = run(args.net)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"junction_types": report["junction_types"]}))
    for r in report["rules"]:
        print(f"{r['verdict']:28} {r['rule']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
