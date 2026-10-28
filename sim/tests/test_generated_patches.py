"""Generated patches: lane-arrow sides, private keep list, the excuse rule, applied patches."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest
import sumolib

from sim import build_net
from sim.checks.movements import Seg
from sim.checks.restrictions import _plausible
from sim.config import NET_PATCH_DIR, NET_PATH
from sim.generated_patches import one_sided


def test_one_sided_arrows():
    assert one_sided({"turn:lanes": "right|right", "oneway": "yes"}) == "right"
    assert one_sided({"turn:lanes": "slight_left|left", "oneway": "yes"}) == "left"
    assert one_sided({"turn:lanes": "through|right", "oneway": "yes"}) is None
    assert one_sided({"turn:lanes": "right"}) is None  # two-way: needs :forward/:backward
    assert one_sided({"turn:lanes:backward": "right"}, "turn:lanes:backward") == "right"


def _mv(points: dict[int, tuple[float, float]]):
    return SimpleNamespace(xy=points)


def test_plausible_excuse_rules():
    # east, then a steady right turn south over two bends (a slip lane): plausible
    slip = _mv({1: (0, 0), 2: (10, 0), 3: (17, -5), 4: (20, -15)})
    assert _plausible([Seg(0, 1, 2), Seg(0, 2, 3), Seg(0, 3, 4)], slip)
    # a U-turn (bend > 140 deg) never excuses
    uturn = _mv({1: (0, 0), 2: (10, 0), 3: (0, 1)})
    assert not _plausible([Seg(0, 1, 2), Seg(0, 2, 3)], uturn)
    # left, then right, then right (around the block): zig-zag, not one movement
    detour = _mv({1: (0, 0), 2: (10, 0), 3: (10, 10), 4: (20, 10), 5: (20, 0)})
    assert not _plausible([Seg(0, 1, 2), Seg(0, 2, 3), Seg(0, 3, 4), Seg(0, 4, 5)], detour)


def test_private_keep_list(tmp_path: Path):
    osm = tmp_path / "x.osm.xml"
    osm.write_text(
        '<osm version="0.6"><node id="1" lat="-37.81" lon="144.95"/>'
        '<node id="2" lat="-37.81" lon="144.951"/>'
        '<way id="7"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/>'
        '<tag k="access" v="private"/></way>'
        '<way id="8"><nd ref="1"/><nd ref="2"/><tag k="highway" v="residential"/></way></osm>'
    )
    path = build_net.write_private_keep_list(osm, tmp_path / "keep.txt")
    ids = path.read_text().split()
    assert {"7", "-7", "7#0", "-7#1"} <= set(ids)
    assert not any(i.lstrip("-").startswith("8") for i in ids)


@pytest.mark.skipif(not NET_PATH.exists(), reason="network not built")
def test_lane_patch_connections_exist():
    net = sumolib.net.readNet(str(NET_PATH))
    root = ET.parse(NET_PATCH_DIR / "turn_lanes.con.xml").getroot()
    conns = list(root.iter("connection"))
    assert conns
    for c in conns:
        assert net.getEdge(c.get("to")) in net.getEdge(c.get("from")).getOutgoing()
    deletes = ET.parse(NET_PATCH_DIR / "turn_restrictions.con.xml").getroot().iter("delete")
    pairs = [(d.get("from"), d.get("to")) for d in deletes]
    assert len(pairs) == len(set(pairs))
