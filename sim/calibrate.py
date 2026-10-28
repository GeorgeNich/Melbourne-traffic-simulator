"""Calibrated AM-peak demand for scenario `am_peak`: real SCATS counts -> routeSampler -> GEH.

Input: data/processed/counts.parquet (data-engineer; real DTP SCATS loop counts for one day, 15-min
bins, Australia/Melbourne local time, one row per approach edge and bin). Nothing here invents a
count: every number fitted or scored is a row of that file.

Clock: the scenario runs 06:30-09:30 local on SCENARIO_DATE; simulation time t = 0 is 06:30, so a
15-min count bin starting at 07:15 is the interval [2700, 3600). 06:30-07:00 is warm-up (fitted,
so the network is not empty at 07:00, but not scored); 07:00-09:30 is scored.

Steps (sim/am_peak.py drives them):

1. `approach_counts`: the counts of each approach (site_id, edge_id, direction) in the window.
2. `one_count_per_edge`: one approach per SUMO edge. If several sites ever map to the same edge
   (none do on 23 Oct 2026) ONE count is kept, never a sum: the approach with the most bins in the
   window, then the lowest (site_id, direction). Dropped approaches are listed in the report.
3. `holdout_sites`: a random HOLDOUT_FRACTION of the SITES (fixed seed), never used in fitting.
4. `edgedata_xml`: the calibration counts as a SUMO edgeData file (one <interval> per 15 min,
   attribute `entered`), the input routeSampler reads. A missing bin (detector alarm) is left out,
   never written as zero.
5. Candidate routes (`od_weights`, `sample_od_pairs`, duarouter): a broad, seeded route set over
   four OD classes (boundary->boundary, boundary->internal, internal->boundary, internal->internal).
   Boundary edges are weighted by lanes x road class, so freeway / arterial entries carry most of
   the candidates. These weights only shape the CANDIDATE set; routeSampler decides the volumes.
6. routeSampler.py fits route counts per 15-min interval to the calibration counts.
7. `score`: GEH per approach per hour (07-08, 08-09; 09:00-09:30 scaled x2) from the simulated
   count = vehicles that LEFT the approach edge into the junction in the interval (SUMO edgeData
   `left`): what a stop-line loop counts. Candidate routes never END on a counted edge, so every
   fitted vehicle that uses a counted edge crosses its stop line.
"""

from __future__ import annotations

import json
import math
import random
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from sim.config import SEED, SUMO_TOOLS, sumo_env

TZ = ZoneInfo("Australia/Melbourne")
SCENARIO_DATE = date(2026, 10, 23)  # Fri 23 Oct 2026: the latest real weekday in counts.parquet
START_LOCAL = time(6, 30)
WARMUP_S = 1800  # 06:30-07:00
END_S = 10800  # 09:30
INTERVAL_S = 900  # SCATS bins
HOLDOUT_FRACTION = 0.15
HOLDOUT_SEED = SEED
GEH_OK = 5.0
GATE_SHARE = 0.85

# Scored windows: (label, begin s, end s, scale to an hourly flow).
SCORE_WINDOWS = (
    ("07-08", 1800, 5400, 1.0),
    ("08-09", 5400, 9000, 1.0),
    ("09-0930", 9000, 10800, 2.0),
)
GATE_WINDOWS = ("07-08", "08-09")


def scenario_start(day: date = SCENARIO_DATE) -> datetime:
    return datetime.combine(day, START_LOCAL, tzinfo=TZ)


def local_time(t: float, day: date = SCENARIO_DATE) -> datetime:
    """Wall-clock time (Australia/Melbourne) of scenario second `t`."""
    return scenario_start(day) + timedelta(seconds=t)


@dataclass(frozen=True, order=True)
class Approach:
    site_id: str
    edge_id: str
    direction: str

    @property
    def key(self) -> str:
        return f"{self.site_id}:{self.direction}:{self.edge_id}"


# --- counts ---------------------------------------------------------------------------------


def read_counts(path: Path) -> list[dict]:
    """counts.parquet rows. Read through a file object: with libsumo loaded in the same process,
    pyarrow's path-based filesystem fails (libsumo bundles its own Arrow)."""
    import pyarrow.parquet as pq

    with path.open("rb") as fh:
        return pq.read_table(fh).to_pylist()


def approach_counts(
    rows: list[dict],
    start: datetime,
    end_s: int = END_S,
    interval_s: int = INTERVAL_S,
) -> dict[Approach, dict[int, int]]:
    """{approach: {interval start t: vehicles}} for the bins inside [start, start + end_s).

    `bin_start` must be timezone-aware; a bin is used only if it lies on the interval grid."""
    out: dict[Approach, dict[int, int]] = defaultdict(dict)
    for r in rows:
        b = r["bin_start"]
        if b.tzinfo is None:
            raise ValueError("bin_start must be timezone-aware (Australia/Melbourne)")
        t = (b - start).total_seconds()
        if not 0 <= t < end_s:
            continue
        if t % interval_s:
            raise ValueError(f"bin {b} is off the {interval_s} s grid")
        a = Approach(str(r["site_id"]), str(r["edge_id"]), str(r["direction"]))
        if int(t) in out[a]:
            raise ValueError(f"duplicate bin {b} for {a}")
        out[a][int(t)] = int(r["vehicles"])
    return dict(out)


def site_key(site_id: str) -> tuple:
    """Sort key: numeric site ids numerically (SCATS site numbers), others after them."""
    return (0, int(site_id), "") if site_id.isdigit() else (1, 0, site_id)


def one_count_per_edge(
    counts: dict[Approach, dict[int, int]],
) -> tuple[dict[Approach, dict[int, int]], list[dict]]:
    """At most one approach per edge (never a sum of two sites' counts): the one with the most
    bins, then the lowest (site_id, direction). Returns (kept, dropped)."""
    by_edge: dict[str, list[Approach]] = defaultdict(list)
    for a in counts:
        by_edge[a.edge_id].append(a)
    kept, dropped = {}, []
    for edge, apps in sorted(by_edge.items()):
        apps.sort(key=lambda a: (-len(counts[a]), site_key(a.site_id), a.direction))
        kept[apps[0]] = counts[apps[0]]
        for a in apps[1:]:
            dropped.append(
                {
                    "approach": a.key,
                    "edge_id": edge,
                    "kept": apps[0].key,
                    "reason": "edge already counted by another site (one count per edge)",
                }
            )
    return kept, dropped


def holdout_sites(sites, fraction: float = HOLDOUT_FRACTION, seed: int = HOLDOUT_SEED) -> list[str]:
    """A fixed random `fraction` of the sites (rounded, at least 1), held out of fitting."""
    pool = sorted(set(sites))
    k = max(1, round(fraction * len(pool)))
    return sorted(random.Random(seed).sample(pool, k))


def edgedata_xml(
    counts: dict[Approach, dict[int, int]],
    begin: int = 0,
    end: int = END_S,
    interval_s: int = INTERVAL_S,
    attr: str = "entered",
) -> str:
    """SUMO edgeData with one <interval> per bin; missing bins are omitted (not zero)."""
    lines = ["<data>"]
    for b in range(begin, end, interval_s):
        lines.append(f'    <interval id="counts" begin="{b}" end="{b + interval_s}">')
        for a in sorted(counts, key=lambda a: a.edge_id):
            if b in counts[a]:
                lines.append(f'        <edge id="{a.edge_id}" {attr}="{counts[a][b]}"/>')
        lines.append("    </interval>")
    lines.append("</data>")
    return "\n".join(lines) + "\n"


# --- scoring --------------------------------------------------------------------------------


def geh(m: float, c: float) -> float:
    """GEH statistic of a modelled hourly flow m against a counted hourly flow c."""
    if m < 0 or c < 0:
        raise ValueError("flows must be non-negative")
    if m + c == 0:
        return 0.0
    return math.sqrt(2.0 * (m - c) ** 2 / (m + c))


def read_interval_counts(edgedata: Path, attr: str = "left") -> dict[str, dict[int, int]]:
    """{edge: {interval begin: count}} from a SUMO edgeData file (`left` = crossed the stop line
    into the junction; `entered` + `departed` = used the edge)."""
    out: dict[str, dict[int, int]] = defaultdict(dict)
    for _, iv in ET.iterparse(edgedata, events=("end",)):
        if iv.tag != "interval":
            continue
        b = int(round(float(iv.get("begin"))))
        for el in iv.iter("edge"):
            if attr == "used":
                v = float(el.get("entered", 0) or 0) + float(el.get("departed", 0) or 0)
            else:
                v = float(el.get(attr, 0) or 0)
            out[el.get("id")][b] = int(round(v))
        iv.clear()
    return dict(out)


def score(
    counts: dict[Approach, dict[int, int]],
    simulated: dict[str, dict[int, int]],
    windows=SCORE_WINDOWS,
    interval_s: int = INTERVAL_S,
) -> list[dict]:
    """One row per approach and window: counted and modelled hourly flows and their GEH.

    A window with a missing count bin is reported with geh None (not scored), never filled."""
    rows = []
    for a in sorted(counts):
        obs = counts[a]
        sim = simulated.get(a.edge_id, {})
        for label, b, e, scale in windows:
            bins = range(b, e, interval_s)
            missing = [t for t in bins if t not in obs]
            c = sum(obs.get(t, 0) for t in bins) * scale
            m = sum(sim.get(t, 0) for t in bins) * scale
            rows.append(
                {
                    "site_id": a.site_id,
                    "edge_id": a.edge_id,
                    "direction": a.direction,
                    "window": label,
                    "counted_vph": round(c, 1),
                    "modelled_vph": round(m, 1),
                    "geh": None if missing else round(geh(m, c), 2),
                    "missing_bins": len(missing),
                }
            )
    return rows


def summarise(rows: list[dict], gate_windows=GATE_WINDOWS) -> dict:
    """Share of approach-hours with GEH < 5, per window and over the gate windows."""
    out: dict = {"per_window": {}}
    for label in sorted({r["window"] for r in rows}):
        sc = [r["geh"] for r in rows if r["window"] == label and r["geh"] is not None]
        out["per_window"][label] = _share(sc)
    gate = [r["geh"] for r in rows if r["window"] in gate_windows and r["geh"] is not None]
    out["gate_windows"] = list(gate_windows)
    out["gate"] = _share(gate)
    # site level: a site passes a window when every scored approach of it has GEH < 5
    site_ok: dict[tuple[str, str], bool] = {}
    for r in rows:
        if r["geh"] is None or r["window"] not in gate_windows:
            continue
        k = (r["site_id"], r["window"])
        site_ok[k] = site_ok.get(k, True) and r["geh"] < GEH_OK
    out["sites_all_approaches_ok"] = {
        "site_hours": len(site_ok),
        "ok": sum(site_ok.values()),
        "share": round(sum(site_ok.values()) / len(site_ok), 4) if site_ok else None,
    }
    return out


def _share(values: list[float]) -> dict:
    n = len(values)
    ok = sum(1 for v in values if v < GEH_OK)
    vs = sorted(values)
    return {
        "n": n,
        "geh_lt_5": ok,
        "share": round(ok / n, 4) if n else None,
        "median_geh": round(vs[n // 2], 2) if n else None,
        "max_geh": round(vs[-1], 2) if n else None,
    }


# --- candidate routes -----------------------------------------------------------------------

# Road-class weight per lane, for picking candidate origins and destinations.
CLASS_WEIGHT = {
    "motorway": 6.0,
    "trunk": 5.0,
    "primary": 4.0,
    "secondary": 3.0,
    "tertiary": 2.0,
    "motorway_link": 2.0,
    "trunk_link": 2.0,
    "primary_link": 1.5,
    "secondary_link": 1.0,
    "tertiary_link": 1.0,
    "unclassified": 1.0,
    "residential": 0.5,
    "living_street": 0.1,
    "service": 0.1,
}
# Internal origins / destinations (garages, kerbside, car parks) are not on fast roads.
NO_INTERNAL_OD = {"motorway", "motorway_link", "trunk_link"}
INTERNAL_MIN_LENGTH_M = 20.0
# Shares of the candidate OD pairs per class (not of the fitted demand).
OD_SHARES = {"b2b": 0.30, "b2i": 0.30, "i2b": 0.25, "i2i": 0.15}
N_CANDIDATE_TRIPS = 40000
MIN_ROUTE_LENGTH_M = 300.0


def road_class(edge) -> str:
    return (edge.getType() or "").split(".")[-1]


# Attempt 3: internal origins / destinations (garages, car parks, kerbside) sit on local streets
# and laneways, hardly on arterial carriageways; weight per 100 m by class, no lane factor.
INTERNAL_CLASS_WEIGHT = {
    "residential": 1.0,
    "unclassified": 1.0,
    "living_street": 0.5,
    "service": 0.5,
    "tertiary": 0.7,
    "tertiary_link": 0.3,
    "secondary": 0.4,
    "secondary_link": 0.2,
    "primary": 0.2,
    "primary_link": 0.1,
    "trunk": 0.05,
}


def od_weights(
    net, counted_edges: set[str], internal_class_weight: dict[str, float] | None = None
) -> dict[str, list[tuple[str, float]]]:
    """Candidate origin / destination edges with weights, per kind:
    `entry` / `exit` (network boundary: no incoming / outgoing connection except a U-turn) and
    `internal_src` / `internal_dst` (edges inside, weight also x length / 100 m, capped at 2;
    with `internal_class_weight` the internal weight is that class weight x length / 100 m
    instead, without the lane factor).
    A counted edge is never a destination (its count is a stop-line count)."""
    out: dict[str, list[tuple[str, float]]] = {
        "entry": [],
        "exit": [],
        "internal_src": [],
        "internal_dst": [],
    }
    for e in sorted(net.getEdges(), key=lambda e: e.getID()):
        if not e.allows("passenger"):
            continue
        cls = road_class(e)
        w = CLASS_WEIGHT.get(cls, 0.1) * sum(1 for ln in e.getLanes() if ln.allows("passenger"))
        if w <= 0:
            continue
        is_entry = e.is_fringe(e._incoming)
        is_exit = e.is_fringe(e._outgoing)
        if is_entry and not is_exit:
            out["entry"].append((e.getID(), w))
        if is_exit and not is_entry and e.getID() not in counted_edges:
            out["exit"].append((e.getID(), w))
        if (
            not is_entry
            and not is_exit
            and cls not in NO_INTERNAL_OD
            and e.getLength() >= INTERNAL_MIN_LENGTH_M
        ):
            per_100m = w if internal_class_weight is None else internal_class_weight.get(cls, 0.0)
            wl = per_100m * min(e.getLength() / 100.0, 2.0)
            if wl <= 0:
                continue
            out["internal_src"].append((e.getID(), wl))
            if e.getID() not in counted_edges:
                out["internal_dst"].append((e.getID(), wl))
    return out


def sample_od_pairs(
    weights: dict[str, list[tuple[str, float]]],
    n: int = N_CANDIDATE_TRIPS,
    shares: dict[str, float] = OD_SHARES,
    seed: int = SEED,
) -> list[tuple[str, str, str]]:
    """(class, from edge, to edge) candidate pairs, seeded; from != to."""
    import numpy as np

    rng = np.random.default_rng(seed)
    kinds = {
        "b2b": ("entry", "exit"),
        "b2i": ("entry", "internal_dst"),
        "i2b": ("internal_src", "exit"),
        "i2i": ("internal_src", "internal_dst"),
    }
    out = []
    for cls in sorted(shares):
        src, dst = (weights[k] for k in kinds[cls])
        k = int(round(n * shares[cls]))
        sw = np.array([w for _, w in src], dtype=float)
        dw = np.array([w for _, w in dst], dtype=float)
        si = rng.choice(len(src), size=k, p=sw / sw.sum())
        di = rng.choice(len(dst), size=k, p=dw / dw.sum())
        for i, j in zip(si, di, strict=True):
            if src[i][0] != dst[j][0]:
                out.append((cls, src[i][0], dst[j][0]))
    return out


def write_trips(pairs: list[tuple[str, str, str]], path: Path, depart: int = 0) -> None:
    """Candidate trips; `depart` only matters for routing on time-dependent weights (duarouter
    uses an edgeData weight only for departures inside its interval)."""
    lines = ["<routes>"]
    for k, (cls, f, t) in enumerate(pairs):
        lines.append(f'    <trip id="{cls}_{k}" depart="{depart}" from="{f}" to="{t}"/>')
    lines.append("</routes>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def route_trips(
    net_path: Path, trips: Path, out: Path, seed: int = SEED, weights: Path | None = None
) -> None:
    """duarouter: fastest path for each candidate pair (unroutable pairs skipped), on free-flow
    times, or on the edge travel times of an earlier run (`weights`: SUMO edgeData)."""
    import sumolib

    cmd = [
        sumolib.checkBinary("duarouter"),
        "-n",
        str(net_path),
        "--route-files",
        str(trips),
        "-o",
        str(out),
        "--ignore-errors",
        "--no-step-log",
        "--no-warnings",
        "--seed",
        str(seed),
        "--repair",
        "--remove-loops",
        "--write-costs",
        "false",
        "--exit-times",
        "false",
    ]
    if weights is not None:
        cmd += ["--weight-files", str(weights), "--weight-attribute", "traveltime"]
    subprocess.run(cmd, check=True, capture_output=True, env=sumo_env())


def unique_routes(
    routed: Path | list[Path],
    counted_edges: set[str],
    edge_length: dict[str, float],
    min_length_m: float = MIN_ROUTE_LENGTH_M,
) -> list[tuple[str, list[str]]]:
    """(class, edges) per distinct route, in first-seen order (over the files in order): routes
    ending on a counted edge or shorter than `min_length_m` are dropped."""
    seen: set[tuple[str, ...]] = set()
    out = []
    files = routed if isinstance(routed, list) else [routed]
    for _, el in (x for f in files for x in ET.iterparse(f, events=("end",))):
        if el.tag != "vehicle":
            continue
        r = el.find("route")
        if r is None:
            el.clear()
            continue
        edges = r.get("edges").split()
        cls = el.get("id").split("_")[0]
        el.clear()
        if edges[-1] in counted_edges:
            continue
        if sum(edge_length[e] for e in edges) < min_length_m:
            continue
        key = tuple(edges)
        if key in seen:
            continue
        seen.add(key)
        out.append((cls, edges))
    return out


def write_candidates(routes: list[tuple[str, list[str]]], path: Path) -> None:
    lines = ["<routes>"]
    for k, (cls, edges) in enumerate(routes):
        lines.append(f'    <route id="{cls}_{k}" edges="{" ".join(edges)}"/>')
    lines.append("</routes>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


VEHICLE_ATTRS = 'departLane="best" departSpeed="max"'


def run_route_sampler(
    candidates: Path,
    edgedata: Path,
    out: Path,
    optimize: str | None = "full",
    seed: int = SEED,
    extra: list[str] | None = None,
) -> str:
    """routeSampler.py over the calibration edgeData; returns its stdout (fit summary)."""
    cmd = [
        sys.executable,
        str(SUMO_TOOLS / "routeSampler.py"),
        "-r",
        str(candidates),
        "-d",
        str(edgedata),
        "-o",
        str(out),
        "--seed",
        str(seed),
        "--prefix",
        "am",
        "-a",
        VEHICLE_ATTRS,
        "--mismatch-output",
        str(out.with_suffix(".mismatch.xml")),
        "--min-count",
        "1",
    ]
    if optimize:
        cmd += ["--optimize", optimize]
    cmd += extra or []
    res = subprocess.run(cmd, check=True, capture_output=True, text=True, env=sumo_env())
    return res.stdout + res.stderr


def fitted_vehicles(routes: Path) -> list[tuple[float, list[str]]]:
    """(depart, edges) of each vehicle in a routeSampler output."""
    out = []
    for _, el in ET.iterparse(routes, events=("end",)):
        if el.tag == "vehicle":
            r = el.find("route")
            out.append((float(el.get("depart")), r.get("edges").split()))
            el.clear()
    return out


def fitted_counts(
    vehicles: list[tuple[float, list[str]]],
    edges: set[str],
    interval_s: int = INTERVAL_S,
) -> dict[str, dict[int, int]]:
    """What routeSampler fitted: vehicles per counted edge per DEPARTURE interval."""
    out: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for dep, route in vehicles:
        b = int(dep // interval_s) * interval_s
        for e in set(route) & edges:
            out[e][b] += 1
    return {e: dict(v) for e, v in out.items()}


# --- freeway reference (checks only, never fitted) ------------------------------------------

FREEWAY_CLASSES = {"motorway", "motorway_link"}


def freeway_checks(
    reference: dict,
    net,
    used: dict[str, dict[int, int]],
    counts_all_day: dict[Approach, int],
    counts_window: dict[Approach, int],
    begin: int = WARMUP_S,
    end: int = END_S,
) -> dict:
    """Simulated freeway flows vs DTP AADT 2019 and TIRTL West Gate (checks only).

    expected window flow ~ AADT x (median SCATS share of the day's count in the window); the
    ratio modelled / expected is a sanity number, not a target."""
    shares = sorted(
        counts_window[a] / counts_all_day[a] for a in counts_window if counts_all_day.get(a)
    )
    window_share = shares[len(shares) // 2] if shares else None
    rows = []
    for seg in reference["aadt_2019"]["segments"]:
        eids = [e for e in seg["matched_edge_ids"] if net.hasEdge(e)]
        fw = [e for e in eids if road_class(net.getEdge(e)) in FREEWAY_CLASSES]
        if not fw:
            continue
        flows = [sum(v for t, v in used.get(e, {}).items() if begin <= t < end) for e in fw]
        modelled = sum(flows) / len(flows)
        expected = seg["aadt_2019"] * window_share if window_share else None
        rows.append(
            {
                "segment_id": seg["segment_id"],
                "road_name": seg["road_name"],
                "description": seg["description"],
                "direction": seg["direction"],
                "aadt_2019": seg["aadt_2019"],
                "edges": fw,
                "modelled_window": round(modelled, 1),
                "expected_window_from_aadt": round(expected, 1) if expected else None,
                "ratio_modelled_to_expected": round(modelled / expected, 3) if expected else None,
            }
        )
    rows.sort(key=lambda r: r["segment_id"])
    ratios = sorted(
        r["ratio_modelled_to_expected"] for r in rows if r["ratio_modelled_to_expected"]
    )
    return {
        "note": (
            "Checks only, never fitted. Modelled = mean vehicles per matched freeway edge "
            "(entered + departed) in the window; expected = AADT 2019 x the median share of a "
            "SCATS approach's 23 Oct day count that falls in the window. Different year, a "
            "single day vs an annual average; order-of-magnitude only."
        ),
        "window_s": [begin, end],
        "scats_median_window_share_of_day": round(window_share, 4) if window_share else None,
        "segments": rows,
        "median_ratio": ratios[len(ratios) // 2] if ratios else None,
    }


def tirtl_check(reference: dict, modelled: dict[str, float], begin_local: time, end_local: time):
    """TIRTL West Gate Bridge volumes in the window next to modelled West Gate boundary flows."""
    out = {"note": reference["tirtl"]["note"], "sites": {}}
    b = (begin_local.hour * 60 + begin_local.minute) // 15
    e = (end_local.hour * 60 + end_local.minute) // 15
    for key, s in reference["tirtl"]["sites"].items():
        out["sites"][key] = {
            "description": s["description"],
            "vehicles_window": sum(s["bins_15min"][b:e]),
            # data-engineer's AM reliability flag (504 inbound: unreliable on 23 Oct); an
            # unreliable site is listed for completeness, not used as a bound
            "am_reliability": s.get("am_reliability", {}).get("status", "not assessed"),
        }
    out["modelled"] = modelled
    return out


def main(argv: list[str] | None = None) -> int:
    """Print the calibration summary of the last am_peak run."""
    from sim.config import CHECKS_DIR

    path = CHECKS_DIR / "am_peak_calibration.json"
    rep = json.loads(path.read_text(encoding="utf-8"))
    print(json.dumps({k: rep[k] for k in ("calibration", "validation") if k in rep}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
