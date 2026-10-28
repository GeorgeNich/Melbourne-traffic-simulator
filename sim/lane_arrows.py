"""Lane arrows from the DTP signal sheets -> netconvert patches applied in build_net's pass 2.

Usage (from the repo root):
    uv run python -m sim.lane_arrows      # then: uv run python -m sim.build_net

Writes
    sim/patches/net/sheet_lane_arrows.con.xml   which lanes of each approach serve which turn
    sim/patches/net/sheet_lane_counts.edg.xml   approach lane counts the sheet shows more of
    sim/out/checks/lane_arrows.json             per approach: what changed and why

Input: the `lane_layout` of the verified sheet file (only where its provenance is in
signals_real.ACCEPTED): per approach "<street> <heading>", lanes numbered from the left kerb with
their use and arrows. OSM turn:lanes are already applied by netconvert (--osm.turn-lanes and
turn_lanes.con.xml); on an approach the sheet covers, the sheet wins.

The patch is generated from the network built without it (pass 2 minus these files,
sim/work/roads_no_sheet_lanes.net.xml), so rebuilding is reproducible. Rules:
- Car lanes of the sheet are those whose use includes general traffic or a bus lane (bicycle and
  tram-only lanes are not in the SUMO network). SUMO lane 0 is the left kerb lane (--lefthand).
- Equal lane counts: lanes map one to one. The sheet shows more lanes: the edge gets that many
  (only on edges whose lanes are all open to cars and that no other edge patch touches); fewer:
  the approach is skipped and logged.
- Arrows choose lanes, never movements: a movement the network has but no lane arrow shows keeps
  its current lanes; an arrow with no matching movement is logged. A "hook turn" note gives the
  kerb-side lane its right turn (CBD hook turns: the right turn is made from the left).
- Edge ids never change.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from sim.config import CHECKS_DIR, NET_PATCH_DIR, REPO_ROOT, WORK_DIR
from sim.signals_real import (
    SHEETS_PATH,
    SITES_CSV,
    TURN_OF_DIR,
    approach_resolver,
    heading_code,
    usable,
)

CON_PATCH = NET_PATCH_DIR / "sheet_lane_arrows.con.xml"
EDG_PATCH = NET_PATCH_DIR / "sheet_lane_counts.edg.xml"
LOG_PATH = CHECKS_DIR / "lane_arrows.json"
PRE_NET = WORK_DIR / "roads_no_sheet_lanes.net.xml"

CAR_USES = {"general", "tram+general", "bus", "bus+general"}
ARROW_TURN = {"through": "T", "left": "L", "right": "R", "uturn": "U", "u-turn": "U"}
LETTERS = {"L": "L", "T": "T", "R": "R", "U": "U"}


def split_key(key: str) -> tuple[str, str] | None:
    """'William St southbound' -> ('William St', 'SB')."""
    parts = key.rsplit(" ", 1)
    if len(parts) != 2:
        return None
    code = heading_code(parts[1])
    return (parts[0], code) if code in {"NB", "SB", "EB", "WB", "NE", "NW", "SE", "SW"} else None


def parse_arrows(arrows: str) -> set[str] | None:
    """'TR' / 'through+right' -> {'T', 'R'}; None if not understood."""
    a = arrows.strip()
    if a and set(a) <= set(LETTERS):
        return {LETTERS[c] for c in a}
    out = set()
    for w in a.lower().split("+"):
        if w.strip() not in ARROW_TURN:
            return None
        out.add(ARROW_TURN[w.strip()])
    return out


def lane_turns(lanes: list[dict]) -> tuple[list[set[str]], list[str]] | None:
    """Turn sets of the sheet's car lanes, left kerb first, plus notes; None if unreadable.
    A lane without arrows follows the road rules (ARR r.46-48): through, plus left from the
    left-most car lane and right from the right-most."""
    if not isinstance(lanes, list) or not all(isinstance(ln, dict) for ln in lanes):
        return None
    out, notes = [], []
    car = sorted((ln for ln in lanes if ln.get("use") in CAR_USES), key=lambda ln: ln["lane"])
    for k, ln in enumerate(car):
        if ln.get("arrows") is None:
            turns = {"T"} | ({"L"} if k == 0 else set()) | ({"R"} if k == len(car) - 1 else set())
            notes.append(
                f"lane {ln['lane']}: no arrows, road-rule default {''.join(sorted(turns))}"
            )
        else:
            turns = parse_arrows(str(ln["arrows"]))
            if turns is None:
                return None
        if "hook" in str(ln.get("hours") or ln.get("notes") or "").lower():
            turns.add("R")
            notes.append(f"lane {ln['lane']}: hook turn, right turn from this lane")
        out.append(turns)
    return out, notes


@dataclass
class Approach:
    site_id: str
    key: str
    edge: str | None = None
    status: str = ""
    lanes_before: int | None = None
    lanes_after: int | None = None
    before: dict = field(default_factory=dict)  # to_edge -> sorted from-lanes
    after: dict = field(default_factory=dict)
    connections: list = field(default_factory=list)  # (to_edge, fromLane, toLane)
    notes: list = field(default_factory=list)


def lane_map(from_lanes: list[int], targets: list[int], turn: str) -> list[tuple[int, int]]:
    """(fromLane, toLane) pairs for lanes `from_lanes` (kerb first) turning into an edge whose car
    lanes are `targets` (kerb first). Left turns and through line up from the kerb, right turns
    and U-turns from the far side; with more target lanes than turning lanes, the outermost
    turning lane also feeds the spare target lanes (as netconvert does), so none is left unfed."""
    n, m = len(from_lanes), len(targets)
    right = turn in ("R", "U")
    out = []
    for k, fl in enumerate(from_lanes):
        j = max(0, min(m - 1, m - n + k)) if right else min(k, m - 1)
        out.append((fl, targets[j]))
    if n < m:
        spare = targets[: m - n] if right else targets[n:]
        edge_lane = from_lanes[0] if right else from_lanes[-1]
        out += [(edge_lane, t) for t in spare]
    return sorted(out)


def plan_approach(edge, turns: list[set[str]], fixed_edges: set[str], ap: Approach) -> None:
    """Fill `ap` with the new lane-to-turn connections of one approach edge (sumolib edge)."""
    n_sumo = edge.getLaneNumber()
    if edge.getToNode().getType().startswith("traffic_light") and all(
        c.getDirection() == "s" for ln in edge.getLanes() for c in ln.getOutgoing()
    ):
        ap.lanes_before = n_sumo
        ap.status = "skipped: edge ends at a through-only light, not the drawn intersection"
        return
    n_sheet = len(turns)
    ap.lanes_before = n_sumo
    movements: dict[str, tuple[str, list[int]]] = {}  # to_edge -> (turn, current from-lanes)
    to_edges = {}
    for lane in edge.getLanes():
        for c in lane.getOutgoing():
            to = c.getTo().getID()
            to_edges[to] = c.getTo()
            t = TURN_OF_DIR.get(c.getDirection(), "T")
            movements.setdefault(to, (t, []))[1].append(lane.getIndex())
    ap.before = {to: sorted(set(ls)) for to, (_, ls) in sorted(movements.items())}
    if n_sheet < n_sumo:
        ap.status = f"skipped: sheet shows {n_sheet} car lanes, network has {n_sumo}"
        return
    if n_sheet > n_sumo:
        all_cars = all(ln.allows("passenger") for ln in edge.getLanes())
        if not all_cars or edge.getID() in fixed_edges:
            ap.status = f"skipped: sheet shows {n_sheet} lanes, edge {n_sumo} (not widened)"
            return
        ap.notes.append(f"lanes {n_sumo} -> {n_sheet}")
    ap.lanes_after = n_sheet
    shown = set().union(*turns) if turns else set()
    for t in sorted(shown - {t for t, _ in movements.values()}):
        ap.notes.append(f"arrow {t} shown, no such movement in the network")
    for to, (t, current) in sorted(movements.items()):
        lanes = [i for i, ts in enumerate(turns) if t in ts]
        if not lanes:
            lanes = sorted(set(current))
            ap.notes.append(f"{t} to {to}: no lane arrow shows it, kept lanes {lanes}")
        tgt = to_edges[to].getLanes()
        cars = [ln.getIndex() for ln in tgt if ln.allows("passenger")] or [
            ln.getIndex() for ln in tgt
        ]
        ap.connections += [(to, fl, tl) for fl, tl in lane_map(lanes, cars, t)]
        ap.after[to] = lanes
    dead = sorted(set(range(n_sheet)) - {fl for _, fl, _ in ap.connections})
    if dead:
        # e.g. the edge ends at a stop-line or mid-block light short of the intersection the
        # sheet draws: its arrows do not belong to this edge
        ap.status = f"skipped: lanes {dead} would have no movement"
        ap.connections, ap.after, ap.lanes_after = [], {}, None
        return
    ap.status = "changed" if ap.after != ap.before or ap.lanes_after != n_sumo else "unchanged"


def edges_in_other_patches() -> set[str]:
    out = set()
    for p in sorted(NET_PATCH_DIR.glob("*.edg.xml")):
        if p == EDG_PATCH:
            continue
        for e in ET.parse(p).getroot():
            if e.get("id"):
                out.add(e.get("id"))
    return out


def plan(sheets: list[dict], net_path: Path, sites_csv: Path = SITES_CSV) -> list[Approach]:
    import sumolib

    from pipeline.count_mapping import NetIndex
    from pipeline.scats import load_sites

    register = load_sites(sites_csv)
    idx = NetIndex(net_path)
    net = sumolib.net.readNet(str(net_path))
    fixed = edges_in_other_patches()
    out: list[Approach] = []
    taken: set[str] = set()
    for site in sorted(sheets, key=lambda s: int(s["site_id"])):
        if not usable(site, "lane_layout"):
            continue
        sid = str(site["site_id"])
        reg = register.get(int(sid))
        sj = idx.site_junction(int(sid), reg.lon, reg.lat) if reg else None
        for key, lanes in sorted(site["lane_layout"].items()):
            ap = Approach(sid, key)
            out.append(ap)
            sk = split_key(key)
            lt = lane_turns(lanes)
            if sj is None or sk is None or lt is None:
                ap.status = "skipped: " + (
                    "site not on a traffic light" if sj is None else "unreadable layout"
                )
                continue
            edges = approach_resolver(idx, sj)(*sk)
            if len(edges) != 1:
                ap.status = f"skipped: approach matches {len(edges)} edges"
                continue
            ap.edge = next(iter(edges))
            if ap.edge in taken:
                ap.status = "skipped: edge already set by another site"
                continue
            turns, ap.notes = lt
            plan_approach(net.getEdge(ap.edge), turns, fixed, ap)
            if ap.status == "changed":
                taken.add(ap.edge)
    return out


def write_patches(approaches: list[Approach], con_path: Path, edg_path: Path) -> tuple[int, int]:
    con = ["<!-- generated by sim/lane_arrows.py: lane arrows from the DTP signal sheets -->\n"]
    con.append("<connections>\n")
    edg = ["<!-- generated by sim/lane_arrows.py: approach lane counts from the sheets -->\n"]
    edg.append("<edges>\n")
    n_con = n_edg = 0
    for ap in approaches:
        if ap.status != "changed":
            continue
        con.append(f"    <!-- site {ap.site_id}, {ap.key} -->\n")
        for to in ap.before:
            con.append(f'    <delete from="{ap.edge}" to="{to}"/>\n')
        for to, fl, tl in ap.connections:
            con.append(
                f'    <connection from="{ap.edge}" to="{to}" fromLane="{fl}" toLane="{tl}"/>\n'
            )
            n_con += 1
        if ap.lanes_after != ap.lanes_before:
            edg.append(f'    <edge id="{ap.edge}" numLanes="{ap.lanes_after}"/>\n')
            n_edg += 1
    con.append("</connections>\n")
    edg.append("</edges>\n")
    con_path.parent.mkdir(parents=True, exist_ok=True)
    con_path.write_text("".join(con), encoding="utf-8")
    edg_path.write_text("".join(edg), encoding="utf-8")
    return n_con, n_edg


def write_log(approaches: list[Approach], path: Path) -> dict:
    status: dict[str, int] = {}
    for ap in approaches:
        k = ap.status.split(":")[0]
        status[k] = status.get(k, 0) + 1
    log = {
        "description": "Lane arrows from the verified DTP signal sheets (sim/lane_arrows.py).",
        "status": status,
        "lanes_widened": sum(
            1 for a in approaches if a.status == "changed" and a.lanes_after != a.lanes_before
        ),
        "approaches": [
            {
                "site_id": a.site_id,
                "approach": a.key,
                "edge": a.edge,
                "status": a.status,
                "lanes_before": a.lanes_before,
                "lanes_after": a.lanes_after,
                "turn_lanes_before": a.before,
                "turn_lanes_after": a.after,
                "notes": a.notes,
            }
            for a in approaches
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(log, indent=1) + "\n", encoding="utf-8")
    return log


def refresh(sheets_path: Path = SHEETS_PATH) -> dict:
    """Rebuild the network without the sheet patches, then regenerate them from it."""
    from sim import build_net

    build_net.run(
        build_net.net_pass_command(
            build_net.RAW_NET, PRE_NET, WORK_DIR / "netconvert_presheet.log", skip_sheets=True
        )
    )
    sheets = json.loads(sheets_path.read_text(encoding="utf-8"))
    aps = plan(sheets, PRE_NET)
    n_con, n_edg = write_patches(aps, CON_PATCH, EDG_PATCH)
    log = write_log(aps, LOG_PATH)
    log["connections"], log["edges"] = n_con, n_edg
    return log


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--sheets", type=Path, default=SHEETS_PATH)
    args = ap.parse_args(argv)
    if not args.sheets.exists():
        print(f"{args.sheets} missing (written by data-engineer)", file=sys.stderr)
        return 1
    log = refresh(args.sheets)
    print(
        f"wrote {CON_PATCH.relative_to(REPO_ROOT)} ({log['connections']} connections), "
        f"{EDG_PATCH.relative_to(REPO_ROOT)} ({log['edges']} lane counts): {log['status']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
