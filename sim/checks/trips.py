"""Checks on an exported scenario (sim/out/trips_<scenario>.json), reported as numbers.

Run as part of `uv run python -m sim.run baseline`; written to
sim/out/checks/trips_<scenario>_checks.json. Inputs are the export's trips with every recorded
sample (not only the points kept in the file), so each number covers what the browser draws.

    alignment       every recorded sample against the nearest OSM drivable centreline of ANY road
                    in the extract. Gate (orchestrator decision): 99 % within a carriageway width
                    of that nearest way (the sample lane's road: lanes on its side of a two-way
                    road, or half a one-way carriageway, at real widths, + 1.5 m):
                    share_within_any_carriageway. Also reported: the distance distribution, the
                    share within 6 m (too tight for 3+ lanes per direction: outer lane centre
                    ~8 m), and share_within_own_way, the same envelope against the OSM way the
                    sample's own edge was built from (lower: joined junctions stretch edges
                    away from their OSM lines, a known issue)
    corner_cutting  between two consecutive points of the file the browser draws a straight chord:
                    how far it leaves the road surface of the edges the car really drove
                    (target < 1.5 m), and its distance from the recorded samples dropped between
    heights         steps between consecutive points (limit 1 m); z against roads_3d at the
                    projected position, split into plain samples (target within 0.5 m),
                    junction samples and smoothed samples
    buildings       points at ground level (|z| < 1 m) inside a building piece that starts low
                    enough to hit a car (min_height_m < CAR_CLEARANCE_M; stacked-tier pieces
                    raised over a road, e.g. sky bridges and overhangs, are skipped), footprint
                    shrunk by BUILDING_TOLERANCE_M; building passages (roads OSM routes under a
                    building) and tunnel ways near their portals are counted separately
    car_free        recorded samples driving along a car-free corridor (Swanston St through the
                    CBD, Bourke St Mall), from the raw OSM data when it is present
"""

from __future__ import annotations

import json
import math
from collections import Counter
from pathlib import Path

import numpy as np

BUILDING_TOLERANCE_M = 1.0
# A building piece whose base is at least this high (m above ground) clears a car (~1.5 m tall).
CAR_CLEARANCE_M = 2.5
ALIGN_TARGET_M = 6.0
CHORD_TARGET_M = 1.5
Z_TARGET_M = 0.5
CORRIDOR_M = 4.0


def _pct(a: np.ndarray) -> dict:
    if len(a) == 0:
        return {"n": 0}
    return {
        "n": int(len(a)),
        "p50": round(float(np.percentile(a, 50)), 2),
        "p90": round(float(np.percentile(a, 90)), 2),
        "p99": round(float(np.percentile(a, 99)), 2),
        "max": round(float(np.max(a)), 2),
    }


def alignment(trips, osm, mv_xy, envelope=None, edge_names=None) -> dict:
    import shapely
    from shapely.strtree import STRtree

    lines = [
        shapely.linestrings([mv_xy[n] for n in refs])
        for refs, _ in osm.ways.values()
        if len(refs) >= 2
    ]
    tree = STRtree(lines)
    xs = np.array([s.x for tr in trips for s in tr.samples])
    ys = np.array([s.y for tr in trips for s in tr.samples])
    _, dist = tree.query_nearest(shapely.points(xs, ys), return_distance=True, all_matches=False)
    within = float(np.mean(dist <= ALIGN_TARGET_M)) if len(dist) else 1.0
    extra = {}
    if envelope is not None:
        # how far a sample may sit from a centreline: the lanes on its side (two-way road) or
        # half the carriageway (one-way), at their real widths, + 1.5 m
        lanes = [s.lane for tr in trips for s in tr.samples]
        allow = np.array([envelope(ln) for ln in lanes])
        ok = dist <= allow + 1.5
        far = Counter(edge_names(lanes[i]) for i in np.nonzero(dist > ALIGN_TARGET_M)[0])
        own = [
            (s.proj_dist_m, envelope(s.lane))
            for tr in trips
            for s in tr.samples
            if s.proj_dist_m is not None
        ]
        own_d = np.array([d for d, _ in own])
        own_ok = np.array([d <= a + 1.5 for d, a in own])
        extra = {
            "share_within_any_carriageway": round(float(np.mean(ok)), 5),
            "share_within_own_way": round(float(np.mean(own_ok)), 5) if len(own) else None,
            "own_way_samples": len(own),
            "own_way_note": "samples on a normal edge (not inside a junction), measured against "
            "the OSM way their edge was built from; lower than the any-carriageway share because "
            "junction joining stretches edges away from their OSM lines near joined junctions",
            "distance_to_own_way_m": _pct(own_d),
            "beyond_6m_by_road": dict(far.most_common(15)),
        }
    return {
        **extra,
        "gate": "share_within_any_carriageway >= 0.99 (orchestrator decision); share_within_6m "
        "and share_within_own_way reported, not gated",
        "samples": int(len(dist)),
        "distance_m": _pct(dist),
        "share_within_6m": round(within, 5),
        "target_share": 0.99,
        "pass": bool(extra.get("share_within_any_carriageway", within) >= 0.99),
    }


def corner_cutting(trips, lane_shapes, edge_lanes, junction_shapes=None, between=None) -> dict:
    """Chords against the road surface: every lane of the edges driven between the two points
    (lane centreline +- half its width). SUMO lane changes are instantaneous, so a chord across a
    lane change runs between lane centrelines but stays on the road. `between(a, b)` names the
    lanes SUMO drove between two consecutive samples' lanes with no sample on them (a short edge
    between two junctions), which are road surface too."""
    from shapely.geometry import LineString, Point, Polygon

    cache: dict = {}
    junction_shapes = junction_shapes or {}

    def shapes(lanes: frozenset):
        if lanes not in cache:
            edges = {ln.rsplit("_", 1)[0] for ln in lanes}
            geoms = [
                (LineString(lane_shapes[ln][0]), lane_shapes[ln][1] / 2)
                for e in edges
                for ln in edge_lanes.get(e, [])
                if ln in lane_shapes
            ]
            # the junction areas at the ends of those edges are road surface too (a 0.5 s sample
            # interval can step over a short junction without a sample inside it)
            for e in edges:
                for j in junction_shapes.get(e, ()):
                    if len(j) >= 3:
                        geoms.append((Polygon(j), 0.0))
            cache[lanes] = geoms
        return cache[lanes]

    chord_dev, dropped_dev = [], []
    worst = None
    for tr in trips:
        ss = tr.samples
        for a, b in zip(tr.kept, tr.kept[1:], strict=False):
            pa, pb = ss[a], ss[b]
            # recorded samples dropped between two kept points, against the drawn chord
            if b - a > 1:
                tt = np.array([ss[i].t for i in range(a + 1, b)])
                f = (tt - pa.t) / (pb.t - pa.t)
                ex = pa.x + f * (pb.x - pa.x) - np.array([ss[i].x for i in range(a + 1, b)])
                ey = pa.y + f * (pb.y - pa.y) - np.array([ss[i].y for i in range(a + 1, b)])
                dropped_dev.append(float(np.max(np.hypot(ex, ey))))
            if math.hypot(pb.x - pa.x, pb.y - pa.y) < 2.0:
                continue  # a chord this short cannot leave the lanes by 1 m
            driven = {ss[i].lane for i in range(a, b + 1)}
            if between is not None:
                for i in range(a, b):
                    if ss[i].lane != ss[i + 1].lane:
                        driven.update(between(ss[i].lane, ss[i + 1].lane))
            geoms = shapes(frozenset(driven))
            if not geoms:
                continue
            dev = 0.0
            for f in (0.25, 0.5, 0.75):
                p = Point(pa.x + f * (pb.x - pa.x), pa.y + f * (pb.y - pa.y))
                dev = max(dev, min(max(0.0, g.distance(p) - half) for g, half in geoms))
            chord_dev.append(dev)
            if worst is None or dev > worst[0]:
                worst = (dev, tr.id, pa.t, pa.lane, pb.lane)
    cd = np.array(chord_dev)
    return {
        "chords_checked": int(len(cd)),
        "chord_outside_road_surface_m": _pct(cd),
        "chords_over_1_5m": int(np.sum(cd > CHORD_TARGET_M)),
        "dropped_sample_to_chord_m": _pct(np.array(dropped_dev)),
        "worst": None
        if worst is None
        else {"m": round(worst[0], 2), "trip": worst[1], "t": worst[2], "lanes": worst[3:]},
        "pass": bool(len(cd) == 0 or np.max(cd) < CHORD_TARGET_M),
    }


def heights(trips) -> dict:
    steps_file, steps_all, plain, smoothed, junction = [], [], [], [], 0
    proj = []
    for tr in trips:
        zs = np.array([s.z for s in tr.samples])
        steps_all.append(np.abs(np.diff(zs)))
        zk = zs[tr.kept]
        steps_file.append(np.abs(np.diff(np.round(zk, 2))))
        for s in tr.samples:
            if s.junction:
                junction += 1
            elif s.z_ref is not None:
                (smoothed if s.smoothed else plain).append(abs(s.z - s.z_ref))
                proj.append(s.proj_dist_m)
    sf = np.concatenate(steps_file) if steps_file else np.array([])
    sa = np.concatenate(steps_all) if steps_all else np.array([])
    pl, sm = np.array(plain), np.array(smoothed)
    return {
        "max_step_between_file_points_m": round(float(sf.max()), 3) if len(sf) else 0.0,
        "max_step_between_samples_m": round(float(sa.max()), 3) if len(sa) else 0.0,
        "steps_over_1m": int(np.sum(sf > 1.0 + 1e-9)),
        "plain_samples_vs_roads_3d_m": _pct(pl),
        "plain_samples_over_0_5m": int(np.sum(pl > Z_TARGET_M)),
        "smoothed_samples": int(len(sm)),
        "smoothed_vs_roads_3d_m": _pct(sm),
        "junction_samples_interpolated": junction,
        "sample_to_mapped_way_m": _pct(np.array(proj)),
        "pass": bool(np.sum(sf > 1.0 + 1e-9) == 0 and np.sum(pl > Z_TARGET_M) == 0),
    }


def buildings(
    trips, buildings_path: Path, to_xy, passage_ways: set[int], edge_names, to_lonlat=None
) -> dict:
    import shapely
    from shapely.strtree import STRtree

    fc = json.loads(buildings_path.read_text())
    polys, ids = [], []
    raised = 0
    for f in fc["features"]:
        if (f["properties"].get("min_height_m") or 0.0) >= CAR_CLEARANCE_M:
            raised += 1
            continue
        g = f["geometry"]
        parts = [g["coordinates"]] if g["type"] == "Polygon" else g["coordinates"]
        for rings in parts:
            outer = [to_xy(c[0], c[1]) for c in rings[0]]
            holes = [[to_xy(c[0], c[1]) for c in r] for r in rings[1:]]
            p = shapely.Polygon(outer, holes).buffer(-BUILDING_TOLERANCE_M)
            if not p.is_empty:
                polys.append(p)
                ids.append(f["properties"].get("id"))
    tree = STRtree(polys)
    pts, meta = [], []
    for tr in trips:
        for i in tr.kept:
            s = tr.samples[i]
            if abs(s.z) < 1.0:
                pts.append((s.x, s.y))
                meta.append(s)
    if not pts:
        return {
            "ground_points": 0,
            "inside_buildings": 0,
            "raised_pieces_skipped": raised,
            "pass": True,
        }
    xy = np.array(pts)
    hit_pts, hit_polys = tree.query(shapely.points(xy[:, 0], xy[:, 1]), predicate="within")
    building_of = dict(zip(hit_pts.tolist(), hit_polys.tolist(), strict=False))
    hit = sorted(building_of)
    passage = [i for i in hit if meta[i].way in passage_ways]
    tunnel = [i for i in hit if meta[i].tunnel]
    other = [i for i in hit if meta[i].way not in passage_ways and not meta[i].tunnel]
    places = Counter((edge_names(meta[i].lane), ids[building_of[i]]) for i in other)
    where = []
    for (road, bid), n in places.most_common(30):
        i = next(k for k in other if (edge_names(meta[k].lane), ids[building_of[k]]) == (road, bid))
        lon, lat = to_lonlat(meta[i].x, meta[i].y) if to_lonlat else (None, None)
        where.append({"road": road, "building": bid, "points": n, "lon": lon, "lat": lat})
    return {
        "ground_points": len(pts),
        "inside_buildings": len(hit),
        "on_building_passages": len(passage),
        "raised_pieces_skipped": raised,
        "on_tunnel_ways_near_ground": len(tunnel),
        "elsewhere": len(other),
        "elsewhere_by_road": dict(Counter(edge_names(meta[i].lane) for i in other).most_common(20)),
        "elsewhere_places": where,
        "pass": len(other) == 0,
    }


def car_free(trips, corridors: dict[str, list] | None, edge_names) -> dict:
    """Samples inside a car-free corridor while on an edge named like that corridor."""
    if corridors is None:
        return {"skipped": "raw OSM data not present (it lives only in the main checkout)"}
    import shapely
    from shapely.ops import unary_union

    out = {}
    xs = np.array([s.x for tr in trips for s in tr.samples])
    ys = np.array([s.y for tr in trips for s in tr.samples])
    lanes = [s.lane for tr in trips for s in tr.samples]
    pts = shapely.points(xs, ys)
    for name, (street, lines) in corridors.items():
        if not lines:
            out[name] = {"osm_ways": 0}
            continue
        poly = unary_union([shapely.linestrings(ln) for ln in lines]).buffer(CORRIDOR_M)
        inside = np.nonzero(shapely.contains(poly, pts))[0]
        along = [i for i in inside if edge_names(lanes[i]) == street]
        out[name] = {
            "osm_ways": len(lines),
            "samples_in_corridor": int(len(inside)),
            "samples_driving_along": len(along),
        }
    out["pass"] = all(v.get("samples_driving_along", 0) == 0 for v in out.values())
    return out


def write(report: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")
