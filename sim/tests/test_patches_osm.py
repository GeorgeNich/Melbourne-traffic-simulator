"""Hand-written OSM-stage patches: still consistent with the extract, applied in the network."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
import sumolib
from pyproj import Proj
from shapely.geometry import Point, Polygon

from sim.checks.netindex import NetIndex, read_joined, read_osm
from sim.config import JOINED_PATH, NET_PATCH_DIR, NET_PATH, OSM_EXTRACT, OSM_PATCH_DIR
from sim.generated_patches import CBD_LIMIT_MPS, hoddle_grid

EB = OSM_PATCH_DIR / "elizabeth_bourke_signals.nod.xml"
UTM55 = "+proj=utm +zone=55 +ellps=WGS84 +datum=WGS84 +units=m +no_defs"


@pytest.fixture(scope="module")
def osm():
    return read_osm(OSM_EXTRACT)


def test_elizabeth_bourke_join_position_matches_the_osm_nodes(osm):
    join = ET.parse(EB).getroot().find("join")
    ids = [int(n) for n in join.get("nodes").split()]
    proj = Proj(UTM55)
    xy = [proj(*osm.nodes[n]) for n in ids]
    assert float(join.get("x")) == pytest.approx(sum(p[0] for p in xy) / len(xy), abs=0.05)
    assert float(join.get("y")) == pytest.approx(sum(p[1] for p in xy) / len(xy), abs=0.05)


def test_hoddle_grid_corners(osm):
    from pipeline.config import SIM_BBOX

    corners = hoddle_grid(osm)
    assert len(corners) == 4
    for lon, lat in corners:
        assert SIM_BBOX.west < lon < SIM_BBOX.east and SIM_BBOX.south < lat < SIM_BBOX.north
    # Bourke St Mall at Elizabeth St lies inside; Southbank (Southgate) does not
    grid = Polygon(corners)
    assert grid.contains(Point(144.9632, -37.8142))
    assert not grid.contains(Point(144.9680, -37.8205))


needs_net = pytest.mark.skipif(not NET_PATH.exists(), reason="network not built")


@needs_net
def test_elizabeth_bourke_is_one_signalised_junction(osm):
    net = sumolib.net.readNet(str(NET_PATH))
    idx = NetIndex(net, read_joined(JOINED_PATH))
    j = idx.junction(2190478936)
    assert all(idx.junction(n) == j for n in (2190478926, 2190561008, 2190561011))
    node = net.getNode(j)
    assert node.getType() == "traffic_light"
    x, y = node.getCoord()
    assert 0 <= y < 10_000 and 0 <= x < 10_000  # not netconvert's mirrored "no position"
    for stop_line in (13634634815, 3394726646, 3394726641):
        sj = idx.junction(stop_line)
        assert sj is None or not net.getNode(sj).getType().startswith("traffic_light")


@needs_net
def test_cbd_speed_patch_applied():
    net = sumolib.net.readNet(str(NET_PATH))
    ids = [e.get("id") for e in ET.parse(NET_PATCH_DIR / "cbd_speed_limit.edg.xml").iter("edge")]
    assert ids
    for eid in ids:
        assert net.getEdge(eid).getSpeed() == pytest.approx(CBD_LIMIT_MPS)
