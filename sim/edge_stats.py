"""SUMO 60 s edgeData -> sim/out/edge_stats_<scenario>.parquet (road colours in the browser).

One row per (edge, 60 s bin) in which at least one vehicle was on the edge. Columns:

- edge_id (string): a non-internal edge of sim/net/roads.net.xml, exactly as in the network and
  in sim/out/edge_geometry.json. Junction-internal lanes (":...") have no rows.
- t_bin (int32): start of the bin in seconds from scenario start, the same clock as the
  timestamps in trips_<scenario>.json. Bins are [t_bin, t_bin + 60). baseline: 0 = end of the
  5 min warm-up, which has no rows. am_peak (sim/am_peak.py): 0 = 06:30 local, the whole run
  06:30-09:30 including its 30 min warm-up.
- mean_speed_mps (float64): SUMO's edgeData `speed` for the bin: distance driven on the edge
  divided by vehicle-seconds spent on it (a time-weighted mean over every vehicle on the edge,
  stopped ones included, so a queue pulls it down). 2 decimals, as SUMO writes it.
- speed_limit_mps (float64): the edge's speed limit in the run: the lane speed in roads.net.xml
  (OSM maxspeed, the CBD 40 km/h area limit from sim/patches/net/cbd_speed_limit.edg.xml,
  netconvert type defaults; all lanes of an edge share it), replaced by `speed_overrides` where a
  scenario changes limits during the run. Conditional limits (school zones, `maxspeed:conditional`,
  sim/conditional_speeds.py) apply only to a scenario with a clock time inside their hours; the
  placeholder `baseline` has no time of day, so none apply to it and it passes no overrides.
  am_peak passes `limit_at`: the limit in force at the start of each bin (school zones and Bay St
  40 km/h from 08:00).
- speed_ratio (float64): mean_speed_mps / speed_limit_mps, 4 decimals. It can exceed 1: SUMO
  drivers keep a personal speed factor around 1 (passenger default: normal, sd 0.1), so free-
  flowing traffic averages near 1 and some edges briefly run above the limit.
- vehicle_count (int32, >= 1): distinct vehicles on the edge at any time during the bin: those
  already on it when the bin starts, plus those that enter it or are inserted on it during the
  bin. Read from the edgeData counters, carried forward from t = 0 (on[k+1] = on[k] + departed +
  entered - arrived - left), so it is exact even on 0.2 m edges a car crosses between two FCD
  samples. SUMO moves a vehicle to the next edge when its FRONT leaves (`left`), but keeps adding
  the time its BACK still overlaps the edge to sampledSeconds and the speed. So in a few rows
  (97 of 20,770 in the baseline) the only vehicle time in the bin is the back of a vehicle that
  had already left (e.g. a car stopped across the junction, its tail on the edge); these rows
  are kept, with vehicle_count = 1, the true lower bound (one vehicle was there).

No-data rule: an edge-bin in which no vehicle spent any time on the edge (SUMO sampledSeconds = 0)
has NO ROW. No speed is invented for it, and there is no NaN anywhere in the file: a reader treats
a missing (edge_id, t_bin) as "no traffic, no data" (grey), not as free flow or as a jam. A vehicle
inserted on an edge in a bin's last step counts in that bin and, if still there, in the next.
The 60 s edgeData output comes from sim/run.py (`edgestats60.xml`,
excludeEmpty, written from t = 0 so the carry-forward starts from an empty network).

Run (from the repo root), after `uv run python -m sim.run baseline` has left sim/work/baseline/:
    uv run python -m sim.edge_stats baseline
(`sim.run baseline` also writes this file itself.)
"""

from __future__ import annotations

import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

COLUMNS = [
    "edge_id",
    "t_bin",
    "mean_speed_mps",
    "speed_limit_mps",
    "speed_ratio",
    "vehicle_count",
]
BIN_S = 60
# SUMO 1.28 edgeData counts a vehicle that teleports off an edge in `left` as well as in
# `teleported`, so `teleported` is not subtracted again (checked on am_peak: subtracting it sends
# the balance negative 2,901 times; without it, never). The baseline has 0 teleports.
_EXITS = ("arrived", "left", "vaporized")


def _int(el, key: str) -> int:
    return int(float(el.get(key, 0) or 0))


def read_bins(
    edgedata: Path,
    begin: float,
    end: float,
    limits: dict[str, float],
    limit_at: Callable[[str, float], float | None] | None = None,
) -> tuple[list[dict], int]:
    """(rows sorted by t_bin, edge_id; rows with only back-overlap time) from a 60 s edgeData
    file written from t = 0.

    `begin` / `end`: the recording window in simulation seconds (t_bin = interval begin - begin).
    `limits`: {edge id: speed limit m/s} for every edge that may get a row.
    `limit_at(edge id, bin begin in simulation seconds)`: a time-of-day limit in force for the
    whole bin (e.g. a school zone from 08:00), or None for the edge's `limits` entry."""
    on: dict[str, int] = defaultdict(int)
    rows = []
    back_only = 0
    for _, iv in ET.iterparse(edgedata, events=("end",)):
        if iv.tag != "interval":
            continue
        b, e = float(iv.get("begin")), float(iv.get("end"))
        if round(e - b) != BIN_S:
            raise ValueError(f"edgeData interval {b}-{e} is not {BIN_S} s")
        record = begin <= b and e <= end
        for el in iv.iter("edge"):
            eid = el.get("id")
            present = on[eid] + _int(el, "departed") + _int(el, "entered")
            on[eid] = present - sum(_int(el, k) for k in _EXITS)
            if on[eid] < 0:
                raise ValueError(f"vehicle balance below zero on {eid} at {b}")
            if not record or eid.startswith(":"):
                continue
            if float(el.get("sampledSeconds", 0) or 0) <= 0 or el.get("speed") is None:
                continue  # nobody on the edge during the bin: no row (see module docstring)
            back_only += present < 1
            limit = limit_at(eid, b) if limit_at is not None else None
            limit = limits[eid] if limit is None else limit
            speed = float(el.get("speed"))
            rows.append(
                {
                    "edge_id": eid,
                    "t_bin": int(round(b - begin)),
                    "mean_speed_mps": speed,
                    "speed_limit_mps": limit,
                    "speed_ratio": round(speed / limit, 4),
                    "vehicle_count": max(present, 1),  # back-overlap only: see docstring
                }
            )
        iv.clear()
    rows.sort(key=lambda r: (r["t_bin"], r["edge_id"]))
    return rows, back_only


def speed_limits(net, overrides: dict[str, float] | None = None) -> dict[str, float]:
    """{edge id: m/s}: the edge's lane speed in the network (max over lanes; they are equal in
    roads.net.xml), then any scenario overrides (e.g. conditional_speeds.edge_speeds)."""
    out = {e.getID(): max(ln.getSpeed() for ln in e.getLanes()) for e in net.getEdges()}
    out.update(overrides or {})
    return out


def write_parquet(rows: list[dict], path: Path) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema(
        [
            ("edge_id", pa.string()),
            ("t_bin", pa.int32()),
            ("mean_speed_mps", pa.float64()),
            ("speed_limit_mps", pa.float64()),
            ("speed_ratio", pa.float64()),
            ("vehicle_count", pa.int32()),
        ]
    )
    table = pa.table({c: [r[c] for r in rows] for c in COLUMNS}, schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    # snappy: readable by small pure-JS Parquet readers without extra codecs. Through a file
    # object: with libsumo loaded in the same process, pyarrow's path-based filesystem fails.
    with path.open("wb") as fh:
        pq.write_table(table, fh, compression="snappy")


def export(
    scenario: str,
    edgedata: Path,
    net_path: Path,
    out_dir: Path,
    begin: float,
    end: float,
    speed_overrides: dict[str, float] | None = None,
    limit_at: Callable[[str, float], float | None] | None = None,
) -> dict:
    """`speed_overrides`: limits that hold for the whole run; `limit_at`: time-of-day limits per
    bin (see read_bins), for a scenario with a clock (am_peak)."""
    import sumolib

    net = sumolib.net.readNet(str(net_path))
    rows, back_only = read_bins(edgedata, begin, end, speed_limits(net, speed_overrides), limit_at)
    path = out_dir / f"edge_stats_{scenario}.parquet"
    write_parquet(rows, path)
    ratios = sorted(r["speed_ratio"] for r in rows)
    return {
        "rows": len(rows),
        "edges": len({r["edge_id"] for r in rows}),
        "bins": len({r["t_bin"] for r in rows}),
        "rows_back_overlap_only": back_only,
        "speed_ratio_min": ratios[0] if ratios else None,
        "speed_ratio_median": ratios[len(ratios) // 2] if ratios else None,
        "speed_ratio_max": ratios[-1] if ratios else None,
        "bytes": path.stat().st_size,
    }


def main(argv: list[str] | None = None) -> int:
    import argparse

    from sim.config import NET_PATH, OUT_DIR, WORK_DIR
    from sim.run import EDGE_STATS_FILE, END_S, WARMUP_S

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("scenario", choices=["baseline"])
    args = ap.parse_args(argv)
    summary = export(
        args.scenario,
        WORK_DIR / args.scenario / EDGE_STATS_FILE,
        NET_PATH,
        OUT_DIR,
        begin=float(WARMUP_S),
        end=float(END_S),
    )
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
