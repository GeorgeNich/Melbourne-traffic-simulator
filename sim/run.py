"""Smoke runs of the network with placeholder random demand (labelled `placeholder_random`).

Usage (from the repo root):
    uv run python -m sim.run smoke                 # light, then moderate density
    uv run python -m sim.run smoke --levels light
    uv run python -m sim.run baseline               # the baseline scenario + trips export
                                                    # + sim/out/edge_stats_baseline.parquet

For each density: randomTrips.py --validate --lanes (fixed seed; edges weighted by lane count)
makes trips over 5 min warm-up + 15 min,
SUMO runs them, and the run's metrics are written to sim/work/smoke_<level>/metrics.json:
vehicles inserted / arrived, teleports (count, top edges and junctions), mean speed, the edges
where cars wait longest (gridlock hot spots), and the wall-clock run time.

This demand is random, not measured (labelled `placeholder_random` in the exports).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

from sim.config import NET_PATH, SEED, SUMO_TOOLS, WORK_DIR, sumo_env

WARMUP_S = 300
MEASURE_S = 900
END_S = WARMUP_S + MEASURE_S

# 60 s edgeData bins for sim/out/edge_stats_<scenario>.parquet (sim/edge_stats.py).
EDGE_STATS_PERIOD_S = 60
EDGE_STATS_FILE = "edgestats60.xml"

# Seconds between departures (randomTrips --period): light ~1,800 veh/h, moderate ~4,500 veh/h,
# heavy ~9,000 veh/h (optional stress level, not part of the default smoke run).
LEVELS = {"light": 2.0, "moderate": 0.8, "heavy": 0.4}

_TELEPORT = re.compile(
    r"Teleporting vehicle '(?P<veh>[^']+)'; (?P<why>[^,]+), lane='(?P<lane>[^']*)'"
    r"(?:, time=(?P<t>\d+(?:\.\d+)?))?"
)


def lane_to_edge(lane: str) -> str:
    """'123#4_1' -> '123#4'; internal lanes ':J_3_0' -> ':J_3'."""
    return lane.rsplit("_", 1)[0] if "_" in lane else lane


def parse_teleports(log_text: str) -> list[dict]:
    out = []
    for m in _TELEPORT.finditer(log_text):
        out.append(
            {
                "vehicle": m["veh"],
                "reason": m["why"].strip(),
                "edge": lane_to_edge(m["lane"]),
                "time": float(m["t"]) if m["t"] else None,
            }
        )
    return out


def parse_statistics(path: Path) -> dict:
    root = ET.parse(path).getroot()

    def attrs(tag: str) -> dict:
        el = root.find(tag)
        return {k: float(v) for k, v in el.attrib.items()} if el is not None else {}

    return {
        "vehicles": attrs("vehicles"),
        "teleports": attrs("teleports"),
        "trips": attrs("vehicleTripStatistics"),
    }


def parse_edgedata(path: Path, top: int = 10) -> list[dict]:
    """Edges ranked by total waiting time over the run."""
    root = ET.parse(path).getroot()
    rows = []
    for interval in root.iter("interval"):
        for e in interval.iter("edge"):
            wt = float(e.get("waitingTime", 0))
            if wt > 0:
                rows.append(
                    {
                        "edge": e.get("id"),
                        "waiting_s": round(wt, 1),
                        "mean_speed_mps": float(e.get("speed", 0) or 0),
                        "entered": int(float(e.get("entered", 0) or 0)),
                    }
                )
    rows.sort(key=lambda r: -r["waiting_s"])
    return rows[:top]


def junction_of_edge(net, edge_id: str) -> str | None:
    if edge_id.startswith(":"):
        return edge_id[1:].rsplit("_", 1)[0]
    return net.getEdge(edge_id).getToNode().getID() if net.hasEdge(edge_id) else None


def make_trips(net: Path, out_dir: Path, period: float, seed: int = SEED) -> Path:
    trips, routes = out_dir / "trips.xml", out_dir / "routes.xml"
    cmd = [
        sys.executable,
        str(SUMO_TOOLS / "randomTrips.py"),
        "-n",
        str(net),
        "-o",
        str(trips),
        "-r",
        str(routes),
        "-b",
        "0",
        "-e",
        str(END_S),
        "-p",
        str(period),
        "--seed",
        str(seed),
        "--validate",
        "--vehicle-class",
        "passenger",
        "--fringe-factor",
        "5",
        "--min-distance",
        "300",
        "--prefix",
        "rt",
        # Weight each edge by its lane count: bigger roads carry more traffic, and the two long
        # CityLink tunnel edges (Burnley / Domain, 3 lanes) get picked; with uniform weights seed 42
        # sent no car through either (orchestrator decision, Phase 2 re-export).
        "--lanes",
    ]
    subprocess.run(cmd, check=True, capture_output=True, env=sumo_env())
    return routes


def run_level(level: str, net_path: Path = NET_PATH) -> dict:
    m = simulate(WORK_DIR / f"smoke_{level}", LEVELS[level], net_path)
    m["level"] = level
    return m


def simulate(
    out: Path,
    period: float,
    net_path: Path = NET_PATH,
    step_length: float | None = None,
    fcd: Path | None = None,
) -> dict:
    """randomTrips + one SUMO run into `out`; returns the run's metrics (also metrics.json).

    With `fcd`, SUMO writes floating-car data every simulation step from WARMUP_S on."""
    import sumolib

    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    routes = make_trips(net_path, out, period)
    t_trips = time.perf_counter() - t0
    # Two passive outputs (they do not change the simulation): the whole recording window for
    # the hot-spot metrics, and 60 s bins from t=0 for sim/edge_stats.py (from t=0 so the number
    # of vehicles already on an edge when a bin starts can be carried forward; see there).
    (out / "edgedata.add.xml").write_text(
        f'<additional><edgeData id="smoke" file="edgedata.xml" begin="{WARMUP_S}" '
        f'end="{END_S}"/>'
        f'<edgeData id="stats{EDGE_STATS_PERIOD_S}" file="{EDGE_STATS_FILE}" begin="0" '
        f'end="{END_S}" period="{EDGE_STATS_PERIOD_S}" excludeEmpty="true"/></additional>\n',
        encoding="utf-8",
    )
    cmd = [
        sumolib.checkBinary("sumo"),
        "-n",
        str(net_path),
        "-r",
        str(routes),
        "-a",
        str(out / "edgedata.add.xml"),
        "--begin",
        "0",
        "--end",
        str(END_S),
        "--seed",
        str(SEED),
        "--no-step-log",
        "--statistic-output",
        str(out / "stats.xml"),
        "--tripinfo-output",
        str(out / "tripinfo.xml"),
        "--log",
        str(out / "sumo.log"),
    ]
    if step_length is not None:
        cmd += ["--step-length", str(step_length)]
    if fcd is not None:
        cmd += ["--fcd-output", str(fcd), "--device.fcd.begin", str(WARMUP_S)]
    t1 = time.perf_counter()
    subprocess.run(cmd, check=True, capture_output=True, env=sumo_env())
    t_sim = time.perf_counter() - t1

    net = sumolib.net.readNet(str(net_path))
    stats = parse_statistics(out / "stats.xml")
    teleports = parse_teleports((out / "sumo.log").read_text(encoding="utf-8", errors="replace"))
    by_edge = Counter(t["edge"] for t in teleports)
    by_junction = Counter(junction_of_edge(net, t["edge"]) for t in teleports)

    def name(eid: str) -> str | None:
        return net.getEdge(eid).getName() if net.hasEdge(eid) else None

    hot = parse_edgedata(out / "edgedata.xml")
    for h in hot:
        h["name"] = name(h["edge"])
    metrics = {
        "demand": "placeholder_random",
        "period_s": period,
        "seed": SEED,
        "warmup_s": WARMUP_S,
        "duration_s": END_S,
        "vehicles_loaded": int(stats["vehicles"].get("loaded", 0)),
        "vehicles_inserted": int(stats["vehicles"].get("inserted", 0)),
        "vehicles_running_at_end": int(stats["vehicles"].get("running", 0)),
        "vehicles_waiting_to_insert": int(stats["vehicles"].get("waiting", 0)),
        "vehicles_arrived": int(stats["trips"].get("count", 0)),
        "teleports": int(stats["teleports"].get("total", 0)),
        "teleports_jam": int(stats["teleports"].get("jam", 0)),
        "teleports_yield": int(stats["teleports"].get("yield", 0)),
        "teleports_wrong_lane": int(stats["teleports"].get("wrongLane", 0)),
        "teleports_in_recording": sum(
            1 for t in teleports if t["time"] is not None and t["time"] >= WARMUP_S
        ),
        "teleport_times": [t["time"] for t in teleports],
        "teleport_top_edges": [
            {"edge": e, "name": name(e), "count": n} for e, n in by_edge.most_common(10)
        ],
        "teleport_top_junctions": [
            {"junction": j, "count": n} for j, n in by_junction.most_common(10)
        ],
        "mean_trip_speed_mps": round(stats["trips"].get("speed", 0.0), 2),
        "mean_trip_duration_s": round(stats["trips"].get("duration", 0.0), 1),
        "mean_waiting_s": round(stats["trips"].get("waitingTime", 0.0), 1),
        "hot_spots_by_waiting_time": hot,
        "seconds_randomtrips": round(t_trips, 1),
        "seconds_sumo": round(t_sim, 1),
    }
    (out / "metrics.json").write_text(json.dumps(metrics, indent=1) + "\n", encoding="utf-8")
    return metrics


# The baseline placeholder scenario: moderate density ("lively but not gridlocked": 0 teleports
# and ~8 m/s mean trip speed in the smoke runs), 0.5 s steps so FCD can be recorded every 0.5 s.
BASELINE_PERIOD_S = LEVELS["moderate"]
STEP_S = 0.5


def run_baseline(net_path: Path = NET_PATH) -> dict:
    from sim import edge_stats
    from sim.config import CHECKS_DIR, OUT_DIR
    from sim.export_trips import export_scenario

    work = WORK_DIR / "baseline"
    fcd = work / "fcd.xml"
    metrics = simulate(work, BASELINE_PERIOD_S, net_path, step_length=STEP_S, fcd=fcd)
    t0 = time.perf_counter()
    result = export_scenario(
        "baseline",
        fcd,
        metrics,
        OUT_DIR,
        CHECKS_DIR,
        net_path,
        begin=float(WARMUP_S),
        end=float(END_S),
        dt=STEP_S,
    )
    result["seconds_export_and_checks"] = round(time.perf_counter() - t0, 1)
    result["edge_stats"] = edge_stats.export(
        "baseline",
        work / EDGE_STATS_FILE,
        net_path,
        OUT_DIR,
        begin=float(WARMUP_S),
        end=float(END_S),
    )
    result["run"] = {k: v for k, v in metrics.items() if not isinstance(v, list)}
    (work / "result.json").write_text(json.dumps(result, indent=1) + "\n", encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", choices=["smoke", "baseline"])
    ap.add_argument("--levels", nargs="+", default=["light", "moderate"], choices=list(LEVELS))
    args = ap.parse_args(argv)
    if args.mode == "baseline":
        print(json.dumps(run_baseline(), indent=1))
        return 0
    for level in args.levels:
        m = run_level(level)
        keys = [k for k in m if not isinstance(m[k], list)]
        print(json.dumps({k: m[k] for k in keys}))
        print(
            " teleports at:",
            [(t["name"] or t["edge"], t["count"]) for t in m["teleport_top_edges"]],
        )
        print(
            " hot spots:",
            [(h["name"] or h["edge"], h["waiting_s"]) for h in m["hot_spots_by_waiting_time"]],
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
