"""Build data/processed/counts.parquet: real SCATS counts per SUMO approach edge, 15-minute bins.

Usage (from the repo root):
    uv run python -m pipeline.counts                       # the Phase 3 target day (TARGET_DAY)
    uv run python -m pipeline.counts --from 2026-10-19 --to 2026-10-23   # any date range

One command: any day in the range that is not cached in data/raw/scats/ is downloaded first
(`pipeline.scats_download`, one range request per day, cached forever), together with each day's
quality-check (QC) days; then every requested day is cleaned, checked and mapped, and
counts.parquet is rewritten with one row per (site, approach edge, 15-minute bin) of every
requested day. `bin_start` is the real local date-time (Australia/Melbourne).

Phase 3 deliverable (orchestrator, George, 2026-10-27): "yesterday's traffic". The calibration
target is ONE real day, TARGET_DAY = Friday 23 October 2026: the most recent normal working
weekday published when this was built on Tue 27 Oct (the October ZIP then held 1-25 Oct; Sat 24
and Sun 25 are a weekend, Mon 26 not yet published), in Term 4 and not a public holiday.

Per-detector rules for each day (never fills a gap from another day; never invents a count):
- interval value -1 -> missing; a bin of an approach is written only if every detector of that
  approach has a valid value in it (partial alarms remove bins, they are flagged);
- fully alarmed detector (all 96 intervals -1) -> its approach is dropped for that day;
- dead detector (zero all day while its QC days average > DEAD_MIN_VEHICLES) -> approach dropped;
- detector zero on the day AND on every QC day -> approach dropped (never counted anything);
- QC: the same weekday in up to QC_WEEKS earlier weeks that are normal working days. If the day's
  total (over bins valid on every compared day) is outside [min/QC_MARGIN, max*QC_MARGIN] of the
  QC days' totals -> approach dropped ("strong deviation"); outside [min/QC_FLAG_MARGIN,
  max*QC_FLAG_MARGIN] but inside the drop margin -> kept and flagged.
Per-approach profile checks (qa round 1, 2026-10-27: a whole-day total can hide a fault):
- AM: the approach's 07:00-10:00 total (only if every bin is valid) outside
  [median/AM_QC_RATIO, median*AM_QC_RATIO] of its QC days' AM totals -> approach dropped;
- overnight: its 00:00-04:00 total above NIGHT_QC_RATIO x the highest QC-day night total AND
  more than NIGHT_MIN_EXCESS vehicles above it -> approach dropped (a stuck or chattering loop).
Only approaches with approach_coverage 'full' in the detector map are written.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from pipeline import scats
from pipeline.count_mapping import (
    DETECTOR_MAP,
    NET_PATH,
    NetIndex,
    load_detector_map,
    match_approach,
)
from pipeline.scats_download import (
    PUBLIC_HOLIDAYS_CHECKED,
    SCHOOL_HOLIDAYS_2026,
    download_volumes,
    volume_path,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
PROCESSED = REPO_ROOT / "data" / "processed"
COUNTS_PARQUET = PROCESSED / "counts.parquet"
REPORT_JSON = PROCESSED / "checks" / "counts_report.json"
TZ = ZoneInfo("Australia/Melbourne")

TARGET_DAY = dt.date(2026, 10, 23)
TARGET_DAY_REASON = (
    "Most recent normal working weekday published on 2026-10-27 (October 2026 ZIP, last modified "
    "2026-10-26T13:03Z, held 1-25 Oct; 24-25 Oct are a weekend and Mon 26 Oct was not yet "
    "published). Friday in Victorian school Term 4 (5 Oct - 18 Dec), not a public holiday."
)
QC_WEEKS = 3
QC_LOOKBACK_WEEKS = 6
QC_MARGIN = 1.5
QC_FLAG_MARGIN = 1.2
DEAD_MIN_VEHICLES = 50
AM_WINDOW = (dt.time(7, 0), dt.time(10, 0))
AM_BINS = range(28, 40)  # 07:00-10:00
NIGHT_BINS = range(0, 16)  # 00:00-04:00
AM_QC_RATIO = 1.5
NIGHT_QC_RATIO = 3.0
NIGHT_MIN_EXCESS = 200

# Public holidays used only to skip QC comparison days. Only dates we have checked are listed
# (data/SOURCES.md, "References"); the Friday before the AFL Grand Final falls in the school
# holidays in 2026 and is skipped with them.
PUBLIC_HOLIDAYS: tuple[dt.date, ...] = PUBLIC_HOLIDAYS_CHECKED


def is_normal_working_day(d: dt.date) -> bool:
    if d.weekday() >= 5 or d in PUBLIC_HOLIDAYS:
        return False
    return not any(a <= d <= b for a, b in SCHOOL_HOLIDAYS_2026)


def qc_days_for(d: dt.date, weeks: int = QC_WEEKS) -> list[dt.date]:
    """The same weekday in earlier weeks (up to QC_LOOKBACK_WEEKS back) that are normal working
    days, most recent first, at most `weeks` of them."""
    out = []
    for k in range(1, QC_LOOKBACK_WEEKS + 1):
        c = d - dt.timedelta(weeks=k)
        if is_normal_working_day(c):
            out.append(c)
        if len(out) == weeks:
            break
    return out


def bin_starts(d: dt.date) -> list[dt.datetime]:
    """96 local bin starts. DST-transition days are not supported (SCATS keeps 96 intervals)."""
    first = dt.datetime.combine(d, dt.time(0), TZ)
    last = dt.datetime.combine(d, dt.time(23, 45), TZ)
    if first.utcoffset() != last.utcoffset():
        raise SystemExit(f"{d} is a daylight-saving transition day; not supported")
    return [first + dt.timedelta(minutes=15 * i) for i in range(scats.N_BINS)]


# --- detector map -> approaches ---------------------------------------------------------------


@dataclass
class Approach:
    approach_id: str
    site: int
    street: str
    heading: str
    coverage: str
    detectors: list[int]
    source: list[str]
    edge_id: str | None = None
    edge_reason: str = ""
    name_match: bool = False
    candidates: list[str] = field(default_factory=list)


def approaches_from_map(ref: dict) -> list[Approach]:
    by_id: dict[str, Approach] = {}
    for r in ref["rows"]:
        a = by_id.get(r["approach_id"])
        if a is None:
            a = Approach(
                r["approach_id"],
                r["site"],
                r["street"],
                r["heading"],
                r["approach_coverage"],
                [],
                [],
            )
            by_id[r["approach_id"]] = a
        a.detectors.append(r["detector"])
        a.source.append(r["source"])
    return list(by_id.values())


def map_edges(approaches: list[Approach], sites: dict[int, scats.Site], index: NetIndex) -> None:
    for a in approaches:
        s = sites.get(a.site)
        sj = index.site_junction(a.site, s.lon, s.lat) if s else None
        if sj is None:
            a.edge_reason = "no SUMO traffic-light junction within 60 m of the site"
            continue
        n = len(a.detectors) if a.coverage == "full" else None
        m = match_approach(sj, a.street, a.heading, index, n_lanes=n)
        a.edge_id, a.edge_reason, a.name_match, a.candidates = (
            m.edge_id,
            m.reason,
            m.name_match,
            m.candidates,
        )


# --- per-day cleaning and QC ------------------------------------------------------------------


def load_days(days: list[dt.date], sites: set[int]) -> dict[dt.date, dict]:
    out = {}
    for d in days:
        p = volume_path(d)
        if p.exists():
            out[d] = {k: v for k, (_day, v) in scats.read_day(p, sites).items()}
    return out


@dataclass
class DetectorQC:
    status: str  # ok / flag_deviation / flag_partial / drop_*
    detail: str
    day_total: float | None = None
    qc_totals: list[float] = field(default_factory=list)


def detector_qc(day: scats.DetectorDay | None, qc: list[scats.DetectorDay | None]) -> DetectorQC:
    if day is None:
        return DetectorQC("drop_missing", "detector not in the day's file")
    if day.fully_alarmed:
        return DetectorQC("drop_fully_alarmed", "all 96 intervals are -1 (alarm)")
    qcv = [q for q in qc if q is not None and not q.fully_alarmed]
    valid = ~np.isnan(day.values)
    for q in qcv:
        valid &= ~np.isnan(q.values)
    tot = float(np.nansum(np.where(valid, day.values, np.nan)))
    qtot = [float(np.nansum(np.where(valid, q.values, np.nan))) for q in qcv]
    n_missing = int(np.isnan(day.values).sum())
    if tot == 0 and qtot and all(t == 0 for t in qtot):
        return DetectorQC("drop_never_counts", "zero on the day and on every QC day", tot, qtot)
    if tot == 0 and qtot and np.mean(qtot) > DEAD_MIN_VEHICLES:
        return DetectorQC("drop_dead", "zero all day while its QC days count traffic", tot, qtot)
    if tot == 0 and not qtot:
        return DetectorQC("drop_never_counts", "zero all day, no QC day to compare", tot, qtot)
    if qtot:
        lo, hi = min(qtot), max(qtot)
        if tot < lo / QC_MARGIN or tot > hi * QC_MARGIN:
            return DetectorQC(
                "drop_deviation",
                f"day total {tot:.0f} outside [{lo / QC_MARGIN:.0f}, {hi * QC_MARGIN:.0f}] "
                f"(QC days {', '.join(f'{t:.0f}' for t in qtot)})",
                tot,
                qtot,
            )
        if tot < lo / QC_FLAG_MARGIN or tot > hi * QC_FLAG_MARGIN:
            return DetectorQC(
                "flag_deviation",
                f"day total {tot:.0f} outside [{lo / QC_FLAG_MARGIN:.0f}, "
                f"{hi * QC_FLAG_MARGIN:.0f}] (QC days {', '.join(f'{t:.0f}' for t in qtot)}) "
                f"but within x{QC_MARGIN}",
                tot,
                qtot,
            )
    if n_missing:
        return DetectorQC("flag_partial", f"{n_missing} intervals -1 (alarm); bins dropped", tot)
    return DetectorQC("ok", "", tot, qtot)


def approach_series(
    a: Approach, day: dict, qc_days: list[dict]
) -> tuple[np.ndarray | None, dict[int, DetectorQC], str]:
    """Per-bin approach volume (NaN where any detector is missing) or None if dropped."""
    qcs: dict[int, DetectorQC] = {}
    for det in a.detectors:
        qcs[det] = detector_qc(day.get((a.site, det)), [q.get((a.site, det)) for q in qc_days])
    drops = {d: q for d, q in qcs.items() if q.status.startswith("drop")}
    if drops:
        why = "; ".join(f"det {d}: {q.status[5:]} ({q.detail})" for d, q in sorted(drops.items()))
        return None, qcs, why
    vals = np.vstack([day[(a.site, d)].values for d in a.detectors])
    total = vals.sum(axis=0)  # NaN if any detector is NaN in that bin
    why = profile_check(a, total, qc_days)
    if why:
        return None, qcs, why
    return total, qcs, ""


def _window_total(series: np.ndarray, bins: range) -> float | None:
    w = series[list(bins)]
    return None if np.isnan(w).any() else float(w.sum())


def qc_series(a: Approach, qc_day: dict) -> np.ndarray | None:
    """The approach's per-bin sum on a QC day (NaN where missing), or None if a detector is
    absent or fully alarmed that day."""
    dds = [qc_day.get((a.site, d)) for d in a.detectors]
    if any(x is None or x.fully_alarmed for x in dds):
        return None
    return np.vstack([x.values for x in dds]).sum(axis=0)


def profile_check(a: Approach, total: np.ndarray, qc_days: list[dict]) -> str:
    """Approach-level AM and overnight checks against the QC days; '' if fine (or no QC)."""
    qs = [q for q in (qc_series(a, d) for d in qc_days) if q is not None]
    am = _window_total(total, AM_BINS)
    q_am = [x for x in (_window_total(q, AM_BINS) for q in qs) if x]
    if am is not None and q_am:
        med = float(np.median(q_am))
        ratio = am / med
        if not 1 / AM_QC_RATIO <= ratio <= AM_QC_RATIO:
            return (
                f"profile: AM 07-10 total {am:.0f} is {ratio:.2f}x the QC days' median {med:.0f} "
                f"(QC days {', '.join(f'{x:.0f}' for x in q_am)}; allowed x{AM_QC_RATIO})"
            )
    night = _window_total(total, NIGHT_BINS)
    q_night = [x for x in (_window_total(q, NIGHT_BINS) for q in qs) if x is not None]
    if night is not None and q_night:
        hi = max(q_night)
        if night > NIGHT_QC_RATIO * hi and night - hi > NIGHT_MIN_EXCESS:
            return (
                f"profile: overnight 00-04 total {night:.0f} vs QC days "
                f"{', '.join(f'{x:.0f}' for x in q_night)} (> x{NIGHT_QC_RATIO} and "
                f"+{NIGHT_MIN_EXCESS} over the highest)"
            )
    return ""


# --- build -------------------------------------------------------------------------------------


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def build(days: list[dt.date], download: bool = True) -> dict:
    import pyarrow as pa
    import pyarrow.parquet as pq

    qc_map = {d: qc_days_for(d) for d in days}
    needed = sorted(set(days) | {q for qs in qc_map.values() for q in qs})
    if download:
        download_volumes(needed)
    missing = [d for d in days if not volume_path(d).exists()]
    if missing:
        raise SystemExit(f"no SCATS file for {missing}; not published yet or not downloaded")

    ref = load_detector_map()
    sites_all = scats.load_sites()
    approaches = approaches_from_map(ref)
    index = NetIndex()
    map_edges(approaches, sites_all, index)
    site_ids = {a.site for a in approaches}
    data = load_days(needed, site_ids)

    rows = {"site_id": [], "edge_id": [], "direction": [], "bin_start": [], "vehicles": []}
    per_day = []
    for d in days:
        qcs = [data[q] for q in qc_map[d] if q in data]
        starts = bin_starts(d)
        am = [i for i, t in enumerate(starts) if AM_WINDOW[0] <= t.time() < AM_WINDOW[1]]
        rec = {"date": d.isoformat(), "qc_days": [q.isoformat() for q in qc_map[d] if q in data]}
        used, dropped, det_status = [], [], {}
        for a in approaches:
            base = {
                "approach_id": a.approach_id,
                "site_id": str(a.site),
                "street": a.street,
                "direction": a.heading,
                "detectors": a.detectors,
                "edge_id": a.edge_id,
            }
            if a.coverage != "full":
                dropped.append({**base, "reason": "partial lane coverage (multi-lane loop)"})
                continue
            if a.edge_id is None:
                dropped.append({**base, "reason": f"no SUMO edge: {a.edge_reason}"})
                continue
            series, dq, why = approach_series(a, data[d], qcs)
            for det, q in dq.items():
                det_status[f"{a.site}:{det}"] = {"status": q.status, "detail": q.detail}
            if series is None:
                kind = "profile check" if why.startswith("profile:") else "detector fault"
                dropped.append({**base, "reason": f"{kind}: {why}"})
                continue
            valid = ~np.isnan(series)
            for i in np.flatnonzero(valid):
                rows["site_id"].append(str(a.site))
                rows["edge_id"].append(a.edge_id)
                rows["direction"].append(a.heading)
                rows["bin_start"].append(starts[i])
                rows["vehicles"].append(int(series[i]))
            am_valid = all(valid[i] for i in am)
            used.append(
                {
                    **base,
                    "edge_reason": a.edge_reason,
                    "edge_name_match": a.name_match,
                    "bins": int(valid.sum()),
                    "vehicles_day": int(np.nansum(series)),
                    "vehicles_07_10": int(np.nansum(series[am])) if am_valid else None,
                    "bins_missing_07_10": int(sum(not valid[i] for i in am)),
                    "flags": {
                        str(det): q.detail for det, q in dq.items() if q.status.startswith("flag")
                    },
                }
            )
        statuses = [v["status"] for v in det_status.values()]
        rec.update(
            {
                "sites_used": sorted({u["site_id"] for u in used}, key=int),
                "approaches_used": used,
                "approaches_dropped": dropped,
                "detectors_checked": len(statuses),
                "detectors_dropped_for_faults": sum(s.startswith("drop") for s in statuses),
                "detectors_flagged": sum(s.startswith("flag") for s in statuses),
                "detector_status_counts": {s: statuses.count(s) for s in sorted(set(statuses))},
                "detector_status": dict(sorted(det_status.items())),
            }
        )
        per_day.append(rec)

    dup = {}
    for s, e, b in zip(rows["site_id"], rows["edge_id"], rows["bin_start"], strict=True):
        dup[(s, e, b)] = dup.get((s, e, b), 0) + 1
    if any(v > 1 for v in dup.values()):
        raise SystemExit("two approaches of one site map to the same edge; fix the detector map")

    table = pa.table(
        {
            "site_id": pa.array(rows["site_id"], pa.string()),
            "edge_id": pa.array(rows["edge_id"], pa.string()),
            "direction": pa.array(rows["direction"], pa.string()),
            "bin_start": pa.array(rows["bin_start"], pa.timestamp("s", tz="Australia/Melbourne")),
            "vehicles": pa.array(rows["vehicles"], pa.int32()),
        }
    )
    validate_table(table, {e.getID() for e in index.net.getEdges()})
    COUNTS_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, COUNTS_PARQUET, compression="zstd")
    return {
        "table": table,
        "per_day": per_day,
        "approaches": approaches,
        "ref": ref,
        "index": index,
    }


CONTRACT_COLUMNS = ("site_id", "edge_id", "direction", "bin_start", "vehicles")
DIRECTIONS = {"NB", "SB", "EB", "WB", "NE", "NW", "SE", "SW"}


def validate_table(table, net_edges: set[str]) -> None:
    """The counts.parquet contract (CLAUDE.md): exact columns, 15-minute local bins, edge IDs
    that exist in sim/net/roads.net.xml, non-negative integer vehicles, no duplicate keys."""
    import pyarrow as pa

    if tuple(table.column_names) != CONTRACT_COLUMNS:
        raise ValueError(f"columns {table.column_names} != {CONTRACT_COLUMNS}")
    t = table.schema.field("bin_start").type
    if not pa.types.is_timestamp(t) or t.tz != "Australia/Melbourne":
        raise ValueError(f"bin_start type {t} is not a Australia/Melbourne timestamp")
    d = table.to_pydict()
    bad_edges = sorted(set(d["edge_id"]) - net_edges)
    if bad_edges:
        raise ValueError(f"edge_ids not in roads.net.xml: {bad_edges[:5]}")
    if set(d["direction"]) - DIRECTIONS:
        raise ValueError(f"bad directions {set(d['direction']) - DIRECTIONS}")
    for b in d["bin_start"]:
        if b.minute % 15 or b.second:
            raise ValueError(f"bin_start {b} is not on a 15-minute boundary")
    if any(v is None or v < 0 for v in d["vehicles"]):
        raise ValueError("negative or null vehicles")
    keys = list(zip(d["site_id"], d["edge_id"], d["bin_start"], strict=True))
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate (site_id, edge_id, bin_start)")


def report(result: dict, days: list[dt.date]) -> dict:
    ref = result["ref"]
    approaches = result["approaches"]
    raw_files = sorted(
        {volume_path(d) for d in days}
        | {volume_path(dt.date.fromisoformat(q)) for r in result["per_day"] for q in r["qc_days"]}
    )
    src_counts = {}
    for r in ref["rows"]:
        src_counts[r["source"]] = src_counts.get(r["source"], 0) + 1
    sites_read = [s for s in ref["sites"]]
    rep = {
        "description": "Verification report for data/processed/counts.parquet (not a contract "
        "file). Real SCATS counts for the listed day(s), cleaned, QC'd against the same weekday "
        "in earlier weeks, mapped to SUMO approach edges.",
        "licence": "Derived from DTP Traffic Signal Volume Data and Traffic Signal Configuration "
        "Data Sheets (CC BY 4.0), modified; see data/SOURCES.md",
        "attribution": "Traffic counts: Contains Department of Transport and Planning (Victoria) "
        "Traffic Signal Volume Data, CC BY 4.0, modified",
        "target_day": TARGET_DAY.isoformat(),
        "target_day_reason": TARGET_DAY_REASON,
        "days": [d.isoformat() for d in days],
        "calibrate_to": "data/processed/counts.parquet rows whose bin_start falls on target_day",
        "rules": {
            "qc_weeks": QC_WEEKS,
            "qc_margin_drop": QC_MARGIN,
            "qc_margin_flag": QC_FLAG_MARGIN,
            "am_qc_ratio": AM_QC_RATIO,
            "night_qc_ratio": NIGHT_QC_RATIO,
            "night_min_excess": NIGHT_MIN_EXCESS,
            "dead_min_vehicles": DEAD_MIN_VEHICLES,
            "am_window": [AM_WINDOW[0].isoformat(), AM_WINDOW[1].isoformat()],
            "coverage": "only approaches whose every general-traffic lane has its own stop-line "
            "loop are written",
        },
        "detector_map": {
            "sites_read": len(sites_read),
            "sites_not_mapped": [
                {"site": s["site"], "note": s["note"]}
                for s in sites_read
                if s["status"] != "mapped"
            ],
            "rows": len(ref["rows"]),
            "rows_by_source": src_counts,
            "approaches": len(approaches),
            "approaches_full_coverage": sum(a.coverage == "full" for a in approaches),
            "approaches_with_edge": sum(a.edge_id is not None for a in approaches),
            "edge_name_mismatches": [
                {"approach_id": a.approach_id, "street": a.street, "edge_id": a.edge_id}
                for a in approaches
                if a.edge_id and not a.name_match
            ],
        },
        "recheck_10pct": ref.get("recheck_10pct"),
        "per_day": result["per_day"],
        "parquet": {
            "path": str(COUNTS_PARQUET.relative_to(REPO_ROOT)),
            "rows": result["table"].num_rows,
            "bytes": COUNTS_PARQUET.stat().st_size,
        },
        "raw_sizes_per_day": {p.name: p.stat().st_size for p in raw_files if p.exists()},
        "inputs_sha256": {
            str(p.relative_to(REPO_ROOT)): sha256(p)
            for p in [NET_PATH, DETECTOR_MAP, scats.SITES_CSV, *raw_files]
            if p.exists()
        },
    }
    return rep


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--from", dest="date_from", type=dt.date.fromisoformat)
    ap.add_argument("--to", dest="date_to", type=dt.date.fromisoformat)
    ap.add_argument("--no-download", action="store_true", help="use cached files only")
    args = ap.parse_args(argv)
    if args.date_from:
        end = args.date_to or args.date_from
        days = [
            args.date_from + dt.timedelta(days=i) for i in range((end - args.date_from).days + 1)
        ]
    else:
        days = [TARGET_DAY]
    result = build(days, download=not args.no_download)
    rep = report(result, days)
    REPORT_JSON.parent.mkdir(parents=True, exist_ok=True)
    from pipeline import counts_spotcheck

    entries = counts_spotcheck.build(result["index"], rep, result["ref"], scats.load_sites())
    rep["spot_check"] = {
        "for": "George",
        "how": "open data/processed/checks/counts_spotcheck/index.html; record a verdict per "
        "site (george_verdict in counts_spotcheck/spotcheck.json)",
        "sites": [e["site"] for e in entries],
        "entries": entries,
    }
    REPORT_JSON.write_text(json.dumps(rep, indent=1, default=str) + "\n")
    from pipeline import freeway_checks

    fw = freeway_checks.build(days[-1], result["index"], REPORT_JSON)
    print(
        f"freeway_reference: {fw['aadt_2019']['segments_with_edges']} of "
        f"{fw['aadt_2019']['segments_count']} AADT segments matched to edges; TIRTL "
        f"{', '.join(f'{k}: {v["vehicles_day"]}' for k, v in fw['tirtl']['sites'].items())}"
    )
    for r in rep["per_day"]:
        print(
            f"{r['date']}: {len(r['sites_used'])} sites, {len(r['approaches_used'])} approaches "
            f"used, {len(r['approaches_dropped'])} dropped, "
            f"{r['detectors_dropped_for_faults']} detectors dropped for faults, "
            f"{r['detectors_flagged']} flagged (QC days {r['qc_days']})"
        )
    print(f"wrote {COUNTS_PARQUET} ({rep['parquet']['rows']} rows, {rep['parquet']['bytes']} B)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
