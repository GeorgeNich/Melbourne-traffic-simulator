import { describe, expect, it } from "vitest";
import type { EdgeGeometry } from "../data/edgeGeometryContract";
import type { EdgeStatsColumns } from "../data/edgeStatsContract";
import { palette, SPEED, speedEvidence } from "../theme/palette";
import { binAt, binColors, binSummary, buildSpeedTable, edgeSpeed, speedPieces } from "./speeds";

type Row = [edge: string, tBin: number, ratio: number, count: number];
/** Hand-written stats (not real data); mean and limit only matter to the validator, not here. */
function stats(rows: Row[]): EdgeStatsColumns {
  return {
    rows: rows.length,
    edgeId: rows.map((r) => r[0]),
    tBin: Float64Array.from(rows.map((r) => r[1])),
    meanSpeed: Float64Array.from(rows.map((r) => r[2] * 10)),
    speedLimit: Float64Array.from(rows.map(() => 10)),
    speedRatio: Float64Array.from(rows.map((r) => r[2])),
    vehicleCount: Float64Array.from(rows.map((r) => r[3])),
  };
}
const EDGES = ["a", "b", "c"];

describe("binAt: the 60 s bin holding t (t_bin <= t < t_bin + 60)", () => {
  it("bin edges belong to the bin that starts there", () => {
    expect(binAt(0, 15)).toBe(0);
    expect(binAt(59.999, 15)).toBe(0);
    expect(binAt(60, 15)).toBe(1);
    expect(binAt(119.5, 15)).toBe(1);
    expect(binAt(840, 15)).toBe(14);
  });
  it("clamps outside the scenario (the end of the replay is the last bin)", () => {
    expect(binAt(900, 15)).toBe(14);
    expect(binAt(-3, 15)).toBe(0);
    expect(binAt(Number.NaN, 15)).toBe(0);
    expect(binAt(10, 0)).toBe(0);
  });
});

describe("buildSpeedTable", () => {
  it("indexes by edge_geometry order and sizes the bins by the scenario duration", () => {
    const { table, skippedRows } = buildSpeedTable(EDGES, stats([["b", 120, 0.8, 2], ["zz", 0, 0.5, 1]]), 900);
    expect(table.nBins).toBe(15);
    expect(skippedRows).toBe(1); // unknown edge
    expect(table.count[2 * 3 + 1]).toBe(2);
    expect(table.ratio[2 * 3 + 1]).toBeCloseTo(0.8);
    expect(Number.isNaN(table.ratio[0]!)).toBe(true); // no row = no data
  });
});

describe("edgeSpeed: vehicle-weighted trailing smoothing", () => {
  it("no row in the current bin = no data, even if the previous bin had cars", () => {
    const { table } = buildSpeedTable(EDGES, stats([["a", 0, 0.9, 5]]), 300);
    expect(edgeSpeed(table, 0, 0)).toEqual({ ratio: expect.closeTo(0.9, 5) as number, vehicles: 5 });
    expect(edgeSpeed(table, 0, 1)).toBeNull();
  });

  it("one stopped car's tail can't outvote the cars that flowed through a minute earlier", () => {
    const { table } = buildSpeedTable(EDGES, stats([["a", 0, 0.9, 6], ["a", 60, 0.0, 1]]), 300);
    const s = edgeSpeed(table, 0, 1)!;
    // (1 * 0 + 0.5 * 6 * 0.9) / (1 + 3) = 0.675: amber, not red; and backed by 4 "vehicles".
    expect(s.ratio).toBeCloseTo(0.675);
    expect(s.vehicles).toBe(4);
  });

  it("a queue that persists stays red; the current minute dominates a light previous one", () => {
    const { table } = buildSpeedTable(EDGES, stats([["a", 0, 0.1, 6], ["a", 60, 0.2, 6], ["b", 0, 1.0, 1], ["b", 60, 0.1, 8]]), 300);
    expect(edgeSpeed(table, 0, 1)!.ratio).toBeCloseTo(0.1667, 3);
    expect(edgeSpeed(table, 1, 1)!.ratio).toBeCloseTo((0.8 + 0.5) / 8.5, 4);
  });

  it("a lone tail with nothing before it is red but faint (evidence for one car)", () => {
    const { table } = buildSpeedTable(EDGES, stats([["c", 120, 0.0, 1]]), 300);
    const colors = binColors(table, 2);
    expect([...colors.subarray(8, 12)]).toEqual([...palette.traffic.jammed, Math.round(255 * SPEED.evidence.minAlpha)]);
  });
});

describe("binColors / binSummary", () => {
  it("blue / amber / red by the smoothed ratio, alpha by evidence, zero where there's no data", () => {
    const { table } = buildSpeedTable(EDGES, stats([["a", 0, 0.7, 4], ["b", 0, 0.4, 4], ["c", 60, 0.39, 4]]), 120);
    const c0 = binColors(table, 0);
    expect([...c0.subarray(0, 4)]).toEqual([...palette.traffic.flowing, Math.round(255 * SPEED.classStrength.flowing)]);
    expect([...c0.subarray(4, 8)]).toEqual([...palette.traffic.slowing, 255]);
    expect([...c0.subarray(8, 12)]).toEqual([0, 0, 0, 0]);
    expect([...binColors(table, 1).subarray(8, 12)]).toEqual([...palette.traffic.jammed, 255]);
    expect(binSummary(table, 0)).toEqual({ flowing: 1, slowing: 1, jammed: 0, noData: 1 });
    expect(speedEvidence(4)).toBe(1);
  });
});

describe("speedPieces: X-ray by the cars' rule (nearest below-ground roads_3d way)", () => {
  const geom = (way: number, zs: number[]): EdgeGeometry => ({
    edge_id: `w${way}`,
    way_id: way,
    path: zs.map((z, i) => [144.96 + i * 0.001, -37.82, z]),
  });
  const ways: Record<number, boolean> = { 1: true, 2: false };
  const wayTunnel = (w: number) => ways[w];

  it("below-ground segments are X-ray where the nearest below-ground way is a tunnel; at grade stays surface", () => {
    const pieces = speedPieces([geom(1, [0, 0, -10, -20])], wayTunnel, () => true, 0);
    expect(pieces.map((p) => [p.tunnel, p.path.length])).toEqual([
      [false, 2],
      [true, 3],
    ]);
    // Runs share their boundary vertex.
    expect(pieces[0]!.path[1]).toEqual(pieces[1]!.path[0]);
  });

  it("the nearest below-ground way wins over the edge's own way_id (multi-way edges at portals and underpasses)", () => {
    // Edge recorded on a tunnel way, but the road under this segment is an open cutting: solid, as the cars there.
    expect(speedPieces([geom(1, [-8, -8])], wayTunnel, () => false, 0).map((p) => p.tunnel)).toEqual([false]);
    // Edge recorded on a non-tunnel way, but the segment runs in a tunnel: X-ray, as the cars there.
    expect(speedPieces([geom(2, [-8, -8])], wayTunnel, () => true, 0).map((p) => p.tunnel)).toEqual([true]);
    // Per segment: the lookup is asked at each segment's midpoint.
    const asked: number[] = [];
    const at = (lon: number) => (asked.push(lon), lon > 144.962); // only the third segment's midpoint
    expect(speedPieces([geom(2, [-8, -8, -8, -8])], wayTunnel, at, 0).map((p) => [p.tunnel, p.path.length])).toEqual([
      [false, 3],
      [true, 2],
    ]);
    expect(asked.map((x) => x.toFixed(4))).toEqual(["144.9605", "144.9615", "144.9625"]);
  });

  it("no below-ground road in reach: the edge's way flag, then depth alone; elevated edges keep their z", () => {
    expect(speedPieces([geom(2, [-8, -8])], wayTunnel, () => null, 0)[0]!.tunnel).toBe(false);
    expect(speedPieces([geom(1, [-8, -8])], wayTunnel, () => null, 0)[0]!.tunnel).toBe(true);
    expect(speedPieces([geom(9, [-8, -8])], wayTunnel, () => null, 0)[0]!.tunnel).toBe(true);
    expect(speedPieces([geom(9, [-8, -8])], wayTunnel, null, 0)[0]!.tunnel).toBe(true);
    const [deck] = speedPieces([geom(2, [12, 14])], wayTunnel, () => true, 0.25);
    expect(deck!.tunnel).toBe(false);
    expect(deck!.path.map((p) => p[2])).toEqual([12.25, 14.25]);
  });
});
