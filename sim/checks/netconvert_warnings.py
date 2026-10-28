"""netconvert warnings, class by class: fixed or justified, with every instance listed.

Usage (from the repo root, after `uv run python -m sim.build_net`):
    uv run python -m sim.checks.netconvert_warnings

Reads sim/work/netconvert_osm.log (pass 1, OSM import) and sim/work/netconvert_patch.log (pass 2,
the network that is shipped) and writes sim/out/checks/netconvert_warnings.json. A warning that
matches no class below is reported as `unclassified` (the tests require there are none).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from sim.config import CHECKS_DIR, WORK_DIR


@dataclass(frozen=True)
class WarningClass:
    key: str
    pattern: str
    verdict: str  # fixed / justified
    reason: str


CLASSES = [
    WarningClass(
        "intersecting_turns",
        r"Intersecting left turns at junction '(?P<junction>[^']+)' from lane '(?P<a>[^']+)' "
        r"and lane '(?P<b>[^']+)'",
        "justified",
        "Two opposing turns across the centre line (right turns here: in a left-hand network "
        "SUMO's 'left turn' check covers them) whose paths overlap inside a compact junction. "
        "SUMO does not make such turns foes, so two cars turning at once may overlap visually "
        "inside the junction; flow is unaffected (no teleports at these junctions in the smoke "
        "runs). A larger junction radius everywhere would distort the city geometry instead.",
    ),
    WarningClass(
        "sharp_turn",
        r"Found sharp turn with radius (?P<radius>[\d.]+) at the (?:start|end) of edge "
        r"'(?P<edge>[^']+)'",
        "justified",
        "OSM geometry bends more tightly than a car could turn where a way meets a junction "
        "(usually a slip lane or a short joined-junction approach). Edge geometry is visual only "
        "in SUMO: it changes neither speed nor routing.",
    ),
    WarningClass(
        "lane_not_fed",
        r"Lane '(?P<lane>[^']+)' is not connected from any incoming edge at junction "
        r"'(?P<junction>[^']+)'",
        "justified",
        "A lane that starts at the junction (the road widens: an added turn pocket or lane). "
        "No incoming lane leads straight into it; cars reach it by changing lanes, as on the road.",
    ),
    WarningClass(
        "edge_without_onward_connection",
        r"Edge '(?P<edge>[^']+)' is not connected to outgoing edges at junction "
        r"'(?P<junction>[^']+)'",
        "fixed",
        "Approaches whose only connection was a banned turn: sim/patches/net/turn_lanes.con.xml "
        "adds the turn their lane arrows show. Any instance listed here is still open.",
    ),
    WarningClass(
        "turn_speed_reduced",
        r"Speed of (?:straight|turning) connection '(?P<connection>[^']+)' reduced",
        "justified",
        "SUMO lowers the speed through a junction where the path through it curves; that is the "
        "intended physics, not an error.",
    ),
    WarningClass(
        "junction_shape_offset",
        r"Shape for junction '(?P<junction>[^']+)' has distance",
        "justified",
        "Large joined junctions whose outline is offset from the merged node position: geometry.",
    ),
    WarningClass(
        "segment_angle",
        r"Found angle of [\d.]+ degrees at edge '(?P<edge>[^']+)'",
        "justified",
        "A kink in the OSM geometry of the edge: visual only.",
    ),
    WarningClass(
        "connection_already_absent",
        r"Target edge '(?P<to>[^']+)' is not connected with '(?P<from>[^']+)'",
        "justified",
        "A <delete> in the restriction patch for a connection that pass 2 no longer has; the "
        "banned movement is absent either way (the turn-restriction audit checks it).",
    ),
    # pass 1 (OSM import) only
    WarningClass(
        "restriction_ignored",
        r"(?:Ignoring restriction relation|direction of restriction relation could not|"
        r"(?:from|to)-edge '[^']*' of restriction relation could not)",
        "fixed",
        "netconvert drops via-way, conditional and ambiguous restrictions; "
        "sim/patches/net/turn_restrictions.con.xml applies them (all 285 relations accounted "
        "for in sim/out/checks/turn_restrictions.json).",
    ),
    WarningClass(
        "turn_signs_not_applicable",
        r"(?:Cannot apply|Ignoring) turn sign information",
        "justified",
        "OSM turn:lanes arrows that do not fit the roads actually connected (arrow counts and "
        "targets differ); netconvert falls back to its own lane assignment for that approach. "
        "Where that left a one-sided approach without its turn, turn_lanes.con.xml adds it.",
    ),
    WarningClass(
        "roundabout_redefined",
        r"(?:Replacing loaded roundabout|Writing incomplete roundabout)",
        "justified",
        "--roundabouts.guess redefines OSM roundabouts after junction joining; 'incomplete' ones "
        "are roundabouts with a part that is not a passenger road.",
    ),
    WarningClass(
        "junction_join_heuristics",
        r"(?:Reducing junction cluster|Not joining junctions)",
        "justified",
        "netconvert explaining why it joined fewer nodes than the 15 m distance allows "
        "(parallel roads, slip lanes, non-compact clusters): its safety rules, by design.",
    ),
    WarningClass(
        "explicit_join_wins",
        r"Ignoring join exclusion for junction '(?P<junction>[^']+)' since it already occurred "
        r"in a list of nodes to be joined",
        "fixed",
        "netconvert had kept these nodes out of its automatic junction joining (it gives no "
        "reason); the hand patch sim/patches/osm/elizabeth_bourke_signals.nod.xml joins them on "
        "purpose (Elizabeth St / Bourke St box, one signalised junction), and the explicit join "
        "wins, as intended.",
    ),
    WarningClass(
        "isolated_road_removed",
        r"Removed a road without junctions",
        "justified",
        "Pieces left with no junction at either end after clipping and dropping service roads "
        "(--remove-edges.isolated).",
    ),
    WarningClass(
        "signal_without_links",
        r"(?:The traffic light '[^']+' does not control any links|Could not build program)",
        "justified",
        "Signals at the end of a road whose continuation is not a car road or lies beyond the "
        "extract (listed as dead_end in sim/out/checks/signals.json).",
    ),
]

LOGS = {"pass1_osm_import": "netconvert_osm.log", "pass2_final_network": "netconvert_patch.log"}


def classify(lines: list[str]) -> dict:
    out: dict = {}
    for line in lines:
        if not line.startswith("Warning:") or "total messages of type" in line:
            continue
        for c in CLASSES:
            m = re.search(c.pattern, line)
            if m:
                row = out.setdefault(
                    c.key, {"verdict": c.verdict, "reason": c.reason, "count": 0, "instances": []}
                )
                row["count"] += 1
                row["instances"].append(m.groupdict() or line[9:].strip())
                break
        else:
            row = out.setdefault(
                "unclassified",
                {"verdict": "open", "reason": "no class matches", "count": 0, "instances": []},
            )
            row["count"] += 1
            row["instances"].append(line[9:].strip())
    return out


def run(work: Path = WORK_DIR) -> dict:
    report: dict = {
        "about": "netconvert warnings of the network build, each class fixed or justified. "
        "pass2_final_network is the shipped sim/net/roads.net.xml.",
    }
    for key, name in LOGS.items():
        path = work / name
        if not path.exists():
            raise FileNotFoundError(f"{path} missing; run uv run python -m sim.build_net first")
        report[key] = classify(path.read_text(encoding="utf-8", errors="replace").splitlines())
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("-o", "--output", type=Path, default=CHECKS_DIR / "netconvert_warnings.json")
    args = ap.parse_args(argv)
    report = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
    for key in LOGS:
        print(key, {k: (v["count"], v["verdict"]) for k, v in report[key].items()})
    return 0


if __name__ == "__main__":
    sys.exit(main())
