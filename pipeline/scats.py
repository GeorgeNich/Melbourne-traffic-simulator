"""SCATS volume data: site register, daily CSV reading, cleaning and the typical-weekday profile.

Inputs (data/raw/scats/, fetched by `pipeline.scats_download`, see data/SOURCES.md):
- `victorian_traffic_signals.csv`: SITE_NO, SITE_NAME, TYPE, MUNICIPALITY, LATITUDE, LONGITUDE.
- `VSDATA_YYYYMMDD.csv.gz`: one row per site x detector per day; V00..V95 = vehicles in each
  15-minute interval (V00 = 00:00-00:15 local time); -1 = no valid count in that interval
  (detector in alarm); CT_ALARM_24HOUR = intervals in alarm; QT_VOLUME_24HOUR is -1 whenever any
  interval is -1, so it is never used (we sum the valid intervals ourselves).

Cleaning rules (Phase 3 plan, Stage 3A step 3):
- an interval value < 0 is "no data" (NaN), never zero;
- a detector-day whose 96 intervals are all < 0 (fully alarmed) is dropped;
- a detector-day with any alarmed interval inside the profile window keeps its valid intervals
  (each bin's median uses only the days that have a valid value in that bin);
- which detectors are vehicle stop-line/approach loops comes from the detector map
  (pipeline/reference/scats_detector_map.json), never from the counts.

Profile: per detector and 15-minute bin, the median across the typical weekdays of the valid
values, with the number of valid days and the inter-quartile range.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from pipeline.config import SIM_BBOX, BBox

REPO_ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = REPO_ROOT / "data" / "raw" / "scats"
SITES_CSV = RAW_DIR / "victorian_traffic_signals.csv"

N_BINS = 96
BIN_MINUTES = 15


@dataclass(frozen=True)
class Site:
    site_no: int
    name: str
    type: str  # INT, POS, FLASH PX, ...
    lon: float
    lat: float


def load_sites(path: Path = SITES_CSV) -> dict[int, Site]:
    """Every site in the register that has coordinates."""
    out: dict[int, Site] = {}
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                lon, lat = float(r["LONGITUDE"]), float(r["LATITUDE"])
            except ValueError:
                continue
            s = int(r["SITE_NO"])
            out[s] = Site(s, r["SITE_NAME"].strip(), r["TYPE"].strip(), lon, lat)
    return out


def sites_in_bbox(sites: dict[int, Site], bbox: BBox = SIM_BBOX) -> dict[int, Site]:
    return {
        k: s
        for k, s in sites.items()
        if bbox.west <= s.lon <= bbox.east and bbox.south <= s.lat <= bbox.north
    }


@dataclass
class DetectorDay:
    values: np.ndarray  # shape (96,), float, NaN where the interval was < 0
    alarm_intervals: int  # CT_ALARM_24HOUR
    fully_alarmed: bool


def parse_row(row: dict[str, str]) -> tuple[int, int, dt.date, DetectorDay]:
    """One CSV row -> (site, detector, date, DetectorDay). -1 (or any negative) -> NaN."""
    raw = np.array([int(row[f"V{i:02d}"]) for i in range(N_BINS)], dtype=float)
    fully = bool((raw < 0).all())
    vals = np.where(raw < 0, np.nan, raw)
    day = dt.date.fromisoformat(row["QT_INTERVAL_COUNT"][:10])
    return (
        int(row["NB_SCATS_SITE"]),
        int(row["NB_DETECTOR"]),
        day,
        DetectorDay(vals, int(row["CT_ALARM_24HOUR"]), fully),
    )


def read_day(
    path: Path, sites: set[int] | None = None
) -> dict[tuple[int, int], tuple[dt.date, DetectorDay]]:
    """(site, detector) -> (date, DetectorDay) for one daily CSV (.csv or .csv.gz)."""
    opener = gzip.open if path.suffix == ".gz" else open
    out = {}
    with opener(path, "rt", newline="") as fh:
        for row in csv.DictReader(fh):
            s = int(row["NB_SCATS_SITE"])
            if sites is not None and s not in sites:
                continue
            site, det, day, dd = parse_row(row)
            out[(site, det)] = (day, dd)
    return out


def day_files(raw_dir: Path = RAW_DIR) -> list[Path]:
    return sorted(raw_dir.glob("VSDATA_*.csv.gz"))


@dataclass
class DetectorSeries:
    """All days for one detector: values (n_days, 96) with NaN for missing intervals."""

    dates: list[dt.date]
    values: np.ndarray
    fully_alarmed_days: list[dt.date]
    rows_in_data: int


def load_detectors(
    paths: list[Path], sites: set[int]
) -> tuple[list[dt.date], dict[tuple[int, int], DetectorSeries]]:
    """Read every day file; per detector stack the usable days (fully alarmed days dropped)."""
    per_day = []
    for p in paths:
        per_day.append(read_day(p, sites))
    dates = sorted({d for day in per_day for (d, _) in day.values()})
    acc: dict[tuple[int, int], dict] = {}
    for day in per_day:
        for key, (d, dd) in day.items():
            a = acc.setdefault(key, {"dates": [], "vals": [], "alarmed": [], "rows": 0})
            a["rows"] += 1
            if dd.fully_alarmed:
                a["alarmed"].append(d)
                continue
            a["dates"].append(d)
            a["vals"].append(dd.values)
    out = {}
    for key, a in acc.items():
        vals = np.array(a["vals"]) if a["vals"] else np.empty((0, N_BINS))
        out[key] = DetectorSeries(a["dates"], vals, sorted(a["alarmed"]), a["rows"])
    return dates, out


@dataclass
class Profile:
    median: np.ndarray  # (96,) NaN where no valid day
    q25: np.ndarray
    q75: np.ndarray
    n_days: np.ndarray  # (96,) valid days per bin


def profile(values: np.ndarray) -> Profile:
    """Median / IQR / valid-day count per bin across days (rows) of `values` (NaN = missing)."""
    if values.size == 0:
        nan = np.full(N_BINS, np.nan)
        return Profile(nan, nan, nan, np.zeros(N_BINS, dtype=int))
    n = np.sum(~np.isnan(values), axis=0)
    with np.errstate(all="ignore"):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            med = np.nanmedian(values, axis=0)
            q25 = np.nanpercentile(values, 25, axis=0)
            q75 = np.nanpercentile(values, 75, axis=0)
    return Profile(med, q25, q75, n.astype(int))


def bin_label(i: int) -> str:
    m = i * BIN_MINUTES
    return f"{m // 60:02d}:{m % 60:02d}"


def sheet_sites(raw_dir: Path = RAW_DIR) -> list[int]:
    """Sites whose configuration sheets we keep a copy of: every SIM_BBOX site that reports in
    the volume data and is an intersection (INT) or a pedestrian-operated signal (POS)."""
    bbox = sites_in_bbox(load_sites(raw_dir / "victorian_traffic_signals.csv"))
    files = day_files(raw_dir)
    if not files:
        raise SystemExit("no VSDATA files in data/raw/scats; run pipeline.scats_download first")
    present = {s for (s, _d) in read_day(files[-1], set(bbox))}
    return sorted(s for s in present if bbox[s].type in ("INT", "POS"))
