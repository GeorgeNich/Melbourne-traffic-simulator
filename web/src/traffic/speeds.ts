// Road speeds: edge_stats (per SUMO edge, per 60 s bin) joined to edge_geometry, ready to draw.
//
// - The table holds speed_ratio and vehicle_count per [bin, edge]; a missing row is NaN / 0 = no data.
// - binAt(t): the 60 s bin holding playback time t (t_bin <= t < t_bin + 60), clamped to the scenario's bins.
// - Smoothing (palette SPEED.smoothing): per-minute bins are noisy (half the baseline rows come from ONE vehicle, and 49
//   are a single car stopped with its tail across the junction, speed ~0). The colour is the vehicle-weighted mean
//   ratio of the current minute (weight 1) and the previous minute (weight 0.5), so one stopped tail can't outvote
//   several cars that flowed through a moment earlier, while a queue that persists across minutes stays red. Trailing,
//   never centred: the colour never anticipates the future. Only where the CURRENT bin has data: no row = no colour.
// - Evidence (palette SPEED.evidence): the colour's strength rises with the vehicles behind it, so a lone car is a
//   fainter mark than a real queue.
// - Pieces: each edge's path split into surface and tunnel runs with EXACTLY the cars' rule (CLAUDE.md, prepare.ts): a
//   segment is X-ray (tunnel) when its mid-height is below TUNNEL_Z_M and the NEAREST BELOW-GROUND roads_3d way at its
//   midpoint (tunnelWays.ts tunnelAt) is tagged tunnel, so a car and the road under it always agree on X-ray and Show
//   tunnels. An edge can span several OSM ways (portals, underpasses), so its single way_id is only the fallback when
//   no below-ground road is in reach; then depth alone decides.
import type { EdgeGeometry, EdgePosition } from "../data/edgeGeometryContract";
import { EDGE_STATS_BIN_S, type EdgeStatsColumns } from "../data/edgeStatsContract";
import { TUNNEL_Z_M } from "../data/tripsContract";
import { palette, SPEED, speedClass, speedEvidence } from "../theme/palette";
import type { TunnelLookup } from "./tunnelWays";

export interface SpeedTable {
  edgeIds: readonly string[];
  nEdges: number;
  nBins: number;
  binS: number;
  /** speed_ratio at [bin * nEdges + edge]; NaN = no data. Float64: Float32 would turn 0.7 into 0.6999999 (amber). */
  ratio: Float64Array;
  /** vehicle_count at [bin * nEdges + edge]; 0 = no data. */
  count: Uint16Array;
}

/**
 * Build the table for the edges in `edgeIds` order (edge_geometry). `durationS` (meta) sets the number of bins, so
 * minutes past the last row are "no data", not the last colour held. Rows for unknown edges are skipped (counted).
 */
export function buildSpeedTable(
  edgeIds: readonly string[],
  stats: EdgeStatsColumns,
  durationS?: number,
): { table: SpeedTable; skippedRows: number } {
  const index = new Map(edgeIds.map((id, i) => [id, i]));
  let maxBin = 0;
  for (let i = 0; i < stats.rows; i++) if (Number.isFinite(stats.tBin[i])) maxBin = Math.max(maxBin, Math.floor(stats.tBin[i]! / EDGE_STATS_BIN_S));
  const nBins = Math.max(maxBin + 1, durationS && durationS > 0 ? Math.ceil(durationS / EDGE_STATS_BIN_S) : 1);
  const nEdges = edgeIds.length;
  const ratio = new Float64Array(nBins * nEdges).fill(NaN);
  const count = new Uint16Array(nBins * nEdges);
  let skippedRows = 0;
  for (let i = 0; i < stats.rows; i++) {
    const e = index.get(stats.edgeId[i]!);
    const r = stats.speedRatio[i]!;
    const n = stats.vehicleCount[i]!;
    const b = stats.tBin[i]! / EDGE_STATS_BIN_S;
    if (e === undefined || !Number.isInteger(b) || b < 0 || !Number.isFinite(r) || !(n >= 1)) {
      skippedRows++;
      continue;
    }
    ratio[b * nEdges + e] = r;
    count[b * nEdges + e] = Math.min(n, 65_535);
  }
  return { table: { edgeIds, nEdges, nBins, binS: EDGE_STATS_BIN_S, ratio, count }, skippedRows };
}

/** The bin holding time t: t_bin <= t < t_bin + binS, clamped to [0, nBins - 1]. */
export function binAt(t: number, nBins: number, binS: number = EDGE_STATS_BIN_S): number {
  if (!(nBins > 0)) return 0;
  const b = Math.floor((Number.isFinite(t) ? t : 0) / binS);
  return Math.min(nBins - 1, Math.max(0, b));
}

export interface EdgeSpeed {
  /** Smoothed speed_ratio. */
  ratio: number;
  /** Vehicles behind it: the current bin's count plus the weighted previous bin's. */
  vehicles: number;
}

/** The smoothed speed of an edge in a bin (header comment), or null with no data in that bin. */
export function edgeSpeed(t: SpeedTable, edge: number, bin: number): EdgeSpeed | null {
  const k = bin * t.nEdges + edge;
  const n0 = t.count[k]!;
  if (n0 === 0) return null;
  let w = n0;
  let sum = n0 * t.ratio[k]!;
  if (bin > 0) {
    const kp = k - t.nEdges;
    const np = t.count[kp]! * SPEED.smoothing.previousWeight;
    if (np > 0) {
      w += np;
      sum += np * t.ratio[kp]!;
    }
  }
  return { ratio: sum / w, vehicles: w };
}

/**
 * RGBA per edge (4 bytes each) for one bin: the traffic colour of the smoothed ratio, alpha = evidence x the class
 * strength (0–255); no data = all zero (nothing drawn).
 */
export function binColors(t: SpeedTable, bin: number): Uint8Array {
  const out = new Uint8Array(t.nEdges * 4);
  for (let e = 0; e < t.nEdges; e++) {
    const s = edgeSpeed(t, e, bin);
    const cls = s ? speedClass(s.ratio) : null;
    if (!s || !cls) continue;
    const [r, g, b] = palette.traffic[cls];
    out.set([r, g, b, Math.round(255 * speedEvidence(s.vehicles) * SPEED.classStrength[cls])], e * 4);
  }
  return out;
}

/** Counts per class in a bin (for the e2e checks and the dev console). */
export function binSummary(t: SpeedTable, bin: number): { flowing: number; slowing: number; jammed: number; noData: number } {
  const s = { flowing: 0, slowing: 0, jammed: 0, noData: 0 };
  for (let e = 0; e < t.nEdges; e++) {
    const v = edgeSpeed(t, e, bin);
    const cls = v ? speedClass(v.ratio) : null;
    if (cls) s[cls]++;
    else s.noData++;
  }
  return s;
}

/** A drawable run of one edge: surface or X-ray tunnel. `edge` indexes the SpeedTable. */
export interface SpeedPiece {
  edge: number;
  tunnel: boolean;
  path: EdgePosition[];
}

/** The tunnel flag of an OSM way in roads_3d (undefined if the way isn't there). */
export type WayTunnel = (wayId: number) => boolean | undefined;

/** Split every edge into surface and tunnel runs (header comment), lifted by `liftM`. Runs share their boundary vertex. */
export function speedPieces(
  edges: readonly EdgeGeometry[],
  wayTunnel: WayTunnel,
  tunnelAt: TunnelLookup | null,
  liftM: number = SPEED.liftM,
): SpeedPiece[] {
  const out: SpeedPiece[] = [];
  edges.forEach((e, edge) => {
    const p = e.path;
    const flag = wayTunnel(e.way_id);
    let cur: SpeedPiece | null = null;
    for (let k = 1; k < p.length; k++) {
      const [a, b] = [p[k - 1]!, p[k]!];
      const below = (a[2] + b[2]) / 2 < TUNNEL_Z_M;
      const tunnel = below && (tunnelAt?.((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) ?? flag ?? true);
      if (!cur || cur.tunnel !== tunnel) {
        cur = { edge, tunnel, path: [a] };
        out.push(cur);
      }
      cur.path.push(b);
    }
  });
  if (liftM) for (const piece of out) piece.path = piece.path.map(([x, y, z]) => [x, y, z + liftM]);
  return out;
}
