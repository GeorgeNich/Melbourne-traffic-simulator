import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { EDGE_STATS_COLUMNS, type EdgeStatsColumns, EdgeStatsSchemaError, readEdgeStats, validateEdgeStats, validateEdgeStatsSchema } from "./edgeStatsContract";

/** A hand-written table (not real data). */
function table(rows: [string, number, number, number, number, number][]): EdgeStatsColumns {
  return {
    rows: rows.length,
    edgeId: rows.map((r) => r[0]),
    tBin: Float64Array.from(rows.map((r) => r[1])),
    meanSpeed: Float64Array.from(rows.map((r) => r[2])),
    speedLimit: Float64Array.from(rows.map((r) => r[3])),
    speedRatio: Float64Array.from(rows.map((r) => r[4])),
    vehicleCount: Float64Array.from(rows.map((r) => r[5])),
  };
}
const ok: [string, number, number, number, number, number] = ["e1", 60, 6.945, 13.89, 0.5, 3];

const schema = (over: Partial<Record<string, Record<string, unknown>>> = {}) => [
  { name: "schema", num_children: 6 },
  ...EDGE_STATS_COLUMNS.map((c) => ({
    name: c.name,
    type: c.type,
    ...(c.name === "edge_id" ? { converted_type: "UTF8", logical_type: { type: "STRING" } } : {}),
    ...over[c.name],
  })),
];

describe("validateEdgeStatsSchema", () => {
  it("accepts the six contract columns in order", () => {
    expect(validateEdgeStatsSchema(schema())).toEqual([]);
  });
  it("rejects a renamed, reordered, missing or retyped column and a binary edge_id", () => {
    expect(validateEdgeStatsSchema(schema({ t_bin: { name: "bin" } }))).toHaveLength(1);
    const s = schema();
    [s[1], s[2]] = [s[2]!, s[1]!];
    expect(validateEdgeStatsSchema(s)[0]).toMatch(/in that order/);
    expect(validateEdgeStatsSchema(schema().slice(0, 6))[0]).toMatch(/expected exactly/);
    expect(validateEdgeStatsSchema(schema({ t_bin: { type: "INT64" } }))[0]).toMatch(/t_bin: type INT64/);
    expect(validateEdgeStatsSchema(schema({ speed_ratio: { type: "FLOAT" } }))[0]).toMatch(/speed_ratio/);
    expect(validateEdgeStatsSchema(schema({ edge_id: { converted_type: undefined, logical_type: undefined } }))[0]).toMatch(/UTF-8/);
  });
});

describe("validateEdgeStats", () => {
  it("accepts contract rows, including ratio > 1 (driver speed factors)", () => {
    const r = validateEdgeStats(table([ok, ["e2", 0, 16.67, 13.89, 1.2001, 1]]), { durationS: 900, edgeIds: new Set(["e1", "e2"]) });
    expect(r.errors).toEqual([]);
  });

  it("rejects nulls, bad bins, bad speeds, ratio != mean / limit, bad counts and duplicates", () => {
    const bad = (row: [string, number, number, number, number, number]) => validateEdgeStats(table([row]), { durationS: 900 }).errors.join();
    expect(bad(["", 60, 6.945, 13.89, 0.5, 3])).toMatch(/edge_id null/);
    expect(bad([":j_0", 60, 6.945, 13.89, 0.5, 3])).toMatch(/internal/);
    expect(bad(["e1", Number.NaN, 6.945, 13.89, 0.5, 3])).toMatch(/non-finite/);
    expect(bad(["e1", 30, 6.945, 13.89, 0.5, 3])).toMatch(/multiple of 60/);
    expect(bad(["e1", -60, 6.945, 13.89, 0.5, 3])).toMatch(/multiple of 60/);
    expect(bad(["e1", 900, 6.945, 13.89, 0.5, 3])).toMatch(/past the scenario/);
    expect(bad(["e1", 60, -1, 13.89, -0.072, 3])).toMatch(/negative mean/);
    expect(bad(["e1", 60, 6.945, 0, 0.5, 3])).toMatch(/limit not positive/);
    expect(bad(["e1", 60, 6.945, 13.89, 0.6, 3])).toMatch(/mean \/ limit/);
    expect(bad(["e1", 60, 30, 13.89, 2.1598, 3])).toMatch(/implausibly high/);
    expect(bad(["e1", 60, 6.945, 13.89, 0.5, 0])).toMatch(/vehicle_count/);
    expect(bad(["e1", 60, 6.945, 13.89, 0.5, 1.5])).toMatch(/vehicle_count/);
    expect(validateEdgeStats(table([ok, ok])).errors.join()).toMatch(/duplicate/);
    // The cap matches the Python contract: exactly 2 passes, above 2 fails.
    expect(validateEdgeStats(table([["e1", 60, 27.78, 13.89, 2, 3]])).errors).toEqual([]);
  });

  it("rounding: the stored 4-decimal ratio is within tolerance of mean / limit", () => {
    // 11.03 / 13.89 = 0.79409...; the writer stores 0.7941.
    expect(validateEdgeStats(table([["e1", 0, 11.03, 13.89, 0.7941, 1]])).ok).toBe(true);
  });

  it("every edge needs a shape in edge_geometry.json", () => {
    const r = validateEdgeStats(table([ok]), { edgeIds: new Set(["other"]) });
    expect(r.errors[0]).toMatch(/no shape in edge_geometry/);
  });
});

const REAL = resolve(import.meta.dirname, "../../../sim/out/edge_stats_baseline.parquet");
const asArrayBuffer = (b: Buffer) => b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength) as ArrayBuffer;

describe("readEdgeStats", () => {
  it("refuses a file that isn't parquet", async () => {
    await expect(readEdgeStats(new TextEncoder().encode("not parquet at all").buffer)).rejects.toThrow();
  });
  it("the schema error carries the contract messages", () => {
    expect(new EdgeStatsSchemaError(["x"]).errors).toEqual(["x"]);
  });
});

describe.skipIf(!existsSync(REAL))("sim/out/edge_stats_baseline.parquet (read with hyparquet)", () => {
  it("reads all rows into columns and passes the web contract validation", async () => {
    const t = await readEdgeStats(asArrayBuffer(readFileSync(REAL)));
    expect(t.rows).toBeGreaterThan(10_000);
    expect(t.edgeId.every((id) => id !== "")).toBe(true);
    const r = validateEdgeStats(t, { durationS: 900 });
    expect(r.errors).toEqual([]);
  });
});
