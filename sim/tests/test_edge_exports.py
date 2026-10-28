"""edge_geometry.json and edge_stats_<scenario>.parquet: unit tests, a synthetic end-to-end run
(determinism, heights, counts vs FCD) and checks on the committed baseline files."""

from __future__ import annotations

import json
import math
import subprocess
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

import numpy as np
import pytest
import sumolib

from pipeline.config import SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX, buffered
from sim import build_net, edge_geometry, edge_stats
from sim.checks.netindex import way_ids
from sim.config import NET_PATH, OUT_DIR
from sim.tests.test_checks_synthetic import write_osm
from sim.tests.test_export_trips import _roads_3d

# --- unit -------------------------------------------------------------------------------------


def _edgedata(path: Path, intervals: list[tuple[float, list[str]]]) -> Path:
    body = "".join(
        f'<interval begin="{b}" end="{b + 60}" id="s">{"".join(edges)}</interval>'
        for b, edges in intervals
    )
    path.write_text(f"<meandata>{body}</meandata>")
    return path


def _e(eid, ss, speed=None, dep=0, ent=0, arr=0, left=0):
    sp = f' speed="{speed}"' if speed is not None else ""
    return (
        f'<edge id="{eid}" sampledSeconds="{ss}"{sp} departed="{dep}" entered="{ent}" '
        f'arrived="{arr}" left="{left}"/>'
    )


def test_read_bins_balance_rules(tmp_path: Path):
    p = _edgedata(
        tmp_path / "e.xml",
        [
            (0.0, [_e("a", 30, 5.0, dep=2, left=1)]),  # warm-up: no rows, but carried forward
            (60.0, [_e("a", 20, 2.5, ent=1, left=2), _e("b", 0, dep=1)]),  # b: no time, no row
            (120.0, [_e("a", 3, 0.2), _e("b", 10, 10.0, left=1)]),  # a: back overlap only
            (180.0, []),
        ],
    )
    rows, back_only = edge_stats.read_bins(p, 60.0, 180.0, {"a": 10.0, "b": 12.5})
    assert [(r["edge_id"], r["t_bin"], r["vehicle_count"]) for r in rows] == [
        ("a", 0, 2),  # one still on a from the warm-up + one entering
        ("a", 60, 1),  # nobody's front on a any more: lower bound 1
        ("b", 60, 1),  # inserted on b in the previous bin's last step
    ]
    assert back_only == 1
    assert rows[0]["speed_ratio"] == 0.25 and rows[2]["speed_ratio"] == 0.8
    assert all(set(r) == set(edge_stats.COLUMNS) for r in rows)


def test_read_bins_rejects_wrong_period(tmp_path: Path):
    p = tmp_path / "e.xml"
    p.write_text('<meandata><interval begin="0" end="900" id="s"/></meandata>')
    with pytest.raises(ValueError):
        edge_stats.read_bins(p, 0.0, 900.0, {})


def test_keep_points_keeps_vertices_and_height_changes():
    d = np.arange(0.0, 50.0, 5.0)  # 10 samples
    z = np.array([0, 0, 0, 0, 0, 0.3, 0.6, 0.9, 0.9, 0.9])
    vertex = [True] + [False] * 8 + [True]
    keep = edge_geometry.keep_points(d, z, vertex)
    assert keep == [0, 4, 7, 9]  # the ramp's corners
    steep = edge_geometry.keep_points(d, z * 3, vertex)  # 2.7 m ramp: kept points step <= 1 m
    assert all(abs(z[b] * 3 - z[a] * 3) <= 1.0 for a, b in zip(steep, steep[1:], strict=False))
    with_vertex = [*vertex[:5], True, *vertex[6:]]
    assert 5 in edge_geometry.keep_points(d, z, with_vertex)  # shape vertices always stay
    flat = edge_geometry.keep_points(d, np.zeros(10), vertex)
    assert flat == [0, 9]


# --- synthetic scenario -----------------------------------------------------------------------

BEGIN, END = 60.0, 180.0


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("edges")
    osm = tmp / "s.osm.xml"
    write_osm(osm)
    net = tmp / "s.net.xml"
    build_net.run(build_net.osm_pass_command(osm, net, tmp / "j.xml", tmp / "nc.log"))
    roads = tmp / "roads_3d.geojson"
    _roads_3d(roads)
    routes = tmp / "r.rou.xml"
    routes.write_text(
        "<routes>"
        '<route id="along" edges="2001 2002 2003"/>'
        '<route id="bridge" edges="-2007 2006 2008"/>'
        '<flow id="a" route="along" begin="0" end="150" period="3"/>'
        '<flow id="b" route="bridge" begin="0" end="150" period="5"/>'
        "</routes>"
    )
    add = tmp / "e.add.xml"
    add.write_text(
        f'<additional><edgeData id="s" file="{tmp / "es.xml"}" begin="0" end="{END}" '
        'period="60" excludeEmpty="true"/></additional>'
    )
    inputs = {"osm": osm, "roads_3d": roads, "joined": tmp / "none.xml"}

    def run(name: str) -> Path:
        out = tmp / name
        out.mkdir()
        cmd = [sumolib.checkBinary("sumo"), "-n", str(net), "-r", str(routes), "-a", str(add)]
        cmd += ["--step-length", "0.5", "--end", str(END), "--seed", "42", "--no-step-log"]
        cmd += ["--fcd-output", str(out / "fcd.xml"), "--device.fcd.begin", str(BEGIN)]
        subprocess.run(cmd, check=True, capture_output=True)
        (tmp / "es.xml").rename(out / "es.xml")
        edge_stats.export("synthetic", out / "es.xml", net, out, BEGIN, END)
        edge_geometry.export(net, out / "edge_geometry.json", inputs)
        return out

    return run, net


def test_synthetic_exports_are_deterministic(synthetic):
    run, _ = synthetic
    a, b = run("one"), run("two")
    for f in ("edge_stats_synthetic.parquet", "edge_geometry.json"):
        assert (a / f).read_bytes() == (b / f).read_bytes(), f


def test_synthetic_geometry_heights_and_ways(synthetic):
    run, net_path = synthetic
    out = run("geo")
    geo = json.loads((out / "edge_geometry.json").read_text())
    net = sumolib.net.readNet(str(net_path))
    assert [g["edge_id"] for g in geo] == sorted(e.getID() for e in edge_geometry.car_edges(net))
    by_id = {g["edge_id"]: g for g in geo}
    assert all(abs(p[2] - 6.0) < 0.01 for p in by_id["2006"]["path"])  # the bridge C
    assert all(p[2] == 0 for p in by_id["2001"]["path"])  # A1, under the bridge
    assert by_id["2003"]["path"][-1][2] == 4.0  # A3
    for g in geo:
        assert g["way_id"] in way_ids(net.getEdge(g["edge_id"]).getLanes()[0].getParam("origId"))
        zs = [p[2] for p in g["path"]]
        assert all(abs(b - a) <= 1.0 for a, b in zip(zs, zs[1:], strict=False))
    # the two directions of a two-way road are drawn apart (lane centres, not the road centre)
    fwd, rev = by_id["2006"]["path"], by_id["-2006"]["path"]
    assert abs(fwd[0][0] - rev[-1][0]) > 1e-5


def test_synthetic_stats_against_fcd(synthetic):
    """vehicle_count is never below the distinct vehicles FCD saw on the edge in the bin, and
    every edge-bin where FCD saw a car has a row."""
    run, _ = synthetic
    out = run("stats")
    import pyarrow.parquet as pq

    t = pq.read_table(out / "edge_stats_synthetic.parquet").to_pylist()
    rows = {(r["edge_id"], r["t_bin"]): r for r in t}
    seen: dict[tuple[str, int], set] = defaultdict(set)
    for _, el in ET.iterparse(out / "fcd.xml", events=("end",)):
        if el.tag == "timestep":
            tt = float(el.get("time"))
            if BEGIN <= tt < END:
                for v in el.iter("vehicle"):
                    lane = v.get("lane")
                    if not lane.startswith(":"):
                        key = (lane.rsplit("_", 1)[0], int((tt - BEGIN) // 60) * 60)
                        seen[key].add(v.get("id"))
            el.clear()
    assert seen
    for key, vehs in seen.items():
        assert key in rows, key
        assert rows[key]["vehicle_count"] >= len(vehs), key
    assert {r["t_bin"] for r in t} == {0, 60}


# --- the committed baseline -------------------------------------------------------------------

GEOMETRY = OUT_DIR / "edge_geometry.json"
STATS = OUT_DIR / "edge_stats_baseline.parquet"
TRIPS = OUT_DIR / "trips_baseline.json"
needs_geometry = pytest.mark.skipif(not GEOMETRY.exists(), reason="edge geometry not exported")
needs_stats = pytest.mark.skipif(not STATS.exists(), reason="edge stats not exported")


@pytest.fixture(scope="module")
def net():
    return sumolib.net.readNet(str(NET_PATH))


@pytest.fixture(scope="module")
def geometry():
    return json.loads(GEOMETRY.read_text())


@pytest.fixture(scope="module")
def stats():
    import pyarrow.parquet as pq

    return pq.read_table(STATS)


@needs_geometry
def test_geometry_schema_and_edges(geometry, net):
    car = {e.getID() for e in edge_geometry.car_edges(net)}
    assert [g["edge_id"] for g in geometry] == sorted(car)
    b = buffered(SIM_EXTRACT_BBOX, 100.0)  # lane offsets past the clipped centrelines
    tb = buffered(TUNNEL_EXTRACT_BBOX, 100.0)
    tunnel_ways = {8066124, 8066126}  # Burnley / Domain, the only ways past SIM_EXTRACT_BBOX
    for g in geometry:
        assert set(g) == {"edge_id", "way_id", "path"}
        assert not g["edge_id"].startswith(":")
        origin = way_ids(net.getEdge(g["edge_id"]).getLanes()[0].getParam("origId"))
        assert isinstance(g["way_id"], int) and g["way_id"] in origin
        assert len(g["path"]) >= 2
        for lon, lat, z in g["path"]:
            assert isinstance(z, (int, float)) and -40 < z < 60
            if not (b.west <= lon <= b.east and b.south <= lat <= b.north):
                assert g["way_id"] in tunnel_ways, g["edge_id"]
                assert tb.west <= lon <= tb.east and tb.south <= lat <= tb.north


@needs_geometry
def test_geometry_is_in_sync_with_the_network(tmp_path: Path):
    """Re-exporting from the committed network gives the committed bytes."""
    edge_geometry.export(NET_PATH, tmp_path / "g.json")
    assert (tmp_path / "g.json").read_bytes() == GEOMETRY.read_bytes()


@needs_geometry
def test_geometry_size_is_browser_sane():
    assert GEOMETRY.stat().st_size < 2e6


@needs_geometry
@pytest.mark.skipif(not TRIPS.exists(), reason="baseline not exported")
def test_geometry_heights_agree_with_the_trips(geometry):
    """Where a car in trips_baseline.json drives an edge (nearest edge within 8 m, at least 10 m
    from its ends, so junction smoothing is out, and no road within 25 m at another height), its
    z matches the edge's z at the same spot."""
    from shapely import LineString, STRtree, points

    lat0 = -37.82
    kx, ky = 111_320.0 * math.cos(math.radians(lat0)), 111_320.0

    def xy(a):
        a = np.asarray(a, dtype=float)
        return np.c_[(a[:, 0] - 144.95) * kx, (a[:, 1] - lat0) * ky]

    lines, zs, cums = [], [], []
    for g in geometry:
        p = xy(g["path"])
        lines.append(LineString(p))
        zs.append(np.array([q[2] for q in g["path"]]))
        cums.append(np.r_[0.0, np.cumsum(np.hypot(*np.diff(p, axis=0).T))])
    tree = STRtree(lines)
    trip_pts = np.array([q for t in json.loads(TRIPS.read_text()) for q in t["path"]])
    pts = points(xy(trip_pts))
    near: dict[int, list[int]] = defaultdict(list)
    for i, j in zip(*tree.query(pts, predicate="dwithin", distance=25.0), strict=True):
        near[int(i)].append(int(j))
    errs = []
    for i, js in near.items():
        cand = []
        for j in js:
            s = lines[j].project(pts[i])
            cand.append((lines[j].distance(pts[i]), float(np.interp(s, cums[j], zs[j])), s, j))
        heights = [c[1] for c in cand]
        if max(heights) - min(heights) > 1.0:
            continue  # stacked roads
        d, z, s, j = min(cand)
        if d > 8.0 or s < 10.0 or cums[j][-1] - s < 10.0:
            continue
        errs.append(abs(z - trip_pts[i, 2]))
    errs = np.array(errs)
    assert len(errs) > 50_000
    assert errs.max() <= 0.25
    assert (errs <= 0.05).mean() >= 0.99


@needs_stats
def test_stats_schema(stats):
    import pyarrow as pa

    assert stats.schema.names == edge_stats.COLUMNS
    assert stats.schema.field("edge_id").type == pa.string()
    assert stats.schema.field("t_bin").type == pa.int32()
    assert stats.schema.field("vehicle_count").type == pa.int32()
    for c in ("mean_speed_mps", "speed_limit_mps", "speed_ratio"):
        assert stats.schema.field(c).type == pa.float64()
    assert all(stats.column(c).null_count == 0 for c in edge_stats.COLUMNS)


@needs_stats
def test_stats_values(stats, net, geometry):
    rows = stats.to_pylist()
    assert rows
    keys = [(r["edge_id"], r["t_bin"]) for r in rows]
    assert len(set(keys)) == len(keys)
    assert keys == sorted(keys, key=lambda k: (k[1], k[0]))
    geo_ids = {g["edge_id"] for g in geometry}
    meta = json.loads((OUT_DIR / "baseline.meta.json").read_text())
    ratios = []
    for r in rows:
        assert r["edge_id"] in geo_ids  # every row can be drawn
        assert r["t_bin"] % 60 == 0 and 0 <= r["t_bin"] < meta["duration_s"]
        limit = max(ln.getSpeed() for ln in net.getEdge(r["edge_id"]).getLanes())
        assert r["speed_limit_mps"] == limit  # baseline: no time-of-day overrides
        assert r["mean_speed_mps"] >= 0 and r["vehicle_count"] >= 1
        assert r["speed_ratio"] == round(r["mean_speed_mps"] / r["speed_limit_mps"], 4)
        ratios.append(r["speed_ratio"])
    ratios = np.array(ratios)
    assert ratios.max() < 2.0  # SUMO's speed factors are capped at 2
    assert np.percentile(ratios, 99) < 1.3
    assert 0.5 < np.median(ratios) < 1.1
    assert {r["t_bin"] for r in rows} == set(range(0, meta["duration_s"], 60))
    # the CBD 40 km/h area limit shows up in the stats
    assert any(abs(r["speed_limit_mps"] - 40 / 3.6) < 0.01 for r in rows)


@needs_stats
def test_stats_size_is_browser_sane():
    assert STATS.stat().st_size < 5e6
