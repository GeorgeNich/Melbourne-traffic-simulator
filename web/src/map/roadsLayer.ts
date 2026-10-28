import type { Layer } from "@deck.gl/core";
import { PathLayer, SolidPolygonLayer } from "@deck.gl/layers";
import type { Position3, RoadFeature, ZSource } from "../data/roadsContract";
import { palette, type RGBA as PaletteRGBA } from "../theme/palette";

type RGBA = [number, number, number, number];

const M_PER_DEG_LAT = 110_574;
const mPerDegLon = (lat: number) => 111_320 * Math.cos((lat * Math.PI) / 180);

/**
 * Which roads become ribbons, and how they fade:
 * - a way is elevated if any vertex is above `minZ` (bridges, viaducts and the ramps leading up to them), and
 *   below ground if any vertex is below -minZ (Stage B: tunnels, the City Road underpass, the CityLink cutting);
 * - only its elevated / below-ground runs are kept, plus one ground-side vertex at each end so ramps meet the ground;
 * - alpha rises with height (or depth), from 0 at ground level to full at `fullAlphaZ`, so ramp feet and tunnel
 *   portals fade into the base map;
 * - below-ground runs of `tunnel: true` ways are X-ray tunnel ribbons (TunnelGeometry, behind "Show tunnels");
 *   below-ground runs of other ways are open to the sky (the CityLink cutting, underpass approaches) and are drawn as
 *   normal solid ribbons, with a rim line at ground level instead of a fascia under the deck (a trench, not a slab);
 * - parts of tunnel ways at ground level or above are not drawn (covered ways at grade).
 */
export const RIBBONS = { minZ: 0.3, fullAlphaZ: 4, dropSpacingM: 35, dropMinZ: 4, fasciaDepthM: 1.6 } as const;

/**
 * X-ray tunnels must never read as bridges: no vertical lines along them (those read as piers holding a deck up).
 * Instead:
 * - hidden-line dashed edges (dashM, metres on / off) and a faint deck;
 * - a "curtain": a faint continuous vertical sheet from the tunnel's centreline up to the street / water surface,
 *   in curtainBands bands that fade towards the surface, so the depth below ground reads at any angle; its top is
 *   marked by a faint dotted surface trace (traceDashM) at z = 0, so the tunnel visibly hangs below the surface;
 * - a portal frame where a tunnel meets street level: two posts at the road edges, portalHeightM tall, joined by a
 *   lintel (the tunnel mouth).
 */
export const TUNNELS = {
  dashM: { on: 9, off: 6 },
  traceDashM: { on: 3, off: 6 },
  portalHeightM: 5,
  curtainBands: [1, 0.5, 0.2],
  curtainMinDepthM: 0.5,
} as const;

/**
 * Deck width (m) by OSM highway class. roads_3d has no lane counts, so these are typical carriageway widths
 * (about 3.3–3.5 m a lane); motorways are mapped as one way per direction.
 */
export const ROAD_WIDTH_M: Readonly<Record<string, number>> = {
  motorway: 11,
  trunk: 11,
  primary: 10,
  secondary: 8.5,
  tertiary: 7.5,
  motorway_link: 6.5,
  trunk_link: 6.5,
  primary_link: 6,
  secondary_link: 6,
  tertiary_link: 6,
  unclassified: 6.5,
  residential: 6.5,
  living_street: 5,
  service: 4.5,
};
export const DEFAULT_ROAD_WIDTH_M = 6;

/**
 * How a ribbon's heights are labelled in the `?heights=source` view: only `measured` z_source counts as measured;
 * `estimated` and `ground` are estimated. Production builds skip contract validation, so a missing z_source also falls
 * back to estimated: nothing is ever shown as measured unless the data says so.
 */
export type HeightConfidence = "measured" | "estimated";

export function heightConfidence(zSource: ZSource | undefined): HeightConfidence {
  return zSource === "measured" ? "measured" : "estimated";
}

/** Dev-only view of elevated roads: "normal" (the look) or "source" (tinted by z_source). */
export type HeightsView = "normal" | "source";

/** Dev-only `?heights=source`; ignored in production builds. */
export function heightsViewFromQuery(search: string, dev: boolean): HeightsView {
  return dev && new URLSearchParams(search).get("heights") === "source" ? "source" : "normal";
}

export interface RibbonColors {
  deck: PaletteRGBA;
  edge: PaletteRGBA;
  edgeHidden: PaletteRGBA;
  fascia: PaletteRGBA;
  /** Edge and fascia lines drawn dashed. */
  dashed: boolean;
}

/** Ribbon colours for a height confidence: measured = the normal ghost colours; estimated = dimmer, greyer, dashed. */
export function ribbonColors(confidence: HeightConfidence): RibbonColors {
  if (confidence === "measured") {
    const [r, g, b] = palette.roadEdge;
    return {
      deck: palette.roadDeck,
      edge: palette.roadEdge,
      edgeHidden: [r, g, b, palette.roadEdgeHiddenAlpha],
      fascia: palette.roadFascia,
      dashed: false,
    };
  }
  const e = palette.roadEstimated;
  const [r, g, b] = e.edge;
  return { deck: e.deck, edge: e.edge, edgeHidden: [r, g, b, e.edgeHiddenAlpha], fascia: e.fascia, dashed: true };
}

export interface Ribbon {
  wayId: number;
  confidence: HeightConfidence;
  name: string | null;
  highway: string;
  width: number;
  path: Position3[];
  /** Per-vertex alpha multiplier (0–1). */
  alpha: number[];
}

export interface RibbonEdge {
  path: Position3[];
  alpha: number[];
  confidence: HeightConfidence;
}

export interface DropLine {
  path: [Position3, Position3];
}

/** X-ray tunnel ribbons (below-ground runs of `tunnel: true` ways). */
export interface TunnelGeometry {
  ribbons: Ribbon[];
  edges: RibbonEdge[];
  /** Edge lines, already cut into dashes (the hidden-line convention). */
  dashes: RibbonEdge[];
  /** Portal frames where a tunnel meets street level: [left foot, left top, right top, right foot]. */
  portals: { path: Position3[] }[];
  /** Dotted surface trace: the tunnel centreline at z = 0, where it is deep enough to have a curtain. */
  trace: RibbonEdge[];
  /** Vertical quads from the centreline up to the surface, per segment and band; `fade` is the band's alpha multiplier. */
  curtain: { polygon: Position3[]; fade: number }[];
}

export interface RoadGeometry {
  ribbons: Ribbon[];
  edges: RibbonEdge[];
  /**
   * Elevated runs: lower edge of the deck's side face (fascia), fasciaDepthM below each edge where the deck is high
   * enough. Open below-ground runs: the trench rim, at ground level above each edge.
   */
  fascia: RibbonEdge[];
  drops: DropLine[];
  tunnel: TunnelGeometry;
}

export function ribbonAlpha(z: number): number {
  const t = Math.min(1, Math.max(0, z / RIBBONS.fullAlphaZ));
  return t * t * (3 - 2 * t);
}

/** Alpha for a below-ground vertex: rises with depth, as ribbonAlpha does with height. */
export function depthAlpha(z: number): number {
  return ribbonAlpha(-z);
}

/** Split a way into runs of vertices where `keep(z)`, each extended by one vertex at both ends (where it meets the ground). */
function runsWhere(coords: Position3[], keep: (z: number) => boolean): Position3[][] {
  const runs: Position3[][] = [];
  let i = 0;
  while (i < coords.length) {
    if (!keep(coords[i]![2])) {
      i++;
      continue;
    }
    const start = Math.max(0, i - 1);
    while (i < coords.length && keep(coords[i]![2])) i++;
    const end = Math.min(coords.length - 1, i); // inclusive: one ground vertex after the run
    const run = coords.slice(start, end + 1);
    if (run.length >= 2) runs.push(run);
  }
  return runs;
}

/** Split a way into its elevated runs, each extended by one vertex at both ends (where it meets the ground). */
export function elevatedRuns(coords: Position3[], minZ: number = RIBBONS.minZ): Position3[][] {
  return runsWhere(coords, (z) => z > minZ);
}

/** Split a way into its below-ground runs (z < -minDepth), each extended by one vertex at both ends. */
export function belowGroundRuns(coords: Position3[], minDepth: number = RIBBONS.minZ): Position3[][] {
  return runsWhere(coords, (z) => z < -minDepth);
}

/**
 * Offset a 3D path sideways (left > 0, in metres) in the horizontal plane, keeping each vertex's z. Mitred joins,
 * with the miter length clamped so sharp turns don't spike.
 */
export function offsetPath(path: Position3[], offsetM: number): Position3[] {
  const n = path.length;
  if (n < 2) return path.slice();
  const lat0 = path[0]![1];
  const kx = mPerDegLon(lat0);
  const xy = path.map((p) => [(p[0] - path[0]![0]) * kx, (p[1] - lat0) * M_PER_DEG_LAT] as const);
  const normal = (a: readonly number[], b: readonly number[]): [number, number] => {
    const dx = b[0]! - a[0]!;
    const dy = b[1]! - a[1]!;
    const l = Math.hypot(dx, dy) || 1;
    return [-dy / l, dx / l];
  };
  const out: Position3[] = [];
  for (let i = 0; i < n; i++) {
    const nPrev = i > 0 ? normal(xy[i - 1]!, xy[i]!) : null;
    const nNext = i < n - 1 ? normal(xy[i]!, xy[i + 1]!) : null;
    let [nx, ny] = nPrev && nNext ? [nPrev[0] + nNext[0], nPrev[1] + nNext[1]] : (nPrev ?? nNext)!;
    const l = Math.hypot(nx, ny);
    if (l < 1e-9) [nx, ny] = (nNext ?? nPrev)!;
    else [nx, ny] = [nx / l, ny / l];
    // Miter: scale by 1 / cos(half the turn), clamped to 2x.
    const ref = nNext ?? nPrev!;
    const cos = Math.max(0.5, nx * ref[0] + ny * ref[1]);
    const d = offsetM / cos;
    const [x, y] = [xy[i]![0] + nx * d, xy[i]![1] + ny * d];
    out.push([path[0]![0] + x / kx, lat0 + y / M_PER_DEG_LAT, path[i]![2]]);
  }
  return out;
}

/** Drop-lines from deck to ground every `spacingM` along a run, where the deck is at least `minZ` high. */
export function dropLines(path: Position3[], spacingM: number = RIBBONS.dropSpacingM, minZ: number = RIBBONS.dropMinZ): DropLine[] {
  const out: DropLine[] = [];
  let next = spacingM / 2;
  let along = 0;
  for (let i = 1; i < path.length; i++) {
    const a = path[i - 1]!;
    const b = path[i]!;
    const seg = Math.hypot((b[0] - a[0]) * mPerDegLon(a[1]), (b[1] - a[1]) * M_PER_DEG_LAT);
    while (seg > 0 && next <= along + seg) {
      const t = (next - along) / seg;
      const p: Position3 = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
      if (p[2] >= minZ) out.push({ path: [p, [p[0], p[1], 0]] });
      next += spacingM;
    }
    along += seg;
  }
  return out;
}

/**
 * Cut a line into dashes `onM` long with `offM` gaps (horizontal metres), interpolating z and alpha at the cuts.
 * Used for estimated-height edges in the `?heights=source` view.
 */
export function dashLine(line: RibbonEdge, onM: number, offM: number): RibbonEdge[] {
  const { path, alpha, confidence } = line;
  const out: RibbonEdge[] = [];
  if (path.length < 2) return out;
  let cur: Position3[] = [path[0]!];
  let curA: number[] = [alpha[0]!];
  let on = true;
  let boundary = onM; // distance along the line of the next on/off switch
  let along = 0;
  for (let i = 1; i < path.length; i++) {
    const a = path[i - 1]!;
    const b = path[i]!;
    const seg = Math.hypot((b[0] - a[0]) * mPerDegLon(a[1]), (b[1] - a[1]) * M_PER_DEG_LAT);
    while (boundary < along + seg) {
      const t = (boundary - along) / seg;
      const p: Position3 = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, a[2] + (b[2] - a[2]) * t];
      const pa = alpha[i - 1]! + (alpha[i]! - alpha[i - 1]!) * t;
      if (on) {
        cur.push(p);
        curA.push(pa);
        out.push({ path: cur, alpha: curA, confidence });
      }
      cur = on ? [] : [p];
      curA = on ? [] : [pa];
      on = !on;
      boundary += on ? onM : offM;
    }
    if (on) {
      cur.push(b);
      curA.push(alpha[i]!);
    }
    along += seg;
  }
  if (on && cur.length >= 2) out.push({ path: cur, alpha: curA, confidence });
  return out;
}

/**
 * The tunnel curtain under a below-ground run: per segment, TUNNELS.curtainBands stacked vertical quads from the
 * centreline (bottom band, fade 1) up to z = 0 (top band, faintest). Segments shallower than curtainMinDepthM at both
 * ends are skipped (nothing to show, and no zero-area quads).
 */
export function curtainQuads(run: Position3[]): { polygon: Position3[]; fade: number }[] {
  const bands = TUNNELS.curtainBands;
  const out: { polygon: Position3[]; fade: number }[] = [];
  const at = (p: Position3, f: number): Position3 => [p[0], p[1], Math.min(0, p[2]) * (1 - f)];
  for (let i = 1; i < run.length; i++) {
    const [a, b] = [run[i - 1]!, run[i]!];
    if (-a[2] < TUNNELS.curtainMinDepthM && -b[2] < TUNNELS.curtainMinDepthM) continue;
    bands.forEach((fade, k) => {
      const [f0, f1] = [k / bands.length, (k + 1) / bands.length];
      out.push({ polygon: [at(a, f0), at(b, f0), at(b, f1), at(a, f1)], fade });
    });
  }
  return out;
}

/** Ribbons, their edge lines and drop-lines from roads_3d features (precomputed once at load). */
export function toRoadGeometry(features: RoadFeature[]): RoadGeometry {
  const ribbons: Ribbon[] = [];
  const edges: RibbonEdge[] = [];
  const fascia: RibbonEdge[] = [];
  const drops: DropLine[] = [];
  const tunnel: TunnelGeometry = { ribbons: [], edges: [], dashes: [], portals: [], trace: [], curtain: [] };
  for (const f of features) {
    const { way_id, highway, name, z_source } = f.properties;
    const confidence = heightConfidence(z_source);
    const width = ROAD_WIDTH_M[highway] ?? DEFAULT_ROAD_WIDTH_M;
    const below = belowGroundRuns(f.geometry.coordinates);
    if (f.properties.tunnel) {
      // X-ray: deck, dashed edges and portal frames; only the underground part (a tunnel way at grade is not drawn).
      for (const run of below) {
        const alpha = run.map((p) => depthAlpha(p[2]));
        tunnel.ribbons.push({ wayId: way_id, confidence, name, highway, width, path: run, alpha });
        const [left, right] = [1, -1].map((side) => offsetPath(run, (side * width) / 2)) as [Position3[], Position3[]];
        for (const edge of [left, right]) {
          const line = { path: edge, alpha, confidence };
          tunnel.edges.push(line);
          tunnel.dashes.push(...dashLine(line, TUNNELS.dashM.on, TUNNELS.dashM.off));
        }
        tunnel.curtain.push(...curtainQuads(run));
        const surface = { path: run.map((p) => [p[0], p[1], 0] as Position3), alpha: run.map((p) => depthAlpha(p[2])), confidence };
        tunnel.trace.push(...dashLine(surface, TUNNELS.traceDashM.on, TUNNELS.traceDashM.off));
        // A portal at each run end that is at street level (not where the way is clipped underground).
        for (const k of [0, run.length - 1]) {
          if (run[k]![2] < -RIBBONS.minZ) continue;
          const [l, r] = [left[k]!, right[k]!];
          const h = TUNNELS.portalHeightM;
          tunnel.portals.push({ path: [[l[0], l[1], 0], [l[0], l[1], h], [r[0], r[1], h], [r[0], r[1], 0]] });
        }
      }
      continue;
    }
    // Open to the sky below street level (cutting, underpass approach): a normal solid ribbon, rim at ground level.
    for (const run of below) {
      const alpha = run.map((p) => depthAlpha(p[2]));
      ribbons.push({ wayId: way_id, confidence, name, highway, width, path: run, alpha });
      for (const side of [1, -1]) {
        const edge = offsetPath(run, (side * width) / 2);
        edges.push({ path: edge, alpha, confidence });
        fascia.push({
          path: edge.map((p) => [p[0], p[1], 0] as Position3),
          alpha: run.map((p) => depthAlpha(p[2] + RIBBONS.fasciaDepthM)),
          confidence,
        });
      }
    }
    for (const run of elevatedRuns(f.geometry.coordinates)) {
      const alpha = run.map((p) => ribbonAlpha(p[2]));
      ribbons.push({ wayId: way_id, confidence, name, highway, width, path: run, alpha });
      for (const side of [1, -1]) {
        const edge = offsetPath(run, (side * width) / 2);
        edges.push({ path: edge, alpha, confidence });
        // The fascia line never goes below ground; where the deck is lower than fasciaDepthM it fades out.
        fascia.push({
          path: edge.map((p) => [p[0], p[1], Math.max(0, p[2] - RIBBONS.fasciaDepthM)] as Position3),
          alpha: run.map((p) => ribbonAlpha(p[2] - RIBBONS.fasciaDepthM)),
          confidence,
        });
      }
      // Paired drop-lines under both deck edges on bridges, like piers.
      if (f.properties.bridge) for (const side of [1, -1]) drops.push(...dropLines(offsetPath(run, (side * width) / 2)));
    }
  }
  return { ribbons, edges, fascia, drops, tunnel };
}

const scaleAlpha = ([r, g, b, a]: readonly number[], alpha: number[]): RGBA[] =>
  alpha.map((t) => [r!, g!, b!, Math.round(a! * t)]);

/**
 * Elevated-road ribbons in the buildings' ghost language, drawn after the buildings:
 * 1. Deck: PathLayer with real z, widths in metres and `billboard: false`, so each road is a flat ribbon lying in
 *    its own sloping deck plane (not a screen-facing line). Translucent; depth TEST on (buildings in front hide it),
 *    depth WRITE off (stacked decks all show through each other, and the alpha fade at ramp feet can't punch holes).
 * 2. Hidden edges: edge lines behind buildings, faint (the x-ray hint, as for rooflines).
 * 3. Visible edges: thin, bright lines along both deck edges.
 * 4. Fascia: a fainter line 1.6 m under each edge, so decks read as slabs with a thickness, not paper.
 * 5. Drop-lines: faint paired verticals from bridge deck edges to the ground every 35 m, to read height.
 *
 * `view: "source"` (dev only, `?heights=source`) tints by z_source: measured heights in the normal colours, estimated
 * heights dimmer and greyer with dashed edge and fascia lines (ribbonColors). "normal" draws everything the normal way.
 */
export function roadsLayers(geometry: RoadGeometry, view: HeightsView = "normal"): Layer[] {
  const { ribbons, drops } = geometry;
  const bySource = view === "source";
  const colors = (c: HeightConfidence) => ribbonColors(bySource ? c : "measured");
  const { on, off } = palette.roadEstimated.dashM;
  const dashIfNeeded = (lines: RibbonEdge[]) =>
    bySource ? lines.flatMap((l) => (colors(l.confidence).dashed ? dashLine(l, on, off) : [l])) : lines;
  const edges = dashIfNeeded(geometry.edges);
  const fascia = dashIfNeeded(geometry.fascia);
  return [
    new PathLayer<Ribbon>({
      id: "roads-deck",
      data: ribbons,
      getPath: (d) => d.path,
      getWidth: (d) => d.width,
      widthUnits: "meters",
      billboard: false,
      jointRounded: false,
      capRounded: false,
      miterLimit: 2,
      getColor: (d) => scaleAlpha(colors(d.confidence).deck, d.alpha),
      parameters: { depthWriteEnabled: false, depthCompare: "less-equal" },
    }),
    new PathLayer<RibbonEdge>({
      id: "roads-edge-hidden",
      data: edges,
      getPath: (d) => d.path,
      getWidth: 1,
      widthUnits: "pixels",
      getColor: (d) => scaleAlpha(colors(d.confidence).edgeHidden, d.alpha),
      parameters: { depthWriteEnabled: false, depthCompare: "greater" },
    }),
    new PathLayer<RibbonEdge>({
      id: "roads-edge",
      data: edges,
      getPath: (d) => d.path,
      getWidth: 1,
      widthUnits: "pixels",
      getColor: (d) => scaleAlpha(colors(d.confidence).edge, d.alpha),
      parameters: { depthWriteEnabled: false, depthCompare: "less-equal" },
    }),
    new PathLayer<RibbonEdge>({
      id: "roads-fascia",
      data: fascia,
      getPath: (d) => d.path,
      getWidth: 1,
      widthUnits: "pixels",
      getColor: (d) => scaleAlpha(colors(d.confidence).fascia, d.alpha),
      parameters: { depthWriteEnabled: false, depthCompare: "less-equal" },
    }),
    new PathLayer<DropLine>({
      id: "roads-drop",
      data: drops,
      getPath: (d) => d.path,
      billboard: true, // vertical lines need screen-facing width (see buildings-tower-edges)
      getWidth: 1,
      widthUnits: "pixels",
      getColor: palette.roadDropLine as unknown as RGBA,
      parameters: { depthWriteEnabled: false, depthCompare: "less-equal" },
    }),
  ];
}

/** Depth rules for X-ray tunnels: drawn whatever is in front (the ground, buildings, decks), never writing depth. */
export const XRAY_PARAMETERS = { depthWriteEnabled: false, depthCompare: "always" } as const;

/**
 * X-ray tunnel ribbons, drawn BEFORE the buildings, so the translucent city tints over them as over anything behind
 * it. Same family as the elevated ribbons (a faint deck and thin edge lines), but dimmer and cooler, with dashed
 * edges (the hidden-line convention) and no verticals along them, so they never read as bridges; the same dashed
 * edges again `ceilingM` higher outline the tube (palette.roadTunnel); a curtain
 * hangs from the surface down to each tunnel (fading towards the surface) to show its depth, and a faint portal frame
 * marks where each tunnel meets street level. Depth test "always": tunnels show through the ground, the water and the
 * buildings in front. `visible` is the "Show tunnels" toggle (which also hides the tunnel cars);
 * toggling only flips `visible`, so nothing is rebuilt.
 */
export function tunnelLayers(geometry: TunnelGeometry, visible: boolean): Layer[] {
  const c = palette.roadTunnel;
  return [
    new SolidPolygonLayer<{ polygon: Position3[]; fade: number }>({
      id: "tunnels-curtain",
      data: geometry.curtain,
      visible,
      _full3d: true, // vertical polygons
      extruded: false,
      getPolygon: (d) => d.polygon,
      getFillColor: (d) => [c.curtain[0], c.curtain[1], c.curtain[2], Math.round(c.curtain[3] * d.fade)],
      parameters: { ...XRAY_PARAMETERS, cullMode: "none" },
    }),
    new PathLayer<RibbonEdge>({
      id: "tunnels-trace",
      data: geometry.trace,
      visible,
      getPath: (d) => d.path,
      getWidth: 1,
      widthUnits: "pixels",
      getColor: (d) => scaleAlpha(c.trace, d.alpha),
      parameters: XRAY_PARAMETERS,
    }),
    new PathLayer<Ribbon>({
      id: "tunnels-deck",
      data: geometry.ribbons,
      visible,
      getPath: (d) => d.path,
      getWidth: (d) => d.width,
      widthUnits: "meters",
      billboard: false,
      jointRounded: false,
      capRounded: false,
      miterLimit: 2,
      getColor: (d) => scaleAlpha(c.deck, d.alpha),
      parameters: XRAY_PARAMETERS,
    }),
    new PathLayer<{ path: Position3[] }>({
      id: "tunnels-portal",
      data: geometry.portals,
      visible,
      getPath: (d) => d.path,
      billboard: true, // vertical posts need screen-facing width (see buildings-tower-edges)
      getWidth: 1,
      widthUnits: "pixels",
      getColor: c.portal as unknown as RGBA,
      parameters: XRAY_PARAMETERS,
    }),
    new PathLayer<RibbonEdge>({
      id: "tunnels-edge",
      data: geometry.dashes,
      visible,
      getPath: (d) => d.path,
      getWidth: 1,
      widthUnits: "pixels",
      getColor: (d) => scaleAlpha(c.edge, d.alpha),
      parameters: XRAY_PARAMETERS,
    }),
    // The tunnel ceiling: the same dashed edges `ceilingM` higher (never above ground), fainter; a tube outline.
    new PathLayer<RibbonEdge>({
      id: "tunnels-ceiling",
      data: geometry.dashes,
      visible,
      getPath: (d) => d.path.map((p) => [p[0], p[1], Math.min(0, p[2] + c.ceilingM)] as Position3),
      getWidth: 1,
      widthUnits: "pixels",
      getColor: (d) => scaleAlpha(c.edge, d.alpha.map((a) => a * c.ceilingAlpha)),
      parameters: XRAY_PARAMETERS,
    }),
  ];
}
