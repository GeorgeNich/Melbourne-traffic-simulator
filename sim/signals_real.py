"""Real signal programs from the DTP signal configuration sheets -> sim/net/signals_real.add.xml.

Usage (from the repo root):
    uv run python -m sim.signals_real    # reads pipeline/reference/signal_sheets_verified.json
    uv run python -m sim.signals_real --sheets X.json

Input: the verified sheet file (one object per SCATS site, schema in the module tests): phase
sequence, per-phase min / max green, yellow and all-red, the signal groups of each phase and,
where verified, the movements (street, heading, turn) of each vehicle signal group. Only field
groups whose `provenance` is in ACCEPTED are used; the raw AI reading is never an input.

Each site is matched to its SUMO traffic light with the Phase 3A count mapping
(pipeline.count_mapping: nearest traffic-light node within 60 m of the register coordinate).
Every traffic light then gets one tier:

    real_phases   the sheet's phase sequence: each phase greens exactly the links of its vehicle
                  signal groups; the sheet's yellow and all-red between phases; min / max green
                  from the sheet (type="actuated"). Needs every car link of the light covered by
                  some phase and no two crossing through movements green together.
    real_timings  netconvert's phase plan kept, run on the sheet's cycle: the sheet's yellow and
                  all-red after each netconvert yellow, and the sheet's max cycle split over
                  netconvert's green phases in their default proportions (actuated; see
                  timings_only_program). Mid-block pedestrian crossings get a
                  vehicle green of at least the sheet's max green (extending up to
                  POS_VEHICLE_GREEN_CAP_S while traffic keeps coming), then yellow, all-red and
                  the sheet's walk + clearance for pedestrians.
    default       no usable sheet data: netconvert's program stays (nothing written here).

The file holds programs with programID "real", loaded after the network so they are active.
The per-light tier and the reason are logged to sim/out/checks/signals_real.json. Timings live
in the sheet file as data, so a later phase can edit them and regenerate this file.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from dataclasses import dataclass, field
from pathlib import Path

from sim.config import CHECKS_DIR, NET_DIR, NET_PATH, REPO_ROOT

SHEETS_PATH = REPO_ROOT / "pipeline" / "reference" / "signal_sheets_verified.json"
SITES_CSV = REPO_ROOT / "data" / "raw" / "scats" / "victorian_traffic_signals.csv"
ADD_PATH = NET_DIR / "signals_real.add.xml"
LOG_PATH = CHECKS_DIR / "signals_real.json"
PROGRAM_ID = "real"

ACCEPTED = {"ai_checked", "re_read"}
# What the sheets' MAXIMUM EXTENSION GREEN (max_green_s in the verified file) means, from the
# text-table (htm) sheets in data/raw/scats/sheets: the CONTROLLER TIMESETTINGS table lists
# MINIMUM GREEN, MAXIMUM INITIAL GREEN and MAXIMUM EXTENSION GREEN per phase; its legend says only
# "A maximum extension green is used only in VA mode" (some sheets: "Isolated mode") and, for
# special signal groups, "SGn is forced off after this maximum extension green time". Whether
# the time counts from the start of green or after the minimum is not stated, so the maximum
# green is taken as min + extension (fixed phases show 0).
MAX_GREEN_NOTE = (
    "unclear on the sheets (legend: 'used only in VA/Isolated mode'; 'forced off after this "
    "maximum extension green time'); the two readings are max green = min green + extension "
    "(MAX_GREEN_TOTAL False) and extension = the total max green (True); both were run on "
    "attempt 3a's routes and the better reading kept (sim/work/am_peak/ablate*/report.json)"
)
# Reading B (total) kept: on attempt 3a's routes it beat reading A on every window
# (gate GEH<5 share 0.575 vs 0.540, teleports 5,040 vs 5,550; sim/work/am_peak/
# ablate4_demotion_fix vs ablate5_maxgreen_total), and the legend's "forced off after this
# maximum extension green time" reads as a total. --max-green-min-plus restores reading A.
MAX_GREEN_TOTAL = True
MID_BLOCK_TYPES = {"POS", "FLASH PX"}  # register TYPE of pedestrian-operated signals
# Split carriageways (e.g. Spencer St): the register point sits by one carriageway's light and
# the twin carriageway crosses at its own SUMO light just past NEAR_NODE_M (35 m), so the site
# saw only one direction. Signal nodes within this radius are pulled into the site.
MID_BLOCK_PAIR_M = 55.0
# Hook-turn intersections: the right turn waits at the far left and completes on the cross
# street's green, so the sheet program rightly gives it no protected green. The demotion rule
# must not compare those rights against netconvert's protected default. Membership needs a
# verified sign: 4599 Queen/Bourke has R2-21 "right turn from left lane" (City of Melbourne
# Sign Panels open data, CC BY, asset 1633286; data/SOURCES.md). Hook turns themselves are not
# modelled (known issue): the right stays a yield link.
HOOK_TURN_SITES = {"4599"}
# Mid-block crossings: no pedestrians are simulated, so the vehicle green cannot be ended by a
# push-button call. It runs at least the sheet's max green and extends while traffic keeps
# coming, up to this cap (an assumption standing in for pedestrian demand, not sheet data).
POS_VEHICLE_GREEN_CAP_S = 90.0

TURN_OF_DIR = {"s": "T", "l": "L", "L": "L", "r": "R", "R": "R", "t": "U", "T": "U"}
HEADING_WORDS = {
    "northbound": "NB",
    "southbound": "SB",
    "eastbound": "EB",
    "westbound": "WB",
    "north-eastbound": "NE",
    "northeastbound": "NE",
    "north-westbound": "NW",
    "northwestbound": "NW",
    "south-eastbound": "SE",
    "southeastbound": "SE",
    "south-westbound": "SW",
    "southwestbound": "SW",
}


# --- sheet reading -------------------------------------------------------------------------------


def provenance(site: dict, group: str) -> str | None:
    p = (site.get("provenance") or {}).get(group)
    return p.get("status") if isinstance(p, dict) else p


def usable(site: dict, group: str) -> bool:
    """Whether a field group of a site may be used (its provenance is checked or re-read).
    Groups: timings, phases (sequence + signal groups per phase), signal_groups (movements),
    pedestrians, lane_layout. Timings and phases are both stored under `phases`."""
    value = site.get("phases" if group == "timings" else group)
    if value is None:
        return False
    status = provenance(site, group)
    if group == "timings" and status == "ai_unverified":
        # unverified AI timings are accepted for non-priority sites (no verified movements)
        return provenance(site, "signal_groups") not in ACCEPTED
    return status in ACCEPTED


def heading_code(h: str) -> str:
    h = h.strip()
    return HEADING_WORDS.get(h.lower(), h.upper())


@dataclass
class PhaseTiming:
    min_green: float
    max_green: float
    yellow: float
    all_red: float
    groups: list[str] = field(default_factory=list)


def phase_timings(site: dict) -> tuple[list[str], dict[str, PhaseTiming]] | None:
    """(sequence of phase letters, timings per letter), or None if the timings or the phase
    sequence are not usable. `max_green_s` holds the sheet's MAXIMUM EXTENSION GREEN, the green
    a phase may run beyond its minimum (it is 0 on phases with a fixed green), so a phase's
    maximum green is min + extension."""
    if not (usable(site, "timings") and usable(site, "phases")):
        return None
    ph = site["phases"]
    seq = [c for c in ph.get("sequence") or "" if c.isalpha()]
    by = ph.get("by_phase") or {}
    out = {}
    for letter in seq:
        p = by.get(letter)
        if not p or p.get("min_green_s") is None or p.get("yellow_s") is None:
            return None
        mn = float(p["min_green_s"])
        ext = float(p.get("max_green_s") or 0.0)
        mx = max(mn, ext) if MAX_GREEN_TOTAL else mn + ext
        out[letter] = PhaseTiming(
            mn, mx, float(p["yellow_s"]), float(p.get("all_red_s") or 0.0), p.get("signal_groups")
        )
    return (seq, out) if seq else None


def phase_pedestrians(site: dict, phase: str) -> set[str]:
    """Pedestrian movements (keys of `pedestrians`) that run in a phase: its groups that are
    pedestrian keys themselves ("P1") or pedestrian signal groups pointing at one."""
    by = ((site.get("phases") or {}).get("by_phase") or {}).get(phase) or {}
    groups = site.get("signal_groups") or {}
    out = set()
    for g in by.get("signal_groups") or []:
        sg = groups.get(str(g)) or {}
        if sg.get("kind") == "pedestrian" and sg.get("ped"):
            out.add(sg["ped"])
        elif str(g) in (site.get("pedestrians") or {}):
            out.add(str(g))
    return out


def pedestrian_time(site: dict, phase: str | None = None) -> float | None:
    """Walk + clearance (s): the longest over the site's pedestrian movements (in `phase`)."""
    if not usable(site, "pedestrians"):
        return None
    best = None
    only = phase_pedestrians(site, phase) if phase is not None else None
    for key, p in (site["pedestrians"] or {}).items():
        if not isinstance(p, dict) or (only is not None and key not in only):
            continue
        walk = p.get("walk_s", p.get("walk"))
        if walk is None:
            continue
        clear = sum(
            float(p.get(k) or 0)
            for k in ("clearance_s", "clearance_1_s", "clearance_2_s", "clearance_1", "clearance_2")
        )
        t = float(walk) + clear
        best = t if best is None else max(best, t)
    return best


def car_phases(site: dict, seq: list[str], timings: dict[str, PhaseTiming]) -> list[str]:
    """The phases of the sequence that can carry cars (the simulation is cars only): phases whose
    signal groups are all pedestrian, tram or bicycle, or that have none, are dropped. A group is
    a car group if its verified kind is "vehicle"; without verified kinds, any group that is not a
    pedestrian id ("P1"). A phase with unknown groups (not read) is kept."""
    kinds = site.get("signal_groups") if usable(site, "signal_groups") else None

    def is_car(g: str) -> bool:
        if kinds is not None and str(g) in kinds:
            return (kinds[str(g)] or {}).get("kind") == "vehicle"
        return not str(g).upper().startswith("P")

    return [
        p for p in seq if timings[p].groups is None or any(is_car(g) for g in timings[p].groups)
    ]


# --- network side --------------------------------------------------------------------------------


@dataclass
class Link:
    index: int
    from_edge: str
    to_edge: str
    turn: str  # L / T / R / U
    conn: object  # sumolib connection
    cars: bool
    dir: str = ""  # SUMO's direction letter (s, l, L, r, R, t)
    street: str = ""  # OSM name of the approach edge


def tls_links(net, tls) -> list[Link]:
    """The light's links in link-index order."""
    out = {}
    for in_lane, out_lane, idx in tls.getConnections():
        conn = next(c for c in in_lane.getOutgoing() if c.getToLane() == out_lane)
        out[idx] = Link(
            idx,
            in_lane.getEdge().getID(),
            out_lane.getEdge().getID(),
            TURN_OF_DIR.get(conn.getDirection(), "T"),
            conn,
            in_lane.allows("passenger") and out_lane.allows("passenger"),
            conn.getDirection(),
            in_lane.getEdge().getName() or "",
        )
    return [out[i] for i in sorted(out)]


def is_mid_block(links: list[Link]) -> bool:
    return bool(links) and all(lk.turn == "T" for lk in links)


def _node_of(link: Link):
    return link.conn.getFrom().getToNode()


def green_states(links: list[Link], green: set[int]) -> str:
    """'G' for a green link no other green link forbids, 'g' if it must yield, else 'r'."""
    chars = []
    for lk in links:
        if lk.index not in green:
            chars.append("r")
            continue
        node = _node_of(lk)
        yields = any(
            o.index in green
            and o is not lk
            and _node_of(o) is node
            and node.forbids(o.conn, lk.conn)
            for o in links
        )
        chars.append("g" if yields else "G")
    return "".join(chars)


def crossing_throughs(links: list[Link], green: set[int]) -> list[tuple[int, int]]:
    """Pairs of green through links from different approaches that cross each other."""
    out = []
    for a in links:
        for b in links:
            if not (a.index < b.index and a.index in green and b.index in green):
                continue
            if a.turn != "T" or b.turn != "T" or a.from_edge == b.from_edge:
                continue
            # the two directions of one street: every real plan runs them together, but inside
            # a large joined junction (e.g. King/Kings Way/Flinders) SUMO can mark the skewed
            # opposing paths as foes; a geometry artifact, not a phase error
            if a.street and a.street == b.street:
                continue
            node = _node_of(a)
            if _node_of(b) is node and node.areFoes(
                node.getLinkIndex(a.conn), node.getLinkIndex(b.conn)
            ):
                out.append((a.index, b.index))
    return out


# --- programs ------------------------------------------------------------------------------------


@dataclass
class Phase:
    duration: float
    state: str
    min_dur: float | None = None
    max_dur: float | None = None


def program_xml(tls_id: str, phases: list[Phase]) -> str:
    # one phase left (every other phase had no car movement): a plain always-on program, since
    # SUMO's actuated controller needs at least two phases
    kind = "actuated" if len(phases) > 1 else "static"
    lines = [f'    <tlLogic id="{tls_id}" type="{kind}" programID="{PROGRAM_ID}" offset="0">']
    for p in phases:
        extra = ""
        if p.min_dur is not None and kind == "actuated":
            extra = f' minDur="{p.min_dur:g}" maxDur="{p.max_dur:g}"'
        lines.append(f'        <phase duration="{p.duration:g}"{extra} state="{p.state}"/>')
    lines.append("    </tlLogic>")
    return "\n".join(lines)


def real_phase_program(
    links: list[Link],
    seq: list[str],
    timings: dict[str, PhaseTiming],
    green_by_phase: dict[str, set[int]],
    ped_time: dict[str, float | None],
) -> list[Phase]:
    """The sheet's phase sequence with yellow + all-red between phases (links green in both
    phases stay green through the change)."""
    # phases with no car movement in SUMO are skipped (cars-only simulation); consecutive phases
    # that green the same links run as one
    seq = [p for p in seq if green_by_phase[p]]
    seq = [p for k, p in enumerate(seq) if green_by_phase[p] != green_by_phase[seq[k - 1]]] or seq[
        :1
    ]
    out = []
    n = len(seq)
    for k, letter in enumerate(seq):
        t = timings[letter]
        g = green_by_phase[letter]
        nxt = green_by_phase[seq[(k + 1) % n]]
        state = green_states(links, g)
        if g:
            out.append(Phase(t.max_green, state, t.min_green, t.max_green))
        else:  # no simulated vehicle movement (pedestrian / tram phase): fixed length
            d = max(t.min_green, ped_time.get(letter) or 0.0)
            out.append(Phase(d, state, d, d))
        ending = g - nxt
        if not ending:
            continue
        keep = "".join(c if lk.index in nxt else "r" for c, lk in zip(state, links, strict=True))
        yellow = "".join(
            "y" if lk.index in ending else c for c, lk in zip(keep, links, strict=True)
        )
        out.append(Phase(t.yellow, yellow))
        if t.all_red > 0:
            out.append(Phase(t.all_red, keep))
    return out


def timings_only_program(phases: list, timings: dict[str, PhaseTiming]) -> list[Phase]:
    """netconvert's phase plan (sumolib phases) on the sheet's cycle: which links go green
    together is netconvert's guess, the timings are the sheet's.

    The sheet's maximum cycle (every phase in the sequence at its max green, plus its yellow and
    all-red) is kept: after netconvert's yellows (each followed by the sheet's all-red) and red
    phases, the remaining green time is split over netconvert's green phases in proportion to
    their default lengths (so its short turn-arrow phases stay short). Min green: the median of
    the sheet's min greens (never above a phase's max)."""
    vals = list(timings.values())
    cycle = sum(t.max_green + t.yellow + t.all_red for t in vals)
    yellow = max(t.yellow for t in vals)
    all_red = max(t.all_red for t in vals)
    min_green = statistics.median(t.min_green for t in vals)
    greens = [ph for ph in phases if ("G" in ph.state or "g" in ph.state) and "y" not in ph.state]
    n_yellow = sum(1 for ph in phases if "y" in ph.state)
    reds = sum(ph.duration for ph in phases if not set(ph.state) & set("GgyY"))
    green_time = max(cycle - n_yellow * (yellow + all_red) - reds, 5.0 * len(greens))
    total = sum(ph.duration for ph in greens) or 1.0
    out = []
    for ph in phases:
        s = ph.state
        if "y" in s:
            out.append(Phase(yellow, s))
            if all_red > 0:
                out.append(Phase(all_red, s.replace("y", "r")))
        elif ph in greens:
            mx = round(max(5.0, green_time * ph.duration / total), 1)
            out.append(Phase(mx, s, min(min_green, mx), mx))
        else:
            out.append(Phase(ph.duration, s))
    return out


def mid_block_program(links: list[Link], vehicle: PhaseTiming, ped_s: float | None) -> list[Phase]:
    """Vehicle green (at least the sheet's max green, up to the cap), then yellow, all-red and
    the pedestrian walk + clearance; with `ped_s` None (pedestrian phase dropped, cars-only
    simulation) the light stays green."""
    g = "G" * len(links)
    if ped_s is None:
        return [Phase(POS_VEHICLE_GREEN_CAP_S, g)]
    mn = vehicle.max_green
    mx = max(mn, POS_VEHICLE_GREEN_CAP_S)
    out = [Phase(mx, g, mn, mx), Phase(vehicle.yellow, "y" * len(links))]
    if vehicle.all_red > 0:
        out.append(Phase(vehicle.all_red, "r" * len(links)))
    out.append(Phase(ped_s, "r" * len(links)))
    return out


# --- movements -> links --------------------------------------------------------------------------


def movement_links(links: list[Link], approach_edges: set[str], turn: str) -> set[int]:
    """Links of a sheet movement. A through movement is SUMO's straight link ("s"); an approach
    with no straight link (a skewed junction) has it as a partial left / right ("L" / "R")."""
    mine = [lk for lk in links if lk.from_edge in approach_edges]
    if turn == "T":
        straight = {lk.index for lk in mine if lk.dir == "s"}
        return straight or {lk.index for lk in mine if lk.dir in ("L", "R")}
    if turn in ("L", "R"):
        has_straight = any(lk.dir == "s" for lk in mine)
        # a partial left/right is this turn only when it is not standing in for the through
        dirs = {turn.lower(), turn} if has_straight else {turn.lower()}
        return {lk.index for lk in mine if lk.dir in dirs}
    return {lk.index for lk in mine if lk.turn == turn}


def resolve_groups(
    site: dict, links: list[Link], approach_of
) -> tuple[dict[str, set[int]], list[str]]:
    """Link indices of each vehicle signal group. `approach_of(street, heading)` returns the set of
    approach edge ids (empty if not found). Returns (links per group, notes)."""
    notes = []
    out: dict[str, set[int]] = {}
    for gid, g in (site.get("signal_groups") or {}).items():
        if not g or g.get("kind") != "vehicle":
            continue
        idx: set[int] = set()
        for m in g.get("movements") or []:
            if m.get("auxiliary"):
                # a separate stop line of the same controller (e.g. 4561's SG11/SG12 at the P4
                # crossing west of Russell): not a movement of this junction, never resolved
                continue
            edges = approach_of(m["street"], heading_code(m["heading"]))
            if not edges:
                notes.append(f"group {gid}: no approach edge for {m['street']} {m['heading']}")
                continue
            hit = movement_links(links, edges, m["turn"])
            if not hit:
                notes.append(f"group {gid}: no {m['turn']} link from {m['street']} {m['heading']}")
            idx |= hit
        out[str(gid)] = idx
    return out, notes


@dataclass
class TlsResult:
    tls: str
    site_id: str | None
    tier: str
    reason: str
    program: list[Phase] | None = None
    notes: list[str] = field(default_factory=list)


def build_for_tls(net, tls, site: dict | None, approach_of, mid_block_site: bool) -> TlsResult:
    """The program for one traffic light (pure: no file IO)."""
    links = tls_links(net, tls)
    tid = tls.getID()
    if site is None:
        return TlsResult(tid, None, "default", "no sheet for this light")
    sid = str(site["site_id"])
    pt = phase_timings(site)
    if pt is None:
        return TlsResult(tid, sid, "default", "phases/timings missing or not verified")
    seq, timings = pt
    notes: list[str] = []
    cars = car_phases(site, seq, timings)
    if not cars:
        return TlsResult(tid, sid, "default", "no phase with a car movement")
    if cars != seq:
        notes.append(f"phases without cars dropped: {sorted(set(seq) - set(cars))}")
    if usable(site, "signal_groups"):
        default_states = [ph.state for ph in _default_phases(tls)]
        prog, more = real_phases(links, site, seq, timings, approach_of, default_states)
        notes += more
        if prog is not None:
            return TlsResult(tid, sid, "real_phases", "sheet phases and movements", prog, notes)
    mid = is_mid_block(links)
    if mid_block_site and not mid:
        return TlsResult(
            tid, sid, "default", "mid-block crossing site on an intersection light", None, notes
        )
    if mid and not mid_block_site:
        # e.g. Swanston St (no cars) at Collins St: only the cross street's through links are
        # simulated, so the intersection's phase plan cannot be built; its timings still apply.
        notes.append("intersection site on a through-only light (cross movements not simulated)")
    elif mid:
        ped = pedestrian_time(site) if cars == seq else None
        if ped is None and cars == seq:
            return TlsResult(tid, sid, "default", "mid-block crossing without walk/clearance")
        prog = mid_block_program(links, timings[cars[0]], ped)
        return TlsResult(tid, sid, "real_timings", "mid-block crossing", prog, notes)
    prog = timings_only_program(_default_phases(tls), {p: timings[p] for p in cars})
    return TlsResult(tid, sid, "real_timings", "netconvert phases, sheet timings", prog, notes)


def real_phases(
    links, site, seq, timings, approach_of, default_states=None
) -> tuple[list[Phase] | None, list[str]]:
    """(the real phase program or None, notes). A movement the network does not have is noted;
    the program is rejected if a car link is in no phase, crossing throughs share one, or a car
    link that netconvert's default program protects (major green 'G' in some phase) is left
    yield-only here — the usual sign that a sheet movement failed to resolve to its links, which
    starves that movement at peak."""
    groups, notes = resolve_groups(site, links, approach_of)
    if not groups or any(not (site["signal_groups"][g] or {}).get("movements") for g in groups):
        return None, [*notes, "real phases: vehicle signal groups without movements"]
    green = {
        p: set().union(*[groups.get(str(g), set()) for g in timings[p].groups or []]) for p in seq
    }
    # qa 2026-10-28: a resolved vehicle group that no phase of the sequence runs is either a
    # sheet gap (event-only group) or a missed phase membership (how 2929 failed); warn so it
    # can never hide again
    in_phase = {str(g) for p in seq for g in timings[p].groups or []}
    orphans = sorted(g for g in groups if g not in in_phase and groups[g])
    if orphans:
        notes.append(f"resolved vehicle groups in no phase of the sequence: {orphans}")
    car = {lk.index for lk in links if lk.cars}
    # Car links no sheet phase serves (hook and filter turns run on the approach's through
    # green and are often not drawn as their own signal group): run them as filter movements
    # in every phase that greens another link of the same approach, instead of rejecting the
    # whole program. green_states() yields them ('g') where they cross a protected movement.
    unphased = car - set().union(*green.values())
    filtered = []
    if unphased:
        by_edge: dict[str, set[int]] = {}
        for lk in links:
            by_edge.setdefault(lk.from_edge, set()).add(lk.index)
        link_of = {lk.index: lk for lk in links}
        for i in sorted(unphased):
            mates = by_edge[link_of[i].from_edge] - unphased
            hit = [p for p in seq if green[p] & mates]
            for p in hit:
                green[p].add(i)
            if hit:
                filtered.append(i)
    if filtered:
        notes.append(
            f"{len(filtered)} unphased car links run as filter with their approach {filtered}"
        )
    missing = sorted(car - set().union(*green.values()))
    clash = sorted({c for p in seq for c in crossing_throughs(links, green[p])})
    if missing or clash:
        why = []
        if missing:
            why.append(f"{len(missing)} car links in no phase {missing}")
        if clash:
            why.append(f"crossing throughs green together {clash}")
        return None, [*notes, "real phases rejected: " + "; ".join(why)]
    ped = {p: pedestrian_time(site, p) for p in seq}
    prog = real_phase_program(links, seq, timings, green, ped)
    if default_states is not None:

        def protected(states: list[str], i: int) -> bool:
            return any(s[i] == "G" for s in states)

        states = [p.state for p in prog]
        hook = str(site["site_id"]) in HOOK_TURN_SITES
        # A yield the sheet prescribes is intended control, not a mapping failure: when a
        # resolved signal group covers the link, the sheet deliberately runs it in a phase
        # where it must give way (filter rights such as 4606's group 4, hook corners); and
        # when every movement resolved cleanly, a link in no group is a movement the sheet
        # leaves unsignalised (a filter), so yield is right there too. The comparison against
        # netconvert's protected default applies only where a resolve failure shows OUR
        # mapping lost something.
        failures = any("no approach edge" in n or " link from " in n for n in notes)
        if not failures:
            return prog, notes
        covered = set().union(*groups.values()) if groups else set()
        demoted = sorted(
            lk.index
            for lk in links
            if lk.cars
            and lk.index not in covered
            and lk.turn != "U"  # a U-turn always gives way (road rule 38): yield is correct
            and not (hook and lk.turn == "R")
            and protected(default_states, lk.index)
            and not protected(states, lk.index)
        )
        if demoted:
            why = f"{len(demoted)} car links lose their protected green vs default {demoted}"
            return None, [*notes, "real phases rejected: " + why]
    return prog, notes


def _default_phases(tls) -> list:
    progs = tls.getPrograms()
    return progs[sorted(progs)[0]].getPhases()


# --- site -> light -------------------------------------------------------------------------------


def augment_split_site(idx, sj, lon: float, lat: float) -> bool:
    """Pull every signal node within MID_BLOCK_PAIR_M of the register point into the site, so a
    split carriageway's twin light gets the site's program and both directions' approaches
    resolve. Returns whether anything was added; approaches are rebuilt with site_junction's
    rule (incoming passenger edges from outside the node set)."""
    import math

    x, y = idx.xy(lon, lat)
    added = False
    for n in idx.tl_nodes:
        nx, ny = n.getCoord()
        if n.getID() not in sj.nodes and math.hypot(nx - x, ny - y) <= MID_BLOCK_PAIR_M:
            sj.nodes.append(n.getID())
            sj.tls_ids = sorted(set(sj.tls_ids) | idx.tls_of.get(n.getID(), set()))
            added = True
    if not added:
        return False
    sj.nodes = sorted(set(sj.nodes))
    nodes = set(sj.nodes)
    sj.approaches = []
    for nid in sj.nodes:
        node = idx.net.getNode(nid)
        for e in node.getIncoming():
            if e.getFromNode().getID() in nodes or not e.allows("passenger"):
                continue
            sj.approaches.append(idx.approach_edge(e))
    return True


def match_sites(sites: list[dict], net_path: Path = NET_PATH, sites_csv: Path = SITES_CSV):
    """{tls id: (site, register type, SiteJunction)}, plus notes for sites that found no light.
    Two sites on one light: the nearer keeps it."""
    from pipeline.count_mapping import NetIndex
    from pipeline.scats import load_sites

    register = load_sites(sites_csv)
    idx = NetIndex(net_path)
    best: dict[str, tuple] = {}
    notes = []
    for site in sites:
        sid = int(site["site_id"])
        reg = register.get(sid)
        if reg is None:
            notes.append({"site_id": str(sid), "note": "not in the signal register"})
            continue
        sj = idx.site_junction(sid, reg.lon, reg.lat)
        if sj is None or not sj.tls_ids:
            notes.append({"site_id": str(sid), "note": "no traffic light within 60 m"})
            continue
        if (reg.type in MID_BLOCK_TYPES or len(sj.approaches) < 2) and augment_split_site(
            idx, sj, reg.lon, reg.lat
        ):
            n_tls = len(sj.tls_ids)
            notes.append({"site_id": str(sid), "note": f"split site: paired to {n_tls} lights"})
        if len(sj.tls_ids) > 1:
            notes.append({"site_id": str(sid), "note": f"spans {len(sj.tls_ids)} lights"})
        for t in sj.tls_ids:
            if t in best and best[t][2].dist_m <= sj.dist_m:
                notes.append({"site_id": str(sid), "note": f"light {t} kept by nearer site"})
                continue
            if t in best:
                notes.append({"site_id": str(best[t][0]["site_id"]), "note": f"light {t} lost"})
            best[t] = (site, reg.type, sj)
    return idx, best, notes


def approach_resolver(idx, sj):
    from pipeline.count_mapping import match_approach

    def approach_of(street: str, heading: str) -> set[str]:
        m = match_approach(sj, street, heading, None)
        if m.edge_id is not None and any(a.edge_id == m.edge_id for a in sj.approaches):
            return {m.edge_id}
        return set(m.candidates) if m.name_match else set()

    return approach_of


def _src_name(p: Path) -> str:
    return str(p.relative_to(REPO_ROOT)) if p.is_relative_to(REPO_ROOT) else p.name


def generate(
    sheets_path: Path = SHEETS_PATH,
    net_path: Path = NET_PATH,
    add_path: Path = ADD_PATH,
    log_path: Path = LOG_PATH,
    sites_csv: Path = SITES_CSV,
) -> dict:
    import sumolib

    sites = json.loads(sheets_path.read_text(encoding="utf-8"))
    idx, matched, site_notes = match_sites(sites, net_path, sites_csv)
    net = sumolib.net.readNet(str(net_path), withPrograms=True)
    results = []
    for tls in sorted(net.getTrafficLights(), key=lambda t: t.getID()):
        m = matched.get(tls.getID())
        if m is None:
            results.append(build_for_tls(net, tls, None, None, False))
            continue
        site, reg_type, sj = m
        results.append(
            build_for_tls(net, tls, site, approach_resolver(idx, sj), reg_type in MID_BLOCK_TYPES)
        )
    body = [program_xml(r.tls, r.program) for r in results if r.program]
    add_path.parent.mkdir(parents=True, exist_ok=True)
    add_path.write_text(
        f"<!-- generated by sim/signals_real.py from {_src_name(sheets_path)}"
        " -->\n<additional>\n" + "\n".join(body) + ("\n" if body else "") + "</additional>\n",
        encoding="utf-8",
    )
    tiers: dict[str, int] = {}
    for r in results:
        tiers[r.tier] = tiers.get(r.tier, 0) + 1
    log = {
        "description": "Tier of each SUMO traffic light's program (sim/signals_real.py).",
        "accepted_provenance": sorted(ACCEPTED),
        "pos_vehicle_green_cap_s": POS_VEHICLE_GREEN_CAP_S,
        "max_green_meaning": MAX_GREEN_NOTE,
        "max_green_reading": "total" if MAX_GREEN_TOTAL else "min_plus_extension",
        "tiers": tiers,
        "sites": len(sites),
        "site_notes": site_notes,
        "lights": [
            {
                "tls": r.tls,
                "site_id": r.site_id,
                "tier": r.tier,
                "reason": r.reason,
                "cycle_max_s": round(sum(p.max_dur or p.duration for p in r.program), 1)
                if r.program
                else None,
                "notes": r.notes,
            }
            for r in results
        ],
    }
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(json.dumps(log, indent=1) + "\n", encoding="utf-8")
    return log


def main(argv: list[str] | None = None) -> int:
    global MAX_GREEN_TOTAL

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--sheets", type=Path, default=SHEETS_PATH)
    ap.add_argument("-o", "--output", type=Path, default=ADD_PATH)
    ap.add_argument("--log", type=Path, default=LOG_PATH)
    ap.add_argument(
        "--max-green-min-plus",
        action="store_true",
        help="read the sheets' MAXIMUM EXTENSION GREEN as extra green on top of the minimum"
        " (reading A), instead of the default total reading (B)",
    )
    args = ap.parse_args(argv)
    if args.max_green_min_plus:
        MAX_GREEN_TOTAL = False
    if not args.sheets.exists():
        print(f"{args.sheets} missing (written by data-engineer)", file=sys.stderr)
        return 1
    log = generate(args.sheets, add_path=args.output, log_path=args.log)
    print(f"wrote {args.output}: tiers {log['tiers']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
