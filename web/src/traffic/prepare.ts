// Turns contract trips into flat GPU-ready buffers, once at load. Playback then only moves `currentTime`; no data
// is touched per frame.
//
// - Stops: SUMO repeats a stopped car's position every sample. A zero-length segment breaks path joins, so a run of
//   identical positions collapses to two points (arrival, departure) with the second nudged NUDGE_M along the road.
// - Tunnels: each trip is split into surface runs and tunnel runs. A segment is in a tunnel when its mid-height is
//   below TUNNEL_Z_M AND the nearest below-ground roads_3d way there is tagged tunnel (tunnelWays.ts; the trips
//   contract), so cars in the open CityLink cutting and the underpasses stay solid surface cars. With no below-ground
//   road near (or no roads loaded), depth alone decides. Tunnel runs are drawn X-ray style behind the "Show tunnels"
//   toggle; with it off, a car vanishes at one portal and reappears at the other.
// - Heads: every segment is also one "head" instance (from, to, t0, t1). The car-head shader shows a glowing dot
//   at the interpolated position while t0 <= currentTime < t1, so stopped cars stay visible after their trail fades.
//   Each head goes to the bucket of its start time, so exactly one dot per car is ever drawn.
// - Time buckets and clip windows: trails are cut into pieces per run (surface / tunnel) and per BUCKET_S-wide time
//   bucket. The glow is additive, so where two pieces meet nothing may be drawn twice. Sharing a vertex with round
//   caps double-draws a disc there (a phantom dot); butt caps would leave a wedge gap and an overlap at a bend.
//   Instead each piece keeps ONE EXTRA neighbouring segment at each end, so its joins match the full path exactly,
//   and carries a time clip window [clip0, clip1): the trail shader discards fragments whose trail time is outside it
//   (trafficLayers.ts). The windows partition time (bucket edges, and the portal times between runs), so every
//   fragment is drawn once, from identical geometry on both sides of a cut. A trip's real start and end stay
//   unclipped (open windows), so its caps draw as before.
//   Per frame only buckets overlapping [currentTime - trail, currentTime] are drawn, so GPU work no longer grows
//   with the length of the run (trafficLayers.ts).
import { type Trip, TUNNEL_Z_M } from "../data/tripsContract";
import type { TunnelLookup } from "./tunnelWays";

/** Cars float this far above the road surface so they never z-fight with ribbon decks. */
export const CAR_LIFT_M = 0.5;
/** How far a stop's departure point is nudged along the road (m): invisible, but gives the segment a direction. */
export const NUDGE_M = 0.05;
/** Width of a time bucket (simulated seconds). */
export const BUCKET_S = 60;

const M_PER_DEG_LAT = 110_574;
const mPerDegLon = (lat: number) => 111_320 * Math.cos((lat * Math.PI) / 180);

/** Open clip bounds: a trip's real start / end is never clipped. */
export const OPEN_CLIP = 1e9;

/** Binary path data for deck.gl PathLayer / TripsLayer (`_pathType: "open"`), plus per-segment head instances. */
export interface TripGroup {
  /** Number of paths (pieces). */
  length: number;
  startIndices: Uint32Array;
  /** [lon, lat, z] per vertex. */
  positions: Float64Array;
  /** Seconds from scenario start per vertex. */
  timestamps: Float32Array;
  /** [clip0, clip1) per vertex (the same for every vertex of a piece): the piece draws trail times in this window only. */
  clip: Float32Array;
  heads: {
    length: number;
    from: Float64Array;
    to: Float64Array;
    /** [t0, t1] per segment. */
    times: Float32Array;
  };
}

/**
 * One time bucket of a group: trail pieces clipped to [index * BUCKET_S, (index + 1) * BUCKET_S), and the heads of
 * segments that START in that window.
 */
export interface TripBucket extends TripGroup {
  index: number;
  /** The bucket is drawn while [startS, endS] overlaps the trail window: its time window, stretched to cover its heads. */
  startS: number;
  endS: number;
}

export interface PreparedTraffic {
  /** Time buckets, ascending by index; empty buckets are left out. */
  surface: TripBucket[];
  tunnel: TripBucket[];
  vehicles: number;
  tunnelVehicles: number;
  /** Total vertices drawn (surface + tunnel), a cost measure. */
  vertices: number;
  /** Sorted trip start and end times, for counting cars on the road at any time. */
  starts: Float64Array;
  ends: Float64Array;
  /** Sorted tunnel-run start and end times, for counting cars underground at any time. */
  tunnelStarts: Float64Array;
  tunnelEnds: Float64Array;
  firstS: number;
  lastS: number;
}

type P3 = [number, number, number];

const same = (a: readonly number[], b: readonly number[]) => a[0] === b[0] && a[1] === b[1] && a[2] === b[2];

/** Point `m` metres from a toward b (horizontal), keeping a's z. */
function toward(a: P3, b: readonly number[], m: number): P3 {
  const kx = mPerDegLon(a[1]);
  const dx = (b[0]! - a[0]) * kx;
  const dy = (b[1]! - a[1]) * M_PER_DEG_LAT;
  const l = Math.hypot(dx, dy);
  if (l < 1e-9) return [a[0] + m / kx, a[1], a[2]];
  return [a[0] + ((dx / l) * m) / kx, a[1] + ((dy / l) * m) / M_PER_DEG_LAT, a[2]];
}

/**
 * Collapse runs of identical positions to (arrival, departure); the departure is nudged NUDGE_M toward the next
 * distinct point (or onward from the previous one at the end of a trip), so no segment has zero length.
 */
export function collapseStops(path: readonly (readonly number[])[], timestamps: readonly number[]): { path: P3[]; timestamps: number[] } {
  const outP: P3[] = [];
  const outT: number[] = [];
  let i = 0;
  while (i < path.length) {
    let j = i;
    while (j + 1 < path.length && same(path[j + 1]!, path[i]!)) j++;
    const p = path[i]! as P3;
    outP.push([p[0], p[1], p[2]]);
    outT.push(timestamps[i]!);
    if (j > i) {
      const next = path[j + 1];
      const prev = outP.length >= 2 ? outP[outP.length - 2]! : null;
      let q: P3;
      if (next) q = toward(p, next, NUDGE_M);
      else if (prev) {
        // Continue onward, away from the previous point.
        const back = toward(p, prev, NUDGE_M);
        q = [2 * p[0] - back[0], 2 * p[1] - back[1], p[2]];
      } else q = toward(p, p, NUDGE_M); // a car that never moves: nudge east
      outP.push(q);
      outT.push(timestamps[j]!);
    }
    i = j + 1;
  }
  return { path: outP, timestamps: outT };
}

export interface Run {
  tunnel: boolean;
  /** First and last vertex of the run in the trip's (collapsed) path. */
  i0: number;
  i1: number;
}

/**
 * Split a path into surface and tunnel runs; consecutive runs share their boundary vertex. A segment is in a tunnel
 * when its mid-height is below `tunnelZ` and `tunnelAt` (the nearest below-ground road's tunnel flag at its midpoint)
 * does not say otherwise; without `tunnelAt`, or with no below-ground road near, depth alone decides.
 */
export function splitRuns(path: P3[], tunnelZ: number = TUNNEL_Z_M, tunnelAt?: TunnelLookup | null): Run[] {
  const runs: Run[] = [];
  for (let k = 1; k < path.length; k++) {
    const a = path[k - 1]!;
    const b = path[k]!;
    const tunnel = (a[2] + b[2]) / 2 < tunnelZ && (tunnelAt?.((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) ?? true);
    const cur = runs.at(-1);
    if (cur && cur.tunnel === tunnel) cur.i1 = k;
    else runs.push({ tunnel, i0: k - 1, i1: k });
  }
  return runs;
}

/** A trail piece: vertices [v0, v1] of the trip path (incl. one extra segment each side), drawn only in [clip0, clip1). */
export interface Piece {
  v0: number;
  v1: number;
  clip0: number;
  clip1: number;
}

/** True if segment [t0, t1] has trail time inside [lo, hi). */
const overlaps = (t0: number, t1: number, lo: number, hi: number) => (t0 === t1 ? t0 >= lo && t0 < hi : t0 < hi && t1 > lo);

/**
 * Cut one run of a trip (segments i0..i1-1 of `timestamps`) into clipped pieces, one per time bucket the run touches.
 * Each piece holds the segments overlapping its bucket plus one neighbouring segment of the trip at each end (for
 * exact joins), and the clip window = bucket window ∩ run window; a run window is open at the trip's real ends.
 */
export function runPieces(timestamps: readonly number[], run: Pick<Run, "i0" | "i1">, bucketS: number = BUCKET_S): Map<number, Piece> {
  const n = timestamps.length;
  const runLo = run.i0 === 0 ? -OPEN_CLIP : timestamps[run.i0]!;
  const runHi = run.i1 === n - 1 ? OPEN_CLIP : timestamps[run.i1]!;
  const out = new Map<number, Piece>();
  const b0 = Math.max(0, Math.floor(timestamps[run.i0]! / bucketS));
  const b1 = Math.max(0, Math.floor(timestamps[run.i1]! / bucketS));
  for (let b = b0; b <= b1; b++) {
    const lo = Math.max(b * bucketS, runLo);
    const hi = Math.min((b + 1) * bucketS, runHi);
    if (!(lo < hi)) continue;
    let first = -1;
    let last = -1;
    for (let k = run.i0; k < run.i1; k++) {
      if (overlaps(timestamps[k]!, timestamps[k + 1]!, lo, hi)) {
        if (first < 0) first = k;
        last = k;
      }
    }
    if (first < 0) continue;
    out.set(b, { v0: Math.max(0, first - 1), v1: Math.min(n - 1, last + 2), clip0: lo, clip1: hi });
  }
  return out;
}

interface BucketBuild {
  pieces: { path: P3[]; timestamps: number[]; piece: Piece }[];
  heads: { a: P3; b: P3; t0: number; t1: number }[];
}

function packBucket(index: number, bucketS: number, build: BucketBuild, liftM: number): TripBucket {
  const { pieces, heads } = build;
  const vertexCount = pieces.reduce((s, p) => s + p.piece.v1 - p.piece.v0 + 1, 0);
  const startIndices = new Uint32Array(pieces.length);
  const positions = new Float64Array(vertexCount * 3);
  const timestamps = new Float32Array(vertexCount);
  const clip = new Float32Array(vertexCount * 2);
  let v = 0;
  pieces.forEach(({ path, timestamps: ts, piece }, i) => {
    startIndices[i] = v;
    for (let k = piece.v0; k <= piece.v1; k++) {
      const p = path[k]!;
      positions[v * 3] = p[0];
      positions[v * 3 + 1] = p[1];
      positions[v * 3 + 2] = p[2] + liftM;
      timestamps[v] = ts[k]!;
      clip[v * 2] = piece.clip0;
      clip[v * 2 + 1] = piece.clip1;
      v++;
    }
  });
  const from = new Float64Array(heads.length * 3);
  const to = new Float64Array(heads.length * 3);
  const times = new Float32Array(heads.length * 2);
  let endS = (index + 1) * bucketS;
  heads.forEach((h, s) => {
    from.set([h.a[0], h.a[1], h.a[2] + liftM], s * 3);
    to.set([h.b[0], h.b[1], h.b[2] + liftM], s * 3);
    times[s * 2] = h.t0;
    times[s * 2 + 1] = h.t1;
    endS = Math.max(endS, h.t1);
  });
  return {
    index,
    startS: index * bucketS,
    endS,
    length: pieces.length,
    startIndices,
    positions,
    timestamps,
    clip,
    heads: { length: heads.length, from, to, times },
  };
}

/** True if a bucket has anything to draw at time t with a trail of trailS seconds (span overlaps [t - trailS, t]). */
export function bucketActive(b: Pick<TripBucket, "startS" | "endS">, t: number, trailS: number): boolean {
  return b.startS <= t && b.endS >= t - trailS;
}

/** Prepare contract trips for rendering (see the header comment). */
export function prepareTrips(
  trips: readonly Trip[],
  { liftM = CAR_LIFT_M, tunnelZ = TUNNEL_Z_M, bucketS = BUCKET_S, tunnelAt = null as TunnelLookup | null } = {},
): PreparedTraffic {
  const groups = [new Map<number, BucketBuild>(), new Map<number, BucketBuild>()]; // surface, tunnel
  const bucket = (tunnel: boolean, b: number) => {
    const g = groups[tunnel ? 1 : 0]!;
    let x = g.get(b);
    if (!x) g.set(b, (x = { pieces: [], heads: [] }));
    return x;
  };
  let tunnelVehicles = 0;
  const starts: number[] = [];
  const ends: number[] = [];
  const tunnelStarts: number[] = [];
  const tunnelEnds: number[] = [];
  for (const t of trips) {
    if (t.path.length < 2) continue;
    const c = collapseStops(t.path, t.timestamps);
    if (c.path.length < 2) continue;
    const ts = c.timestamps;
    starts.push(ts[0]!);
    ends.push(ts.at(-1)!);
    let inTunnel = false;
    for (const r of splitRuns(c.path, tunnelZ, tunnelAt)) {
      if (r.tunnel) {
        inTunnel = true;
        tunnelStarts.push(ts[r.i0]!);
        tunnelEnds.push(ts[r.i1]!);
      }
      for (const [b, piece] of runPieces(ts, r, bucketS)) bucket(r.tunnel, b).pieces.push({ path: c.path, timestamps: ts, piece });
      for (let k = r.i0; k < r.i1; k++) {
        bucket(r.tunnel, Math.max(0, Math.floor(ts[k]! / bucketS))).heads.push({ a: c.path[k]!, b: c.path[k + 1]!, t0: ts[k]!, t1: ts[k + 1]! });
      }
    }
    if (inTunnel) tunnelVehicles++;
  }
  const pack = (g: Map<number, BucketBuild>) =>
    [...g.entries()].sort((x, y) => x[0] - y[0]).map(([b, build]) => packBucket(b, bucketS, build, liftM));
  const s = pack(groups[0]!);
  const u = pack(groups[1]!);
  const vertexCount = (bs: TripBucket[]) => bs.reduce((n, b) => n + b.positions.length / 3, 0);
  const sorted = (a: number[]) => Float64Array.from(a).sort();
  const sortedStarts = sorted(starts);
  const sortedEnds = sorted(ends);
  return {
    surface: s,
    tunnel: u,
    vehicles: starts.length,
    tunnelVehicles,
    vertices: vertexCount(s) + vertexCount(u),
    starts: sortedStarts,
    ends: sortedEnds,
    tunnelStarts: sorted(tunnelStarts),
    tunnelEnds: sorted(tunnelEnds),
    firstS: sortedStarts[0] ?? 0,
    lastS: sortedEnds.at(-1) ?? 0,
  };
}

/** Number of values in a sorted array that are <= x (binary search). */
function countAtMost(sorted: Float64Array, x: number): number {
  let lo = 0;
  let hi = sorted.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sorted[mid]! <= x) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** Cars on the road at time t: started at or before t and not yet finished. */
export function activeCars(p: Pick<PreparedTraffic, "starts" | "ends">, t: number): number {
  return countAtMost(p.starts, t) - countAtMost(p.ends, t);
}

/** Cars in a tunnel at time t (a car is in at most one tunnel run at a time). */
export function carsInTunnels(p: Pick<PreparedTraffic, "tunnelStarts" | "tunnelEnds">, t: number): number {
  return countAtMost(p.tunnelStarts, t) - countAtMost(p.tunnelEnds, t);
}
