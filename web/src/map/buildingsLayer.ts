import type { Layer } from "@deck.gl/core";
import { PathLayer, PolygonLayer } from "@deck.gl/layers";
import type { BuildingPolygon, Position } from "../data/buildingsContract";
import { type BBox, SIM_BBOX } from "../config/bbox";
import {
  buildingFill,
  hiddenRoofline,
  lowRiseFade,
  outerRingFade,
  palette,
  rooflineOpacity,
  rooflineZoomT,
  TOWER_EDGE_MIN_OPACITY,
} from "../theme/palette";

type RGBA = [number, number, number, number];

const M_PER_DEG_LAT = 110_574;
const mPerDegLon = (lat: number) => 111_320 * Math.cos((lat * Math.PI) / 180);

/** Roof outline: a footprint's outer ring lifted to the building's height. */
export interface Roofline {
  path: Position[];
  height: number;
  /** Line alpha multiplier (0–1): 1 inside SIM_BBOX, fading with distance outside it (palette.outerRingFade). */
  fade: number;
}

/** Distance in metres from a point to a bbox (0 inside). */
export function distanceOutsideBBoxM(b: BBox, lon: number, lat: number): number {
  const dx = Math.max(b.west - lon, 0, lon - b.east) * mPerDegLon(lat);
  const dy = Math.max(b.south - lat, 0, lat - b.north) * M_PER_DEG_LAT;
  return Math.hypot(dx, dy);
}

export function toRooflines(data: BuildingPolygon[]): Roofline[] {
  return data.map((d) => {
    const ring = d.polygon[0] ?? [];
    const first = ring[0];
    const fade = first ? outerRingFade(distanceOutsideBBoxM(SIM_BBOX, first[0], first[1])) : 1;
    return { path: ring.map((p) => [p[0], p[1], d.height] as Position), height: d.height, fade };
  });
}

/**
 * Vertical corner edges for tall towers. Rooflines alone make a 300 m tower read as a faint column; a few vertical
 * lines at its real corners make it a solid ghost form. To avoid a haze of lines (the Phase 1 lesson), only:
 * - buildings at least `minHeightM` tall get edges;
 * - only vertices where the footprint turns by at least `minTurnDeg` count as corners (curved facades get none);
 * - corners closer than `minSpacingM` to a sharper one are dropped (no double lines at tiny notches);
 * - at most `maxPerBuilding` per building, sharpest first;
 * - stacked tiers share lines: see toTowerEdges (George's "silhouette" pick at the Stage C look review).
 */
export const TOWER_EDGES = { minHeightM: 60, minTurnDeg: 35, minSpacingM: 4, maxPerBuilding: 12 } as const;

export interface TowerEdge {
  path: [Position, Position];
  fade: number;
}

/** A tier counts as stacked on another when its base is within this of the other's top (metres). */
const STACK_TOLERANCE_M = 1;
/** A corner line carries on through a stacked tier whose outline passes within this (metres). */
const SILHOUETTE_TOLERANCE_M = 1.5;
/** Tiers shorter than this (metres) start no corner lines of their own (their rooflines outline them). */
const SILHOUETTE_MIN_TIER_M = 15;

function pointInRing(x: number, y: number, ring: readonly (readonly number[])[]): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i]! as [number, number];
    const [xj, yj] = ring[j]! as [number, number];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

const ringCentre = (ring: Position[]): [number, number] => {
  let x = 0, y = 0;
  for (const p of ring) {
    x += p[0];
    y += p[1];
  }
  return [x / ring.length, y / ring.length];
};

/** Distance in metres from a lon/lat point to a ring's outline (local flat approximation). */
function distanceToRingM(lon: number, lat: number, ring: Position[]): number {
  const kx = mPerDegLon(lat);
  let best = Infinity;
  for (let i = 0; i + 1 < ring.length; i++) {
    const ax = (ring[i]![0] - lon) * kx, ay = (ring[i]![1] - lat) * M_PER_DEG_LAT;
    const bx = (ring[i + 1]![0] - lon) * kx, by = (ring[i + 1]![1] - lat) * M_PER_DEG_LAT;
    const dx = bx - ax, dy = by - ay;
    const l2 = dx * dx + dy * dy;
    const t = l2 > 0 ? Math.min(1, Math.max(0, -(ax * dx + ay * dy) / l2)) : 0;
    best = Math.min(best, Math.hypot(ax + t * dx, ay + t * dy));
  }
  return best;
}

/**
 * Which pieces are stacked on which: a piece U sits on piece D when U's base is within STACK_TOLERANCE_M of D's top
 * and their footprints overlap (either centre inside the other's outer ring, or a vertex of U inside D). Indexed by
 * rounded base and top height, and computed lazily per piece, so it stays fast on ~50k pieces.
 */
export function stackLinks(data: BuildingPolygon[]) {
  const index = (key: (d: BuildingPolygon) => number) => {
    const m = new Map<number, number[]>();
    data.forEach((d, i) => {
      const k = Math.round(key(d));
      (m.get(k) ?? m.set(k, []).get(k)!).push(i);
    });
    return m;
  };
  const byBase = index((d) => d.base);
  const byTop = index((d) => d.height);
  const boxes = new Map<number, [number, number, number, number]>();
  const box = (i: number) => {
    let b = boxes.get(i);
    if (!b) {
      b = [Infinity, Infinity, -Infinity, -Infinity];
      for (const p of data[i]!.polygon[0] ?? []) {
        b[0] = Math.min(b[0], p[0]);
        b[1] = Math.min(b[1], p[1]);
        b[2] = Math.max(b[2], p[0]);
        b[3] = Math.max(b[3], p[1]);
      }
      boxes.set(i, b);
    }
    return b;
  };
  const boxesTouch = (i: number, j: number) => {
    const a = box(i), b = box(j);
    return a[0] <= b[2] && b[0] <= a[2] && a[1] <= b[3] && b[1] <= a[3];
  };
  const overlaps = (lo: Position[], up: Position[]) => {
    if (lo.length < 3 || up.length < 3) return false;
    const [lx, ly] = ringCentre(lo);
    const [ux, uy] = ringCentre(up);
    return pointInRing(ux, uy, lo) || pointInRing(lx, ly, up) || up.some((p) => pointInRing(p[0], p[1], lo));
  };
  const near = (m: Map<number, number[]>, h: number, key: (d: BuildingPolygon) => number, self: number) => {
    const out: number[] = [];
    const k = Math.round(h);
    for (let x = k - 1; x <= k + 1; x++) for (const j of m.get(x) ?? []) if (j !== self && Math.abs(key(data[j]!) - h) <= STACK_TOLERANCE_M) out.push(j);
    return out;
  };
  const memo = (f: (i: number) => number[]) => {
    const cache = new Map<number, number[]>();
    return (i: number) => cache.get(i) ?? cache.set(i, f(i)).get(i)!;
  };
  const ring = (i: number) => data[i]!.polygon[0] ?? [];
  return {
    /** Pieces sitting on piece i. */
    above: memo((i) =>
      data[i]!.height > 0 ? near(byBase, data[i]!.height, (d) => d.base, i).filter((j) => data[j]!.base > 0 && boxesTouch(i, j) && overlaps(ring(i), ring(j))) : [],
    ),
    /** Pieces piece i sits on. */
    below: memo((i) => (data[i]!.base > 0 ? near(byTop, data[i]!.base, (d) => d.height, i).filter((j) => boxesTouch(i, j) && overlaps(ring(j), ring(i))) : [])),
  };
}

/** Indices (into pts) of the convex hull's vertices (monotone chain). */
function hullIndices(pts: readonly (readonly [number, number])[]): Set<number> {
  const order = [...pts.keys()].sort((a, b) => pts[a]![0] - pts[b]![0] || pts[a]![1] - pts[b]![1]);
  const cross = (o: number, a: number, b: number) =>
    (pts[a]![0] - pts[o]![0]) * (pts[b]![1] - pts[o]![1]) - (pts[a]![1] - pts[o]![1]) * (pts[b]![0] - pts[o]![0]);
  const half = (idx: number[]) => {
    const h: number[] = [];
    for (const i of idx) {
      while (h.length >= 2 && cross(h[h.length - 2]!, h[h.length - 1]!, i) <= 0) h.pop();
      h.push(i);
    }
    return h;
  };
  return new Set([...half(order), ...half([...order].reverse())]);
}

/**
 * Indices of a piece's corner vertices (see TOWER_EDGES), sharpest first; ring without the closing vertex. Only
 * corners on the footprint's convex hull count (the outer silhouette): notches and re-entrant corners never show as a
 * tower's outline.
 */
function cornerVertices(ring: Position[], opts: typeof TOWER_EDGES): { src: Position[]; kept: number[] } {
  const closed = ring.length > 1 && ring[0]![0] === ring.at(-1)![0] && ring[0]![1] === ring.at(-1)![1];
  const src = closed ? ring.slice(0, -1) : ring;
  const n = src.length;
  if (ring.length < 4 || n < 3) return { src, kept: [] };
  const minTurn = (opts.minTurnDeg * Math.PI) / 180;
  const [lon0, lat0] = src[0]!;
  const kx = mPerDegLon(lat0);
  const pts = src.map((p) => [(p[0] - lon0) * kx, (p[1] - lat0) * M_PER_DEG_LAT] as const);
  const corners: { i: number; turn: number }[] = [];
  for (let i = 0; i < n; i++) {
    const a = pts[(i - 1 + n) % n]!;
    const b = pts[i]!;
    const c = pts[(i + 1) % n]!;
    const ux = b[0] - a[0], uy = b[1] - a[1];
    const vx = c[0] - b[0], vy = c[1] - b[1];
    const lu = Math.hypot(ux, uy), lv = Math.hypot(vx, vy);
    if (lu < 1e-6 || lv < 1e-6) continue;
    const turn = Math.acos(Math.min(1, Math.max(-1, (ux * vx + uy * vy) / (lu * lv))));
    if (turn >= minTurn) corners.push({ i, turn });
  }
  const hull = hullIndices(pts);
  const outer = corners.filter((c) => hull.has(c.i));
  outer.sort((p, q) => q.turn - p.turn);
  const kept: number[] = [];
  for (const { i } of outer) {
    if (kept.length >= opts.maxPerBuilding) break;
    const p = pts[i]!;
    if (kept.every((k) => Math.hypot(pts[k]![0] - p[0], pts[k]![1] - p[1]) >= opts.minSpacingM)) kept.push(i);
  }
  return { src, kept };
}

/**
 * Corner edges of tall pieces, as silhouette lines: only corners on each footprint's convex hull count, and each
 * corner line carries on up and down through stacked tiers whose outline passes the same spot (within
 * SILHOUETTE_TOLERANCE_M). A straight tower gets one continuous line per corner instead of one per tier, and a tier's
 * corner already covered by a longer line is dropped. Set-backs keep their own (hull) corners; tiers under
 * SILHOUETTE_MIN_TIER_M start none.
 */
export function toTowerEdges(data: BuildingPolygon[], opts = TOWER_EDGES): TowerEdge[] {
  const out: TowerEdge[] = [];
  const links = stackLinks(data);
  // Longest pieces first, so their lines win; corners consumed by a longer line are skipped.
  const tall = [...data.keys()]
    .filter((i) => data[i]!.height >= opts.minHeightM)
    .sort((a, b) => data[b]!.height - data[b]!.base - (data[a]!.height - data[a]!.base));
  const consumed = new Map<number, [number, number][]>();
  const isConsumed = (i: number, lon: number, lat: number) =>
    (consumed.get(i) ?? []).some(([x, y]) => Math.hypot((x - lon) * mPerDegLon(lat), (y - lat) * M_PER_DEG_LAT) < SILHOUETTE_TOLERANCE_M * 2);
  for (const index of tall) {
    const d = data[index]!;
    if (d.height - d.base < SILHOUETTE_MIN_TIER_M) continue;
    const { src, kept } = cornerVertices(d.polygon[0] ?? [], opts);
    if (!kept.length) continue;
    const fade = outerRingFade(distanceOutsideBBoxM(SIM_BBOX, src[0]![0], src[0]![1]));
    for (const i of kept) {
      const [lon, lat] = src[i]!;
      if (isConsumed(index, lon, lat)) continue;
      // Follow the corner up (or down) while the next tier's outline passes the same spot. Every step must reach a
      // strictly higher top (lower base) and a piece not visited yet: thin tiers at one level (within
      // STACK_TOLERANCE_M) each sit "on" the other, and must not send the walk round in a loop.
      const seen = new Set<number>([index]);
      const follow = (start: number, next: (i: number) => number[], end: (d: BuildingPolygon) => number, up: boolean): number => {
        let cur = start;
        for (;;) {
          const from = end(data[cur]!);
          const n = next(cur).find((j) => {
            if (seen.has(j) || (up ? end(data[j]!) <= from : end(data[j]!) >= from)) return false;
            const r = data[j]!.polygon[0] ?? [];
            return r.length >= 3 && distanceToRingM(lon, lat, r) <= SILHOUETTE_TOLERANCE_M;
          });
          if (n === undefined) return cur;
          seen.add(n);
          (consumed.get(n) ?? consumed.set(n, []).get(n)!).push([lon, lat]);
          cur = n;
        }
      };
      const topPiece = follow(index, links.above, (d) => d.height, true);
      const bottomPiece = follow(index, links.below, (d) => d.base, false);
      out.push({ path: [[lon, lat, data[bottomPiece]!.base], [lon, lat, data[topPiece]!.height]], fade });
    }
  }
  return out;
}

const withFade = ([r, g, b, a]: readonly number[], fade: number): RGBA => [r!, g!, b!, Math.round(a! * fade)];

/**
 * The "ghost city", in three passes:
 *
 * 1. Fills: extruded, translucent, back faces culled, depth writes ON. Each pixel shows only the nearest building
 *    face at the slider's 15–30% alpha, so the dense CBD no longer stacks dozens of faces into a pale haze.
 * 2. Hidden rooflines: roof edges BEHIND other buildings (depth test "greater"), faint. This keeps the x-ray feel:
 *    you can still see towers through the ones in front.
 * 3. Visible rooflines: roof edges in front, thin and bright.
 * 4. Tower edges: faint vertical silhouette lines at the outer corners of tall towers (toTowerEdges), visible ones only.
 * Rooflines fade when zoomed out (palette.rooflineOpacity), with distance outside SIM_BBOX (outerRingFade) and,
 * zoomed out, on low-rise buildings (lowRiseFade), so thousands of small roofs don't turn into bright speckle.
 *
 * Rooflines replace deck.gl's `wireframe`, which draws a vertical line at every footprint vertex: on real,
 * many-vertex footprints that was most of the haze. Phase 1 look decision (task C), chosen by screenshots.
 */
export function buildingsLayers(
  data: BuildingPolygon[],
  rooflines: Roofline[],
  towerEdges: TowerEdge[],
  opacity: number,
  zoom: number,
): Layer[] {
  const lineOpacity = rooflineOpacity(zoom);
  const hidden = hiddenRoofline();
  // Colours depend on zoom only inside the fade band; outside it the trigger is constant and nothing recomputes.
  const zoomT = rooflineZoomT(zoom);
  const rooflineFade = (d: Roofline) => d.fade * lowRiseFade(d.height, zoom);
  const fill = buildingFill(opacity) as unknown as RGBA;
  return [
    new PolygonLayer<BuildingPolygon>({
      id: "buildings-fill",
      data,
      extruded: true,
      wireframe: false,
      // Vertices carry z = base; deck.gl adds the elevation on top, so a piece spans base..height.
      getPolygon: (d) => (d.base > 0 ? d.polygon.map((ring) => ring.map((p) => [p[0], p[1], d.base] as Position)) : d.polygon),
      getElevation: (d) => d.height - d.base,
      getFillColor: fill,
      material: { ambient: 0.55, diffuse: 0.45, shininess: 12, specularColor: [40, 48, 56] },
      parameters: {
        depthWriteEnabled: true,
        depthCompare: "less-equal",
        cullMode: "back",
        // Push fills slightly back so rooflines sitting exactly on their edges win the depth test.
        depthBias: 1,
        depthBiasSlopeScale: 1,
      },
      updateTriggers: { getFillColor: fill[3] },
    }),
    new PathLayer<Roofline>({
      id: "buildings-roofline-hidden",
      data: rooflines,
      getPath: (d) => d.path,
      getColor: (d) => withFade(hidden, rooflineFade(d)),
      updateTriggers: { getColor: zoomT },
      opacity: lineOpacity,
      getWidth: 1,
      widthUnits: "pixels",
      parameters: { depthWriteEnabled: false, depthCompare: "greater" },
    }),
    new PathLayer<Roofline>({
      id: "buildings-roofline",
      data: rooflines,
      getPath: (d) => d.path,
      getColor: (d) => withFade(palette.buildingOutline, rooflineFade(d)),
      updateTriggers: { getColor: zoomT },
      opacity: lineOpacity,
      getWidth: 1,
      widthUnits: "pixels",
      parameters: { depthWriteEnabled: false, depthCompare: "less-equal" },
    }),
    // Tower corner edges: visible ones only (no hidden pass), so a tower's back corners are hidden by its own
    // front faces and it reads as a solid form rather than a wire cage.
    new PathLayer<TowerEdge>({
      id: "buildings-tower-edges",
      data: towerEdges,
      getPath: (d) => d.path,
      // Screen-facing: with billboard off, deck.gl extrudes line width in the horizontal plane, so a vertical
      // line has zero width and never draws. Horizontal rooflines keep billboard off (thinner when tilted).
      billboard: true,
      getColor: (d) => withFade([...palette.buildingOutline.slice(0, 3), palette.towerEdgeAlpha], d.fade),
      opacity: Math.max(lineOpacity, TOWER_EDGE_MIN_OPACITY),
      getWidth: 1,
      widthUnits: "pixels",
      parameters: { depthWriteEnabled: false, depthCompare: "less-equal" },
    }),
  ];
}
