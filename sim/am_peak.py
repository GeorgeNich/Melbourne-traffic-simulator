"""Scenario `am_peak`: the real AM peak of Fri 23 Oct 2026, calibrated to SCATS counts.

Usage (from the repo root):
    uv run python -m sim.am_peak                  # final attempt: demand, SUMO, report, exports
    uv run python -m sim.am_peak run --attempt 1  # one attempt only (sim/work/am_peak/attempt1/)
    uv run python -m sim.am_peak export           # exports + report from the final attempt
    uv run python -m sim.am_peak run --attempt 2 --reuse-demand  # re-run SUMO on fitted routes
    uv run python -m sim.am_peak quick            # no-fit network check: attempt 2's fitted routes
                                                  # with sim/net/signals_real.add.xml, no FCD
To reproduce the full report (every attempt): run --attempt 1, 2, 3a, 3b, then export.

Clock: simulation second 0 = 06:30 local (Australia/Melbourne) on 23 Oct 2026; the run ends at
10800 = 09:30. 06:30-07:00 is warm-up (demand fitted to its counts too, so the network is loaded
at 07:00, but not scored); 07:00-09:30 is scored. Every export uses this one clock:

- sim/out/edge_stats_am_peak.parquet: t_bin 0..10740 (the whole run, warm-up included);
- sim/out/trips_am_peak.json: only 07:30-08:00 (inside the calibrated hour 07-08), so timestamps
  run 3600..5400 (FCD every 1 s);
- sim/out/am_peak.meta.json: duration_s 10800, demand "calibrated_counts", teleports over the
  whole run, vehicles / tunnel_vehicles of the trips file, start_local = wall-clock time of t = 0.

Road rules at the clock time (ordinary Friday, school term):
- timed bus lanes close to cars inside their hours (sim/bus_lane_hours.py): Lonsdale St south
  side 07:00-09:30, City Rd inbound 06:30-10:00; the PM-only lanes stay open; always-on bus lanes
  are closed in the network itself;
- conditional speed limits (sim/conditional_speeds.py; school zones 08:00-09:30 and Bay St
  08:00-24:00) apply from 08:00, and the edge stats use the limit in force in each minute.
Signals are SUMO's default programs from netconvert (static, 90 s cycle), not real SCATS timings.

SUMO runs through libsumo in a child process (its own Arrow build never meets pyarrow here); the
child applies the clock-time changes and records FCD for 07:30-08:00 only (sumo's own FCD output
has no end time, and the whole run at 1 s would be gigabytes).
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

from sim import calibrate as cal
from sim.config import CHECKS_DIR, NET_PATH, OUT_DIR, REPO_ROOT, SEED, WORK_DIR, sumo_env

SCENARIO = "am_peak"
WORK = WORK_DIR / SCENARIO
COUNTS_PATH = REPO_ROOT / "data" / "processed" / "counts.parquet"
FREEWAY_REF = REPO_ROOT / "data" / "processed" / "checks" / "freeway_reference.json"

STEP_S = 1.0
FCD_BEGIN = 3600  # 07:30
FCD_END = 5400  # 08:00
HOT_BEGIN = cal.WARMUP_S

# Output file names in WORK. The 60 s edgeData is NOT called edgestats60.xml: the qa contract test
# cross-checks a file of that name on the baseline clock (t_bin = sim time - 300 s), which is not
# this scenario's clock; sim/tests/test_am_peak.py does the same check on the am_peak clock.
EDGE_STATS_FILE = "edgedata60_clock0630.xml"
COUNTS_FILE = "edgecounts900.xml"
HOT_FILE = "edgedata_0700_0930.xml"
FCD_FILE = "fcd_0730_0800.xml"

# Calibration attempts: the first plus two more (the task's limit); attempt 3 was run in two
# variants. Each runs in its own work dir sim/work/am_peak/attempt<key>/; the exports come from
# FINAL_ATTEMPT, the variant with the best gate share. The report lists every attempt that ran.
_BASE = {
    "route_sampler_optimize": "3",
    "candidate_trips": cal.N_CANDIDATE_TRIPS,
    "od_shares": cal.OD_SHARES,
    "class_weight": cal.CLASS_WEIGHT,
    "vehicle_attributes": cal.VEHICLE_ATTRS,
    "time_to_teleport_s": 300,
    "step_length_s": STEP_S,
    # "static": the network's netconvert programs as built (fixed 90 s cycle);
    # "actuated": the same phase plans under SUMO's default actuated controller (see
    # actuated_programs), what the approved Phase 3 plan calls "SUMO default actuated programs".
    "signals": "static",
    "ignore_junction_blocker_s": None,
    "internal_od": "lanes x class (CLASS_WEIGHT) x length",
    "internal_class_weight": None,
    # None: free-flow candidate routes; an attempt key: also route the candidate pairs on that
    # attempt's 07:00-09:30 travel times (it is run first if missing)
    "congestion_routes_from": None,
}
_ACTUATED = {
    "signals": "actuated",
    "ignore_junction_blocker_s": 20,
}
_LOCAL_OD = {
    "internal_od": "INTERNAL_CLASS_WEIGHT x length (local streets, not arterials)",
    "internal_class_weight": cal.INTERNAL_CLASS_WEIGHT,
}
ATTEMPTS = {
    "1": {**_BASE, "change": "first attempt: free-flow candidate routes, network signals as built"},
    "2": {
        **_BASE,
        **_ACTUATED,
        "change": (
            "signals: same netconvert phase plans, SUMO default actuated control (gap-out, "
            "min 5 s / max 50 s green); junction blockers ignored after 20 s"
        ),
    },
    "3a": {
        **_BASE,
        **_ACTUATED,
        **_LOCAL_OD,
        "change": (
            "as 2, and route set: internal origins/destinations on local streets instead of "
            "arterial carriageways (vehicles were inserted into arterial queues)"
        ),
    },
    "3b": {
        **_BASE,
        **_ACTUATED,
        **_LOCAL_OD,
        "congestion_routes_from": "3a",
        "change": (
            "as 3a, and candidate routes = free-flow routes + the same OD pairs routed on the "
            "07:00-09:30 travel times of run 3a; refitted"
        ),
    },
    "4": {
        **_BASE,
        **_ACTUATED,
        **_LOCAL_OD,
        "signals": "real",
        "change": (
            "as 3a on the repaired network (2026-10-28): real signal programs (18 real-phase "
            "sites, sheet max green = reading B, split-carriageway pairing) and the verified "
            "turn bans; fresh fit"
        ),
    },
    "5": {
        **_BASE,
        **_ACTUATED,
        **_LOCAL_OD,
        "change": (
            "official Phase 3 calibration on the finished network (gate decision, PLAN "
            "2026-10-28): adaptive signal control for the scenario of record; the real signal "
            "programs (49 real-phase lights) stay a labelled data layer, selectable but not "
            "the gate"
        ),
    },
}
# George (2026-10-28): attempt 5 is the scenario of record (gate decision, PLAN decisions log);
# attempt 2 (kept 2026-10-27) and the rest stay archived as evidence.
FINAL_ATTEMPT = "5"


def work_dir(attempt: str = FINAL_ATTEMPT) -> Path:
    return WORK / f"attempt{attempt}"


# --- demand ---------------------------------------------------------------------------------


def build_demand(
    work: Path,
    net_path: Path = NET_PATH,
    settings: dict = _BASE,
    travel_times: Path | None = None,
) -> dict:
    """Candidate routes + routeSampler fit into `work`. With `travel_times` (edgeData of an
    earlier pass) the candidate pairs are also routed on those times and both route sets used."""
    import sumolib

    work.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    rows = cal.read_counts(COUNTS_PATH)
    counts = cal.approach_counts(rows, cal.scenario_start())
    kept, dropped = cal.one_count_per_edge(counts)
    sites = sorted({a.site_id for a in kept})
    held = cal.holdout_sites(sites)
    calib = {a: v for a, v in kept.items() if a.site_id not in held}
    (work / "counts_calibration.edgedata.xml").write_text(cal.edgedata_xml(calib), "utf-8")

    net = sumolib.net.readNet(str(net_path))
    counted = {a.edge_id for a in kept}
    weights = cal.od_weights(net, counted, settings.get("internal_class_weight"))
    pairs = cal.sample_od_pairs(
        weights, n=settings["candidate_trips"], shares=settings["od_shares"]
    )
    cal.write_trips(pairs, work / "candidates.trips.xml")
    cal.route_trips(net_path, work / "candidates.trips.xml", work / "candidates.routed.xml")
    routed = [work / "candidates.routed.xml"]
    if travel_times is not None:
        # departing at 07:00, inside the travel times' interval (07:00-09:30)
        cal.write_trips(pairs, work / "candidates.trips_0700.xml", depart=HOT_BEGIN)
        cal.route_trips(
            net_path,
            work / "candidates.trips_0700.xml",
            work / "candidates.congested.xml",
            weights=travel_times,
        )
        routed.append(work / "candidates.congested.xml")
    lengths = {e.getID(): e.getLength() for e in net.getEdges()}
    routes = cal.unique_routes(routed, counted, lengths)
    cal.write_candidates(routes, work / "candidates.rou.xml")
    log = cal.run_route_sampler(
        work / "candidates.rou.xml",
        work / "counts_calibration.edgedata.xml",
        work / "fitted.rou.xml",
        optimize=settings["route_sampler_optimize"],
    )
    (work / "routesampler.log").write_text(log, "utf-8")
    vehicles = cal.fitted_vehicles(work / "fitted.rou.xml")
    info = {
        "sites": len(sites),
        "approaches": len(kept),
        "approaches_dropped_shared_edge": dropped,
        "held_out_sites": held,
        "calibration_sites": [s for s in sites if s not in held],
        "calibration_approaches": len(calib),
        "held_out_approaches": len(kept) - len(calib),
        "candidate_pairs": len(pairs),
        "candidate_routes": len(routes),
        "candidate_routes_by_class": dict(sorted(Counter(c for c, _ in routes).items())),
        "candidate_routes_passing_a_count": sum(1 for _, e in routes if set(e) & counted),
        "od_edges": {k: len(v) for k, v in weights.items()},
        "fitted_vehicles": len(vehicles),
        "fitted_distinct_routes": len({tuple(e) for _, e in vehicles}),
        "seconds": round(time.perf_counter() - t0, 1),
    }
    (work / "demand.json").write_text(json.dumps(info, indent=1) + "\n", "utf-8")
    return info


# --- clock-time road rules ------------------------------------------------------------------


def schedule(net_path: Path = NET_PATH, step_s: int = 60) -> dict:
    """Changes to apply during the run, from the scenario clock: {"lanes": [[t, lane, closed]],
    "speeds": [[t, edge, m/s]]}, plus the edge limits per minute for the edge stats."""
    import sumolib

    from sim import bus_lane_hours as blh
    from sim import conditional_speeds as cs
    from sim.checks.netindex import NetIndex, read_joined, read_osm
    from sim.config import JOINED_PATH, OSM_EXTRACT

    net = sumolib.net.readNet(str(net_path))
    osm = read_osm(OSM_EXTRACT)
    idx = NetIndex(net, read_joined(JOINED_PATH))
    timed = {
        ln.getID(): ln.getParam(blh.PARAM)
        for e in net.getEdges()
        for ln in e.getLanes()
        if ln.getParam(blh.PARAM) is not None
    }
    base_speed = {e.getID(): max(ln.getSpeed() for ln in e.getLanes()) for e in net.getEdges()}
    lanes, speeds, limits = [], [], {}
    closed_prev = dict.fromkeys(timed, False)  # the network has the timed lanes open
    speed_prev: dict[str, float] = {}
    for t in range(0, cal.END_S, step_s):
        when = cal.local_time(t)
        for lane, key in sorted(timed.items()):
            c = blh.active(blh.BY_KEY[key], when)
            if c != closed_prev[lane]:
                lanes.append([t, lane, c])
                closed_prev[lane] = c
        now = cs.edge_speeds(osm, idx, when)
        for e in sorted(set(now) | set(speed_prev)):
            v = now.get(e, base_speed[e])
            if v != speed_prev.get(e, base_speed[e]):
                speeds.append([t, e, v])
        speed_prev = now
        if now:
            limits[t] = now
    return {"lanes": lanes, "speeds": speeds, "limits": limits}


# --- simulation (child process, libsumo) ----------------------------------------------------


def sumo_args(work: Path, routes: Path, net_path: Path, settings: dict) -> list[str]:
    adds = [str(work / "outputs.add.xml")]
    if settings["signals"] in ("actuated", "real"):
        adds.insert(0, str(work / "actuated.add.xml"))
    if settings["signals"] == "real":  # loaded after the defaults, so its programs are active
        from sim.signals_real import ADD_PATH

        adds.insert(1, str(ADD_PATH))
    extra = []
    if settings["ignore_junction_blocker_s"] is not None:
        extra = ["--ignore-junction-blocker", str(settings["ignore_junction_blocker_s"])]
    return [
        "-n",
        str(net_path),
        "-r",
        str(routes),
        "-a",
        ",".join(adds),
        *extra,
        "--begin",
        "0",
        "--end",
        str(cal.END_S),
        "--step-length",
        str(STEP_S),
        "--seed",
        str(SEED),
        "--time-to-teleport",
        str(settings["time_to_teleport_s"]),
        "--no-step-log",
        "--statistic-output",
        str(work / "stats.xml"),
        "--tripinfo-output",
        str(work / "tripinfo.xml"),
        "--tripinfo-output.write-undeparted",
        "--summary-output",
        str(work / "summary.xml"),
        "--log",
        str(work / "sumo.log"),
    ]


ACT_MIN_DUR_S = 5  # netconvert's defaults for actuated programs (--tls.min-dur / --tls.max-dur)
ACT_MAX_DUR_S = 50


def actuated_programs(net_path: Path, dst: Path) -> int:
    """An additional file that runs every traffic light's netconvert phase plan under SUMO's
    default actuated controller (program id "actuated", loaded last, so active): green phases get
    minDur / maxDur, yellow and red phases keep their durations. Phases, links and the network
    are unchanged."""
    import sumolib

    net = sumolib.net.readNet(str(net_path), withPrograms=True)
    lines = ["<additional>"]
    n = 0
    for tl in sorted(net.getTrafficLights(), key=lambda t: t.getID()):
        for _, prog in sorted(tl.getPrograms().items()):
            lines.append(
                f'    <tlLogic id="{tl.getID()}" type="actuated" programID="actuated" offset="0">'
            )
            for ph in prog.getPhases():
                green = ("G" in ph.state or "g" in ph.state) and "y" not in ph.state
                dur = f"{ph.duration:g}"
                if green:
                    lines.append(
                        f'        <phase duration="{dur}" minDur="{ACT_MIN_DUR_S}" '
                        f'maxDur="{ACT_MAX_DUR_S}" state="{ph.state}"/>'
                    )
                else:
                    lines.append(f'        <phase duration="{dur}" state="{ph.state}"/>')
            lines.append("    </tlLogic>")
            n += 1
    lines.append("</additional>")
    dst.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return n


def outputs_additional(work: Path) -> None:
    end = cal.END_S
    (work / "outputs.add.xml").write_text(
        "<additional>\n"
        f'    <edgeData id="stats60" file="{EDGE_STATS_FILE}" begin="0" end="{end}" '
        'period="60" excludeEmpty="true"/>\n'
        f'    <edgeData id="counts900" file="{COUNTS_FILE}" begin="0" end="{end}" '
        f'period="{cal.INTERVAL_S}"/>\n'
        f'    <edgeData id="hot" file="{HOT_FILE}" begin="{HOT_BEGIN}" end="{end}"/>\n'
        "</additional>\n",
        encoding="utf-8",
    )


def _child(work: Path, routes: Path, net_path: Path) -> None:
    """Runs inside the child process: SUMO via libsumo with the clock-time changes and FCD."""
    import libsumo as ls

    sched = json.loads((work / "schedule.json").read_text(encoding="utf-8"))
    settings = json.loads((work / "settings.json").read_text(encoding="utf-8"))
    actions: dict[int, list] = {}
    for t, lane, closed in sched["lanes"]:
        actions.setdefault(int(t), []).append(("lane", lane, closed))
    for t, edge, v in sched["speeds"]:
        actions.setdefault(int(t), []).append(("speed", edge, v))
    ls.start(["sumo", *sumo_args(work, routes, net_path, settings)])
    orig_disallowed = {}
    for _t, lane, _ in sched["lanes"]:
        orig_disallowed.setdefault(lane, list(ls.lane.getDisallowed(lane)))
    v_pos, v_lane, v_lpos, v_speed = (
        ls.constants.VAR_POSITION,
        ls.constants.VAR_LANE_ID,
        ls.constants.VAR_LANEPOSITION,
        ls.constants.VAR_SPEED,
    )
    variables = [v_pos, v_lane, v_lpos, v_speed]
    applied = []
    fcd_on = settings.get("fcd", True)
    fcd_begin, fcd_end = (FCD_BEGIN, FCD_END) if fcd_on else (-1, -2)
    with (work / FCD_FILE).open("w", encoding="utf-8") as fcd:
        fcd.write('<fcd-export source="sim/am_peak.py libsumo" period="1">\n')
        t = 0
        while t < cal.END_S:
            for kind, obj, val in actions.get(t, []):
                if kind == "lane":
                    dis = orig_disallowed[obj] + (["passenger"] if val else [])
                    ls.lane.setDisallowed(obj, dis)
                else:
                    ls.edge.setMaxSpeed(obj, float(val))
                applied.append([t, kind, obj, val])
            ls.simulationStep()
            t = int(round(ls.simulation.getTime()))
            if t == fcd_begin:
                for vid in ls.vehicle.getIDList():
                    ls.vehicle.subscribe(vid, variables)
            if fcd_begin < t <= fcd_end:
                for vid in ls.simulation.getDepartedIDList():
                    ls.vehicle.subscribe(vid, variables)
            if fcd_begin <= t <= fcd_end:
                res = ls.vehicle.getAllSubscriptionResults()
                fcd.write(f'    <timestep time="{t:.2f}">\n')
                for vid in sorted(res):
                    r = res[vid]
                    x, y = r[v_pos]
                    fcd.write(
                        f'        <vehicle id="{vid}" x="{x:.2f}" y="{y:.2f}" '
                        f'speed="{r[v_speed]:.2f}" pos="{r[v_lpos]:.2f}" lane="{r[v_lane]}"/>\n'
                    )
                fcd.write("    </timestep>\n")
            if t == fcd_end:
                for vid in ls.vehicle.getIDList():
                    ls.vehicle.unsubscribe(vid)
        fcd.write("</fcd-export>\n")
    ls.close()
    (work / "applied_rules.json").write_text(json.dumps(applied, indent=0) + "\n", "utf-8")


def simulate(work: Path, settings: dict, net_path: Path = NET_PATH) -> dict:
    """SUMO run of the fitted demand (child process); returns its metrics (also metrics.json)."""
    import sumolib

    from sim.run import junction_of_edge, parse_edgedata, parse_statistics, parse_teleports

    (work / "settings.json").write_text(json.dumps(settings, indent=1) + "\n", "utf-8")
    if settings["signals"] in ("actuated", "real"):
        actuated_programs(net_path, work / "actuated.add.xml")
    sched = schedule(net_path)
    (work / "schedule.json").write_text(
        json.dumps({k: sched[k] for k in ("lanes", "speeds")}) + "\n", "utf-8"
    )
    outputs_additional(work)
    t0 = time.perf_counter()
    subprocess.run(
        [sys.executable, "-m", "sim.am_peak", "_child", str(work), str(net_path)],
        check=True,
        env={**sumo_env(), "PYTHONPATH": str(REPO_ROOT)},
        cwd=REPO_ROOT,
    )
    t_sim = time.perf_counter() - t0

    net = sumolib.net.readNet(str(net_path))
    stats = parse_statistics(work / "stats.xml")
    teleports = parse_teleports((work / "sumo.log").read_text(encoding="utf-8", errors="replace"))

    def name(eid: str) -> str | None:
        return net.getEdge(eid).getName() if net.hasEdge(eid) else None

    by_edge = Counter(t["edge"] for t in teleports)
    by_junction = Counter(junction_of_edge(net, t["edge"]) for t in teleports)
    by_reason = Counter(t["reason"] for t in teleports)
    hot = parse_edgedata(work / HOT_FILE, top=15)
    for h in hot:
        h["name"] = name(h["edge"])
    summary = _summary_series(work / "summary.xml")
    metrics = {
        "vehicles_loaded": int(stats["vehicles"].get("loaded", 0)),
        "vehicles_inserted": int(stats["vehicles"].get("inserted", 0)),
        "vehicles_running_at_end": int(stats["vehicles"].get("running", 0)),
        "vehicles_waiting_to_insert_at_end": int(stats["vehicles"].get("waiting", 0)),
        # arrived = inserted - still running at 09:30 (SUMO's vehicleTripStatistics count also
        # includes the vehicles still running, so it is not used for this)
        "vehicles_arrived": int(stats["vehicles"].get("inserted", 0))
        - int(stats["vehicles"].get("running", 0)),
        "collisions": _collisions(work / "stats.xml"),
        "teleports": int(stats["teleports"].get("total", 0)),
        "teleports_jam": int(stats["teleports"].get("jam", 0)),
        "teleports_yield": int(stats["teleports"].get("yield", 0)),
        "teleports_wrong_lane": int(stats["teleports"].get("wrongLane", 0)),
        "teleports_by_reason": dict(sorted(by_reason.items())),
        "teleports_fcd_window": sum(
            1 for t in teleports if t["time"] is not None and FCD_BEGIN <= t["time"] <= FCD_END
        ),
        "teleport_top_edges": [
            {"edge": e, "name": name(e), "count": n} for e, n in by_edge.most_common(15)
        ],
        "teleport_top_junctions": [
            {"junction": j, "count": n} for j, n in by_junction.most_common(10)
        ],
        # trip statistics over every inserted vehicle (finished or still running at 09:30)
        "mean_trip_speed_mps": round(stats["trips"].get("speed", 0.0), 2),
        "mean_trip_duration_s": round(stats["trips"].get("duration", 0.0), 1),
        "mean_waiting_s": round(stats["trips"].get("waitingTime", 0.0), 1),
        "mean_depart_delay_s": round(stats["trips"].get("departDelay", 0.0), 1),
        "network_by_quarter_hour": summary,
        "hot_spots_by_waiting_time_0700_0930": hot,
        "seconds_sumo": round(t_sim, 1),
    }
    (work / "metrics.json").write_text(json.dumps(metrics, indent=1) + "\n", "utf-8")
    return metrics


def _collisions(path: Path) -> int:
    import xml.etree.ElementTree as ET

    el = ET.parse(path).getroot().find("safety")
    return int(el.get("collisions", 0)) if el is not None else 0


def _summary_series(path: Path) -> list[dict]:
    """Running / halting / waiting-to-insert vehicles and mean speed every 15 min."""
    import xml.etree.ElementTree as ET

    out = []
    for _, el in ET.iterparse(path, events=("end",)):
        if el.tag != "step":
            continue
        t = float(el.get("time"))
        if t % cal.INTERVAL_S == 0:
            out.append(
                {
                    "t": int(t),
                    "local": cal.local_time(t).strftime("%H:%M"),
                    "running": int(el.get("running")),
                    "halting": int(el.get("halting")),
                    "waiting_to_insert": int(el.get("waiting")),
                    "mean_speed_mps": round(float(el.get("meanSpeed")), 2),
                    "teleports_so_far": int(el.get("teleports", 0)),
                }
            )
        el.clear()
    return out


# --- calibration report ---------------------------------------------------------------------


def report(work: Path, settings: dict, net_path: Path = NET_PATH, run_info: dict | None = None):
    """The calibration report of one attempt (deterministic: wall-clock timings are kept apart
    under "timings_s")."""
    import sumolib

    rows = cal.read_counts(COUNTS_PATH)
    start = cal.scenario_start()
    counts = cal.approach_counts(rows, start)
    kept, _ = cal.one_count_per_edge(counts)
    demand = json.loads((work / "demand.json").read_text(encoding="utf-8"))
    held = set(demand["held_out_sites"])
    calib = {a: v for a, v in kept.items() if a.site_id not in held}
    hold = {a: v for a, v in kept.items() if a.site_id in held}
    simulated = cal.read_interval_counts(work / COUNTS_FILE, "left")
    fitted = cal.fitted_counts(
        cal.fitted_vehicles(work / "fitted.rou.xml"), {a.edge_id for a in kept}
    )
    cal_rows, hold_rows = cal.score(calib, simulated), cal.score(hold, simulated)
    fit_rows = cal.score(calib, fitted)

    net = sumolib.net.readNet(str(net_path))
    used = cal.read_interval_counts(work / COUNTS_FILE, "used")
    all_day = cal.approach_counts(rows, start.replace(hour=0, minute=0), end_s=86400)
    ref = json.loads(FREEWAY_REF.read_text(encoding="utf-8"))
    # shares on the all-day clock (t from midnight): 07:00-09:30 = [25200, 34200)
    window = {a: sum(n for b, n in v.items() if 25200 <= b < 34200) for a, v in all_day.items()}
    day = {a: sum(v.values()) for a, v in all_day.items() if len(v) == 96}
    freeway = cal.freeway_checks(ref, net, used, day, window)
    wg = _west_gate_boundary(net, used)
    tirtl = cal.tirtl_check(ref, wg, cal.time(7, 0), cal.time(9, 30))

    for r in (*cal_rows, *hold_rows):
        r["name"] = net.getEdge(r["edge_id"]).getName()
        r["sumo_lanes"] = net.getEdge(r["edge_id"]).getLaneNumber()
    rep = {
        "scenario": SCENARIO,
        "demand": "calibrated_counts",
        "day": cal.SCENARIO_DATE.isoformat(),
        "clock": "t = 0 at 06:30 Australia/Melbourne; warm-up 06:30-07:00; scored 07:00-09:30",
        "counts": {
            "file": "data/processed/counts.parquet",
            "sha256": _sha256(COUNTS_PATH),
            "note": (
                "Real DTP SCATS stop-line counts for the day; loops in kerbside bus lanes "
                "(e.g. site 2902) include buses, which this car-only model does not simulate."
            ),
        },
        "measure": (
            "modelled count = vehicles leaving the approach edge into the junction (SUMO edgeData "
            "`left`) per interval, i.e. crossing the stop line; GEH on hourly flows, 09:00-09:30 "
            "scaled x2"
        ),
        "gate": {
            "rule": "GEH < 5 on >= 85% of calibration approach-hours, 07-08 and 08-09",
            "share": cal.summarise(cal_rows)["gate"]["share"],
            "pass": (cal.summarise(cal_rows)["gate"]["share"] or 0) >= cal.GATE_SHARE,
        },
        "settings": settings,
        "demand_build": demand,
        "calibration": cal.summarise(cal_rows),
        "validation_held_out": cal.summarise(hold_rows),
        "route_sampler_fit_by_departure_interval": cal.summarise(fit_rows),
        "run": run_info or {},
        "freeway_aadt_2019": freeway,
        "tirtl_west_gate": tirtl,
        "lane_count_notes": _lane_notes(rows, kept, net),
        "geh_calibration": cal_rows,
        "geh_held_out": hold_rows,
    }
    return rep


def _west_gate_boundary(net, used: dict) -> dict:
    """Modelled 07:00-09:30 vehicles on the West Gate boundary edges (entry / exit)."""
    out = {}
    for e in net.getEdges():
        if (
            "West Gate" in (e.getName() or "")
            and cal.road_class(e) == "motorway"
            and (e.is_fringe(e._incoming) or e.is_fringe(e._outgoing))
        ):
            side = "entry" if e.is_fringe(e._incoming) else "exit"
            n = sum(v for t, v in used.get(e.getID(), {}).items() if t >= cal.WARMUP_S)
            out[e.getID()] = {"name": e.getName(), "side": side, "vehicles_0700_0930": n}
    return dict(sorted(out.items()))


def _lane_notes(rows, kept, net) -> list[dict]:
    """Approaches whose loop count (detectors) exceeds the SUMO car lanes (possible lane-count
    mismatch, from data/processed/checks/counts_report.json); reported, network unchanged."""
    path = REPO_ROOT / "data" / "processed" / "checks" / "counts_report.json"
    rep = json.loads(path.read_text(encoding="utf-8"))
    out = []
    edges = {a.edge_id for a in kept}
    for ap in rep["per_day"][0]["approaches_used"]:
        e = ap["edge_id"]
        if e not in edges or not net.hasEdge(e):
            continue
        lanes = sum(1 for ln in net.getEdge(e).getLanes() if ln.allows("passenger"))
        if len(ap["detectors"]) > lanes:
            out.append(
                {
                    "approach_id": ap["approach_id"],
                    "edge_id": e,
                    "street": ap["street"],
                    "loops": len(ap["detectors"]),
                    "sumo_car_lanes": lanes,
                }
            )
    return out


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- exports --------------------------------------------------------------------------------


def export(work: Path, net_path: Path = NET_PATH, metrics: dict | None = None) -> dict:
    from sim import edge_stats
    from sim.export_trips import export_scenario

    metrics = metrics or json.loads((work / "metrics.json").read_text(encoding="utf-8"))
    sched = schedule(net_path)
    limits = {int(t): v for t, v in sched["limits"].items()}

    def limit_at(edge: str, b: float) -> float | None:
        return limits.get(int(b) - int(b) % 60, {}).get(edge)

    t0 = time.perf_counter()
    stats = edge_stats.export(
        SCENARIO, work / EDGE_STATS_FILE, net_path, OUT_DIR, 0.0, float(cal.END_S), None, limit_at
    )
    trips = export_scenario(
        SCENARIO,
        work / FCD_FILE,
        metrics,
        OUT_DIR,
        CHECKS_DIR,
        net_path,
        begin=float(FCD_BEGIN),
        end=float(FCD_END),
        dt=STEP_S,
        demand="calibrated_counts",
        clock0=0.0,
        duration_s=cal.END_S,
        teleports=metrics["teleports"],
        start_local=cal.scenario_start().isoformat(),
    )
    return {
        "edge_stats": stats,
        "trips": trips,
        "seconds_export": round(time.perf_counter() - t0, 1),
    }


def write_report(rep: dict) -> Path:
    CHECKS_DIR.mkdir(parents=True, exist_ok=True)
    path = CHECKS_DIR / f"{SCENARIO}_calibration.json"
    path.write_text(json.dumps(rep, indent=1) + "\n", encoding="utf-8")
    return path


def run_attempt(attempt: str, net_path: Path = NET_PATH, reuse_demand: bool = False) -> dict:
    """Demand + SUMO + calibration report of one attempt (in its work dir, report.json).
    `reuse_demand`: keep the attempt's existing fitted routes (fitted.rou.xml), only re-run SUMO."""
    settings = {"attempt": attempt, **ATTEMPTS[attempt]}
    work = work_dir(attempt)
    t0 = time.perf_counter()
    travel_times = None
    if settings["congestion_routes_from"] and not reuse_demand:
        src = settings["congestion_routes_from"]
        if not (work_dir(src) / "report.json").exists():
            run_attempt(src, net_path)
        travel_times = work_dir(src) / HOT_FILE
    if reuse_demand and (work / "fitted.rou.xml").exists():
        demand = json.loads((work / "demand.json").read_text(encoding="utf-8"))
    else:
        demand = build_demand(work, net_path, settings, travel_times)
    metrics = simulate(work, settings, net_path)
    rep = report(work, settings, net_path, run_info=metrics)
    timings = {"demand": demand.get("seconds"), "sumo": metrics.get("seconds_sumo")}
    rep["demand_build"].pop("seconds", None)
    rep["run"].pop("seconds_sumo", None)
    timings["attempt_total"] = round(time.perf_counter() - t0, 1)
    rep["timings_s"] = timings
    (work / "report.json").write_text(json.dumps(rep, indent=1) + "\n", "utf-8")
    return rep


QUICK = "quick_real_signals"


def quick_test(
    src: str = FINAL_ATTEMPT, net_path: Path = NET_PATH, name: str = QUICK, signals: str = "real"
) -> dict:
    """No-fit network check: `src`'s fitted routes, unchanged, on the current network with the
    real signal programs (sim/net/signals_real.add.xml); edge counts only, no FCD. Work dir
    sim/work/am_peak/quick_real_signals/ (report.json there)."""
    import shutil

    work = WORK_DIR / SCENARIO / name
    work.mkdir(parents=True, exist_ok=True)
    for f in ("fitted.rou.xml", "demand.json"):
        shutil.copyfile(work_dir(src) / f, work / f)
    settings = {
        "attempt": name,
        **ATTEMPTS[src],
        "signals": signals,
        "fcd": False,
        "change": f"attempt {src}'s fitted routes, unchanged; sheet signals and lane arrows",
    }
    metrics = simulate(work, settings, net_path)
    rep = report(work, settings, net_path, run_info=metrics)
    rep["run"].pop("seconds_sumo", None)
    rep["timings_s"] = {"sumo": metrics.get("seconds_sumo")}
    (work / "report.json").write_text(json.dumps(rep, indent=1) + "\n", "utf-8")
    (work / FCD_FILE).unlink(missing_ok=True)
    return rep


def attempt_summary(rep: dict) -> dict:
    run = rep["run"]
    cw, hw = rep["calibration"]["per_window"], rep["validation_held_out"]["per_window"]
    line = (
        f"attempt {rep['settings']['attempt']}: GEH<5 calibration "
        f"07-08 {cw['07-08']['share']:.0%}, "
        f"08-09 {cw['08-09']['share']:.0%}; held-out 07-08 {hw['07-08']['share']:.0%}, "
        f"08-09 {hw['08-09']['share']:.0%}; teleports {run['teleports']} (06:30-09:30)"
    )
    return {
        "summary": line,
        "settings": rep["settings"],
        "gate_share": rep["gate"]["share"],
        "calibration_per_window": rep["calibration"]["per_window"],
        "held_out_per_window": rep["validation_held_out"]["per_window"],
        "vehicles_inserted": run["vehicles_inserted"],
        "vehicles_arrived": run["vehicles_arrived"],
        "vehicles_waiting_to_insert_at_end": run["vehicles_waiting_to_insert_at_end"],
        "teleports": run["teleports"],
        "mean_trip_speed_mps": run["mean_trip_speed_mps"],
    }


STATUS = (
    "Official calibration on the repaired network (attempt 5, gate decision PLAN 2026-10-28): "
    "adaptive signal control; the real signal programs (49 real-phase lights of 387) are a "
    "labelled data layer. 08-09 remains a known limitation (no trams/pedestrians, SCATS "
    "coordination not published)."
)


def finalise(net_path: Path = NET_PATH) -> dict:
    """Exports from FINAL_ATTEMPT + sim/out/checks/am_peak_calibration.json with every attempt
    that has been run."""
    work = work_dir(FINAL_ATTEMPT)
    rep = json.loads((work / "report.json").read_text(encoding="utf-8"))
    metrics = json.loads((work / "metrics.json").read_text(encoding="utf-8"))
    exp = export(work, net_path, metrics)
    rep["timings_s"]["export"] = exp.pop("seconds_export")
    rep["exports"] = exp
    rep["attempts"] = {
        str(n): attempt_summary(json.loads((work_dir(n) / "report.json").read_text("utf-8")))
        for n in sorted(ATTEMPTS)
        if (work_dir(n) / "report.json").exists()
    }
    rep["final_attempt"] = FINAL_ATTEMPT
    rep["status"] = STATUS
    rep["replay_window"] = {"sim_s": [FCD_BEGIN, FCD_END], "local": "07:30-08:00"}
    write_report(rep)
    return rep


def main(argv: list[str] | None = None) -> int:
    import argparse

    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "_child":
        _child(Path(argv[1]), Path(argv[1]) / "fitted.rou.xml", Path(argv[2]))
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("mode", nargs="?", default="all", choices=["all", "run", "export", "quick"])
    ap.add_argument("--attempt", default=FINAL_ATTEMPT, choices=sorted(ATTEMPTS))
    ap.add_argument(
        "--reuse-demand", action="store_true", help="keep the fitted routes, only re-run SUMO"
    )
    args = ap.parse_args(argv)
    if args.mode == "quick":
        rep = quick_test(args.attempt)
        print(json.dumps(attempt_summary(rep), indent=1))
        return 0
    if args.mode in ("all", "run"):
        rep = run_attempt(args.attempt, reuse_demand=args.reuse_demand)
        keys = ("gate", "calibration", "validation_held_out", "timings_s")
        print(json.dumps({k: rep[k] for k in keys}, indent=1))
    if args.mode in ("all", "export"):
        rep = finalise()
        print(json.dumps(rep["exports"], indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
