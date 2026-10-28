"""Roof heights of building tiers from the City of Melbourne 3D Point Cloud 2018 (for buildings.py).

Source and licence: data/SOURCES.md (CC BY 4.0, photogrammetry, read as published heights under
George's exception in CLAUDE.md constraint 1; no computer vision). Coordinates are handled as in
pipeline/deck_pointcloud.py, except that City of Melbourne lon/lat are read as GDA94 (see COM_CRS)
and moved to the tiles' MGA zone 55; points are bucketed in CELL_M cells.

**Roof statistic** (`roof_surface`), same style as deck_pointcloud's `surface`: for a tier, the
exposed roof is its footprint minus every tier of its structure standing on it, shrunk by
ROOF_EDGE_M (walls, parapets and the photogrammetric 'drape' at roof edges stay out). The roof
level is the median of the points there, then the median again of the points within
ROOF_INLIER_M of it (plant, lift overruns, antennas above and a lower roof seen past an edge are
minorities). It counts only with >= MIN_POINTS inliers making up >= MIN_INLIER_SHARE of the
region; failing that, the dominant level (the fullest 0.5 m height bin, refined the same way)
counts if it holds >= MODE_MIN_SHARE of the points (`dominant_level`: tall towers, whose roof
regions also catch facade points). The height compared with the City of Melbourne tier top is
    roof level - structure ground (City of Melbourne structure_min_elevation) - datum offset
where the datum offset (point-cloud datum minus the City of Melbourne elevations' datum) is the
median of (roof level - footprint_max_elevation) over every flat-roofed tier captured with the
cloud in May 2018 that counts (reported with its n and spread, never assumed).

**Rule** (`apply_rule`): |cloud - CoM| <= AGREE_M -> the CoM height is confirmed by the cloud
(`height_source: com_pointcloud_2018`). A larger difference is reported; the cloud replaces the
City of Melbourne height only if that tier's CoM capture (`date_captured`) predates the cloud's
capture (CLOUD_CAPTURE, May 2018), else the CoM height is kept (`com_open_data`: a re-capture is
newer than the cloud, e.g. a tower finished after 2018). No tier is measured only from the cloud.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import shapely
from shapely.strtree import STRtree

from pipeline.deck_pointcloud import POINTCLOUD_DIR

# City of Melbourne footprint lon/lat are GDA94-based (MGA94 published as lon/lat without the
# GDA2020 shift): read as GDA94 they sit on the cloud's roofs (mean roof inlier share 0.78 on 880
# CBD tiers) and read as GDA2020 (as deck_pointcloud does for OSM roads) they miss by ~1.5 m
# (share 0.47). The output keeps the published lon/lat (they are 0.7 m from OSM outlines, the
# shifted ones 0.9 m).
COM_CRS = "EPSG:4283"  # GDA94 geographic
CLOUD_CRS = "EPSG:28355"  # GDA94 / MGA zone 55


def to_mga():
    from pyproj import Transformer

    return Transformer.from_crs(COM_CRS, CLOUD_CRS, always_xy=True)


CELL_M = 10.0
ROOF_EDGE_M = 1.0
ROOF_INLIER_M = 1.0
MIN_POINTS = 30
MIN_INLIER_SHARE = 0.4
MODE_MIN_SHARE = 0.3
MAX_POINTS_PER_TILE = 20_000  # per tier per tile (uniform stride), plenty for a median
AGREE_M = 3.0
CLOUD_CAPTURE = "20180501"  # May 2018 (SOURCES.md); a CoM capture before this is older
DATUM_MIN_N = 200


@dataclass
class Roof:
    z: float | None  # roof level in the cloud's datum, None if not usable
    n: int  # inliers
    n_region: int
    share: float | None
    p90: float | None
    reason: str | None = None
    method: str = "median"


def roof_surface(zs: np.ndarray) -> Roof:
    """Two-pass median roof level with its confidence; the dominant level as a fallback (see
    module docstring)."""
    n = len(zs)
    if n < MIN_POINTS:
        return Roof(None, n, n, None, None, "too few points")
    p90 = float(np.percentile(zs, 90))
    m0 = float(np.median(zs))
    inl = zs[np.abs(zs - m0) <= ROOF_INLIER_M]
    if len(inl):
        inl = zs[np.abs(zs - float(np.median(inl))) <= ROOF_INLIER_M]
    share = len(inl) / n
    if len(inl) >= MIN_POINTS and share >= MIN_INLIER_SHARE:
        return Roof(float(np.median(inl)), len(inl), n, round(share, 3), p90)
    m = dominant_level(zs)
    if m is not None:
        z, k, sh, method = m
        return Roof(z, k, n, sh, p90, None, method)
    reason = "too few inliers" if len(inl) < MIN_POINTS else "mixed surfaces (inliers < share)"
    return Roof(None, len(inl), n, round(share, 3), p90, reason)


def dominant_level(zs: np.ndarray) -> tuple[float, int, float, str] | None:
    """Fallback for roofs whose median is pulled off by wall points (tall towers) or a lower
    roof: the fullest 0.5 m height bin, refined to the median of the points within
    ROOF_INLIER_M; it counts with >= MODE_MIN_SHARE of the region's points."""
    lo, hi = float(np.floor(zs.min())), float(np.ceil(zs.max())) + 0.5
    h, e = np.histogram(zs, bins=np.arange(lo, hi + 0.1, 0.5))
    k = int(np.argmax(h))
    inl = zs[np.abs(zs - (e[k] + e[k + 1]) / 2) <= ROOF_INLIER_M]
    if len(inl) < MIN_POINTS:
        return None
    m = float(np.median(inl))
    inl = zs[np.abs(zs - m) <= ROOF_INLIER_M]
    share = len(inl) / len(zs)
    if len(inl) < MIN_POINTS or share < MODE_MIN_SHARE:
        return None
    return float(np.median(inl)), len(inl), round(share, 3), "dominant_level"


def tiles_for(area: Any, pc_dir: Path = POINTCLOUD_DIR) -> list[Path]:
    """Downloaded tiles whose index footprint intersects `area` (lon/lat). Missing ones raise."""
    from shapely.geometry import Polygon

    from pipeline.download import parse_tile_index

    idx = pc_dir / "Tile_Index.KML"
    if not idx.exists():
        raise FileNotFoundError(
            f"{idx} missing; run `uv run python -m pipeline.download --pointcloud-only`"
        )
    index = parse_tile_index(idx.read_text(encoding="iso-8859-1"))
    names = sorted(n for n, ring in index.items() if Polygon(ring).intersects(area))
    paths = [pc_dir / f"{n}.las" for n in names]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"point-cloud tiles missing: {missing[:5]} ... ({len(missing)})")
    return paths


def to_mga_geom(g: Any) -> Any:
    tr = to_mga()

    def f(coords: np.ndarray) -> np.ndarray:
        x, y = tr.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(g, f)


def roof_regions(tiers: list[Any]) -> list[Any]:
    """For each tier: its exposed roof in MGA metres, eroded by ROOF_EDGE_M (or None).

    Exposed = the footprint minus every tier of the same structure whose base is at or above
    this tier's top (what stands on the roof hides it from the air)."""
    by_struct: dict[str, list[int]] = {}
    for i, t in enumerate(tiers):
        by_struct.setdefault(t.structure_id, []).append(i)
    mga = [to_mga_geom(t.geom) for t in tiers]
    out: list[Any] = [None] * len(tiers)
    for idxs in by_struct.values():
        for i in idxs:
            t = tiers[i]
            g = mga[i]
            above = [
                mga[j]
                for j in idxs
                if j != i and tiers[j].base >= t.top - 0.3 and mga[j].intersects(g)
            ]
            if above:
                g = g.difference(shapely.union_all(above))
            g = g.buffer(-ROOF_EDGE_M)
            out[i] = None if g.is_empty or g.area < 1.0 else g
    return out


def measure(
    regions: list[Any], tiles: list[Path], log=print, stat=None, xyz: bool = False
) -> list[Any]:
    """`stat` (default `roof_surface`) of the points in each region (MGA metres), reading each
    tile once, sequentially. None for regions that are None. `xyz=True`: `stat` gets an (n, 3)
    array of x, y, z instead of the z values."""
    stat = stat or roof_surface
    import laspy

    valid = [i for i, r in enumerate(regions) if r is not None]
    tree = STRtree([regions[i] for i in valid])
    acc: dict[int, list[np.ndarray]] = {}
    for k, path in enumerate(tiles):
        with laspy.open(path) as f:
            h = f.header
            tb = shapely.box(h.mins[0], h.mins[1], h.maxs[0], h.maxs[1])
            hits = [valid[j] for j in tree.query(tb, predicate="intersects")]
            if not hits:
                continue
            las = f.read()
        x, y = np.asarray(las.x), np.asarray(las.y)
        z = np.asarray(las.z).astype(np.float32)
        cx = np.floor((x - h.mins[0]) / CELL_M).astype(np.int64)
        cy = np.floor((y - h.mins[1]) / CELL_M).astype(np.int64)
        ny = int(cy.max()) + 1 if len(cy) else 1
        key = cx * ny + cy
        order = np.argsort(key, kind="stable")
        ks = key[order]
        for i in hits:
            r = regions[i]
            x0, y0, x1, y1 = r.bounds
            i0, i1 = math.floor((x0 - h.mins[0]) / CELL_M), math.floor((x1 - h.mins[0]) / CELL_M)
            j0, j1 = math.floor((y0 - h.mins[1]) / CELL_M), math.floor((y1 - h.mins[1]) / CELL_M)
            i0, j0 = max(i0, 0), max(j0, 0)
            parts = []
            for ci in range(i0, i1 + 1):
                lo = np.searchsorted(ks, ci * ny + j0, side="left")
                hi = np.searchsorted(ks, ci * ny + j1, side="right")
                if hi > lo:
                    parts.append(order[lo:hi])
            if not parts:
                continue
            sel = np.concatenate(parts)
            m = shapely.contains_xy(r, x[sel], y[sel])
            idx = sel[m]
            if len(idx) > MAX_POINTS_PER_TILE:
                idx = idx[:: math.ceil(len(idx) / MAX_POINTS_PER_TILE)]
            if len(idx):
                acc.setdefault(i, []).append(
                    np.column_stack([x[idx], y[idx], z[idx]]) if xyz else z[idx]
                )
        if log and (k + 1) % 25 == 0:
            log(f"  point cloud: {k + 1}/{len(tiles)} tiles")
    out: list[Any] = [None] * len(regions)
    for i in valid:
        empty = np.empty((0, 3)) if xyz else np.empty(0, dtype=np.float32)
        zs = np.concatenate(acc[i]) if i in acc else empty
        out[i] = stat(zs)
    return out


GROUND_RING_M = (1.5, 6.0)  # ring around an elevated tier where the ground beside it is sampled
GROUND_PCT = 10  # the ground is the low end of what the ring sees (walls, cars, trees above)
GROUND_TOL_M = 1.5  # ground beside a tier this close to its base: it stands on sloping ground


def ground_low(zs: np.ndarray) -> float | None:
    return float(np.percentile(zs, GROUND_PCT)) if len(zs) >= MIN_POINTS else None


def ground_rings(tiers: list[Any]) -> list[Any]:
    """A ring GROUND_RING_M around each tier's footprint (MGA metres)."""
    out = []
    for t in tiers:
        g = to_mga_geom(t.geom)
        out.append(g.buffer(GROUND_RING_M[1]).difference(g.buffer(GROUND_RING_M[0])))
    return out


def datum_offset(tiers: list[Any], roofs: list[Roof | None], tops_ahd: list[float]) -> dict:
    """Median (cloud roof - CoM footprint_max_elevation) over flat 2018 tiers whose two-pass
    median counts."""
    d = [
        r.z - top
        for t, r, top in zip(tiers, roofs, tops_ahd, strict=True)
        if r is not None
        and r.z is not None
        and r.method == "median"
        and t.flat
        and t.top_date < "20190101"
    ]
    if len(d) < DATUM_MIN_N:
        return {"offset_m": 0.0, "n": len(d), "used": False}
    a = np.asarray(d)
    q1, med, q3 = np.percentile(a, [25, 50, 75])
    return {
        "offset_m": round(float(med), 3),
        "n": len(d),
        "iqr_m": round(float(q3 - q1), 3),
        "used": True,
    }


def apply_rule(com_height: float, cloud_height: float | None, com_date: str) -> tuple[float, str]:
    """(height_m, height_source) for one CoM tier; see the module docstring."""
    if cloud_height is None:
        return com_height, "com_open_data"
    if abs(cloud_height - com_height) <= AGREE_M:
        return com_height, "com_pointcloud_2018"
    if com_date and com_date < CLOUD_CAPTURE:
        return cloud_height, "com_pointcloud_2018"
    return com_height, "com_open_data"


# --- crowns ---------------------------------------------------------------------------------
# City of Melbourne tier tops stop at the main roof: crowns, masts and roof frames that the cloud
# sees are missing (101 Collins St: CoM 194 m, its crown frame ~235 m). A CROWN piece is added on
# top of a structure where the cloud clearly shows more above its CoM top.

CROWN_UPPER_SHARE = 0.8  # the structure's upper tiers: tops >= this x the structure's CoM top
CROWN_MIN_STRUCTURE_M = 25.0  # crowns are a skyline matter: structures at least this tall
CROWN_RISE_M = 1.0  # crown points: higher than the CoM top by more than this
CROWN_PCT = 98  # crown top: this percentile of the crown points (antenna tips are outliers)
CROWN_MIN_EXCESS_M = 5.0  # a crown counts if its top exceeds the CoM top by more than this
CROWN_MIN_POINTS = 30
CROWN_MIN_M2 = 4.0  # crown footprint (hull of the crown points) at least this big ...
CROWN_MIN_FRACTION = 0.01  # ... and this share of the upper roof (a bird or a lamp is not)
CROWN_MIN_DENSITY = 1.0  # crown points per m2 of its hull (a sparse scatter is noise)
# A tower crane's jib seen from the air is a long thin line high above a roof: an elongated hull
# (minimum rotated rectangle, long / short side >= CRANE_ASPECT) that the points fill only
# sparsely (occupied 1 m cells / hull area < CRANE_MAX_OCCUPANCY). Seen in the 2018 cloud: the
# Aurora Melbourne Central crane over the Melbourne Central office tower (aspect 28, 0.07).
CRANE_ASPECT = 4.0
CRANE_MAX_OCCUPANCY = 0.3
CROWN_SIMPLIFY_M = 0.5
CONFIRM_SHARE = 0.5  # "the cloud shows the structure": a confirmed tier at least this high ...


@dataclass
class Crown:
    structure_id: str
    geom: Any  # lon/lat, the City of Melbourne footprints' datum (COM_CRS)
    base: float  # the structure's CoM top (m above its ground)
    top: float
    n: int
    area_m2: float
    date: str


def from_mga_geom(g: Any) -> Any:
    from pyproj import Transformer

    tr = Transformer.from_crs(CLOUD_CRS, COM_CRS, always_xy=True)

    def f(coords: np.ndarray) -> np.ndarray:
        x, y = tr.transform(coords[:, 0], coords[:, 1])
        return np.column_stack([x, y])

    return shapely.transform(g, f)


def crown_upper_regions(tiers: list[Any], regions: list[Any]) -> dict[str, Any]:
    """Structure id -> union of the exposed, eroded roofs of its upper tiers (MGA metres), for
    structures at least CROWN_MIN_STRUCTURE_M tall."""
    by: dict[str, list[int]] = {}
    for i, t in enumerate(tiers):
        by.setdefault(t.structure_id, []).append(i)
    out = {}
    for sid, idx in by.items():
        top = max(tiers[i].top for i in idx)
        if top < CROWN_MIN_STRUCTURE_M:
            continue
        upper = [
            regions[i]
            for i in idx
            if regions[i] is not None and tiers[i].top >= CROWN_UPPER_SHARE * top
        ]
        if upper:
            out[sid] = shapely.union_all(upper)
    return out


def shows_structure(tiers: list[Any], roofs: list[Any], off: float, upper_p: float | None) -> bool:
    """The 2018 cloud shows this structure as it is in the City of Melbourne data: a tier at
    least CONFIRM_SHARE of the CoM top high is confirmed within AGREE_M, or (a 2018 capture)
    the cloud over its upper roofs reaches the CoM top within AGREE_M."""
    top = max(t.top for t in tiers)
    for t, r in zip(tiers, roofs, strict=True):
        if r is None or r.z is None or t.top < CONFIRM_SHARE * top:
            continue
        if abs(r.z - off - t.ground_ahd - t.top) <= AGREE_M:
            return True
    top_t = max(tiers, key=lambda t: t.top)
    return top_t.top_date < "20190101" and upper_p is not None and upper_p >= top - AGREE_M


def crown_of(
    pts: np.ndarray, region: Any, ground_ahd: float, off: float, com_top: float
) -> tuple[Any, float, int, float] | str | None:
    """(hull in MGA, crown top, n, area m2) from the upper-roof points; "crane" for a
    crane-like scatter; None if nothing rises clearly above the CoM top."""
    if pts is None or len(pts) < CROWN_MIN_POINTS:
        return None
    z = pts[:, 2] - off - ground_ahd
    h = pts[z > com_top + CROWN_RISE_M]
    if len(h) < CROWN_MIN_POINTS:
        return None
    ztop = float(np.percentile(h[:, 2] - off - ground_ahd, CROWN_PCT))
    if ztop - com_top <= CROWN_MIN_EXCESS_M:
        return None
    hull = shapely.MultiPoint(h[:, :2]).convex_hull.intersection(region)
    hull = hull.simplify(CROWN_SIMPLIFY_M)
    if hull.is_empty or hull.geom_type not in ("Polygon", "MultiPolygon"):
        return None
    a = hull.area
    if a < max(CROWN_MIN_M2, CROWN_MIN_FRACTION * region.area) or len(h) / a < CROWN_MIN_DENSITY:
        return None
    if crane_like(h[:, :2], hull):
        return "crane"
    return hull, ztop, len(h), a


def crane_like(xy: np.ndarray, hull: Any) -> bool:
    """A long thin, sparsely filled hull (see CRANE_ASPECT)."""
    rr = hull.minimum_rotated_rectangle
    if rr.geom_type != "Polygon":
        return False
    xs, ys = rr.exterior.coords.xy
    a = math.hypot(xs[1] - xs[0], ys[1] - ys[0])
    b = math.hypot(xs[2] - xs[1], ys[2] - ys[1])
    aspect = max(a, b) / max(min(a, b), 0.1)
    cells = len(
        set(zip(np.floor(xy[:, 0]).astype(int), np.floor(xy[:, 1]).astype(int), strict=True))
    )
    return aspect >= CRANE_ASPECT and cells / max(hull.area, 1.0) < CRANE_MAX_OCCUPANCY


def crowns(
    tiers: list[Any], regions: list[Any], roofs: list[Any], off: float, tiles: list[Path], log=None
) -> tuple[list[Crown], dict[str, Any]]:
    """Crown pieces (see the constants above) and counts for the report."""
    upper = crown_upper_regions(tiers, regions)
    sids = sorted(upper)
    pts = measure([upper[s] for s in sids], tiles, log=log, stat=lambda a: a, xyz=True)
    by: dict[str, list[int]] = {}
    for i, t in enumerate(tiers):
        by.setdefault(t.structure_id, []).append(i)
    out: list[Crown] = []
    st: dict[str, Any] = {
        "structures_checked": len(sids),
        "not_shown_by_cloud": [],
        "rejected_small_or_sparse": [],
        "rejected_crane_like": [],
    }
    for sid, p in zip(sids, pts, strict=True):
        ts = [tiers[i] for i in by[sid]]
        rs = [roofs[i] for i in by[sid]]
        top_t = max(ts, key=lambda t: t.top)
        z = None if p is None or len(p) == 0 else p[:, 2] - off - top_t.ground_ahd
        upper_p = float(np.percentile(z, CROWN_PCT)) if z is not None and len(z) else None
        c = crown_of(p, upper[sid], top_t.ground_ahd, off, top_t.top)
        if c == "crane":
            st["rejected_crane_like"].append(sid)
            continue
        if c is None:
            if upper_p is not None and upper_p - top_t.top > CROWN_MIN_EXCESS_M:
                st["rejected_small_or_sparse"].append(sid)
            continue
        if not shows_structure(ts, rs, off, upper_p):
            st["not_shown_by_cloud"].append(sid)
            continue
        hull, ztop, n, a = c
        out.append(Crown(sid, from_mga_geom(hull), top_t.top, round(ztop, 1), n, a, top_t.top_date))
    return out, st
