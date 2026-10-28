import { describe, expect, it } from "vitest";
import { buildGraph, mulberry32, randomWalk, roundStrict, shortestPath, timeRoute } from "./road-graph.mjs";

// A hand-written 3-way network (test-only): A -> B one-way (way 1), B <-> C two-way (way 2), A <-> C two-way service (way 3).
type F = Parameters<typeof buildGraph>[0][number];
const way = (id: number, highway: string, coords: [number, number, number][]): F => ({
  properties: { way_id: id, highway, name: `W${id}`, tunnel: false },
  geometry: { coordinates: coords },
});
const A: [number, number, number] = [144.96, -37.818, 0];
const B: [number, number, number] = [144.961, -37.818, 0];
const C: [number, number, number] = [144.961, -37.819, 0];
const g = buildGraph([way(1, "primary", [A, B]), way(2, "primary", [B, C]), way(3, "service", [A, C])], new Map([[1, 1 as const]]), new Set([`${C[0]},${C[1]}`]));

describe("road graph (test data generator)", () => {
  it("joins ways at shared vertices and honours one-way streets", () => {
    expect(g.coords).toHaveLength(3);
    expect(g.out[0]!.map((e) => e.to)).toEqual([1, 2]); // A -> B (one-way), A -> C
    expect(g.out[1]!.map((e) => e.to)).toEqual([2]); // no B -> A against the one-way
    expect(g.signals.has(2)).toBe(true);
  });

  it("routes the fast way and never against a one-way", () => {
    expect(shortestPath(g, 0, 2)!.vertices).toEqual([0, 1, 2]); // via primaries, not the service road
    expect(shortestPath(g, 1, 0)!.vertices).toEqual([1, 2, 0]); // around, not back down way 1
  });

  it("times a route at class speeds with stops, and walks reproducibly from a seed", () => {
    const route = shortestPath(g, 0, 2)!;
    const t = timeRoute(g, route, { startS: 10, stops: new Map([[1, 5]]) });
    expect(t.path).toHaveLength(4); // the stop repeats B
    expect(t.timestamps[0]).toBe(10);
    expect(t.timestamps[2]! - t.timestamps[1]!).toBe(5);
    expect(randomWalk(g, 0, 500, mulberry32(1)).vertices).toEqual(randomWalk(g, 0, 500, mulberry32(1)).vertices);
  });

  it("rounds timestamps but keeps them strictly increasing (the trips contract)", () => {
    expect(roundStrict([0, 0.004, 0.012, 5.555, 5.556])).toEqual([0, 0.01, 0.02, 5.56, 5.57]);
  });
});
