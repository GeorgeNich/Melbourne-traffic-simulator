"""SCATS reading and cleaning: -1 handling, fully alarmed days, the site register, the profile."""

from __future__ import annotations

import datetime as dt
import gzip

import numpy as np

from pipeline import scats
from pipeline.config import SIM_BBOX


def _row(site=4562, det=3, values=None, alarm=0, day="2026-10-23"):
    values = values if values is not None else [1] * 96
    r = {"NB_SCATS_SITE": str(site), "QT_INTERVAL_COUNT": day, "NB_DETECTOR": str(det)}
    r.update({f"V{i:02d}": str(v) for i, v in enumerate(values)})
    r.update({"NM_REGION": "MC1", "CT_RECORDS": "96", "CT_ALARM_24HOUR": str(alarm)})
    r["QT_VOLUME_24HOUR"] = str(sum(values) if min(values) >= 0 else -1)
    return r


def test_minus_one_becomes_missing_not_zero():
    vals = [5] * 96
    vals[10] = -1
    site, det, day, dd = scats.parse_row(_row(values=vals, alarm=1))
    assert (site, det, day) == (4562, 3, dt.date(2026, 10, 23))
    assert np.isnan(dd.values[10]) and dd.values[9] == 5
    assert not dd.fully_alarmed and dd.alarm_intervals == 1
    assert np.nansum(dd.values) == 5 * 95  # never QT_VOLUME_24HOUR (-1 here)


def test_fully_alarmed_detector_day():
    _, _, _, dd = scats.parse_row(_row(values=[-1] * 96, alarm=96))
    assert dd.fully_alarmed and np.isnan(dd.values).all()


def test_read_day_gz_filters_sites(tmp_path):
    rows = [_row(site=4562, det=1), _row(site=100, det=1)]
    head = list(rows[0])
    p = tmp_path / "VSDATA_20261023.csv.gz"
    with gzip.open(p, "wt", newline="") as fh:
        fh.write(",".join(head) + "\n")
        for r in rows:
            fh.write(",".join(r[k] for k in head) + "\n")
    out = scats.read_day(p, {4562})
    assert set(out) == {(4562, 1)}


def test_load_sites_handles_bom_and_missing_coordinates(tmp_path):
    p = tmp_path / "sites.csv"
    p.write_text(
        "﻿SITE_NO,SITE_NAME,TYPE,MUNICIPALITY,LATITUDE,LONGITUDE\n"
        "4562,SWANSTON/FLINDERS,INT,MBN,-37.817453,144.967438\n"
        "9999,NOWHERE,INT,MBN,,\n"
        "100,BURWOOD HWY,INT,WTH,-37.85642,145.181054\n",
        encoding="utf-8",
    )
    sites = scats.load_sites(p)
    assert set(sites) == {4562, 100}
    inside = scats.sites_in_bbox(sites, SIM_BBOX)
    assert set(inside) == {4562}


def test_profile_median_iqr_and_day_count():
    v = np.array([[1.0, 10.0], [3.0, np.nan], [5.0, 30.0], [7.0, 20.0]])
    v = np.hstack([v, np.zeros((4, 94))])
    p = scats.profile(v)
    assert p.median[0] == 4.0 and p.n_days[0] == 4
    assert p.median[1] == 20.0 and p.n_days[1] == 3  # NaN day skipped, not zero
    assert p.q25[0] == 2.5 and p.q75[0] == 5.5


def test_profile_of_no_days_is_all_missing():
    p = scats.profile(np.empty((0, 96)))
    assert np.isnan(p.median).all() and (p.n_days == 0).all()


def test_load_detectors_drops_fully_alarmed_days(tmp_path):
    head = list(_row())
    for d, vals in (("2026-10-22", [-1] * 96), ("2026-10-23", [2] * 96)):
        p = tmp_path / f"VSDATA_{d.replace('-', '')}.csv.gz"
        with gzip.open(p, "wt", newline="") as fh:
            fh.write(",".join(head) + "\n")
            r = _row(values=vals, day=d)
            fh.write(",".join(r[k] for k in head) + "\n")
    days, det = scats.load_detectors(sorted(tmp_path.glob("*.gz")), {4562})
    s = det[(4562, 3)]
    assert days == [dt.date(2026, 10, 22), dt.date(2026, 10, 23)]
    assert s.dates == [dt.date(2026, 10, 23)] and s.fully_alarmed_days == [dt.date(2026, 10, 22)]
    assert s.values.shape == (1, 96) and s.rows_in_data == 2


def test_bin_label():
    assert scats.bin_label(0) == "00:00" and scats.bin_label(34) == "08:30"
