"""Trips export: unit tests, a synthetic end-to-end scenario (determinism, heights, checks) and the
schema of the committed sim/out/trips_baseline.json + baseline.meta.json."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import sumolib
from shapely.geometry import Point

from pipeline.config import SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX, buffered
from sim import build_net
from sim.config import OUT_DIR
from sim.export_trips import (
    MAX_STEP_M,
    Sample,
    export_scenario,
    fill_and_smooth,
    simplify,
    split_trips,
)
from sim.tests.test_checks_synthetic import NODES, WAYS, write_osm


def _line(n: int, z_ref=None, dx: float = 5.0, t0: float = 0.0, lane: str = "e_0"):
    return [Sample(t0 + 0.5 * i, dx * i, 0.0, lane, 10.0, z_ref=z_ref) for i in range(n)]


def test_heights_step_limited_through_a_junction():
    before = _line(10, z_ref=0.0)
    inside = _line(2, t0=5.0, lane=":J_0_0")
    for k, s in enumerate(inside):
        s.x = 50 + 5 * k
        s.junction = True
    after = _line(10, z_ref=4.0, t0=6.0)
    for k, s in enumerate(after):
        s.x = 60 + 5 * k
    samples = before + inside + after
    fill_and_smooth(samples)
    steps = [abs(b.z - a.z) for a, b in zip(samples, samples[1:], strict=False)]
    assert max(steps) <= MAX_STEP_M
    assert samples[0].z == 0.0 and samples[-1].z == 4.0
    assert any(s.smoothed for s in samples)


def test_split_at_teleport_gap_and_jump():
    a = _line(4)
    b = _line(4, t0=10.0)  # 8 s gap: vanished from the network
    c = _line(3, t0=12.0)
    for s in c:
        s.x += 500  # an impossible jump
    trips = split_trips("v", a + b + c, 0.5)
    assert [t.id for t in trips] == ["v", "v#1", "v#2"]


def test_simplify_keeps_turns_and_stops_drops_straight_runs():
    s = _line(20, z_ref=0.0)
    for k in range(10, 20):  # turn 90 degrees after sample 9
        s[k].x, s[k].y = 45.0, 5.0 * (k - 9)
    for k in range(20):
        s[k].z = 0.0
    keep = simplify(s)
    assert keep[0] == 0 and keep[-1] == 19
    assert 9 in keep  # the corner
    assert len(keep) <= 4


# --- synthetic scenario -----------------------------------------------------------------------


def _roads_3d(path: Path) -> None:
    """Synthetic heights: the bridge C at 6 m, A2 and A3 at 4 m (a 4 m jump at node 1002)."""
    feats = []
    for wid, (refs, tags) in WAYS.items():
        z = 6.0 if tags.get("bridge") else 4.0 if tags["name"] in ("A2", "A3") else 0.0
        feats.append(
            {
                "type": "Feature",
                "properties": {"way_id": wid},
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[*NODES[n], z] for n in refs],
                },
            }
        )
    path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}))


@pytest.fixture(scope="module")
def scenario(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("scenario")
    osm = tmp / "s.osm.xml"
    write_osm(osm)
    net = tmp / "s.net.xml"
    build_net.run(build_net.osm_pass_command(osm, net, tmp / "j.xml", tmp / "nc.log"))
    roads = tmp / "roads_3d.geojson"
    _roads_3d(roads)
    bld = tmp / "buildings.geojson"
    bld.write_text('{"type": "FeatureCollection", "features": []}')
    routes = tmp / "r.rou.xml"
    routes.write_text(
        "<routes>"
        '<route id="along" edges="2001 2002 2003"/>'
        '<route id="bridge" edges="-2007 2006 2008"/>'
        '<flow id="a" route="along" begin="0" end="60" period="4"/>'
        '<flow id="b" route="bridge" begin="0" end="60" period="6"/>'
        "</routes>"
    )

    def run(name: str) -> tuple[Path, dict]:
        fcd = tmp / f"{name}.fcd.xml"
        subprocess.run(
            [
                sumolib.checkBinary("sumo"),
                "-n",
                str(net),
                "-r",
                str(routes),
                "--step-length",
                "0.5",
                "--end",
                "120",
                "--seed",
                "42",
                "--fcd-output",
                str(fcd),
                "--device.fcd.begin",
                "10",
                "--no-step-log",
            ],
            check=True,
            capture_output=True,
        )
        out = tmp / name
        result = export_scenario(
            "synthetic",
            fcd,
            {},
            out,
            out,
            net,
            begin=10.0,
            end=120.0,
            dt=0.5,
            inputs={
                "osm": osm,
                "roads_3d": roads,
                "buildings": bld,
                "joined": tmp / "none.xml",
                "pbf": tmp / "none.pbf",
            },
        )
        return out, result

    return run


def test_synthetic_export_is_deterministic(scenario):
    a, _ = scenario("one")
    b, _ = scenario("two")
    assert (a / "trips_synthetic.json").read_bytes() == (b / "trips_synthetic.json").read_bytes()
    ma = json.loads((a / "synthetic.meta.json").read_text())
    mb = json.loads((b / "synthetic.meta.json").read_text())
    ma.pop("generated_at"), mb.pop("generated_at")
    assert ma == mb


def test_synthetic_heights_and_checks(scenario):
    out, result = scenario("checks")
    trips = json.loads((out / "trips_synthetic.json").read_text())
    assert trips
    bridge = [t for t in trips if t["id"].startswith("b")]
    assert bridge and any(abs(p[2] - 6.0) < 0.01 for t in bridge for p in t["path"])
    along = [t for t in trips if t["id"].startswith("a")]
    assert any(p[2] == 4.0 for t in along for p in t["path"])  # after the jump at 1002
    for t in trips:
        zs = [p[2] for p in t["path"]]
        assert all(abs(b - a) <= MAX_STEP_M + 1e-9 for a, b in zip(zs, zs[1:], strict=False))
    report = json.loads((out / "trips_synthetic_checks.json").read_text())
    assert report["heights"]["pass"]
    assert report["heights"]["smoothed_samples"] > 0  # the 4 m jump was smoothed
    assert report["corner_cutting"]["chords_over_1_5m"] == 0
    assert report["buildings"]["inside_buildings"] == 0
    assert "skipped" in report["car_free"]


# --- the committed baseline -------------------------------------------------------------------

TRIPS = OUT_DIR / "trips_baseline.json"
META = OUT_DIR / "baseline.meta.json"
needs_export = pytest.mark.skipif(not TRIPS.exists(), reason="baseline not exported")


@pytest.fixture(scope="module")
def baseline():
    return json.loads(TRIPS.read_text()), json.loads(META.read_text())


def _motorway_tunnel_lines():
    """roads_3d motorway / motorway_link ways tagged tunnel, in lon/lat (plan view)."""
    from shapely.geometry import LineString, MultiLineString

    fc = json.loads((build_net.REPO_ROOT / "data" / "processed" / "roads_3d.geojson").read_text())
    return MultiLineString(
        [
            LineString([c[:2] for c in f["geometry"]["coordinates"]])
            for f in fc["features"]
            if f["properties"].get("tunnel") not in (None, False, "no", "building_passage")
            and f["properties"].get("highway") in ("motorway", "motorway_link")
        ]
    )


@needs_export
def test_trips_schema(baseline):
    trips, _ = baseline
    assert isinstance(trips, list) and trips
    ids = set()
    # lanes are drawn beside the clipped OSM centrelines, so allow a little past the extract edge;
    # beyond SIM_EXTRACT_BBOX only on CityLink motorway tunnel ways, inside TUNNEL_EXTRACT_BBOX
    b = buffered(SIM_EXTRACT_BBOX, 100.0)
    tb = buffered(TUNNEL_EXTRACT_BBOX, 100.0)
    tunnels = _motorway_tunnel_lines()
    for t in trips:
        assert set(t) == {"id", "type", "path", "timestamps"}
        assert isinstance(t["id"], str) and t["id"] not in ids
        ids.add(t["id"])
        assert t["type"] == "passenger"
        assert len(t["path"]) == len(t["timestamps"]) >= 2
        for lon, lat, z in t["path"]:
            if not (b.west <= lon <= b.east and b.south <= lat <= b.north):
                assert tb.west <= lon <= tb.east and tb.south <= lat <= tb.north, (lon, lat)
                assert tunnels.distance(Point(lon, lat)) < 2e-4, (t["id"], lon, lat)
            assert isinstance(z, (int, float))
        ts = t["timestamps"]
        assert all(0 <= x <= 900 for x in ts)
        assert all(b2 > a2 for a2, b2 in zip(ts, ts[1:], strict=False))


@needs_export
def test_meta_schema(baseline):
    trips, meta = baseline
    assert set(meta) == {
        "scenario",
        "demand",
        "duration_s",
        "seed",
        "sumo_version",
        "vehicles",
        "teleports",
        "tunnel_vehicles",
        "generated_at",
    }
    assert meta["scenario"] == "baseline"
    assert meta["demand"] == "placeholder_random"
    assert meta["duration_s"] == 900
    assert meta["vehicles"] == len({t["id"].split("#")[0] for t in trips})
    assert 0 <= meta["tunnel_vehicles"] <= meta["vehicles"]


@needs_export
def test_trips_size_is_browser_sane():
    assert TRIPS.stat().st_size < 15e6


def test_junction_point_inserted_on_skipped_lane():
    """A 0.5 s step from one edge to the next across a corner gets a point inside the junction."""
    from sim.export_trips import insert_junction_points

    class FakePaths:
        def via_shape(self, a, b):
            return ([(10.0, 0.0), (14.0, 1.0), (15.0, 5.0)], ":J_0_0")

    a = Sample(0.0, 10.0, 0.0, "in_0", 10.0)
    b = Sample(0.5, 15.0, 5.0, "out_0", 10.0)
    out = insert_junction_points([a, b], FakePaths())
    assert len(out) == 3 and out[1].lane == ":J_0_0"
    assert 0.0 < out[1].t < 0.5
    straight = insert_junction_points([a, Sample(0.5, 15.0, 0.0, "in_0", 10.0)], FakePaths())
    assert len(straight) == 2  # same edge: nothing to add
