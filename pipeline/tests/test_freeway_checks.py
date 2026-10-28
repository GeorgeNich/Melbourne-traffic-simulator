"""TIRTL summary and the SCATS-vs-AADT sanity ratio, on synthetic inputs."""

from __future__ import annotations

import gzip

from pipeline import freeway_checks as fc


def test_tirtl_summary_sums_classes_and_heavy_share(tmp_path):
    p = tmp_path / "TIRTLDATA_20261023.csv.gz"
    rows = [
        "date,time_bin,site,heading,vehicle_class,speed_bin,volume",
        "2026-10-23,0:00,504,E,1,80km/hr to < 85km/hr,10",
        "2026-10-23,0:00,504,E,10,80km/hr to < 85km/hr,2",
        "2026-10-23,8:15,504,E,1,40km/hr to < 45km/hr,30",
        "2026-10-23,8:15,999,E,1,40km/hr to < 45km/hr,99",
    ]
    with gzip.open(p, "wt") as fh:
        fh.write("\n".join(rows) + "\n")
    s = fc.tirtl_summary(p)["504_E"]
    assert s["vehicles_day"] == 42 and s["bins_15min"][0] == 12 and s["bins_15min"][33] == 30
    assert s["vehicles_07_10"] == 30 and s["heavy_share"] == round(2 / 42, 3)


def test_scats_vs_aadt_flags_out_of_range_ratios():
    report = {
        "per_day": [
            {
                "date": "2026-10-23",
                "approaches_used": [
                    {"approach_id": "a", "edge_id": "e1", "vehicles_day": 9000, "bins": 96},
                    {"approach_id": "b", "edge_id": "e2", "vehicles_day": 100, "bins": 96},
                ],
            }
        ]
    }
    segs = [
        {"segment_id": 1, "aadt_2019": 10000, "methodology": "Actual", "matched_edge_ids": ["e1"]},
        {
            "segment_id": 2,
            "aadt_2019": 10000,
            "methodology": "Estimated",
            "matched_edge_ids": ["e2"],
        },
    ]
    out = fc.scats_vs_aadt(report, segs)
    assert out["pairs"] == 2 and out["flagged"] == 1
    assert [r["ratio"] for r in out["rows"]] == [0.9, 0.01]
