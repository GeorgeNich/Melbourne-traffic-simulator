import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { validateEdgeGeometry } from "./edgeGeometryContract";

// Hand-written edges (not real data): a Collins St-like segment and a CityLink-like tunnel edge east of the sim area.
const edge = (over: Record<string, unknown> = {}) => ({
  edge_id: "e1",
  way_id: 101,
  path: [
    [144.96, -37.816, 0],
    [144.961, -37.8165, 0],
  ],
  ...over,
});

describe("validateEdgeGeometry", () => {
  it("accepts contract-shaped edges, incl. a tunnel edge out to TUNNEL_EXTRACT_BBOX with its portal at grade", () => {
    const tunnel = edge({ edge_id: "t1", path: [[144.979, -37.8243, -30], [144.98, -37.8243, -30], [144.9843, -37.8264, 0]] });
    const r = validateEdgeGeometry([edge(), tunnel]);
    expect(r.errors).toEqual([]);
    expect(r.edgeCount).toBe(2);
  });

  it("rejects a non-array, extra or missing keys, duplicates, internal edges and bad way ids", () => {
    expect(validateEdgeGeometry({}).ok).toBe(false);
    expect(validateEdgeGeometry([]).ok).toBe(false);
    expect(validateEdgeGeometry([edge({ speed: 1 })]).errors[0]).toMatch(/keys are/);
    const noWay: Record<string, unknown> = edge();
    delete noWay.way_id;
    expect(validateEdgeGeometry([noWay]).ok).toBe(false);
    expect(validateEdgeGeometry([edge(), edge()]).errors.join()).toMatch(/duplicate edge_id/);
    expect(validateEdgeGeometry([edge({ edge_id: ":j1_0" })]).errors.join()).toMatch(/internal/);
    expect(validateEdgeGeometry([edge({ way_id: 0 })]).ok).toBe(false);
    expect(validateEdgeGeometry([edge({ way_id: "101" })]).ok).toBe(false);
    expect(validateEdgeGeometry([edge({ edge_id: 7 })]).ok).toBe(false);
  });

  it("rejects short or malformed paths and swapped lon/lat; a surface point past the sim extract is out", () => {
    expect(validateEdgeGeometry([edge({ path: [[144.96, -37.816, 0]] })]).ok).toBe(false);
    expect(validateEdgeGeometry([edge({ path: [[144.96, -37.816], [144.961, -37.8165]] })]).ok).toBe(false);
    expect(validateEdgeGeometry([edge({ path: [[144.96, -37.816, Number.NaN], [144.961, -37.8165, 0]] })]).ok).toBe(false);
    expect(validateEdgeGeometry([edge({ path: [[-37.816, 144.96, 0], [-37.8165, 144.961, 0]] })]).errors[0]).toMatch(/swapped/);
    // Surface (z = 0) east of the sim extract: only tunnel samples may go there.
    expect(validateEdgeGeometry([edge({ path: [[144.979, -37.8243, 0], [144.98, -37.8243, 0]] })]).ok).toBe(false);
  });

  it("warns (doesn't fail) on implausible heights", () => {
    const r = validateEdgeGeometry([edge({ path: [[144.96, -37.816, 0], [144.961, -37.8165, 80]] })]);
    expect(r.ok).toBe(true);
    expect(r.warnings[0]).toMatch(/z values/);
  });
});

const REAL = resolve(import.meta.dirname, "../../../sim/out/edge_geometry.json");
describe.skipIf(!existsSync(REAL))("sim/out/edge_geometry.json", () => {
  it("passes the web contract validation", () => {
    const r = validateEdgeGeometry(JSON.parse(readFileSync(REAL, "utf8")));
    expect(r.errors).toEqual([]);
    expect(r.edgeCount).toBeGreaterThan(1000);
  });
});
