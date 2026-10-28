"""Stacked building tiers from the City of Melbourne 2023 Building Footprints (for buildings.py).

Each City of Melbourne `Structure` footprint is one horizontal SLICE of a structure: the area whose
roof is at `footprint_max_elevation`, standing on `footprint_min_elevation` (both metres AHD), in a
structure whose ground is `structure_min_elevation`. A tower on a podium is a stack of slices
(Eureka Tower: 11). Each slice becomes a tier with
    min_height_m = footprint_min_elevation - structure_min_elevation   (tier base above ground)
    height_m     = footprint_max_elevation - structure_min_elevation   (tier top above ground)
and is drawn from min_height_m to height_m, so the tower sits on its podium.

Tidying, per structure, in this order (all thresholds below, counts in the build report):

0. **Versions** (`drop_superseded`, `drop_z_contained`). The dataset keeps an older capture of a
   slice next to its re-capture (date_captured 2018 next to 2022/2023, ~1,000 slices): an older
   slice that overlaps a newer slice of its structure in height (> ADJ_TOL_M) and in plan
   (>= SUPERSEDE_SHARE of the older one) is dropped. Then, where one slice's height range lies
   inside another's and they overlap in plan (same base, lower top: a lower roof around a
   taller part), the lower slice loses the overlapping area, so nothing is drawn twice.
1. **Support** (`tidy_supports`). A slice that is mostly (>= SUPPORT_SHARE of its area) over a
   lower slice of the same structure sits on it; a gap between them (CoM slices sometimes leave
   one) is closed by lowering the slice's base onto the highest such slice, unless the slice
   already rests on the top of another slice over >= REST_SHARE of its area (the Arts Centre
   spire stands on a small plinth slice, not on the wide roof below it). A slice with
   nothing of its structure beneath it and a base below TERRAIN_MAX_M stands on sloping ground
   (structure_min_elevation is the structure's LOWEST ground; the model ground is flat), so its
   base becomes 0. Higher unsupported slices are kept in the air: sky bridges, covered entries,
   cantilevers.
2. **Thin tiers** (`merge_thin`): a slice thinner than THIN_M is merged into the vertically
   adjacent slice (top/base within ADJ_TOL_M, overlapping) whose footprint is most similar
   (intersection over union). The merged tier spans both and keeps the footprint of the bigger
   (area x thickness) of the two. If the most similar neighbour is a much wider slice
   (IoU < MERGE_IOU) the thin slice is small plant or a parapet on (or under) it: it is dropped
   (<= THIN_M of height lost over its small area) and anything resting on it is lowered onto
   its base. A thin slice with no neighbour, or wider than its only neighbour, stays.
3. **Near-identical stacked tiers** (`merge_similar`): two stacked tiers whose footprints are
   nearly the same (IoU >= SIMILAR_IOU, setbacks of ~1 m) are merged as above, so a podium with
   small setbacks does not read as a staircase.
4. **Cap** (`cap_depth`): at most MAX_TIERS tiers stacked on top of each other. While a stack is
   deeper, the vertically adjacent pair in it with the most similar footprints (highest IoU, at
   least CAP_MIN_IOU) is merged as above. Side-by-side parts of one structure (a precinct-sized
   structure) are not stacked and are never merged.

A tier's id is `c<structure_id>_<objectid>` with the objectid of its lowest CoM slice, so ids stay
stable when merging elsewhere in the structure changes.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import shapely
from shapely.geometry import shape

SUPERSEDE_SHARE = 0.5  # an older capture overlapping a newer slice this much is replaced by it
MIN_SLICE_M2 = 2.0  # slice remnants smaller than this are dropped
SUPPORT_SHARE = 0.5  # a slice mostly over a lower slice of its structure sits on it
REST_SHARE = 0.1  # ... and one touching the top of another slice over this share rests on it
TERRAIN_MAX_M = 3.0  # unsupported slices lower than this stand on sloping ground (base -> 0)
THIN_M = 2.0  # tiers thinner than this are merged into a neighbour
ADJ_TOL_M = 0.3  # top of one slice and base of the next agree within this to be adjacent
ADJ_OVERLAP = 0.5  # adjacent slices overlap by at least this share of the smaller one
MERGE_IOU = 0.5  # a thin slice with a less similar neighbour is plant / a parapet: dropped
SIMILAR_IOU = 0.9  # stacked tiers this similar are merged (no staircase of small setbacks)
MAX_TIERS = 6  # tiers stacked on top of each other, at most
CAP_MIN_IOU = 0.3  # the cap only merges stacked tiers at least this similar
Z_DECIMALS = 2


@dataclass
class Tier:
    structure_id: str
    objectids: list[str]  # CoM objectids merged into this tier, lowest slice first
    geom: Any  # shapely (Multi)Polygon, WGS84
    base: float  # metres above the structure's ground
    top: float
    ground_ahd: float  # structure_min_elevation (m AHD)
    dates: list[str] = field(default_factory=list)  # date_captured of the merged slices
    top_objectid: str = ""  # the CoM slice whose roof is this tier's top
    flat: bool = True  # CoM roof_type of that slice is "Flat"
    area: float = 0.0  # in square degrees, for ratios only

    def __post_init__(self) -> None:
        self.area = self.geom.area
        if not self.top_objectid:
            self.top_objectid = self.objectids[-1]

    @property
    def id(self) -> str:
        return f"c{self.structure_id}_{self.objectids[0]}"

    @property
    def top_date(self) -> str:
        """date_captured of the slice whose roof is this tier's top."""
        return self.dates[self.objectids.index(self.top_objectid)]

    @property
    def thickness(self) -> float:
        return self.top - self.base

    @property
    def volume(self) -> float:
        return self.area * self.thickness


def _polygonal(g: Any) -> Any:
    g = g if g.is_valid else shapely.make_valid(g)
    if g.geom_type in ("Polygon", "MultiPolygon"):
        return g if not g.is_empty else None
    polys = [p for p in getattr(g, "geoms", []) if p.geom_type in ("Polygon", "MultiPolygon")]
    if not polys:
        return None
    u = shapely.union_all(polys)
    return u if not u.is_empty and u.area > 0 else None


def load_slices(path: Path, area: Any | None = None) -> dict[str, list[Tier]]:
    """CoM `Structure` slices grouped by structure_id (one Tier per slice, untidied).

    `area` (shapely, WGS84): keep only slices that intersect it (clipping happens at output).
    """
    data = json.loads(Path(path).read_text())
    prepared = None
    if area is not None:
        prepared = area
        shapely.prepare(prepared)
    out: dict[str, list[Tier]] = defaultdict(list)
    for f in data["features"]:
        p = f.get("properties") or {}
        if p.get("footprint_type") != "Structure" or not f.get("geometry"):
            continue
        fmin, fmax = p.get("footprint_min_elevation"), p.get("footprint_max_elevation")
        smin = p.get("structure_min_elevation")
        if fmin is None or fmax is None or smin is None:
            continue
        base, top = float(fmin) - float(smin), float(fmax) - float(smin)
        if not top > base or base < 0:
            continue
        g = _polygonal(shape(f["geometry"]))
        if g is None or (prepared is not None and not prepared.intersects(g)):
            continue
        sid = str(p.get("structure_id"))
        out[sid].append(
            Tier(
                sid,
                [str(p.get("objectid"))],
                g,
                round(base, Z_DECIMALS),
                round(top, Z_DECIMALS),
                float(smin),
                [str(p.get("date_captured") or "")],
                flat=(p.get("roof_type") or "Flat") == "Flat",
            )
        )
    for tiers in out.values():
        tiers.sort(key=lambda t: (t.base, t.top, t.objectids[0]))
    return dict(out)


def _inter(a: Tier, b: Tier) -> float:
    if not a.geom.intersects(b.geom):
        return 0.0
    return a.geom.intersection(b.geom).area


def _iou(a: Tier, b: Tier) -> float:
    i = _inter(a, b)
    u = a.area + b.area - i
    return i / u if u > 0 else 0.0


def _adjacent(lower: Tier, upper: Tier) -> bool:
    """`upper` rests on `lower`: touching in z and overlapping in plan."""
    if abs(lower.top - upper.base) > ADJ_TOL_M:
        return False
    small = min(lower.area, upper.area)
    return small > 0 and _inter(lower, upper) >= ADJ_OVERLAP * small


def drop_superseded(tiers: list[Tier], stats: Counter) -> list[Tier]:
    """Step 0a: drop older captures of a slice that a newer capture of its structure replaces."""
    newest = max(t.dates[0] for t in tiers)
    if all(t.dates[0] == newest for t in tiers):
        return tiers
    keep = []
    for o in tiers:
        newer = [n for n in tiers if n.dates[0] > o.dates[0]]
        if any(
            min(o.top, n.top) - max(o.base, n.base) > ADJ_TOL_M
            and _inter(o, n) >= SUPERSEDE_SHARE * o.area
            for n in newer
        ):
            stats["superseded_older_capture"] += 1
            continue
        keep.append(o)
    return keep


def _m2(g: Any) -> float:
    import math

    if g.is_empty:
        return 0.0
    return g.area * 111_320.0 * 110_950.0 * math.cos(math.radians(g.centroid.y))


def _oid(t: Tier) -> tuple[int, str]:
    o = t.objectids[0]
    return (int(o), o) if o.isdigit() else (-1, o)


def drop_z_contained(tiers: list[Tier], stats: Counter) -> list[Tier]:
    """Step 0b: a slice whose height range lies within a taller one's loses the shared area."""
    out = []
    for a in tiers:
        g = a.geom
        for b in tiers:
            if b is a or not g.intersects(b.geom):
                continue
            inside = b.base - ADJ_TOL_M <= a.base and a.top <= b.top + ADJ_TOL_M
            same = (a.base, a.top) == (b.base, b.top)
            if not inside or (same and _oid(a) < _oid(b)):
                # identical ranges: only the slice with the larger objectid gives way
                continue
            if b.thickness < a.thickness:
                continue
            if g.intersection(b.geom).area <= 0:
                continue
            g = _polygonal(g.difference(b.geom))
            if g is None:
                break
        if g is None or _m2(g) < MIN_SLICE_M2:
            stats["z_contained_dropped"] += 1
            continue
        if g is not a.geom:
            stats["z_contained_trimmed"] += 1
            a.geom = g
            a.area = g.area
        out.append(a)
    return out


def tidy_supports(tiers: list[Tier], stats: Counter) -> None:
    """Step 1: close gaps onto supporting slices; drop sloping-ground bases to 0 (in place)."""
    for t in sorted(tiers, key=lambda t: t.base):
        if t.base <= 0.05:
            t.base = 0.0
            continue
        sup = [
            s
            for s in tiers
            if s is not t
            and s.base < t.base - 0.05
            and t.area > 0
            and _inter(s, t) >= SUPPORT_SHARE * t.area
        ]
        if sup:
            rests = any(
                s is not t
                and abs(s.top - t.base) <= ADJ_TOL_M
                and _inter(s, t) >= REST_SHARE * t.area
                for s in tiers
            )
            below = [s.top for s in sup if s.top <= t.base + ADJ_TOL_M]
            if not rests and below and max(below) < t.base - ADJ_TOL_M:
                stats["gap_closed"] += 1
                t.base = max(below)
            continue
        if t.base < TERRAIN_MAX_M:
            stats["terrain_base_to_ground"] += 1
            t.base = 0.0
        else:
            stats["elevated_kept"] += 1


def _merge(a: Tier, b: Tier) -> Tier:
    """One tier spanning a and b, with the footprint of the bigger (area x thickness)."""
    keep = a if a.volume >= b.volume else b
    lower, upper = (a, b) if (a.base, a.top) <= (b.base, b.top) else (b, a)
    top_t = a if a.top >= b.top else b
    return Tier(
        keep.structure_id,
        lower.objectids + upper.objectids,
        keep.geom,
        min(a.base, b.base),
        max(a.top, b.top),
        keep.ground_ahd,
        lower.dates + upper.dates,
        top_t.top_objectid,
        top_t.flat,
    )


def merge_thin(tiers: list[Tier], stats: Counter) -> list[Tier]:
    """Step 2 (see module docstring). Returns the new list."""
    tiers = list(tiers)
    done: set[int] = set()
    while True:
        cands = [t for t in tiers if t.thickness < THIN_M and id(t) not in done]
        if not cands:
            return tiers
        s = min(cands, key=lambda t: (t.thickness, t.base, t.objectids[0]))
        nbrs = [(n, "below") for n in tiers if n is not s and _adjacent(n, s)] + [
            (n, "above") for n in tiers if n is not s and _adjacent(s, n)
        ]
        if not nbrs:
            done.add(id(s))
            continue
        n, side = max(nbrs, key=lambda x: (_iou(s, x[0]), x[0].volume))
        if _iou(s, n) >= MERGE_IOU:
            m = _merge(s, n)
            tiers = [t for t in tiers if t is not s and t is not n] + [m]
            stats["thin_merged"] += 1
            continue
        if s.area < n.area:
            # small thin plant / parapet on (or under) a wider slice: drop it; what rested on it
            # comes down onto its base
            for t in tiers:
                if t is not s and t is not n and _adjacent(s, t):
                    t.base = s.base
            if side == "above" and not any(
                t is not s and t is not n and _adjacent(t, n) for t in tiers
            ):
                n.base = s.base
            n.objectids = n.objectids + s.objectids
            n.dates = n.dates + s.dates
            tiers = [t for t in tiers if t is not s]
            stats["thin_dropped_small"] += 1
            continue
        done.add(id(s))
        stats["thin_kept_wide"] += 1


def _depths(tiers: list[Tier]) -> tuple[dict[int, int], dict[int, int], list[tuple[Tier, Tier]]]:
    """Tiers stacked below (incl. itself) and above (incl. itself) each tier, + adjacent pairs."""
    pairs = [(a, b) for a in tiers for b in tiers if a is not b and _adjacent(a, b)]
    below: dict[int, list[Tier]] = defaultdict(list)
    above: dict[int, list[Tier]] = defaultdict(list)
    for a, b in pairs:
        below[id(b)].append(a)
        above[id(a)].append(b)
    order = sorted(tiers, key=lambda t: (t.base, t.top))
    down: dict[int, int] = {}
    for t in order:
        down[id(t)] = 1 + max((down.get(id(a), 1) for a in below[id(t)]), default=0)
    up: dict[int, int] = {}
    for t in reversed(order):
        up[id(t)] = 1 + max((up.get(id(b), 1) for b in above[id(t)]), default=0)
    return down, up, pairs


def merge_similar(tiers: list[Tier], stats: Counter) -> list[Tier]:
    """Step 3 (see module docstring). Returns the new list."""
    tiers = list(tiers)
    while True:
        pairs = [
            (a, b)
            for a in tiers
            for b in tiers
            if a is not b and _adjacent(a, b) and _iou(a, b) >= SIMILAR_IOU
        ]
        if not pairs:
            return tiers
        a, b = max(pairs, key=lambda p: (_iou(*p), -p[0].base))
        m = _merge(a, b)
        tiers = [t for t in tiers if t is not a and t is not b] + [m]
        stats["similar_merged"] += 1


def cap_depth(tiers: list[Tier], stats: Counter, max_tiers: int = MAX_TIERS) -> list[Tier]:
    """Step 4 (see module docstring). Returns the new list."""
    tiers = list(tiers)
    while True:
        down, up, pairs = _depths(tiers)
        over = [
            (a, b)
            for a, b in pairs
            if down[id(a)] + up[id(b)] > max_tiers and _iou(a, b) >= CAP_MIN_IOU
        ]
        if not over:
            if any(down[id(t)] + up[id(t)] - 1 > max_tiers for t in tiers):
                stats["structures_over_cap_left"] += 1
            return tiers
        a, b = max(over, key=lambda p: (_iou(*p), -p[0].base))
        m = _merge(a, b)
        tiers = [t for t in tiers if t is not a and t is not b] + [m]
        stats["cap_merged"] += 1


def max_stack(tiers: list[Tier]) -> int:
    down, up, _ = _depths(tiers)
    return max((down[id(t)] + up[id(t)] - 1 for t in tiers), default=0)


def build_tiers(slices: dict[str, list[Tier]], stats: Counter | None = None) -> list[Tier]:
    """Steps 1-3 for every structure. Returns all tiers, sorted by id."""
    stats = stats if stats is not None else Counter()
    out: list[Tier] = []
    for sid in sorted(slices):
        tiers = [
            Tier(
                t.structure_id,
                list(t.objectids),
                t.geom,
                t.base,
                t.top,
                t.ground_ahd,
                list(t.dates),
                t.top_objectid,
                t.flat,
            )
            for t in slices[sid]
        ]
        stats["slices_in"] += len(tiers)
        tiers = drop_superseded(tiers, stats)
        tiers = drop_z_contained(tiers, stats)
        tidy_supports(tiers, stats)
        tiers = merge_thin(tiers, stats)
        tiers = merge_similar(tiers, stats)
        tiers = cap_depth(tiers, stats)
        for t in tiers:
            t.base, t.top = round(t.base, Z_DECIMALS), round(t.top, Z_DECIMALS)
        out.extend(tiers)
        stats["structures"] += 1
    stats["tiers_out"] = len(out)
    out.sort(key=lambda t: t.id)
    return out


def unsupported_elevated(tiers: list[Tier]) -> list[Tier]:
    """Tiers above the ground with nothing of their structure beneath them (sky bridges,
    covered entries, cantilevers, or slices on sloping ground)."""
    by: dict[str, list[Tier]] = defaultdict(list)
    for t in tiers:
        by[t.structure_id].append(t)
    out = []
    for t in tiers:
        if t.base <= 0:
            continue
        if not any(
            s is not t
            and s.base < t.base - 0.05
            and t.area > 0
            and _inter(s, t) >= SUPPORT_SHARE * t.area
            for s in by[t.structure_id]
        ):
            out.append(t)
    return out
