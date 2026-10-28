"""Freeway / arterial reference volumes: data/processed/checks/freeway_reference.json.

CHECK ONLY: never used to build counts.parquet or to fit demand (orchestrator, 2026-10-27).

1. DTP Historical AADT 2019 (`yearly_aadt_volume_2019.geojson`, CC BY 4.0): every directional
   segment that intersects SIM_BBOX, with AADT, heavy-vehicle volume and share, the methodology
   ("Actual" / "Estimated") and the SUMO edges it matches: car edges with the same street name
   whose midpoint lies within MATCH_DIST_M of the segment and whose travel bearing is within
   MATCH_BEARING_DEG of the segment's travel direction. 2019 is pre-COVID and pre-West Gate
   Tunnel: an old, coarse reference.
2. DTP TIRTL counts, sites 504 / 505 (West Gate Bridge inbound EB / outbound WB, CC BY 4.0):
   15-minute volumes on the counts target day, all vehicle classes summed, plus the heavy share
   (Austroads class 3 and above). Labelled as an UPSTREAM BOUND: the bridge is about 3 km west of
   our boundary and several exits lie in between, so it bounds, not equals, the West Gate Fwy
   flow entering the model.
3. SCATS vs AADT sanity (written into counts_report.json -> aadt_sanity): for each counted
   approach edge that an AADT segment also matches, the ratio of the SCATS day total to the 2019
   AADT. Order-of-magnitude only: a ratio outside [0.2, 5] is flagged.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
from pathlib import Path

from pipeline.config import SIM_BBOX

REPO_ROOT = Path(__file__).resolve().parent.parent
AADT_RAW = REPO_ROOT / "data" / "raw" / "scats" / "yearly_aadt_volume_2019.geojson"
TIRTL_RAW = REPO_ROOT / "data" / "raw" / "tirtl"
OUT_JSON = REPO_ROOT / "data" / "processed" / "checks" / "freeway_reference.json"

TIRTL_SITES = {"504": "West Gate Bridge inbound (EB)", "505": "West Gate Bridge outbound (WB)"}
TIRTL_ZIP_OCT_2026 = (
    "https://opendata.transport.vic.gov.au/dataset/e2d78fb5-e16d-43b9-bcdc-607d9b4855f5/"
    "resource/026ef37b-b60a-423a-9b3f-22d9c3247bdb/download/"
    "tirtl_15min_volume_classification_october_2026.zip"
)
TIRTL_SITES_CSV = (
    "https://opendata.transport.vic.gov.au/dataset/e2d78fb5-e16d-43b9-bcdc-607d9b4855f5/"
    "resource/1f685833-24fd-4eb0-af11-2e7cfc94da74/download/tirtl_sites.csv"
)
MATCH_DIST_M = 25.0
MATCH_BEARING_DEG = 45.0
SANITY_RANGE = (0.2, 5.0)
# A 15-minute bin in 06:00-10:00 below this share of that window's highest bin marks the TIRTL
# series as unreliable in the AM peak (qa round 1: 504 inbound collapses to 278/15 min).
TIRTL_AM_COLLAPSE = 0.35
# AADT road names whose OSM/SUMO edges carry another name (the CityLink tunnels are named
# "CityLink" in OSM).
NAME_ALIASES = {"BURNLEY TUNNEL": "CityLink", "DOMAIN TUNNEL": "CityLink"}

DIRECTION_BEARING = {
    "NORTH BOUND": 0,
    "NORTH EAST BOUND": 45,
    "EAST BOUND": 90,
    "SOUTH EAST BOUND": 135,
    "SOUTH BOUND": 180,
    "SOUTH WEST BOUND": 225,
    "WEST BOUND": 270,
    "NORTH WEST BOUND": 315,
}


def tirtl_day_path(day: dt.date) -> Path:
    return TIRTL_RAW / f"TIRTLDATA_{day:%Y%m%d}.csv.gz"


def download_tirtl(day: dt.date) -> Path:
    """The day's TIRTL CSV out of its monthly ZIP (one range request), cached gzipped."""
    import hashlib
    import urllib.request

    from pipeline.download import USER_AGENT, _fetch
    from pipeline.scats_download import _fetch_member, _remote_zip

    TIRTL_RAW.mkdir(parents=True, exist_ok=True)
    sites = TIRTL_RAW / "tirtl_sites.csv"
    if not sites.exists():
        _fetch(
            urllib.request.Request(TIRTL_SITES_CSV, headers={"User-Agent": USER_AGENT}), sites, 60
        )
    dest = tirtl_day_path(day)
    if dest.exists():
        return dest
    if (day.year, day.month) != (2026, 10):
        raise SystemExit(f"TIRTL ZIP URL for {day:%B %Y} not configured; add it first")
    remote, zf = _remote_zip(TIRTL_ZIP_OCT_2026)
    info = zf.getinfo(f"TIRTLDATA_{day:%Y%m%d}.csv")
    data = _fetch_member(remote, info)
    dest.write_bytes(gzip.compress(data, mtime=0))
    mpath = TIRTL_RAW / "MANIFEST.json"
    manifest = json.loads(mpath.read_text()) if mpath.exists() else {}
    manifest[dest.name] = {
        "zip_url": TIRTL_ZIP_OCT_2026,
        "zip_member": info.filename,
        "zip_offset": info.header_offset,
        "compressed_bytes": info.compress_size,
        "bytes": info.file_size,
        "sha256_csv": hashlib.sha256(data).hexdigest(),
        "downloaded": dt.date.today().isoformat(),
    }
    mpath.write_text(json.dumps(manifest, indent=1) + "\n")
    return dest


def tirtl_summary(path: Path, sites: dict[str, str] = TIRTL_SITES) -> dict:
    """Per site and heading: 96 x 15-minute totals (all classes) and the heavy share."""
    acc: dict[tuple[str, str], dict[str, list[int]]] = {}
    with gzip.open(path, "rt", newline="") as fh:
        for r in csv.DictReader(fh):
            if r["site"] not in sites:
                continue
            h, m = (int(x) for x in r["time_bin"].split(":"))
            i = (h * 60 + m) // 15
            a = acc.setdefault((r["site"], r["heading"]), {"all": [0] * 96, "heavy": [0] * 96})
            v = int(r["volume"])
            a["all"][i] += v
            if int(r["vehicle_class"]) >= 3:
                a["heavy"][i] += v
    out = {}
    for (site, heading), a in sorted(acc.items()):
        tot = sum(a["all"])
        out[f"{site}_{heading}"] = {
            "site": site,
            "description": sites[site],
            "heading": heading,
            "vehicles_day": tot,
            "vehicles_07_10": sum(a["all"][28:40]),
            "heavy_share": round(sum(a["heavy"]) / tot, 3) if tot else None,
            "am_reliability": am_reliability(a["all"]),
            "bins_15min": a["all"],
        }
    return out


def am_reliability(bins: list[int]) -> dict:
    """'unreliable' if a 06:00-10:00 bin falls below TIRTL_AM_COLLAPSE x that window's peak."""
    am = bins[24:40]
    peak = max(am) if am else 0
    low = min(am) if am else 0
    i = 24 + am.index(low) if am else None
    bad = peak > 0 and low < TIRTL_AM_COLLAPSE * peak
    return {
        "status": "unreliable" if bad else "plausible",
        "am_peak_bin": peak,
        "am_min_bin": low,
        "am_min_at": f"{i * 15 // 60:02d}:{i * 15 % 60:02d}" if i is not None else None,
    }


def _angle_diff(a: float, b: float) -> float:
    d = abs(a - b) % 360.0
    return min(d, 360.0 - d)


def aadt_segments(path: Path = AADT_RAW) -> list[dict]:
    from shapely.geometry import box, shape

    clip = box(*SIM_BBOX)
    fc = json.loads(path.read_text())
    out = []
    for f in fc["features"]:
        if not f.get("geometry"):
            continue
        g = shape(f["geometry"])
        if not g.intersects(clip):
            continue
        p = f["properties"]
        out.append({"geometry": g, "props": p})
    return out


def match_segments(segments: list[dict], index) -> list[dict]:
    """AADT segment -> SUMO car edges (same street name, near, same travel direction)."""
    from shapely.geometry import LineString

    from pipeline.count_mapping import bearing_deg, names_match

    edges = []
    for e in index.net.getEdges():
        if e.getFunction() == "internal" or not e.allows("passenger"):
            continue
        shp = e.getShape()
        line = LineString(shp)
        mid = line.interpolate(0.5, normalized=True)
        brg = bearing_deg(*shp[0], *shp[-1])
        edges.append((e.getID(), e.getName() or "", mid, brg))
    out = []
    for s in segments:
        p = s["props"]
        xy = [index.xy(lon, lat) for lon, lat in s["geometry"].coords]
        seg = LineString(xy)
        want = DIRECTION_BEARING.get(p["Travel Direction"])
        ids = []
        for eid, name, mid, brg in edges:
            road = NAME_ALIASES.get(p["Road Name"], p["Road Name"].title())
            if not names_match(name, road):
                continue
            if seg.distance(mid) > MATCH_DIST_M:
                continue
            if want is not None and _angle_diff(brg, want) > MATCH_BEARING_DEG:
                continue
            ids.append(eid)
        out.append(
            {
                "segment_id": int(p["Road Segment ID"]),
                "road_name": p["Road Name"],
                "description": p["Road Section Description"].strip(),
                "direction": p["Travel Direction"],
                "aadt_2019": p["Average Annual Daily Traffic Volume"],
                "heavy_vehicles_2019": p["Average Annual Daily Heavy Vehicle Volume"],
                "heavy_share_2019": p["Percentage of Heavy Vehicles"],
                "methodology": p["Calculation Methodology"],
                "matched_edge_ids": sorted(ids),
            }
        )
    return out


def scats_vs_aadt(counts_report: dict, segments: list[dict]) -> dict:
    by_edge: dict[str, list[dict]] = {}
    for s in segments:
        for e in s["matched_edge_ids"]:
            by_edge.setdefault(e, []).append(s)
    rows = []
    for day in counts_report.get("per_day", []):
        for a in day["approaches_used"]:
            for s in by_edge.get(a["edge_id"], []):
                ratio = a["vehicles_day"] / s["aadt_2019"] if s["aadt_2019"] else None
                rows.append(
                    {
                        "date": day["date"],
                        "approach_id": a["approach_id"],
                        "edge_id": a["edge_id"],
                        "scats_vehicles_day": a["vehicles_day"],
                        "scats_bins": a["bins"],
                        "aadt_segment_id": s["segment_id"],
                        "aadt_2019": s["aadt_2019"],
                        "aadt_methodology": s["methodology"],
                        "ratio": round(ratio, 2) if ratio is not None else None,
                        "flag": ratio is not None
                        and not (SANITY_RANGE[0] <= ratio <= SANITY_RANGE[1]),
                    }
                )
    ratios = sorted(r["ratio"] for r in rows if r["ratio"] is not None)
    return {
        "note": "Order-of-magnitude check only: SCATS day total on the approach edge vs the "
        "2019 AADT of an AADT segment matched to the same edge. Different year (2019 vs 2026), "
        "a single day vs an annual average, and a stop-line approach vs a road segment; a "
        f"ratio outside {list(SANITY_RANGE)} is flagged.",
        "pairs": len(rows),
        "approaches_compared": len({r["approach_id"] for r in rows}),
        "median_ratio": ratios[len(ratios) // 2] if ratios else None,
        "flagged": sum(r["flag"] for r in rows),
        "rows": rows,
    }


def build(target_day: dt.date, index=None, counts_report_path: Path | None = None) -> dict:
    from pipeline.count_mapping import NetIndex

    index = index or NetIndex()
    segs = aadt_segments()
    matched = match_segments(segs, index)
    tirtl_path = download_tirtl(target_day)
    rep = {
        "description": "Reference volumes for checks only; never used for fitting.",
        "licence": "Derived from DTP Historical Annual Average Daily Traffic Volume (2019) and "
        "DTP TIRTL Traffic Counts and Classification, CC BY 4.0, modified; see data/SOURCES.md",
        "attribution": "Contains Department of Transport and Planning (Victoria) data, CC BY 4.0",
        "aadt_2019": {
            "note": "Directional AADT segments intersecting SIM_BBOX, matched to SUMO car edges "
            f"by name, distance <= {MATCH_DIST_M} m and travel bearing within "
            f"{MATCH_BEARING_DEG} deg. 2019 is pre-COVID and pre-West Gate Tunnel.",
            "segments_count": len(matched),
            "segments_with_edges": sum(bool(m["matched_edge_ids"]) for m in matched),
            "segments": matched,
        },
        "tirtl": {
            "date": target_day.isoformat(),
            "note": "UPSTREAM BOUND: West Gate Bridge is about 3 km west of the model boundary, "
            "with exits in between; these volumes bound, not equal, the West Gate Fwy flow "
            "entering the model. bins_15min[i] covers local time i*15 min to (i+1)*15 min.",
            "sites": tirtl_summary(tirtl_path),
        },
    }
    from pipeline.counts import qc_days_for

    cmp_day = qc_days_for(target_day)[0]
    cmp = tirtl_summary(download_tirtl(cmp_day))
    rep["tirtl"]["comparison_day"] = {
        "date": cmp_day.isoformat(),
        "sites": {
            k: {f: v[f] for f in ("vehicles_day", "vehicles_07_10", "am_reliability")}
            for k, v in cmp.items()
        },
    }
    rep["tirtl"]["assessment"] = (
        "504 inbound (EB) is INCOMPLETE / UNRELIABLE in the AM peak: on the target day it "
        "collapses mid-peak (see am_reliability) and its day total is well below outbound; the "
        "same pattern appears on the comparison day (same weekday a week earlier). Either the "
        "logger undercounts in stop-and-go traffic or flow really breaks down; we cannot tell "
        "which, so do not use 504 as a bound for the AM peak. 505 outbound (WB) looks plausible."
    )
    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(rep, indent=1) + "\n")
    if counts_report_path and counts_report_path.exists():
        cr = json.loads(counts_report_path.read_text())
        cr["aadt_sanity"] = scats_vs_aadt(cr, matched)
        counts_report_path.write_text(json.dumps(cr, indent=1, default=str) + "\n")
    return rep
