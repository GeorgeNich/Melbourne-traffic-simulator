import { describe, expect, it } from "vitest";
import type { Trip } from "../data/tripsContract";
import { TRAIL_SECONDS } from "../theme/palette";
import { advance, type PlaybackState } from "./playback";
import { BUCKET_S, bucketActive, collapseStops, OPEN_CLIP, type Piece, prepareTrips, runPieces, splitRuns } from "./prepare";
import { trafficLayerId, trafficRenderer } from "./trafficLayers";

type P3 = [number, number, number];
/** A straight eastbound test path (hand-written test data), one point per timestamp; z from zAt(index). */
const eastbound = (timestamps: number[], lat = -37.818, zAt: (i: number) => number = () => 0) => ({
  path: timestamps.map((_, i) => [144.95 + i * 0.0002, lat, zAt(i)] as P3),
  timestamps,
});
const trip = (id: string, timestamps: number[], lat?: number, zAt?: (i: number) => number): Trip => ({
  id,
  type: "passenger",
  ...eastbound(timestamps, lat, zAt),
});

/** All pieces of a trip (both groups), the way prepareTrips cuts them. */
function piecesOf(t: Trip): (Piece & { tunnel: boolean; bucket: number })[] {
  const c = collapseStops(t.path, t.timestamps);
  return splitRuns(c.path).flatMap((r) => [...runPieces(c.timestamps, r)].map(([bucket, p]) => ({ ...p, tunnel: r.tunnel, bucket })));
}

/**
 * The two guarantees that make the joins invisible under additive blending:
 * - exactly once: every trail time on every segment is inside the clip window of exactly ONE piece holding it;
 * - same joins: a piece that draws any of a segment also holds the segment's neighbours, so the joins on either side of
 *   a cut are computed from the same geometry as the uncut path.
 */
function checkPartition(t: Trip) {
  const c = collapseStops(t.path, t.timestamps);
  const ts = c.timestamps;
  const ps = piecesOf(t);
  for (let k = 0; k + 1 < ts.length; k++) {
    const [t0, t1] = [ts[k]!, ts[k + 1]!];
    const samples = t0 === t1 ? [t0] : [t0, t0 + (t1 - t0) * 0.25, (t0 + t1) / 2, t0 + (t1 - t0) * 0.999];
    for (const tau of samples) {
      const owners = ps.filter((p) => p.v0 <= k && k + 1 <= p.v1 && p.clip0 <= tau && tau < p.clip1);
      expect(owners.length, `${t.id} segment ${k} at ${tau}`).toBe(1);
    }
    for (const p of ps) {
      const draws = p.v0 <= k && k + 1 <= p.v1 && (t0 === t1 ? t0 >= p.clip0 && t0 < p.clip1 : t0 < p.clip1 && t1 > p.clip0);
      if (!draws) continue;
      if (k > 0) expect(p.v0, `${t.id} piece b${p.bucket} draws segment ${k}: holds the one before`).toBeLessThanOrEqual(k - 1);
      if (k + 2 < ts.length) expect(p.v1, `${t.id} piece b${p.bucket} draws segment ${k}: holds the one after`).toBeGreaterThanOrEqual(k + 2);
    }
  }
}

describe("runPieces: clipped pieces per time bucket", () => {
  it("cuts at bucket edges in time; each piece borrows one segment each side for its joins", () => {
    const ts = [0, 30, 59, 61, 90, 125];
    const ps = runPieces(ts, { i0: 0, i1: 5 });
    expect([...ps.entries()]).toEqual([
      [0, { v0: 0, v1: 4, clip0: 0, clip1: 60 }], // segments 0-2 (59 -> 61 crosses 60 s) + segment 3 borrowed
      [1, { v0: 1, v1: 5, clip0: 60, clip1: 120 }], // segments 2-4 + segment 1 borrowed
      [2, { v0: 3, v1: 5, clip0: 120, clip1: 180 }], // segment 4 (90 -> 125 crosses 120 s) + segment 3 borrowed
    ]);
  });

  it("clips at the portal between runs, and leaves a trip's real start and end open", () => {
    const ts = [0, 10, 20, 30, 40];
    expect(runPieces(ts, { i0: 0, i1: 2 }).get(0)).toEqual({ v0: 0, v1: 3, clip0: 0, clip1: 20 });
    expect(runPieces(ts, { i0: 2, i1: 4 }).get(0)).toEqual({ v0: 1, v1: 4, clip0: 20, clip1: 60 });
    // A trip that starts mid-bucket keeps the bucket edge as its lower bound; the start cap draws inside it.
    expect(runPieces([70, 80], { i0: 0, i1: 1 }).get(1)).toEqual({ v0: 0, v1: 1, clip0: 60, clip1: 120 });
    expect(OPEN_CLIP).toBeGreaterThan(1e6);
  });

  it("draws every trail time exactly once, with matching joins: many buckets, exact edges, a long stop, tunnels", () => {
    checkPartition(trip("long", Array.from({ length: 300 }, (_, i) => 5 + i * 1.3)));
    checkPartition(trip("exact-edges", [0, 30, 60, 90, 120, 150, 180]));
    checkPartition(trip("long-stop", [0, 50, 200, 210])); // one segment spanning buckets 0-3
    // In and out of a tunnel across bucket edges (z below -0.5 m for points 20-59).
    const tunnelZ = (i: number) => (i >= 20 && i < 60 ? -8 : 0);
    checkPartition(trip("tunnel", Array.from({ length: 90 }, (_, i) => 40 + i * 1.5), -37.818, tunnelZ));
  });

  it("keeps a bucket on for its whole window and every head in it, so a long stop never drops out", () => {
    const p = prepareTrips([trip("stop", [0, 50, 200, 210])]); // stopped (one slow segment) from 50 s to 200 s
    expect(p.surface.map((b) => [b.index, b.startS, b.endS])).toEqual([
      [0, 0, 200], // the 50 -> 200 s head starts here
      [1, 60, 120],
      [2, 120, 180],
      [3, 180, 240],
    ]);
    expect(bucketActive(p.surface[0]!, 190, 20)).toBe(true);
    expect(bucketActive(p.surface[0]!, 221, 20)).toBe(false);
  });
});

// A busy synthetic scene (test data): 60 cars over 15 minutes, staggered starts, some signal stops.
const scene: Trip[] = Array.from({ length: 60 }, (_, c) => {
  const ts: number[] = [];
  let t = (c * 13) % 700;
  for (let i = 0; i < 120; i++) {
    ts.push(t);
    t += i % 37 === 36 ? 25 : 1.5; // a 25 s red light every 37 points
  }
  return trip(`car${c}`, ts, -37.818 + c * 1e-4);
});

describe("visibility over time", () => {
  const prepared = prepareTrips(scene);
  const trail = TRAIL_SECONDS;
  const render = trafficRenderer(prepared);
  const visibleIds = (t: number, tunnels = true) =>
    render(t, tunnels)
      .filter((l) => l.props.visible)
      .map((l) => l.id)
      .sort();

  /** Every bucket holding trail drawn at t (inside [t - trail, t]) or the car's head must be visible. */
  function assertNothingMissing(t: number) {
    const visible = new Set(prepared.surface.filter((b) => bucketActive(b, t, trail)).map((b) => b.index));
    for (const tr of scene) {
      for (let k = 1; k < tr.timestamps.length; k++) {
        const [t0, t1] = [tr.timestamps[k - 1]!, tr.timestamps[k]!];
        if (!(t0 <= t && t1 >= t - trail)) continue;
        const lo = Math.max(t0, t - trail);
        const hi = Math.min(t1, t);
        for (let b = Math.floor(lo / BUCKET_S); b <= Math.floor(hi / BUCKET_S); b++) {
          expect(visible.has(b), `segment ${tr.id}#${k} at t=${t}: bucket ${b}`).toBe(true);
        }
        if (t < t1) expect(visible.has(Math.floor(t0 / BUCKET_S)), `head ${tr.id}#${k} at t=${t}`).toBe(true);
      }
    }
  }

  it("playback at 30x: nothing that should show is ever hidden, few buckets are drawn, and the set changes rarely", () => {
    let s: PlaybackState = { time: 0, playing: true, speed: 30, start: 0, duration: 900 };
    let maxVisible = 0;
    let changes = 0;
    let prev = "";
    for (let frame = 0; frame < 30 * 60; frame++) {
      // 30 s of real time at 60 fps = the whole 900 s scenario
      s = advance(s, 1 / 60);
      assertNothingMissing(s.time);
      const ids = visibleIds(s.time);
      maxVisible = Math.max(maxVisible, ids.length / 3);
      const key = ids.join();
      if (key !== prev) changes++;
      prev = key;
    }
    expect(prepared.surface.length).toBeGreaterThanOrEqual(14);
    // A 20 s trail spans at most 2 buckets; heads of 25 s stops can keep one more on.
    expect(maxVisible).toBeLessThanOrEqual(4);
    // Each bucket turns on once and off once (plus the loop back to 0).
    expect(changes).toBeLessThanOrEqual(2 * prepared.surface.length + 2);
    for (const tr of scene.slice(0, 10)) checkPartition(tr);
  });

  it("a scrub that jumps across buckets shows exactly the buckets at the new time", () => {
    const at30 = visibleIds(30);
    const at500 = visibleIds(500);
    expect(at30).toContain(trafficLayerId(false, "core", 0));
    expect(at30).not.toContain(trafficLayerId(false, "core", 8));
    expect(at500).toContain(trafficLayerId(false, "core", 8));
    expect(at500).not.toContain(trafficLayerId(false, "core", 0));
    for (const t of [30, 500, 899, 0]) assertNothingMissing(t);
  });

  it("between frames only currentTime changes on visible buckets; hidden buckets don't change at all", () => {
    const a = render(130, true);
    const b = render(131, true);
    expect(a.map((l) => l.id)).toEqual(b.map((l) => l.id));
    a.forEach((la, i) => {
      const pa = la.props as unknown as Record<string, unknown>;
      const pb = b[i]!.props as unknown as Record<string, unknown>;
      expect(pb.visible).toBe(pa.visible); // same bucket set at 130 s and 131 s
      const changed = Object.keys(pa).filter((k) => pa[k] !== pb[k]);
      expect(changed, la.id).toEqual(pa.visible ? ["currentTime"] : []);
    });
  });
});
