"""Measured road-deck heights from the City of Melbourne 3D Point Cloud 2018 (for roads_3d).

Used by pipeline.roads_3d during the build; not run on its own. Source and licence: see
data/SOURCES.md (CC BY 4.0, photogrammetry, read as published heights under George's exception in
CLAUDE.md constraint 1). Only tiles over SIM_EXTRACT_BBOX are read. Point coordinates are GDA94 /
MGA zone 55 (EPSG:28355); OSM lon/lat are taken as GDA2020 (EPSG:7844) and transformed with the
published GDA94-GDA2020 Helmert transformation (~1.5 m). Only height DIFFERENCES between nearby
points are used, so the vertical datum (the tiles say EGM96, the publisher says AHD) cancels out.

Two kinds of measurement:

  crossing   where a road deck passes over another road (a rule-1 crossing in roads_3d), the
             surface-to-surface separation
                 deck surface at the crossing - lower road surface at the crossing
             The deck surface is sampled in a box DECK_ALONG_M along the deck x 0.8 x half its
             width across, centred on the crossing. The aerial capture cannot see under a
             deck, so the lower surface is sampled on the lower way at LOWER_OFFSETS_M
             (12-40 m) either side, skipping stations under any higher way, and interpolated
             to the crossing from the nearest good station on EACH side; the two must agree
             within a road grade (SIDES_GRADE), otherwise a vehicle or another surface is
             suspected. If only one side is clear, its two nearest good stations must agree
             (half the tolerance) and the nearest is used. Crossings over railways are not
             measured: the capture shows trains, trams and overhead gear on the tracks (seen:
             separations ~4 m too small at stabling sidings and on the Kings Way tram line).
  open deck  (`kind: open_deck`, a deck profile) a bridge way that is not on top at any
             crossing (river bridges, decks over paths), or that is (`over_roads`) but has no
             usable crossing measurement (then it must clear MIN_SEP_M somewhere):
             at each densified vertex, deck surface - street level. Street level is, in
             order: the at-grade approach roads APPROACH_OFFSETS_M (50-90 m) beyond the two
             bridge ends, walking along the chain past any embankment and never along a
             *_link ramp (interpolated along the bridge; the two ends must agree within
             APPROACH_MISMATCH_M); else `StreetLevel`, the median of at-grade street stations
             (not service, not motorway, > STREET_DECK_CLEAR_M from any deck) within the first
             of STREET_RADII_M (60 m ... 1 km) holding enough of them, the radius recorded as
             `street_radius_m` (the Bolte Bridge needs ~500 m-1 km over the Yarra); else the
             lowest clean surface beside the deck (`side_ground`, freeway decks must then clear
             it by >= MIN_SEP_M or are taken as missing from the 2018 cloud). Over water, where
             the box mixes deck and water, the deck's top surface (`top_surface`, the highest
             histogram mode with >= TOP_MODE_SHARE of the points) is used. Every road bridge is
             profiled, never a tunnel; a deck with crossing measurements is profiled only away
             from them (PROFILE_PIN_CLEAR_M) and roads_3d uses that only over water.

Robust surface statistic (`surface`): the median of the points in the box, then the median again
of the points within INLIER_M of it. The road surface is the bulk of the points; vehicles, lamp
posts, signs, overhanging trees (above) and the ground or water seen past a deck edge (below) are
minorities in a box this narrow, so the two-pass median lands on the road. A sample counts only
with >= MIN_POINTS inliers making up >= MIN_INLIER_SHARE of the box and an inlier IQR <=
MAX_IQR_M (a flat road surface; a mix of tree crowns, roofs or edges spreads wider).

A crossing measurement is used only if both surfaces count, the deck box is not under a higher
way (it would show that way, not this deck), the separation is plausible for one level
(MIN_SEP_M..MAX_SEP_M) and not below the lower road's own signed clearance (`maxheight`).
Below MIN_SEP_M the deck is not in the 2018 cloud (e.g. the West Gate Tunnel Project viaducts,
built later): the box shows the road beneath. An open-deck profile is used
only if at least one end has ground, >= MIN_PROFILE_SHARE of its vertices count and the measured
vertices form a smooth profile (no change steeper than MAX_PROFILE_GRADE + PROFILE_TOL_M between
them: a jump means the box saw two different surfaces, e.g. a deck built after 2018 over an older
road); gaps are interpolated, the ends held flat, and heights clamped to >= 0.
Service / car-park ramps may be steeper (MAX_PROFILE_GRADE_SERVICE). Anything not used falls back
to the OSM `maxheight` lower bound, then the rule.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

REPO_ROOT = Path(__file__).resolve().parent.parent
POINTCLOUD_DIR = REPO_ROOT / "data" / "raw" / "com_pointcloud_2018"

CELL_M = 10.0
DECK_ALONG_M = 8.0  # deck box length (centred on the crossing / vertex)
STATION_ALONG_M = 6.0  # lower-road / approach box length
CORE_SHARE = 0.8  # box half-width = CORE_SHARE x half the road width
LOWER_OFFSETS_M = (12.0, 16.0, 20.0, 25.0, 30.0, 35.0, 40.0)
SIDES_GRADE = 0.08  # the two lower-road samples must agree within this grade (+ SIDES_TOL_M)
SIDES_TOL_M = 0.5
# approach ground is sampled beyond the embankment that rises to the deck (as street level is:
# STREET_DECK_CLEAR_M); a short approach that ends sooner gives no ground at that end
APPROACH_OFFSETS_M = (50.0, 60.0, 75.0, 90.0)
SHADOW_PAD_M = 4.0  # a station this close to a higher way's tagged edge counts as under it
# (covers shoulders and the photogrammetric 'drape' between a deck edge and the ground)
INLIER_M = 0.5
MIN_POINTS = 25
MIN_INLIER_SHARE = 0.5
MAX_IQR_M = 0.35
MIN_SEP_M = 3.5
MAX_SEP_M = 15.0
SIDE_OFFSETS_M = (8.0, 14.0, 20.0)  # (unused since street level replaced side ground)
SIDE_BOX_HW_M = 2.5
MAX_SIDE_DECK_M = 40.0  # a deck over water / open ground may be high (Bolte Bridge approach)
SIDE_MIN_DECK_M = 1.0  # street-referenced open deck lower than this: not in the 2018 cloud
STREET_RADIUS_M = 60.0  # street level = median road surface of at-grade roads this close
STREET_RADIUS_FAR_M = 120.0  # ... or this close, if nothing within STREET_RADIUS_M
# Over water (Docklands, the Bolte Bridge) the nearest at-grade street can be far: the search
# widens to these radii, using the STREET_NEAREST_K nearest stations; beyond the last, no level.
STREET_RADII_M = (STREET_RADIUS_M, STREET_RADIUS_FAR_M, 250.0, 500.0, 1000.0)
STREET_NEAREST_K = 40
STREET_DECK_CLEAR_M = 50.0  # a street station this close to an elevated deck is its approach
MIN_PROFILE_SHARE_SERVICE = 0.2  # measured car-park / service ramps (George, 2026-10-26) ...
MIN_PROFILE_VERTICES = 3  # ... with at least this many clean vertices
MAX_PROFILE_GRADE_SERVICE = 0.13  # a service ramp may be steeper (the Crown ramp is ~11 %)
PROFILE_PIN_CLEAR_M = 30.0  # a deck with crossing pins is profiled only this far from them
APPROACH_MISMATCH_M = 1.5  # two approach grounds further apart: use street level instead
APPROACH_CHAIN_M = 400.0  # an end with no at-grade approach: follow the deck chain this far
# A deck measured against street level (over water): cables, towers and lamp posts hide much of
# e.g. the Bolte Bridge deck (gaps are interpolated), and high decks (~30 m) come out noisier
# in the photogrammetry than a road surface at street level.
MIN_PROFILE_SHARE_STREET = 0.2
MAX_IQR_STREET_M = 0.6
CUTTING_HIGHWAYS = frozenset({"motorway", "motorway_link"})  # the roads here that run in cuttings
STREET_STEP_M = 10.0
STREET_MIN_STATIONS = 3
TRENCH_MIN_M = 1.5  # a lower road this far below street level is in a cutting (a trench)
MAX_PROFILE_GRADE = 0.10  # measured deck vertices may not differ by more (+ PROFILE_TOL_M)
PROFILE_TOL_M = 0.5
MIN_PROFILE_SHARE = 0.6
MIN_PROFILE_SHARE_OVER_ROADS = 0.4  # a deck over roads is often partly under a higher deck
LANE_M = 3.3
DEFAULT_LANES = {"motorway": 2, "trunk": 2, "primary": 2, "secondary": 2, "tertiary": 2}


_KEY_OFF = 1 << 24  # cell indices may be negative (tests); MGA cells are < 2**20
_KEY_BITS = 26


def cell_key(i, j):  # works on ints and int64 arrays
    return ((i + _KEY_OFF) << _KEY_BITS) + (j + _KEY_OFF)


def cell_of_key(k: int) -> tuple[int, int]:
    return (k >> _KEY_BITS) - _KEY_OFF, (k & ((1 << _KEY_BITS) - 1)) - _KEY_OFF


@dataclass
class Surface:
    z: float | None  # robust surface height (point-cloud datum), None if not usable
    n: int  # inlier points
    n_box: int
    iqr: float | None
    reason: str | None = None  # why not usable


@dataclass
class PointIndex:
    """Points bucketed in CELL_M cells (MGA55 metres)."""

    x: np.ndarray
    y: np.ndarray
    z: np.ndarray
    cells: dict[tuple[int, int], np.ndarray] = field(default_factory=dict)

    @classmethod
    def build(cls, x: np.ndarray, y: np.ndarray, z: np.ndarray) -> PointIndex:
        idx = cls(x, y, z)
        cx = np.floor(x / CELL_M).astype(np.int64)
        cy = np.floor(y / CELL_M).astype(np.int64)
        key = cell_key(cx, cy)
        order = np.argsort(key, kind="stable")
        ks = key[order]
        bounds = np.flatnonzero(np.diff(ks)) + 1
        for chunk in np.split(order, bounds):
            if len(chunk):
                k = int(key[chunk[0]])
                idx.cells[cell_of_key(k)] = chunk
        return idx

    def box(
        self, cx: float, cy: float, ux: float, uy: float, half_along: float, half_across: float
    ) -> np.ndarray:
        """z of points in the oriented box centred (cx, cy), unit direction (ux, uy)."""
        r = math.hypot(half_along, half_across)
        ids = [
            self.cells[(i, j)]
            for i in range(math.floor((cx - r) / CELL_M), math.floor((cx + r) / CELL_M) + 1)
            for j in range(math.floor((cy - r) / CELL_M), math.floor((cy + r) / CELL_M) + 1)
            if (i, j) in self.cells
        ]
        if not ids:
            return np.empty(0)
        sel = np.concatenate(ids)
        dx, dy = self.x[sel] - cx, self.y[sel] - cy
        along = dx * ux + dy * uy
        across = -dx * uy + dy * ux
        m = (np.abs(along) <= half_along) & (np.abs(across) <= half_across)
        return self.z[sel[m]]


def surface(zs: np.ndarray, max_iqr: float = MAX_IQR_M) -> Surface:
    """Two-pass median road surface with its confidence (see module docstring)."""
    n_box = len(zs)
    if n_box < MIN_POINTS:
        return Surface(None, n_box, n_box, None, "too few points")
    m0 = float(np.median(zs))
    inl = zs[np.abs(zs - m0) <= INLIER_M]
    if len(inl) < MIN_POINTS:
        return Surface(None, len(inl), n_box, None, "too few inliers")
    m1 = float(np.median(inl))
    inl = zs[np.abs(zs - m1) <= INLIER_M]
    if len(inl) < MIN_POINTS:
        return Surface(None, len(inl), n_box, None, "too few inliers")
    q1, q3 = np.percentile(inl, [25, 75])
    iqr = float(q3 - q1)
    s = Surface(float(np.median(inl)), len(inl), n_box, round(iqr, 3))
    if len(inl) < MIN_POINTS:
        s.z, s.reason = None, "too few inliers"
    elif len(inl) < MIN_INLIER_SHARE * n_box:
        s.z, s.reason = None, "mixed surfaces (inliers < half the box)"
    elif iqr > max_iqr:
        s.z, s.reason = None, "not flat (inlier IQR too large)"
    return s


TOP_MODE_SHARE = 0.1  # a top-surface mode must hold at least this share of the box


def top_surface(zs: np.ndarray) -> Surface:
    """The TOP surface in a box (a deck seen from the air over water or ground past its edge):
    the highest 0.5 m histogram mode holding >= TOP_MODE_SHARE of the points, then the median
    of the points within INLIER_M of it, with the usual inlier and flatness tests."""
    n_box = len(zs)
    if n_box < MIN_POINTS:
        return Surface(None, n_box, n_box, None, "too few points")
    lo, hi = float(np.floor(zs.min())), float(np.ceil(zs.max())) + 0.5
    h, e = np.histogram(zs, bins=np.arange(lo, hi + 0.1, 0.5))
    need = max(6, TOP_MODE_SHARE * n_box)
    peaks = [
        (e[k] + e[k + 1]) / 2
        for k in range(len(h))
        if h[k] >= need
        and h[k] >= (h[k - 1] if k else 0)
        and h[k] >= (h[k + 1] if k + 1 < len(h) else 0)
    ]
    if not peaks:
        return Surface(None, 0, n_box, None, "no top surface")
    top = max(peaks)
    inl = zs[np.abs(zs - top) <= INLIER_M]
    if len(inl) < MIN_POINTS:
        return Surface(None, len(inl), n_box, None, "too few inliers")
    z = float(np.median(inl))
    inl = zs[np.abs(zs - z) <= INLIER_M]
    q1, q3 = np.percentile(inl, [25, 75])
    s = Surface(float(np.median(inl)), len(inl), n_box, round(float(q3 - q1), 3))
    if s.iqr > MAX_IQR_STREET_M:
        s.z, s.reason = None, "not flat (inlier IQR too large)"
    return s


def half_width(tags: dict[str, str]) -> float:
    hw = tags.get("highway", "")
    try:
        lanes = int(float(tags.get("lanes", "").split(";")[0]))
    except ValueError:
        lanes = (
            1
            if hw.endswith("_link") or hw in ("service", "residential")
            else DEFAULT_LANES.get(hw, 2)
        )
    if tags.get("railway"):
        lanes = 1
    return max(lanes, 1) * LANE_M / 2


# --- geometry on MGA lines ----------------------------------------------------------------------


class Line:
    """A polyline in MGA metres with arc-length lookups."""

    def __init__(self, xy: list[tuple[float, float]]):
        self.xy = xy
        seg = [math.dist(a, b) for a, b in zip(xy, xy[1:], strict=False)]
        self.cum = [0.0]
        for s in seg:
            self.cum.append(self.cum[-1] + s)
        self.length = self.cum[-1]

    def at(self, s: float) -> tuple[float, float, float, float]:
        """(x, y, ux, uy) at arc length s (clamped)."""
        s = max(0.0, min(self.length, s))
        i = max(0, min(len(self.xy) - 2, int(np.searchsorted(self.cum, s, side="right")) - 1))
        (x1, y1), (x2, y2) = self.xy[i], self.xy[i + 1]
        L = math.hypot(x2 - x1, y2 - y1) or 1.0
        f = (s - self.cum[i]) / L
        return x1 + f * (x2 - x1), y1 + f * (y2 - y1), (x2 - x1) / L, (y2 - y1) / L

    def project(self, x: float, y: float) -> tuple[float, float]:
        """(arc length of the nearest point, distance to it)."""
        best = (math.inf, 0.0)
        for i, ((x1, y1), (x2, y2)) in enumerate(zip(self.xy, self.xy[1:], strict=False)):
            dx, dy = x2 - x1, y2 - y1
            L2 = dx * dx + dy * dy
            f = 0.0 if L2 == 0 else max(0.0, min(1.0, ((x - x1) * dx + (y - y1) * dy) / L2))
            d = math.hypot(x1 + f * dx - x, y1 + f * dy - y)
            if d < best[0]:
                best = (d, self.cum[i] + f * math.sqrt(L2))
        return best[1], best[0]


# --- measurement --------------------------------------------------------------------------------


@dataclass
class Shadow:
    """Every way with its line, effective layer and half width, to test 'is this point under a
    higher way'."""

    lines: dict[int, Line]
    layer: dict[int, int]
    hw: dict[int, float]

    def under_higher(self, x: float, y: float, layer: int, near: list[int]) -> bool:
        for w in near:
            if self.layer[w] > layer:
                _, d = self.lines[w].project(x, y)
                if d <= self.hw[w] + SHADOW_PAD_M:
                    return True
        return False


class StreetLevel:
    """At-grade street level near a point: the median road surface (`surface`) of stations every
    STREET_STEP_M on at-grade streets (not bridges, not tunnels, effective layer 0, not
    `service`, not `motorway` / `motorway_link`: the cuttings here are freeways) within
    STREET_RADIUS_M (widening to STREET_RADII_M, nearest stations first, over water), not under
    any elevated way and not within STREET_DECK_CLEAR_M of one (an approach embankment rises to
    its deck), excluding given ways (the road being judged, the ways joined to it and ways of the
    same name). This is the reference ("ground") the model's z = 0 stands for; a cutting floor,
    an approach ramp or water is not."""

    def __init__(
        self, stations: list[tuple[float, float, float, float, int]], hw: dict[int, float]
    ):
        from shapely.geometry import Point
        from shapely.strtree import STRtree

        self.stations = stations
        self.hw = hw
        self.tree = STRtree([Point(s[0], s[1]) for s in stations])

    def near(
        self, x: float, y: float, exclude: set[int], radius: float = STREET_RADIUS_FAR_M
    ) -> list[int]:
        from shapely.geometry import Point

        idx = self.tree.query(Point(x, y).buffer(radius)).tolist()
        return [i for i in idx if self.stations[i][4] not in exclude]

    def candidates(self, x: float, y: float, exclude: set[int], radius: float) -> list[int]:
        """Stations within `radius`, nearest first, at most STREET_NEAREST_K beyond 120 m."""
        idx = [
            i
            for i in self.near(x, y, exclude, radius)
            if math.hypot(self.stations[i][0] - x, self.stations[i][1] - y) <= radius
        ]
        idx.sort(key=lambda i: math.hypot(self.stations[i][0] - x, self.stations[i][1] - y))
        return idx if radius <= STREET_RADIUS_FAR_M else idx[:STREET_NEAREST_K]

    def at(
        self, pts: PointIndex, x: float, y: float, exclude: set[int], shadow: Shadow, near
    ) -> tuple[float | None, int, float | None]:
        """(street level, stations used, radius used); None if nothing within 1 km."""
        for radius in STREET_RADII_M:
            vals = []
            for i in self.candidates(x, y, exclude, radius):
                sx, sy, ux, uy, w = self.stations[i]
                if shadow.under_higher(sx, sy, 0, near(sx, sy, 30.0)):
                    continue
                g = surface(pts.box(sx, sy, ux, uy, STATION_ALONG_M / 2, CORE_SHARE * self.hw[w]))
                if g.z is not None:
                    vals.append(g.z)
            if len(vals) >= STREET_MIN_STATIONS:
                return float(np.median(vals)), len(vals), radius
        return None, 0, None


def measure_crossing(
    pts: PointIndex,
    min_sep: float | None,
    up: Line,
    up_hw: float,
    low: Line,
    low_hw: float,
    x: float,
    y: float,
    up_layer: int,
    low_layer: int,
    shadow: Shadow,
    near: list[int],
    street: float | None = None,
) -> dict[str, Any]:
    """`street` = the at-grade street level here (`StreetLevel`), given only when the lower road
    is a freeway (CUTTING_HIGHWAYS). If its surface is more than TRENCH_MIN_M below street
    level, it is in a cutting: the separation used is
    deck - street level (what the model, with the cutting still at z = 0 in Stage A, can show),
    and the cutting's depth is recorded for Stage B (`trench_depth_m`)."""
    su, _ = up.project(x, y)
    cx, cy, ux, uy = up.at(su)
    rec: dict[str, Any] = {}
    if shadow.under_higher(cx, cy, up_layer, near):
        rec.update(used=False, reason="deck under a higher way (occluded from the air)")
        return rec
    deck = surface(pts.box(cx, cy, ux, uy, DECK_ALONG_M / 2, CORE_SHARE * up_hw))
    rec.update(deck_points=deck.n, deck_box_points=deck.n_box, deck_iqr_m=deck.iqr)
    if deck.z is None:
        rec.update(used=False, reason=f"deck: {deck.reason}")
        return rec
    sl, _ = low.project(x, y)
    found: dict[int, list[tuple[float, float, Surface]]] = {-1: [], 1: []}
    for sign in (-1, 1):
        for off in LOWER_OFFSETS_M:
            s = sl + sign * off
            if s < 0 or s > low.length:
                break
            px, py, vx, vy = low.at(s)
            if shadow.under_higher(px, py, low_layer, near):
                continue
            g = surface(pts.box(px, py, vx, vy, STATION_ALONG_M / 2, CORE_SHARE * low_hw))
            if g.z is not None:
                found[sign].append((sign * off, s, g))
                if len(found[sign]) == 2:
                    break
    sides = [found[k][0] for k in (-1, 1) if found[k]]
    if not sides:
        rec.update(used=False, reason="no clear lower-road surface within 40 m either side")
        return rec
    rec.update(
        lower_offsets_m=[o for o, _, _ in sides],
        lower_points=[g.n for _, _, g in sides],
        lower_iqr_m=[g.iqr for _, _, g in sides],
    )
    if len(sides) == 1:
        one = found[-1] or found[1]
        if len(one) < 2:
            rec.update(used=False, reason="lower road clear on one side only, one sample")
            return rec
        (o1, _, g1), (o2, _, g2) = one
        if abs(g2.z - g1.z) > SIDES_GRADE * abs(o2 - o1) + SIDES_TOL_M / 2:
            rec.update(used=False, reason="one-sided lower-road samples disagree")
            return rec
        rec.update(
            lower_offsets_m=[o1, o2], lower_points=[g1.n, g2.n], lower_iqr_m=[g1.iqr, g2.iqr]
        )
        lower = g1.z  # nearest; the second only confirms it is the road, not a vehicle
    else:
        (o1, _, g1), (o2, _, g2) = sides
        if abs(g2.z - g1.z) > SIDES_GRADE * (o2 - o1) + SIDES_TOL_M:
            rec.update(used=False, reason="the two lower-road samples disagree (vehicle / other)")
            return rec
        lower = g1.z + (g2.z - g1.z) * (0 - o1) / (o2 - o1)
    sep = deck.z - lower
    rec["separation_m"] = round(sep, 2)
    if street is not None and lower < street - TRENCH_MIN_M:
        rec.update(
            trench_depth_m=round(street - lower, 2),
            separation_to_lower_surface_m=round(sep, 2),
            separation_m=round(deck.z - street, 2),
            street_level_ref=True,
        )
        if min_sep is not None and sep < min_sep:
            rec.update(
                used=False, reason="below the signed clearance of the road beneath (impossible)"
            )
        elif deck.z - street < 0:
            rec.update(used=False, reason="deck below street level over a cutting")
        elif sep > MAX_SEP_M:
            rec.update(used=False, reason="separation implausible for one level (roof / tree?)")
        else:
            rec.update(used=True, reason=None)
        return rec
    if sep < MIN_SEP_M:
        rec.update(used=False, reason="deck at ground level in the 2018 cloud (built later?)")
    elif min_sep is not None and sep < min_sep:
        rec.update(used=False, reason="below the signed clearance of the road beneath (impossible)")
    elif sep > MAX_SEP_M:
        rec.update(used=False, reason="separation implausible for one level (roof / tree?)")
    else:
        rec.update(used=True, reason=None)
    return rec


def side_ground(
    pts: PointIndex,
    deck: Line,
    hw: float,
    vertex_s: list[float],
    shadow: Shadow,
    near: list[int],
) -> float | None:
    """Ground (or water) beside a deck: at each vertex, the LOWEST clean surface among stations
    SIDE_OFFSETS_M beyond the deck edge on both sides that are not under any elevated way (the
    lowest, so a flat roof beside the deck is not taken for ground); the median over vertices."""
    per_vertex = []
    for s in vertex_s:
        cx, cy, ux, uy = deck.at(s)
        vals = []
        for side in (-1, 1):
            for off in SIDE_OFFSETS_M:
                d = side * (hw + off)
                px, py = cx - uy * d, cy + ux * d
                if shadow.under_higher(px, py, 0, near):
                    continue
                g = surface(pts.box(px, py, ux, uy, DECK_ALONG_M / 2, SIDE_BOX_HW_M))
                if g.z is not None:
                    vals.append(g.z)
        if vals:
            per_vertex.append(min(vals))
    if len(per_vertex) < MIN_PROFILE_SHARE * len(vertex_s):
        return None
    return float(np.median(per_vertex))


def drop_fallback_jumps(
    vertex_s: list[float], heights: list[float | None], fallback: set[int], grade: float
) -> int:
    """Set to None (in place) every top-surface fallback height that jumps from the profile.

    The top surface is the highest well-populated mode in the box: the deck over water, but a
    roof or canopy where the deck runs beside a building (seen: the last vertex of the Crown ramp
    1132332719 read 21.5 m, its neighbours 2.7 m). Starting from the vertices measured on a flat
    road surface (not fallback), the profile is walked outwards in both directions and a
    fallback vertex is kept only if it is within `grade` (+ PROFILE_TOL_M) of the last kept
    vertex. Vertices on a flat road surface are never dropped here, so a profile whose road
    surface itself jumps still fails the smoothness test. With no road-surface vertex at all the
    heights are left alone. Returns the number dropped."""
    core = [i for i, h in enumerate(heights) if h is not None and i not in fallback]
    if not core:
        return 0
    dropped = 0

    def walk(order: list[int], last: int) -> None:
        nonlocal dropped
        for i in order:
            h = heights[i]
            if h is None:
                continue
            if i in fallback:
                hl = heights[last]
                assert hl is not None
                if abs(h - hl) > grade * abs(vertex_s[i] - vertex_s[last]) + PROFILE_TOL_M:
                    heights[i] = None
                    dropped += 1
                    continue
            last = i

    walk(list(range(core[0] - 1, -1, -1)), core[0])
    walk(list(range(core[0] + 1, len(heights))), core[0])
    return dropped


def measure_open_deck(
    pts: PointIndex,
    deck: Line,
    hw: float,
    vertex_s: list[float],
    approaches: list[list[tuple[Line, float, int]]],
    layer: int,
    shadow: Shadow,
    near: list[int],
    street: float | None = None,
    over_roads: bool = False,
    service: bool = False,
    skip_s: set[int] | None = None,
    freeway: bool = False,
) -> dict[str, Any]:
    """`approaches` = for the start and end of the deck: (line, start arc length, direction)
    of each non-bridge way leaving that end. `over_roads`: the deck is on top at a crossing
    (its crossing measurements failed), so it must clear MIN_SEP_M somewhere (and may be
    partly hidden by higher decks: MIN_PROFILE_SHARE_OVER_ROADS). `street` = the at-grade
    street level near the deck (`StreetLevel`), used only when neither end has a clean approach.
    `service`: a measured service / car-park ramp may be steeper and thinner (George 2026-10-26).
    `skip_s`: vertex indices not to measure (next to the deck's own crossing pins); they are
    None in `heights_m`."""
    grounds = []
    for ends in approaches:
        vals = []
        for line, s0, sign in ends:
            for off in APPROACH_OFFSETS_M:
                s = s0 + sign * off
                if s < 0 or s > line.length:
                    break
                px, py, vx, vy = line.at(s)
                if shadow.under_higher(px, py, 0, near):
                    continue
                g = surface(pts.box(px, py, vx, vy, STATION_ALONG_M / 2, CORE_SHARE * LANE_M))
                if g.z is not None:
                    vals.append(g.z)
                    break
        grounds.append(float(np.median(vals)) if vals else None)
    rec: dict[str, Any] = {"ground_start": grounds[0], "ground_end": grounds[1]}
    side_mode = grounds[0] is None and grounds[1] is None
    if side_mode:
        rec["ground_street"] = street
        if street is None:
            rec.update(used=False, reason="no approach ground or street level nearby")
            return rec
        g0 = g1 = street
    else:
        g0 = grounds[0] if grounds[0] is not None else grounds[1]
        g1 = grounds[1] if grounds[1] is not None else grounds[0]
        if abs(g0 - g1) > APPROACH_MISMATCH_M:
            # one end samples an embankment or a dip: not a usable reference to interpolate
            rec["approach_mismatch_m"] = round(abs(g0 - g1), 2)
            g0 = g1 = street if street is not None else min(g0, g1)
            rec["ground_street"] = street
    skip_s = skip_s or set()
    heights: list[float | None] = []
    fallback: set[int] = set()  # vertices measured by the top-surface fallback only
    for i, s in enumerate(vertex_s):
        cx, cy, ux, uy = deck.at(s)
        if i in skip_s or shadow.under_higher(cx, cy, layer, near):
            heights.append(None)
            continue
        box = pts.box(cx, cy, ux, uy, DECK_ALONG_M / 2, CORE_SHARE * hw)
        sf = surface(box, MAX_IQR_STREET_M if side_mode else MAX_IQR_M)
        if sf.z is None and side_mode:
            sf = top_surface(box)  # the deck is the top surface over water / open ground
            fallback.add(i)
        if sf.z is None:
            heights.append(None)
            continue
        ground = g0 + (g1 - g0) * (s / deck.length if deck.length else 0.0)
        heights.append(sf.z - ground)
    grade = MAX_PROFILE_GRADE_SERVICE if service else MAX_PROFILE_GRADE
    dropped = drop_fallback_jumps(vertex_s, heights, fallback, grade)
    if dropped:
        rec["vertices_top_surface_dropped"] = dropped
    good = [h for h in heights if h is not None]
    share = len(good) / max(len(heights) - len(skip_s), 1)
    rec.update(vertices=len(heights), vertices_measured=len(good), share=round(share, 2))
    if service:
        need_share = MIN_PROFILE_SHARE_SERVICE
    elif side_mode:
        need_share = MIN_PROFILE_SHARE_STREET
    else:
        need_share = MIN_PROFILE_SHARE_OVER_ROADS if over_roads else MIN_PROFILE_SHARE
    if share < need_share or len(good) < MIN_PROFILE_VERTICES:
        rec.update(used=False, reason="too few deck vertices with a clean surface")
        return rec
    if over_roads and not service and max(good) < MIN_SEP_M:
        rec.update(used=False, reason="deck over a road but low in the 2018 cloud (built later?)")
        return rec
    if side_mode and freeway and max(good) < SIDE_MIN_DECK_M:
        # (a freeway viaduct at street level is one built after 2018, e.g. the West Gate Tunnel
        # decks; a street bridge over the river can be level with the street, e.g. Princes)
        rec.update(used=False, reason="deck at street level in the 2018 cloud (built later?)")
        return rec
    if max(good) > (MAX_SIDE_DECK_M if side_mode else MAX_SEP_M):
        rec.update(used=False, reason="profile implausibly high (roof / tree?)")
        return rec
    meas = [(s, h) for s, h in zip(vertex_s, heights, strict=True) if h is not None]
    for (s1, h1), (s2, h2) in zip(meas, meas[1:], strict=False):
        if abs(h2 - h1) > grade * (s2 - s1) + PROFILE_TOL_M:
            rec.update(
                used=False, reason="profile not smooth (mixed surfaces, or built after 2018)"
            )
            return rec
    # fill gaps between clean measured vertices by linear interpolation (not beyond the first
    # and last: those vertices stay unmeasured, None); clamp at ground
    xs = [s for s, h in zip(vertex_s, heights, strict=True) if h is not None]
    filled = np.interp(vertex_s, xs, good)
    rec.update(
        used=True,
        reason=None,
        heights_m=[
            None if i in skip_s or not xs[0] <= s <= xs[-1] else round(max(0.0, float(h)), 2)
            for i, (s, h) in enumerate(zip(vertex_s, filled, strict=True))
        ],
        max_m=round(max(0.0, float(max(good))), 2),
    )
    return rec


def measure_cutting(
    pts: PointIndex,
    line: Line,
    hw: float,
    vertex_s: list[float],
    layer: int,
    shadow: Shadow,
    near: list[int],
    street_at: Any,
) -> dict[str, Any]:
    """Stage B: the depth profile of a freeway in a cutting (open to the sky, so the aerial
    capture sees its floor): at every densified vertex, street level (`street_at(x, y)`, the
    `StreetLevel` of at-grade streets nearby, the same reference as the trench crossings) minus
    the road surface (`surface` in a STATION_ALONG_M box, CORE_SHARE x half the road width).
    Vertices under a higher way (the Power St bridges over the cutting) are not measured.

    A measured vertex whose depth differs from the median of its measured neighbours (two on
    each side) by more than PROFILE_TOL_M is dropped (a vehicle, a wall top). The profile is
    used if at least MIN_PROFILE_VERTICES vertices remain and one of them is at least
    TRENCH_MIN_M deep; gaps between measured vertices are interpolated (not beyond the first and
    last). `depths_m` are positive below street level (None where not measured)."""
    depths: list[float | None] = []
    for s in vertex_s:
        cx, cy, ux, uy = line.at(s)
        if shadow.under_higher(cx, cy, layer, near(cx, cy)):
            depths.append(None)
            continue
        sf = surface(pts.box(cx, cy, ux, uy, STATION_ALONG_M / 2, CORE_SHARE * hw))
        street = street_at(cx, cy) if sf.z is not None else None
        depths.append(None if sf.z is None or street is None else street - sf.z)
    meas = [(i, d) for i, d in enumerate(depths) if d is not None]
    dropped = 0
    for k, (i, d) in enumerate(meas):
        nb = [dj for j, dj in meas[max(0, k - 2) : k + 3] if j != i]
        if len(nb) >= 2 and abs(d - float(np.median(nb))) > PROFILE_TOL_M:
            depths[i] = None
            dropped += 1
    good = [(s, d) for s, d in zip(vertex_s, depths, strict=True) if d is not None]
    rec: dict[str, Any] = {
        "vertices": len(vertex_s),
        "vertices_measured": len(good),
        "vertices_dropped_outliers": dropped,
    }
    if len(good) < MIN_PROFILE_VERTICES:
        rec.update(used=False, reason="too few vertices with a clean road surface")
        return rec
    if max(d for _, d in good) < TRENCH_MIN_M:
        rec.update(used=False, reason=f"not in a cutting (never {TRENCH_MIN_M} m below street)")
        return rec
    xs = [s for s, _ in good]
    filled = np.interp(vertex_s, xs, [d for _, d in good])
    rec.update(
        used=True,
        reason=None,
        max_depth_m=round(max(d for _, d in good), 2),
        depths_m=[
            round(float(d), 2) if xs[0] <= s <= xs[-1] else None
            for s, d in zip(vertex_s, filled, strict=True)
        ],
        measured=[d is not None for d in depths],
    )
    return rec


def cutting_candidates(g: Any, crossings: list[Any], r3: Any) -> list[int]:
    """Freeway ways (CUTTING_HIGHWAYS, at grade: not a deck, not a tunnel) under a road-on-top
    crossing, and the at-grade freeway ways joined to them: the ways whose cutting profile is
    measured. Only those under a crossing that finds a cutting (`trench_depth_m`), or joined to
    one, are used (`cutting_ways`)."""

    def at_grade_freeway(w: int) -> bool:
        way = g.ways[w]
        return (
            not way.rail
            and way.tags.get("highway") in CUTTING_HIGHWAYS
            and not r3.is_deck_way(way)
            and not r3.is_tunnel(way.tags)
        )

    lowers = {
        c.lower
        for c in crossings
        if c.dropped is None and not g.ways[c.upper].rail and at_grade_freeway(c.lower)
    }
    out = set(lowers)
    for w in lowers:
        for v in g.lines[w]:
            out |= {w2 for _, _, w2 in g.adj[v] if at_grade_freeway(w2)}
    return sorted(out)


def cutting_ways(g: Any, crossings: list[Any], candidates: list[int]) -> list[int]:
    """Ways to profile: the candidates under a crossing that found a cutting (`trench_depth_m`),
    and the candidates joined to them (the cutting continues past the crossings, e.g. into the
    CityLink tunnel portals)."""
    found = {c.lower for c in crossings if c.trench is not None and c.lower in set(candidates)}
    out = set(found)
    cand = set(candidates)
    for w in found:
        for v in g.lines[w]:
            out |= {w2 for _, _, w2 in g.adj[v] if w2 in cand}
    return sorted(out)


# --- io -----------------------------------------------------------------------------------------


def sim_tiles(pc_dir: Path = POINTCLOUD_DIR) -> list[Path]:
    """The downloaded tiles whose index footprint intersects SIM_EXTRACT_BBOX."""
    from pipeline.config import SIM_EXTRACT_BBOX
    from pipeline.download import parse_tile_index, tiles_in_bbox

    idx = pc_dir / "Tile_Index.KML"
    if not idx.exists():
        raise FileNotFoundError(
            f"{idx} missing; run `uv run python -m pipeline.download --pointcloud-only`"
        )
    names = tiles_in_bbox(parse_tile_index(idx.read_text(encoding="iso-8859-1")), SIM_EXTRACT_BBOX)
    paths = [pc_dir / f"{n}.las" for n in names]
    missing = [p.name for p in paths if not p.exists()]
    if missing:
        raise FileNotFoundError(f"point-cloud tiles missing: {missing[:5]} ... ({len(missing)})")
    return paths


def load_points(tiles: list[Path], want: set[tuple[int, int]]) -> PointIndex:
    """Points from `tiles` that fall in the wanted CELL_M cells."""
    import laspy

    want_keys = np.array(sorted(cell_key(i, j) for i, j in want), dtype=np.int64)
    xs, ys, zs = [], [], []
    for t in tiles:
        with laspy.open(t) as f:
            h = f.header
            i0, i1 = math.floor(h.mins[0] / CELL_M), math.floor(h.maxs[0] / CELL_M)
            j0, j1 = math.floor(h.mins[1] / CELL_M), math.floor(h.maxs[1] / CELL_M)
            if not any(i0 <= i <= i1 and j0 <= j <= j1 for i, j in want):
                continue
            las = f.read()
        x, y, z = np.asarray(las.x), np.asarray(las.y), np.asarray(las.z)
        key = cell_key(np.floor(x / CELL_M).astype(np.int64), np.floor(y / CELL_M).astype(np.int64))
        m = np.isin(key, want_keys)
        xs.append(x[m])
        ys.append(y[m])
        zs.append(z[m].astype(np.float32))
    if not xs:
        return PointIndex.build(np.empty(0), np.empty(0), np.empty(0, dtype=np.float32))
    return PointIndex.build(np.concatenate(xs), np.concatenate(ys), np.concatenate(zs))


def to_mga():
    from pyproj import Transformer

    return Transformer.from_crs("EPSG:7844", "EPSG:28355", always_xy=True)


def cells_around(points: list[tuple[float, float]], radius: float) -> set[tuple[int, int]]:
    out: set[tuple[int, int]] = set()
    for x, y in points:
        for i in range(math.floor((x - radius) / CELL_M), math.floor((x + radius) / CELL_M) + 1):
            for j in range(
                math.floor((y - radius) / CELL_M), math.floor((y + radius) / CELL_M) + 1
            ):
                out.add((i, j))
    return out


def near_ways(lines: dict[int, Line], hw: dict[int, float]) -> Any:
    """A function (x, y, r) -> way ids whose line passes within r of (x, y) (STRtree)."""
    from shapely.geometry import LineString, Point
    from shapely.strtree import STRtree

    ids = list(lines)
    geoms = [LineString(lines[w].xy) for w in ids]
    tree = STRtree(geoms)

    def query(x: float, y: float, r: float) -> list[int]:
        return [ids[k] for k in tree.query(Point(x, y).buffer(r)).tolist()]

    return query


def summarise_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = defaultdict(int)
    for r in records:
        out[f"{r['kind']}_{'used' if r.get('used') else 'not_used'}"] += 1
        if not r.get("used"):
            out[f"reason: {r.get('reason')}"] += 1
    return dict(sorted(out.items()))


# --- the roads_3d hook --------------------------------------------------------------------------


def approach_roads(g: Any, lines: dict[int, Line], end: int, deck: int, r3: Any) -> list:
    """At-grade road ways leaving a deck end: directly, or, if the end joins other bridge
    pieces (a river bridge split in OSM), at the first at-grade junction found by following the
    bridge ways up to APPROACH_CHAIN_M. Returns [(line, start arc length, direction)]."""
    import heapq as hq

    def at_grade(w2: int) -> bool:
        # a *_link next to a deck is a ramp on an embankment, not street level
        ow = g.ways[w2]
        return (
            not ow.rail
            and not r3.is_bridge(ow.tags)
            and not r3.is_tunnel(ow.tags)
            and not ow.tags.get("highway", "").endswith("_link")
        )

    def leaving(u: int) -> list:
        out = []
        for _, _, w2 in g.adj[u]:
            if w2 == deck or not at_grade(w2):
                continue
            ol = g.lines[w2]
            if ol[0] == u:
                out.append((lines[w2], 0.0, 1))
            elif ol[-1] == u:
                out.append((lines[w2], lines[w2].length, -1))
        return out

    found = leaving(end)
    if found:
        return found
    dist = {end: 0.0}
    heap = [(0.0, end)]
    while heap:
        d, u = hq.heappop(heap)
        if d > dist[u] or d > APPROACH_CHAIN_M:
            continue
        if u != end:
            here = leaving(u)
            if here:
                found.extend(here)
                continue
        for v2, length, w2 in g.adj[u]:
            if w2 == deck or g.ways[w2].rail or not r3.is_bridge(g.ways[w2].tags):
                continue
            nd = d + length
            if nd < dist.get(v2, math.inf):
                dist[v2] = nd
                hq.heappush(heap, (nd, v2))
    return found


def measure_all(
    g: Any, crossings: list[Any], tiles: list[Path] | None = None
) -> tuple[dict[int, float], list[dict[str, Any]]]:
    """Measure every road-on-top crossing and every open road deck in roads_3d's graph.

    Updates crossings in place (separation, source="pointcloud", pointcloud=record) where the
    measurement is used; returns ({vertex: open-deck height}, all records).
    """
    from pipeline import roads_3d as r3

    tiles = sim_tiles() if tiles is None else tiles
    tr = to_mga()
    vx, vy = tr.transform(np.asarray(g.lon), np.asarray(g.lat))
    vxy = list(zip(vx.tolist(), vy.tolist(), strict=True))
    lines = {w: Line([vxy[v] for v in line]) for w, line in g.lines.items()}
    layer = {w: r3.effective_layer(g.ways[w].tags) for w in g.lines}
    hw = {w: half_width(g.ways[w].tags) for w in g.lines}
    shadow = Shadow(lines, layer, hw)
    query = near_ways(lines, hw)
    stations = []
    for w, way in g.ways.items():
        t = way.tags
        if (
            way.rail
            or r3.is_bridge(t)
            or r3.is_tunnel(t)
            or r3.effective_layer(t) != 0
            or t.get("highway") in ("service", "motorway", "motorway_link")
        ):
            continue
        ln = lines[w]
        for s in np.arange(STREET_STEP_M / 2, ln.length, STREET_STEP_M):
            sx, sy, ux, uy = ln.at(float(s))
            near_decks = [
                o
                for o in query(sx, sy, STREET_DECK_CLEAR_M)
                if not g.ways[o].rail and r3.is_deck_way(g.ways[o])
            ]
            if any(lines[o].project(sx, sy)[1] <= STREET_DECK_CLEAR_M for o in near_decks):
                continue  # an approach embankment rising to a deck, not street level
            stations.append((sx, sy, ux, uy, w))
    streets = StreetLevel(stations, hw)
    joined: dict[int, set[int]] = defaultdict(set)  # way -> ways sharing a vertex with it
    for w, line in g.lines.items():
        for v in line:
            for _, _, w2 in g.adj[v]:
                joined[w].add(w2)

    def street_exclusions(w: int) -> set[int]:
        name = g.ways[w].tags.get("name")
        out = {w} | joined[w]
        if name:
            out |= {o for o in streets_named.get(name, ())}
        return out

    streets_named: dict[str, set[int]] = defaultdict(set)
    for w, way in g.ways.items():
        if way.tags.get("name"):
            streets_named[way.tags["name"]].add(w)

    todo = [
        c
        for c in crossings
        if c.dropped is None
        and not g.ways[c.upper].rail
        and not g.ways[c.lower].rail  # rolling stock on the tracks, see the module docstring
        and not r3.is_real_tunnel(g.ways[c.upper].tags)
    ]
    on_top = {c.upper for c in crossings if c.dropped is None}
    # deck profiles are measured for every road bridge (open decks, and decks whose crossing
    # measurements all fail); which ones are used is decided after the crossings
    # (bridges only: a tunnel under water is never a deck, and gets no water-based height)
    open_ids = sorted(
        w.id
        for w in g.ways.values()
        if not w.rail and r3.is_bridge(w.tags) and not r3.is_tunnel(w.tags)
    )

    # which cells to read
    want_pts: list[tuple[float, float]] = []
    cross_xy = {}
    for c in todo:
        x, y = tr.transform(c.lon, c.lat)
        cross_xy[id(c)] = (x, y)
        want_pts.append((x, y))
        low = lines[c.lower]
        s0, _ = low.project(x, y)
        for s in np.arange(s0 - 46, s0 + 46.1, 4.0):
            if 0 <= s <= low.length:
                want_pts.append(low.at(float(s))[:2])
    street_q: list[tuple[float, float]] = list(cross_xy.values())
    approaches: dict[int, list[list[tuple[Line, float, int]]]] = {}
    cells = cells_around(want_pts, 12.0)
    for w in open_ids:
        line = g.lines[w]
        cells |= cells_around([vxy[v] for v in line], 12.0)
        street_q.append(lines[w].at(lines[w].length / 2)[:2])
        ends = []
        for end in (line[0], line[-1]):
            found = approach_roads(g, lines, end, w, r3)
            uniq = {(id(a), s, d): (a, s, d) for a, s, d in found}
            ends.append(list(uniq.values()))
            for a, s0, d in uniq.values():
                pts_ = [a.at(s0 + d * o)[:2] for o in APPROACH_OFFSETS_M]
                cells |= cells_around(pts_, 8.0)
        approaches[w] = ends
    # Stage B: freeway ways that may run in a cutting (profiled after the crossings)
    cut_cands = cutting_candidates(g, crossings, r3)
    for w in cut_cands:
        pts_w = [vxy[v] for v in g.lines[w]]
        cells |= cells_around(pts_w, 8.0)
        street_q.extend(pts_w)
    for x, y in street_q:
        for radius in STREET_RADII_M:
            for i in streets.candidates(x, y, set(), radius):
                cells |= cells_around([streets.stations[i][:2]], 6.0)
    pts = load_points(tiles, cells)

    records: list[dict[str, Any]] = []
    for c in todo:
        x, y = cross_xy[id(c)]
        rec = measure_crossing(
            pts,
            c.clearance_m,
            lines[c.upper],
            hw[c.upper],
            lines[c.lower],
            hw[c.lower],
            x,
            y,
            layer[c.upper],
            layer[c.lower],
            shadow,
            query(x, y, 60.0),
            streets.at(pts, x, y, street_exclusions(c.lower), shadow, query)[0]
            if g.ways[c.lower].tags.get("highway") in CUTTING_HIGHWAYS
            else None,
        )
        rec = {
            "kind": "crossing",
            "upper_way": c.upper,
            "upper_name": g.ways[c.upper].tags.get("name"),
            "lower_way": c.lower,
            "lower_name": g.ways[c.lower].tags.get("name"),
            "lower_is_rail": g.ways[c.lower].rail,
            "lon": round(c.lon, 6),
            "lat": round(c.lat, 6),
            "maxheight_separation_m": round(c.separation, 2) if c.source == "maxheight" else None,
            **rec,
        }
        if rec.get("used"):
            c.separation = rec["separation_m"]
            c.source = "pointcloud"
        if "trench_depth_m" in rec:
            c.trench = {
                "trench_depth_m": rec["trench_depth_m"],
                "separation_to_lower_surface_m": rec["separation_to_lower_surface_m"],
            }
        c.pointcloud = rec
        records.append(rec)

    open_decks: dict[int, float] = {}
    pin_xy: dict[int, list[tuple[float, float]]] = defaultdict(list)
    for c in crossings:
        if c.source == "pointcloud":
            pin_xy[c.upper].append(cross_xy[id(c)])
    for w in open_ids:
        line = g.lines[w]
        ln = lines[w]
        vs = [ln.cum[i] for i in range(len(line))]
        mx, my = ln.at(ln.length / 2)[:2]
        # a deck with crossing pins is profiled only away from them ("deck_profile")
        skip = {
            i
            for i, v in enumerate(line)
            if any(math.dist(vxy[v], p) <= PROFILE_PIN_CLEAR_M for p in pin_xy.get(w, ()))
        }
        street, n_st, radius = streets.at(pts, mx, my, {w}, shadow, query)
        rec = measure_open_deck(
            pts,
            ln,
            hw[w],
            vs,
            approaches[w],
            layer[w],
            shadow,
            query(mx, my, ln.length / 2 + 60),
            street,
            over_roads=w in on_top,
            service=g.ways[w].tags.get("highway") == "service",
            skip_s=skip,
            freeway=r3.road_class(g.ways[w].tags) == "freeway",
        )
        if rec.get("ground_street") is not None:
            rec.update(street_stations=n_st, street_radius_m=radius)
        lon, lat = g.lon[line[len(line) // 2]], g.lat[line[len(line) // 2]]
        partial = rec.get("used") and any(h is None for h in rec["heights_m"])
        rec = {
            # open_deck: a full measured profile; deck_profile: held at some vertices only
            "kind": "deck_profile" if (w in pin_xy or partial) else "open_deck",
            "deck_has_crossing_pins": w in pin_xy,
            "over_roads": w in on_top,
            "upper_way": w,
            "upper_name": g.ways[w].tags.get("name"),
            "lon": round(lon, 6),
            "lat": round(lat, 6),
            **{k: (round(v, 3) if isinstance(v, float) else v) for k, v in rec.items()},
        }
        if rec.get("used"):
            for v, h in zip(line, rec["heights_m"], strict=True):
                if h is not None:
                    open_decks[v] = max(open_decks.get(v, 0.0), h)
        records.append(rec)
    cuts = []
    for w in cutting_ways(g, crossings, cut_cands):
        ln = lines[w]
        excl = street_exclusions(w)
        rec = measure_cutting(
            pts,
            ln,
            hw[w],
            [ln.cum[i] for i in range(len(g.lines[w]))],
            layer[w],
            shadow,
            lambda x, y: query(x, y, 30.0),
            lambda x, y, excl=excl: streets.at(pts, x, y, excl, shadow, query)[0],
        )
        mid = g.lines[w][len(g.lines[w]) // 2]
        cuts.append(
            {
                "kind": "cutting_profile",
                "way": w,
                "name": g.ways[w].tags.get("name"),
                "lon": round(g.lon[mid], 6),
                "lat": round(g.lat[mid], 6),
                **rec,
            }
        )
    g.cutting_profiles = cuts  # read by roads_3d (Stage B trench depths)
    return open_decks, records


REPORT_PATH = REPO_ROOT / "data" / "processed" / "checks" / "deck_pointcloud.json"
# The single combined map credit for City of Melbourne heights (data/SOURCES.md).
ATTRIBUTION = (
    "Building and road heights: Contains City of Melbourne Open Data (CC BY 4.0), modified"
)


def per_way(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """One confidence summary per deck way: what was measured and how well."""
    out: dict[str, dict[str, Any]] = {}
    for r in records:
        w = out.setdefault(
            str(r["upper_way"]),
            {
                "name": r.get("upper_name"),
                "crossings_sampled": 0,
                "crossings_measured": 0,
                "separations_m": [],
                "deck_points_min": None,
                "deck_iqr_max_m": None,
                "open_deck": None,
            },
        )
        if r["kind"] == "crossing":
            w["crossings_sampled"] += 1
            if r.get("used"):
                w["crossings_measured"] += 1
                w["separations_m"].append(r["separation_m"])
                n, iqr = r["deck_points"], r["deck_iqr_m"]
                w["deck_points_min"] = (
                    n if w["deck_points_min"] is None else min(n, w["deck_points_min"])
                )
                w["deck_iqr_max_m"] = (
                    iqr if w["deck_iqr_max_m"] is None else max(iqr, w["deck_iqr_max_m"])
                )
        else:
            w["open_deck"] = {
                "used": bool(r.get("used")),
                "reason": r.get("reason"),
                "max_m": r.get("max_m"),
                "vertices_measured": r.get("vertices_measured"),
                "vertices": r.get("vertices"),
            }
    return out


def write_report(
    records: list[dict[str, Any]],
    tiles: list[Path],
    calibration: dict[str, Any] | None = None,
    layer_floors: dict[str, Any] | None = None,
    precedence: dict[str, Any] | None = None,
    pending_trench_depth: list[dict[str, Any]] | None = None,
    anchors: list[dict[str, Any]] | None = None,
    inputs_sha256: dict[str, str] | None = None,
    below_ground: dict[str, Any] | None = None,
    path: Path = REPORT_PATH,
) -> None:
    import json

    manifest_path = POINTCLOUD_DIR / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    report = {
        "about": "Road-deck measurements from the City of Melbourne 3D Point Cloud 2018 used by "
        "pipeline/roads_3d.py (see pipeline/deck_pointcloud.py for the method). One record per "
        "road-on-top crossing over a road (`crossing`: deck surface minus lower road surface, "
        "or minus street level where the lower road is in a cutting) and per bridge way on top "
        "of nothing (`open_deck`: deck minus approach / street level). `used` = the measurement "
        "went into roads_3d. `anchors`: every vertex held by a measurement (way, vertex index, "
        "measured z, final z). Stage B: `cutting_profiles` = depth below street level along "
        "the CityLink cutting (street level minus road surface, per vertex); `trench_crossings` "
        "= every crossing over the cutting with its measured trench depth, the cutting height "
        "drawn there and the final clearance; `pending_trench_depth` = those still under 4 m "
        "(none expected); `below_ground` = tunnel depths, crossings over tunnels, the river-bed "
        "assumptions and the measured cutting pins demoted by the grade limits.",
        "licence": "Derived from OpenStreetMap (ODbL 1.0, (c) OpenStreetMap contributors) and "
        "City of Melbourne Open Data (CC BY 4.0)",
        "attribution": ATTRIBUTION,
        "inputs_sha256": inputs_sha256 or {},
        "tiles_read": [
            {"tile": t.name, "sha256": manifest.get(t.name, {}).get("sha256")} for t in tiles
        ],
        "thresholds": {
            "min_points": MIN_POINTS,
            "min_inlier_share": MIN_INLIER_SHARE,
            "max_iqr_m": MAX_IQR_M,
            "inlier_m": INLIER_M,
            "separation_m": [MIN_SEP_M, MAX_SEP_M],
            "lower_offsets_m": list(LOWER_OFFSETS_M),
            "min_profile_share": MIN_PROFILE_SHARE,
        },
        "summary": summarise_records(records),
        "calibration": calibration,
        "layer_floors": layer_floors,
        "precedence": precedence,
        "pending_trench_depth": pending_trench_depth or [],
        "trench_crossings": (below_ground or {}).get("trench_crossings", []),
        "cutting_profiles": (below_ground or {}).get("cutting_profiles", []),
        "below_ground": {
            k: v
            for k, v in (below_ground or {}).items()
            if k not in ("trench_crossings", "cutting_profiles")
        },
        "anchors": anchors or [],
        "per_deck_way": per_way(records),
        "records": records,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1, ensure_ascii=False) + "\n")
