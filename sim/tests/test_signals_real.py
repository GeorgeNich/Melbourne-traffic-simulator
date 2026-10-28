"""Real signal programs (sim/signals_real.py) and sheet lane arrows (sim/lane_arrows.py).

Uses a hand-written fixture in the verified-sheet schema (sim/tests/fixtures/, not real sites)
and a small synthetic network: a signalised cross (Test St north-south, 2 lanes each way; Cross
St east-west, 1 lane) and a mid-block light further north.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import sumolib

from sim import lane_arrows as la
from sim import signals_real as sr
from sim.config import NET_PATH, REPO_ROOT, sumo_env

FIXTURE = Path(__file__).parent / "fixtures" / "signal_sheets_fixture.json"

NODES = """<nodes>
 <node id="C" x="0" y="0" type="traffic_light"/>
 <node id="N" x="0" y="200"/><node id="S" x="0" y="-200"/>
 <node id="E" x="200" y="0"/><node id="W" x="-200" y="0"/>
 <node id="M" x="0" y="400" type="traffic_light"/><node id="F" x="0" y="600"/>
</nodes>"""
EDGES = """<edges>
 <edge id="sn" from="S" to="C" numLanes="2" name="Test Street"/>
 <edge id="cn" from="C" to="N" numLanes="2" name="Test Street"/>
 <edge id="nc" from="N" to="C" numLanes="2" name="Test Street"/>
 <edge id="cs" from="C" to="S" numLanes="2" name="Test Street"/>
 <edge id="we" from="W" to="C" numLanes="1" name="Cross Street"/>
 <edge id="ce" from="C" to="E" numLanes="1" name="Cross Street"/>
 <edge id="ec" from="E" to="C" numLanes="1" name="Cross Street"/>
 <edge id="cw" from="C" to="W" numLanes="1" name="Cross Street"/>
 <edge id="nm" from="N" to="M" numLanes="1" name="Test Street"/>
 <edge id="mf" from="M" to="F" numLanes="1" name="Test Street"/>
</edges>"""
APPROACHES = {
    ("Test St", "NB"): {"sn"},
    ("Test St", "SB"): {"nc"},
    ("Cross St", "EB"): {"we"},
    ("Cross St", "WB"): {"ec"},
}


def _approach_of(street: str, heading: str) -> set[str]:
    return APPROACHES.get((street, heading), set())


@pytest.fixture(scope="module")
def syn(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("syn")
    (d / "n.nod.xml").write_text(NODES)
    (d / "n.edg.xml").write_text(EDGES)
    net = d / "syn.net.xml"
    subprocess.run(
        [
            shutil.which("netconvert") or "netconvert",
            "-n",
            str(d / "n.nod.xml"),
            "-e",
            str(d / "n.edg.xml"),
            "--lefthand",
            "--no-turnarounds",
            "-o",
            str(net),
        ],
        check=True,
        capture_output=True,
        env=sumo_env(),
    )
    return net


@pytest.fixture(scope="module")
def net(syn):
    return sumolib.net.readNet(str(syn), withPrograms=True)


@pytest.fixture(scope="module")
def sheets() -> dict[str, dict]:
    return {s["site_id"]: s for s in json.loads(FIXTURE.read_text())}


def test_fixture_is_labelled(sheets):
    assert all("TEST FIXTURE" in s["_comment"] for s in sheets.values())


def test_sheet_reading(sheets):
    seq, t = sr.phase_timings(sheets["9001"])
    assert seq == ["A", "B"]
    # reading B (default): max green = the sheet's extension as a total (min 8, extension 17)
    assert (t["B"].min_green, t["B"].max_green, t["B"].yellow, t["B"].all_red) == (8, 17, 3.5, 1.5)
    assert sr.pedestrian_time(sheets["9001"], "A") == 22
    assert sr.pedestrian_time(sheets["9001"], "B") is None
    assert sr.heading_code("southbound") == "SB" and sr.heading_code("nb") == "NB"
    assert sr.phase_timings(sheets["9002"])[0] == ["A", "B", "C"]
    for group in ("timings", "phases"):
        unverified = copy.deepcopy(sheets["9001"])
        unverified["provenance"][group]["status"] = "ai_unverified"
        assert sr.phase_timings(unverified) is None
    assert sr.pedestrian_time(sheets["9003"]) == 20
    assert not sr.usable(sheets["9002"], "signal_groups")


def test_sheet_reading_min_plus(sheets, monkeypatch):
    # reading A (--max-green-min-plus): max green = min green + extension
    monkeypatch.setattr(sr, "MAX_GREEN_TOTAL", False)
    _, t = sr.phase_timings(sheets["9001"])
    assert (t["B"].min_green, t["B"].max_green) == (8, 25)


def test_real_phases(net, sheets):
    tls = net.getTLS("C")
    r = sr.build_for_tls(net, tls, sheets["9001"], _approach_of, False)
    assert r.tier == "real_phases", r.notes
    links = sr.tls_links(net, tls)
    test_st = {lk.index for lk in links if lk.from_edge in {"sn", "nc"}}
    cross_st = {lk.index for lk in links if lk.from_edge in {"we", "ec"}}
    greens = [p for p in r.program if p.min_dur is not None]
    assert [(p.min_dur, p.max_dur) for p in greens] == [(10, 30), (8, 17)]
    a, b = (p.state for p in greens)
    assert {i for i, c in enumerate(a) if c in "Gg"} == test_st
    assert {i for i, c in enumerate(b) if c in "Gg"} == cross_st
    # right turns filter (yield to the opposing through); throughs and lefts are protected
    for lk in links:
        if lk.index in test_st:
            assert a[lk.index] == ("g" if lk.turn == "R" else "G")
    # A -> B: yellow 4 s then all-red 2 s; B -> A: yellow 3.5 s then all-red 1.5 s
    durations = [p.duration for p in r.program]
    assert durations == [30, 4, 2, 17, 3.5, 1.5]
    assert set(r.program[2].state) == {"r"} and set(r.program[1].state) == {"y", "r"}


def test_real_phases_skip_phases_without_cars(net, sheets):
    site = copy.deepcopy(sheets["9001"])
    site["phases"]["sequence"] = "ACB"
    site["phases"]["by_phase"]["C"] = {
        "signal_groups": ["4"],
        "min_green_s": 7.0,
        "max_green_s": 0.0,
        "yellow_s": 3.0,
        "all_red_s": 2.0,
    }
    r = sr.build_for_tls(net, net.getTLS("C"), site, _approach_of, False)
    assert r.tier == "real_phases"
    assert [p.duration for p in r.program] == [30, 4, 2, 17, 3.5, 1.5]


def test_real_phases_rejected_when_a_movement_is_missing(net, sheets):
    site = copy.deepcopy(sheets["9001"])
    site["signal_groups"]["3"]["movements"] = site["signal_groups"]["3"]["movements"][:3]
    r = sr.build_for_tls(net, net.getTLS("C"), site, _approach_of, False)
    assert r.tier == "real_timings"
    assert any("in no phase" in n for n in r.notes)


def test_turn_of_dir_knows_both_uturn_letters():
    # lefthand networks write U-turn connections with dir="T"; "t" is the righthand letter
    assert sr.TURN_OF_DIR["t"] == "U" and sr.TURN_OF_DIR["T"] == "U"


def test_unphased_link_runs_as_filter_and_orphan_group_warns(net, sheets):
    # move the NB right turn into a group no phase runs: its link must join phase A as a
    # filter (its approach-mates are green there), the orphan group must be warned about,
    # and with every movement still resolving the program is accepted
    site = copy.deepcopy(sheets["9001"])
    site["signal_groups"]["1"]["movements"] = site["signal_groups"]["1"]["movements"][:2]
    site["signal_groups"]["9"] = {
        "kind": "vehicle",
        "movements": [{"street": "Test St", "heading": "NB", "turn": "R"}],
    }
    r = sr.build_for_tls(net, net.getTLS("C"), site, _approach_of, False)
    assert r.tier == "real_phases"
    assert any("run as filter" in n for n in r.notes)
    assert any("no phase of the sequence" in n and "9" in n for n in r.notes)


def test_demotion_applies_only_at_sites_with_resolve_failures(net, sheets):
    # the Cross St WB right is orphaned (joins phase B as a filter, yielding to the EB
    # through) against a default program that protects every link: trusted while all
    # movements resolve, demoted once a resolve failure shows the mapping lost something
    tls = net.getTLS("C")
    links = sr.tls_links(net, tls)
    site = copy.deepcopy(sheets["9001"])
    site["signal_groups"]["3"]["movements"] = [
        m
        for m in site["signal_groups"]["3"]["movements"]
        if not (m["heading"] == "WB" and m["turn"] == "R")
    ]
    seq, timings = sr.phase_timings(site)
    all_protected = ["G" * len(links)]
    prog, notes = sr.real_phases(links, site, seq, timings, _approach_of, all_protected)
    assert prog is not None and any("run as filter" in n for n in notes)
    site["signal_groups"]["1"]["movements"].append(
        {"street": "Nowhere St", "heading": "NB", "turn": "T"}
    )
    prog, notes = sr.real_phases(links, site, seq, timings, _approach_of, all_protected)
    assert prog is None
    assert any("lose their protected green" in n for n in notes)


def test_auxiliary_movements_never_resolve_or_fail(net, sheets):
    # a separate stop line of the same controller: skipped before approach lookup, so it
    # contributes no links and no resolve-failure note
    site = copy.deepcopy(sheets["9001"])
    site["signal_groups"]["1"]["movements"].append(
        {"street": "Nowhere St", "heading": "NB", "turn": "T", "auxiliary": True}
    )
    r = sr.build_for_tls(net, net.getTLS("C"), site, _approach_of, False)
    assert r.tier == "real_phases"
    assert not any("Nowhere" in n for n in r.notes)


def test_real_phases_rejected_when_throughs_cross(net, sheets):
    site = copy.deepcopy(sheets["9001"])
    site["phases"]["by_phase"]["A"]["signal_groups"] = ["1", "2", "3"]
    r = sr.build_for_tls(net, net.getTLS("C"), site, _approach_of, False)
    assert r.tier == "real_timings"
    assert any("crossing throughs" in n for n in r.notes)


def test_timings_only(net, sheets):
    tls = net.getTLS("C")
    r = sr.build_for_tls(net, tls, sheets["9002"], _approach_of, False)
    assert r.tier == "real_timings"
    default = sr._default_phases(tls)
    greens = [p for p in r.program if p.min_dur is not None]
    # netconvert's two green phases (equal default lengths) share the sheet's max cycle;
    # phase C (no signal groups, no cars) is dropped from it
    assert [p.state for p in greens] == [p.state for p in default if "y" not in p.state]
    cycle = (38 + 4 + 3) + (14 + 3 + 2)  # 64 s (reading B: max green = the sheet's extension)
    assert {(p.min_dur, p.max_dur) for p in greens} == {(9, (cycle - 2 * (4 + 3)) / 2)}
    assert any("dropped" in n for n in r.notes)
    assert sum(p.max_dur or p.duration for p in r.program) == pytest.approx(cycle)
    yellows = [p for p in r.program if "y" in p.state]
    assert {p.duration for p in yellows} == {4}
    reds = [p for p in r.program if set(p.state) <= {"r"}]
    assert {p.duration for p in reds} == {3}


def test_mid_block(net, sheets):
    tls = net.getTLS("M")
    r = sr.build_for_tls(net, tls, sheets["9003"], _approach_of, True)
    assert r.tier == "real_timings" and r.reason == "mid-block crossing"
    # the pedestrian-only phase C is dropped (cars-only simulation): the light stays green
    assert [(x.state, x.duration) for x in r.program] == [("G", sr.POS_VEHICLE_GREEN_CAP_S)]
    # a crossing whose second phase also holds a car group keeps walk + clearance
    site = copy.deepcopy(sheets["9003"])
    site["phases"]["by_phase"]["C"]["signal_groups"] = ["P1", "7"]
    p = sr.build_for_tls(net, tls, site, _approach_of, True).program
    assert (p[0].state, p[0].min_dur, p[0].max_dur) == ("G", 32, sr.POS_VEHICLE_GREEN_CAP_S)
    assert [(x.state, x.duration) for x in p[1:]] == [("y", 3.5), ("r", 2), ("r", 20)]
    # a mid-block site never takes over an intersection's light
    r = sr.build_for_tls(net, net.getTLS("C"), sheets["9003"], _approach_of, True)
    assert r.tier == "default"


def test_default_and_sumo_loads_programs(net, syn, sheets, tmp_path):
    rs = [
        sr.build_for_tls(net, net.getTLS("C"), sheets["9001"], _approach_of, False),
        sr.build_for_tls(net, net.getTLS("M"), None, None, False),
    ]
    assert rs[1].tier == "default" and rs[1].program is None
    add = tmp_path / "s.add.xml"
    body = "\n".join(sr.program_xml(r.tls, r.program) for r in rs if r.program)
    add.write_text(f"<additional>\n{body}\n</additional>\n")
    proc = subprocess.run(
        [shutil.which("sumo") or "sumo", "-n", str(syn), "-a", str(add), "--end", "200"],
        capture_output=True,
        text=True,
        env=sumo_env(),
    )
    assert proc.returncode == 0, proc.stderr
    assert "Error" not in proc.stderr


# --- lane arrows -------------------------------------------------------------------------------


def test_lane_key_and_turns(sheets):
    assert la.split_key("William St southbound") == ("William St", "SB")
    assert la.split_key("Spring St SB") == ("Spring St", "SB")
    assert la.split_key("Nowhere") is None
    turns, notes = la.lane_turns(sheets["9001"]["lane_layout"]["Test St NB"])
    assert turns == [{"L", "T"}, {"R"}]  # bicycle lane left out
    hook = [{"lane": 1, "use": "general", "arrows": "through+left", "hours": "Hook turn right"}]
    assert la.lane_turns(hook)[0] == [{"T", "L", "R"}]
    assert la.lane_turns([{"lane": 1, "use": "general", "arrows": "sideways"}]) is None
    # unmarked lanes: road-rule defaults (left from the left lane, right from the right lane)
    bare = [{"lane": n, "use": "general", "arrows": None} for n in (1, 2, 3)]
    assert la.lane_turns(bare)[0] == [{"L", "T"}, {"T"}, {"T", "R"}]
    tram = [
        {"lane": 1, "use": "general", "arrows": "TR"},
        {"lane": 2, "use": "tram", "arrows": None},
    ]
    assert la.lane_turns(tram)[0] == [{"T", "R"}]
    assert la.lane_turns({"lanes": 1}) is None


def test_lane_map():
    assert la.lane_map([0, 1], [0, 1], "T") == [(0, 0), (1, 1)]
    assert la.lane_map([0], [0, 1, 2], "T") == [(0, 0), (0, 1), (0, 2)]
    assert la.lane_map([2], [0, 1], "R") == [(2, 0), (2, 1)]
    assert la.lane_map([1, 2], [0, 1, 2], "R") == [(1, 0), (1, 1), (2, 2)]
    assert la.lane_map([0, 1, 2], [0, 1], "T") == [(0, 0), (1, 1), (2, 1)]


def test_plan_approach(net, sheets):
    turns, _ = la.lane_turns(sheets["9001"]["lane_layout"]["Test St NB"])
    ap = la.Approach("9001", "Test St NB", "sn")
    la.plan_approach(net.getEdge("sn"), turns, set(), ap)
    assert ap.status == "changed"
    assert ap.after == {"cw": [0], "cn": [0], "ce": [1]}  # left + through from lane 0, right from 1
    assert ("cn", 0, 0) in ap.connections and ("cn", 0, 1) in ap.connections
    # three general lanes shown on a two-lane edge: widened, unless another patch owns the edge
    wide = [{"T", "L"}, {"T"}, {"R"}]
    ap = la.Approach("9001", "k", "sn")
    la.plan_approach(net.getEdge("sn"), wide, set(), ap)
    assert (ap.lanes_before, ap.lanes_after, ap.status) == (2, 3, "changed")
    ap = la.Approach("9001", "k", "sn")
    la.plan_approach(net.getEdge("sn"), wide, {"sn"}, ap)
    assert ap.status.startswith("skipped")
    # a lane whose arrows match no movement would be a dead end: skipped
    ap = la.Approach("9001", "k", "sn")
    la.plan_approach(net.getEdge("sn"), [{"T", "L", "R"}, {"U"}], set(), ap)
    assert ap.status.startswith("skipped: lanes [1]") and not ap.connections
    # an approach to a through-only (mid-block) light is not the drawn intersection
    ap = la.Approach("9001", "k", "nm")
    la.plan_approach(net.getEdge("nm"), [{"T"}], set(), ap)
    assert ap.status.startswith("skipped: edge ends at a through-only light")
    # fewer lanes than the edge: skipped
    ap = la.Approach("9001", "k", "sn")
    la.plan_approach(net.getEdge("sn"), [{"T", "L", "R"}], set(), ap)
    assert ap.status.startswith("skipped")


def test_patch_applies_in_netconvert(syn, net, sheets, tmp_path):
    turns, _ = la.lane_turns(sheets["9001"]["lane_layout"]["Test St NB"])
    ap = la.Approach("9001", "Test St NB", "sn")
    la.plan_approach(net.getEdge("sn"), turns, set(), ap)
    con, edg = tmp_path / "a.con.xml", tmp_path / "a.edg.xml"
    la.write_patches([ap], con, edg)
    out = tmp_path / "patched.net.xml"
    subprocess.run(
        [
            shutil.which("netconvert") or "netconvert",
            "--sumo-net-file",
            str(syn),
            "--lefthand",
            "--no-turnarounds",
            "-x",
            str(con),
            "-o",
            str(out),
        ],
        check=True,
        capture_output=True,
        env=sumo_env(),
    )
    e = sumolib.net.readNet(str(out)).getEdge("sn")
    have = {
        (c.getTo().getID(), ln.getIndex(), c.getToLane().getIndex())
        for ln in e.getLanes()
        for c in ln.getOutgoing()
    }
    assert have == set(ap.connections)


# --- the shipped network -----------------------------------------------------------------------


@pytest.mark.skipif(not NET_PATH.exists(), reason="network not built")
def test_count_edges_exist_in_network():
    import pyarrow.parquet as pq

    counts = REPO_ROOT / "data" / "processed" / "counts.parquet"
    if not counts.exists():
        pytest.skip("counts.parquet missing")
    with counts.open("rb") as fh:
        edges = set(pq.read_table(fh, columns=["edge_id"]).column("edge_id").to_pylist())
    net = sumolib.net.readNet(str(NET_PATH))
    assert edges - {e.getID() for e in net.getEdges()} == set()


@pytest.mark.skipif(not la.CON_PATCH.exists(), reason="no sheet lane patch")
def test_sheet_lane_patch_is_in_network():
    import xml.etree.ElementTree as ET

    net = sumolib.net.readNet(str(NET_PATH))
    # local-knowledge turn bans load after the sheet arrows and may delete their connections
    banned: set[tuple[str, str]] = set()
    lk = la.CON_PATCH.parent / "local_knowledge_turns.con.xml"
    if lk.exists():
        banned = {(d.get("from"), d.get("to")) for d in ET.parse(lk).getroot().iter("delete")}
    want: dict[str, set] = {}
    for c in ET.parse(la.CON_PATCH).getroot().iter("connection"):
        if (c.get("from"), c.get("to")) in banned:
            continue
        want.setdefault(c.get("from"), set()).add(
            (c.get("to"), int(c.get("fromLane")), int(c.get("toLane")))
        )
    for e, w in want.items():
        edge = net.getEdge(e)
        have = {
            (c.getTo().getID(), ln.getIndex(), c.getToLane().getIndex())
            for ln in edge.getLanes()
            for c in ln.getOutgoing()
        }
        assert have == w, e


@pytest.mark.skipif(not sr.ADD_PATH.exists(), reason="no real signal programs")
def test_sumo_loads_network_and_programs():
    proc = subprocess.run(
        [shutil.which("sumo") or "sumo", "-n", str(NET_PATH), "-a", str(sr.ADD_PATH)]
        + ["--end", "1", "--no-step-log", "--no-warnings"],
        capture_output=True,
        text=True,
        env=sumo_env(),
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(not sr.ADD_PATH.exists(), reason="no real signal programs")
def test_shipped_programs_match_network():
    import xml.etree.ElementTree as ET

    net = sumolib.net.readNet(str(NET_PATH))
    n_links = {
        t.getID(): len({i for _, _, i in t.getConnections()}) for t in net.getTrafficLights()
    }
    log = json.loads(sr.LOG_PATH.read_text())
    assert sum(log["tiers"].values()) == len(n_links)
    for tl in ET.parse(sr.ADD_PATH).getroot().iter("tlLogic"):
        assert tl.get("programID") == sr.PROGRAM_ID
        assert tl.get("type") == ("actuated" if len(list(tl.iter("phase"))) > 1 else "static")
        for ph in tl.iter("phase"):
            assert len(ph.get("state")) == n_links[tl.get("id")]
