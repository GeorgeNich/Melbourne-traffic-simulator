"""Unit tests for the signal-sheet validation (pipeline/signal_sheets.py) and its outputs."""

import json
from types import SimpleNamespace

import pytest

from pipeline import signal_sheets as ss

AI_OK = {
    "site_no": "1",
    "site_name": "A ST / B ST",
    "phases": {
        "phase_letters": ["A", "B"],
        "sequence": "AB",
        "signal_groups_by_phase": {"A": ["1"], "B": ["2", "P1"]},
    },
    "timings": {
        "phases": {
            "A": {
                "minimum_green": 10,
                "maximum_extension_green": 30,
                "yellow": 3.5,
                "all_red": 2,
                "special_all_red": None,
            },
            "B": {
                "minimum_green": 6,
                "maximum_extension_green": None,
                "yellow": 3,
                "all_red": 2.5,
                "special_all_red": 0,
            },
        },
        "pedestrians": {"P1": {"walk": 8, "clearance_1": 15, "clearance_2": 0, "phase": None}},
    },
    "detectors": [],
}


def test_arrows_and_approach_parsing():
    assert ss.arrows_to_letters("through+left") == "LT"
    assert ss.arrows_to_letters("left_through_right") == "LTR"
    assert ss.arrows_to_letters(None) == ""
    assert ss.parse_approach("Elizabeth St northbound") == ("Elizabeth St", "NB")
    assert ss.parse_approach("Flinders Lane WB") == ("Flinders Lane", "WB")
    assert ss.parse_approach("Somewhere")[1] is None


def test_norm_lane_layout_both_shapes():
    lay = {
        "X St eastbound": [{"lane": 1, "use": "general", "arrows": "through+right"}],
        "Y St NB": {"lanes": 2, "movements": ["left_through", "tram_only"]},
    }
    n = ss.norm_lane_layout(lay)
    assert n["X St eastbound"] == [{"lane": 1, "use": "general", "arrows": "TR"}]
    assert [ln["use"] for ln in n["Y St NB"]] == ["general", "tram"]
    assert n["Y St NB"][0]["arrows"] == "LT"


def test_schema_ranges_and_phase_checks():
    assert ss.check_schema(AI_OK) == []
    assert ss.check_ranges(AI_OK) == []
    bad = json.loads(json.dumps(AI_OK))
    bad["timings"]["phases"]["A"]["yellow"] = 9
    bad["timings"]["phases"]["B"]["minimum_green"] = 2
    out = ss.check_ranges(bad)
    assert any("A.yellow=9" in x for x in out) and any("B.minimum_green=2" in x for x in out)
    bad["phases"]["sequence"] = "ABC"
    bad["phases"]["signal_groups_by_phase"]["A"] = []
    msgs = " ".join(ss.check_phase_consistency(bad))
    assert "!= timings phases" in msgs and "phase A: no signal groups" in msgs
    assert ss.check_schema({"site_no": "1"})  # missing keys reported


def test_register_check():
    reg = {1: SimpleNamespace(lon=144.96, lat=-37.815), 2: SimpleNamespace(lon=145.2, lat=-37.9)}
    assert ss.check_register(1, reg) == []
    assert "outside" in ss.check_register(2, reg)[0]
    assert ss.check_register(3, reg) == ["not in victorian_traffic_signals.csv"]


def test_compare_timings_and_peds_zero_is_blank():
    ours = {
        "A": {
            "minimum_green": 10,
            "maximum_extension_green": 30,
            "yellow": 3.5,
            "all_red": 2.0,
            "special_all_red": 0,
        },
        "B": {
            "minimum_green": 6,
            "maximum_extension_green": 0,
            "yellow": 3.0,
            "all_red": 2.5,
            "special_all_red": None,
        },
    }
    items = ss.compare_timings(AI_OK, ours)
    wrong = [i["item"] for i in items if not i["ok"]]
    assert wrong == ["B.maximum_extension_green"]  # 0 on the sheet, blank in the AI: wrong
    peds = ss.compare_pedestrians(
        AI_OK, {"P1": {"walk": 8, "clearance_1": 15, "clearance_2": None}}
    )
    assert all(i["ok"] for i in peds)


def test_compare_phases_excludes_peds_and_flags_empty():
    items = ss.compare_phases(AI_OK, "A B", {"A": ["1", "5"], "B": ["2", "6"]}, peds={"5", "6"})
    assert [i["ok"] for i in items] == [True, True, True]
    ai = json.loads(json.dumps(AI_OK))
    ai["phases"]["signal_groups_by_phase"] = {"A": [], "B": ["2"]}
    items = ss.compare_phases(ai, "AB", None, phase_letters=["A", "B"])
    assert [(i["item"], i["ok"]) for i in items] == [("sequence", True), ("A.signal_groups", False)]
    assert ss.seq_letters("AB1CDF") == "ABCDF"


def test_compare_lane_layout_rules():
    ai_lay = {
        "X St northbound": [
            {"lane": 1, "use": "general", "arrows": "through+left"},
            {"lane": 2, "use": "tram+general", "arrows": "through"},
        ],
        "Y St eastbound": [{"lane": 1, "use": "general", "arrows": "left"}],
    }
    ours = {
        "X St NB": [
            {"lane": 1, "use": "general", "arrows": "LT"},
            {"lane": 2, "use": "tram", "arrows": None},
        ],
        "Y St EB": None,  # not legible: not compared
    }
    items = ss.compare_lane_layout(ai_lay, ours)
    assert [i["ok"] for i in items] == [True, False]  # tram+general (car) vs tram
    ours["X St NB"].append({"lane": 3, "use": "general", "arrows": None})
    assert len(ss.compare_lane_layout(ai_lay, ours)) == 3


def test_decision_thresholds():
    assert ss.decision(0.05) == "use_ai_fix_errors"
    assert ss.decision(0.0501) == "reread_priority_others_unverified"
    assert ss.decision(0.15) == "reread_priority_others_unverified"
    assert ss.decision(0.151) == "reject_ai_reread_priority"
    assert ss.decision(None) == "no_sample_items"


def test_draw_sample_is_deterministic_and_stratified():
    pool = list(range(1000, 1100)) + [2902, 2903, 2904, 2909, 2922]
    ll = set(range(1000, 1030)) | {2922, 2902, 2903, 2904, 2909}
    a = ss.draw_sample(pool, ll)
    assert a == ss.draw_sample(pool, ll)
    assert len(a) == ss.SAMPLE_SIZE and len(set(a)) == ss.SAMPLE_SIZE
    assert 2922 in a and any(s in a for s in ss.SAMPLE_ONE_OF)
    assert sum(s in ll for s in a) >= ss.SAMPLE_MIN_LANE_LAYOUT


def test_parse_reread():
    text = """# header
## 7
T p9 | A 10 34 3.5 4.0 5.0 | B 10 26 3.0 4.5 -
P p9 | P1 15 15 -
S p3 | AB | A 1 2 5 | B 3
G p2 | 1 v X St NB LT; Y St EB R | 2 bus X St SB R | 5 p P1 Y St
L p2 | X St NB: general/LT, tram/- | Y St EB: ?
N a note
"""
    r = ss.parse_reread(text)[7]
    assert r["timings"]["phases"]["B"]["special_all_red"] is None
    assert r["timings"]["page"] == "PDF page 9"
    assert r["phases"]["by_phase"] == {"A": ["1", "2", "5"], "B": ["3"]}
    g = r["signal_groups"]["groups"]
    assert g["1"]["movements"][2] == {"street": "Y St", "heading": "EB", "turn": "R"}
    assert g["2"]["kind"] == "bus" and g["5"]["ped"] == "P1"
    assert r["ped_groups"] == {"5"}
    assert r["lane_layout"]["approaches"]["Y St EB"] is None
    assert r["lane_layout"]["approaches"]["X St NB"][1] == {
        "lane": 2,
        "use": "tram",
        "arrows": None,
    }
    with pytest.raises(ValueError):
        ss.parse_reread("## 1\nG p2 | 1 v X St NORTH LT\n")


def test_parse_htm_sheet(tmp_path):
    files = tmp_path / "9_files"
    files.mkdir()
    (tmp_path / "9.htm").write_text("<html></html>")
    rows = [
        ["PHASE TIMESETTINGS", ""],
        ["MINIMUM GREEN (1)", "3", "10", "6", "", "", "", "", ""],
        ["MAXIMUM EXTENSION GREEN", "6", "30", "0", "", "", "", "", ""],
        ["YELLOW", "8", "3.5", "3.0", "", "", "", "", ""],
        ["ALL RED", "9", "2.0", "2.5", "", "", "", "", ""],
        ["SPECIAL ALL RED", "10", "0*", "-", "", "", "", "", ""],
        ["PEDESTRIAN TIMESETTINGS", ""],
        ["WALK*", "2", "8.0", "", "", "", "", "", "", ""],
        ["CLEARANCE 1", "3", "15.0", "", "", "", "", "", "", ""],
    ]
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    (files / "sheet007.htm").write_text(f"<table>{body}</table>", encoding="windows-1252")
    seq = "<table><tr><td>V.A. SEQUENCE</td><td>A B</td></tr></table>"
    (files / "sheet003.htm").write_text(seq, encoding="windows-1252")
    h = ss.parse_htm_sheet(tmp_path / "9.htm")
    assert h["sequence"] == "A B"
    assert h["timings"]["A"] == {
        "minimum_green": 10.0,
        "maximum_extension_green": 30.0,
        "yellow": 3.5,
        "all_red": 2.0,
        "special_all_red": 0.0,
    }
    assert h["timings"]["B"]["maximum_extension_green"] == 0.0
    assert h["pedestrians"] == {"P1": {"walk": 8.0, "clearance_1": 15.0}}


def test_site_name_streets():
    assert ss.site_name_streets("ELIZABETH STREET / LA TROBE STREET") == [
        "ELIZABETH STREET",
        "LA TROBE STREET",
    ]
    assert ss.site_name_streets("COLLINS NR BATMAN") == ["COLLINS"]


STATUSES = {"ai_checked", "ai_unverified", "re_read", "rejected", "not_read", "no_sheet"}


@pytest.mark.skipif(not ss.OUT_PATH.exists(), reason="reference not built")
def test_verified_reference_schema():
    out = json.loads(ss.OUT_PATH.read_text())
    assert len(out) == 221 and len({o["site_id"] for o in out}) == 221
    for o in out:
        assert set(o) >= {
            "site_id",
            "sheet_date",
            "sheet_file",
            "phases",
            "signal_groups",
            "pedestrians",
            "lane_layout",
            "provenance",
        }
        assert isinstance(o["site_id"], str)
        for g in (*ss.FIELD_GROUPS, "signal_groups"):
            p = o["provenance"][g]
            assert p["status"] in STATUSES
            if p["status"] == "re_read":
                assert p["page"], (o["site_id"], g)
        if o["phases"]:
            for v in o["phases"]["by_phase"].values():
                assert set(v) == {
                    "signal_groups",
                    "min_green_s",
                    "max_green_s",
                    "yellow_s",
                    "all_red_s",
                }
        for g, v in (o["signal_groups"] or {}).items():
            assert g.isdigit() and v["kind"] in {"vehicle", "pedestrian", "tram", "bicycle", "bus"}
            for m in v["movements"]:
                assert m["turn"] in {"L", "T", "R", "U", "crossing"}
        for appr, lanes in (o["lane_layout"] or {}).items():
            assert ss.parse_approach(appr)[1] is not None
            assert [ln["lane"] for ln in lanes] == list(range(1, len(lanes) + 1))
        if o["provenance"]["lane_layout"]["status"] == "rejected":
            assert o["lane_layout"] is None


@pytest.mark.skipif(not ss.VALIDATION_PATH.exists(), reason="validation not built")
def test_validation_report_consistent():
    v = json.loads(ss.VALIDATION_PATH.read_text())
    assert len(v["sample"]["sites"]) == ss.SAMPLE_SIZE and 2922 in v["sample"]["sites"]
    assert v["sample"]["lane_layout_sites_in_sample"] >= ss.SAMPLE_MIN_LANE_LAYOUT
    for g in ss.FIELD_GROUPS:
        rate = v["error_rates"][g]
        assert v["decisions"][g] == ss.decision(rate)
        n, w = v["items_compared"][g], v["items_wrong"][g]
        assert rate == pytest.approx(w / n, abs=1e-4)
    assert v["automatic_checks_summary"]["sites"] == 221


SPOT = ss.CHECKS_DIR / "signal_sheets_spotcheck" / "index.html"


@pytest.mark.skipif(not SPOT.exists(), reason="spot-check not built")
def test_spotcheck_pack_has_ten_sites_with_images():
    from pipeline.signal_sheets_spotcheck import LANE_SITES, TIMING_SITES

    text = SPOT.read_text()
    assert len(LANE_SITES) == 5 and len(TIMING_SITES) == 5
    for s in (*LANE_SITES, *TIMING_SITES):
        assert f"<h3>{s} " in text
    for src in set(__import__("re").findall(r"src='([^']+)'", text)):
        assert (SPOT.parent / src).exists()
