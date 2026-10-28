import { describe, expect, it } from "vitest";
import type { Trip } from "../data/tripsContract";
import { activeCars, CAR_LIFT_M, carsInTunnels, collapseStops, NUDGE_M, prepareTrips, splitRuns } from "./prepare";

const M_PER_DEG_LAT = 110_574;
const P = (lon: number, lat: number, z = 0): [number, number, number] => [lon, lat, z];

describe("collapseStops", () => {
  it("collapses a stop (repeated positions) to arrival + departure, nudged a few cm along the road", () => {
    const path = [P(144.96, -37.818), P(144.96, -37.8179), P(144.96, -37.8179), P(144.96, -37.8179), P(144.96, -37.8178)];
    const c = collapseStops(path, [0, 1, 1.5, 30, 31]);
    expect(c.timestamps).toEqual([0, 1, 30, 31]);
    expect(c.path).toHaveLength(4);
    const north = (c.path[2]![1] - c.path[1]![1]) * M_PER_DEG_LAT;
    expect(north).toBeCloseTo(NUDGE_M, 4); // toward the next point (north)
    expect(c.path[2]![0]).toBeCloseTo(144.96, 9);
  });

  it("nudges onward at the end of a trip and east for a car that never moves", () => {
    const end = collapseStops([P(144.96, -37.818), P(144.96, -37.8179), P(144.96, -37.8179)], [0, 1, 9]);
    expect(end.path).toHaveLength(3);
    expect(end.path[2]![1]).toBeGreaterThan(end.path[1]![1]); // still heading north
    const parked = collapseStops([P(144.96, -37.818), P(144.96, -37.818)], [0, 60]);
    expect(parked.path).toHaveLength(2);
    expect(parked.path[1]![0]).toBeGreaterThan(parked.path[0]![0]);
    expect(parked.timestamps).toEqual([0, 60]);
  });

  it("leaves a moving car alone", () => {
    const path = [P(144.96, -37.818), P(144.961, -37.818), P(144.962, -37.818)];
    expect(collapseStops(path, [0, 5, 10])).toEqual({ path, timestamps: [0, 5, 10] });
  });
});

describe("splitRuns", () => {
  it("splits a trip into surface and tunnel runs that share the portal vertices", () => {
    const path = [P(144.96, -37.818, 0), P(144.961, -37.818, -0.2), P(144.962, -37.818, -6), P(144.963, -37.818, -6), P(144.964, -37.818, 0), P(144.965, -37.818, 0)];
    expect(splitRuns(path).map((r) => [r.tunnel, r.i0, r.i1])).toEqual([
      [false, 0, 1],
      [true, 1, 4],
      [false, 4, 5],
    ]);
  });

  it("keeps below-ground segments on the surface where the nearest below-ground road is not a tunnel (open cutting)", () => {
    const path = [P(144.96, -37.818, 0), P(144.961, -37.818, -6), P(144.962, -37.818, -6), P(144.963, -37.818, -6), P(144.964, -37.818, 0)];
    // The cutting is west of 144.9615, the tunnel east of it.
    const lookup = (lon: number) => lon > 144.9615;
    expect(splitRuns(path, -0.5, lookup).map((r) => [r.tunnel, r.i0, r.i1])).toEqual([
      [false, 0, 2],
      [true, 2, 4],
    ]);
    // No below-ground road near (null): depth alone decides.
    expect(splitRuns(path, -0.5, () => null).map((r) => r.tunnel)).toEqual([true]);
    // Never a tunnel above TUNNEL_Z_M, whatever the road says.
    expect(splitRuns([P(144.96, -37.818, 0), P(144.961, -37.818, 0)], -0.5, () => true).map((r) => r.tunnel)).toEqual([false]);
  });

  it("keeps a bridge-only trip as one surface run", () => {
    const runs = splitRuns([P(144.96, -37.818, 0), P(144.961, -37.818, 12), P(144.962, -37.818, 30)]);
    expect(runs).toEqual([{ tunnel: false, i0: 0, i1: 2 }]);
  });
});

describe("prepareTrips", () => {
  const trips: Trip[] = [
    { id: "a", type: "passenger", path: [P(144.96, -37.818), P(144.961, -37.818), P(144.962, -37.818)], timestamps: [0, 5, 10] },
    {
      id: "b",
      type: "passenger",
      path: [P(144.96, -37.82, 0), P(144.961, -37.82, 0), P(144.962, -37.82, -8), P(144.963, -37.82, 0), P(144.964, -37.82, 0)],
      timestamps: [20, 25, 30, 35, 40],
    },
    { id: "c", type: "passenger", path: [P(144.96, -37.81)], timestamps: [3] }, // too short: skipped
  ];
  const p = prepareTrips(trips);

  it("draws a car in an open cutting as a surface car, not a tunnel car", () => {
    const cut = prepareTrips(trips, { tunnelAt: () => false });
    expect(cut.tunnelVehicles).toBe(0);
    expect(cut.tunnel).toEqual([]);
    expect(prepareTrips(trips, { tunnelAt: () => true }).tunnelVehicles).toBe(1);
  });

  it("packs binary pieces per bucket with one extra segment each side and a clip window", () => {
    expect(p.vehicles).toBe(2);
    expect(p.tunnelVehicles).toBe(1);
    expect([p.surface.length, p.tunnel.length]).toEqual([1, 1]); // everything happens before 60 s: one bucket each
    const s = p.surface[0]!;
    expect([s.index, s.startS, s.endS]).toEqual([0, 0, 60]);
    // Surface pieces: trip a (3 vertices, nothing to borrow); b's first stub (+1 tunnel segment); b's last stub
    // (+1 tunnel segment before it). Tunnel piece: b's middle run plus one surface segment each side.
    expect(s.length).toBe(3);
    expect(Array.from(s.startIndices)).toEqual([0, 3, 6]);
    expect(s.positions.length).toBe(9 * 3);
    expect(s.positions[2]).toBeCloseTo(CAR_LIFT_M);
    const clips = (g: typeof s) => Array.from(g.startIndices).map((v) => [g.clip[v * 2], g.clip[v * 2 + 1]]);
    expect(clips(s)).toEqual([
      [0, 60], // trip a: its own bucket window (its real ends stay open inside it)
      [0, 25], // b before the portal at 25 s
      [35, 60], // b after the portal at 35 s
    ]);
    const u = p.tunnel[0]!;
    expect(Array.from(u.timestamps)).toEqual([20, 25, 30, 35, 40]);
    expect(clips(u)).toEqual([[25, 35]]);
    expect(p.vertices).toBe(14);
  });

  it("makes one head instance per segment, in the bucket of its start time, never twice", () => {
    expect(p.surface[0]!.heads.length).toBe(2 + 1 + 1);
    expect(Array.from(p.surface[0]!.heads.times)).toEqual([0, 5, 5, 10, 20, 25, 35, 40]);
    expect(Array.from(p.surface[0]!.heads.from.slice(0, 3))).toEqual([144.96, -37.818, CAR_LIFT_M]);
    expect(Array.from(p.surface[0]!.heads.to.slice(0, 3))).toEqual([144.961, -37.818, CAR_LIFT_M]);
    expect(Array.from(p.tunnel[0]!.heads.times)).toEqual([25, 30, 30, 35]);
  });

  it("counts cars in tunnels at any time", () => {
    expect([carsInTunnels(p, 24), carsInTunnels(p, 26), carsInTunnels(p, 35)]).toEqual([0, 1, 0]);
  });

  it("counts cars on the road at any time", () => {
    expect([p.firstS, p.lastS]).toEqual([0, 40]);
    expect(activeCars(p, -1)).toBe(0);
    expect(activeCars(p, 0)).toBe(1);
    expect(activeCars(p, 12)).toBe(0);
    expect(activeCars(p, 22)).toBe(1);
    expect(activeCars(p, 40)).toBe(0);
  });

  it("handles no trips", () => {
    const e = prepareTrips([]);
    expect([e.vehicles, e.surface.length, e.tunnel.length, e.firstS, e.lastS]).toEqual([0, 0, 0, 0, 0]);
  });
});
