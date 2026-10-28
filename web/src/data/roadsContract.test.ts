import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX } from "../config/bbox";
import { hasExtendedClip, validateRoads } from "./roadsContract";

// Small hand-written features (inline, test-only; not a data file and never bundled).
const road = (props: Record<string, unknown> = {}, coordinates: unknown = [[144.96, -37.82, 0], [144.961, -37.82, 6.5]]) => ({
  type: "Feature",
  properties: { way_id: 1, highway: "primary", layer: 1, bridge: true, tunnel: false, name: "Test Bridge", z_source: "estimated", ...props },
  geometry: { type: "LineString", coordinates },
});
const fc = (...features: unknown[]) => ({ type: "FeatureCollection", features });

describe("validateRoads", () => {
  it("accepts a contract-shaped collection, including a null name and every z_source", () => {
    const r = validateRoads(
      fc(
        road(),
        road({ way_id: 2, name: null, bridge: false, layer: 0, z_source: "ground" }),
        road({ way_id: 3, z_source: "measured" }),
      ),
    );
    expect(r.errors).toEqual([]);
    expect(r.warnings).toEqual([]);
    expect(r.ok).toBe(true);
    expect(r.featureCount).toBe(3);
  });

  it("rejects a non-collection", () => {
    expect(validateRoads({ type: "Feature" }).ok).toBe(false);
    expect(validateRoads(null).ok).toBe(false);
  });

  it("requires exactly the seven contract properties with the right types", () => {
    expect(validateRoads(fc(road({ lanes: 2 }))).errors[0]).toMatch(/expected exactly \[bridge, highway, layer, name, tunnel, way_id, z_source\]/);
    const noName: Record<string, unknown> = { ...road().properties };
    delete noName.name;
    expect(validateRoads(fc({ ...road(), properties: noName })).ok).toBe(false);
    expect(validateRoads(fc(road({ bridge: "yes" }))).errors).toContain("feature 0: bridge is not a boolean");
    expect(validateRoads(fc(road({ layer: 1.5 }))).errors).toContain("feature 0: layer is not an integer");
    expect(validateRoads(fc(road({ way_id: "w1" }))).errors).toContain("feature 0: way_id is not a positive integer");
    expect(validateRoads(fc(road(), road())).errors).toContain("feature 1: duplicate way_id 1");
  });

  it("requires way_id > 0", () => {
    expect(validateRoads(fc(road({ way_id: 0 }))).errors).toContain("feature 0: way_id is not a positive integer");
    expect(validateRoads(fc(road({ way_id: -5 }))).errors).toContain("feature 0: way_id is not a positive integer");
  });

  it("requires name to be a non-empty string or null", () => {
    expect(validateRoads(fc(road({ name: "" }))).errors).toContain("feature 0: name is not a non-empty string or null");
    expect(validateRoads(fc(road({ name: 7 }))).errors).toContain("feature 0: name is not a non-empty string or null");
    expect(validateRoads(fc(road({ name: null }))).ok).toBe(true);
  });

  it("requires z_source to be ground, estimated or measured", () => {
    for (const bad of ["surveyed", "", null, 1]) {
      expect(validateRoads(fc(road({ z_source: bad }))).errors[0]).toMatch(/z_source .* is not one of ground, estimated, measured/);
    }
  });

  it("requires z_source: the pre-z_source six-property shape is rejected", () => {
    const old: Record<string, unknown> = { ...road().properties };
    delete old.z_source;
    const r = validateRoads(fc({ ...road(), properties: old }));
    expect(r.ok).toBe(false);
    expect(r.errors[0]).toMatch(/expected exactly \[bridge, highway, layer, name, tunnel, way_id, z_source\]/);
  });

  it("requires 3D LineStrings [lon, lat, z_m] inside the road extract bbox", () => {
    expect(validateRoads(fc({ ...road(), geometry: { type: "Point", coordinates: [144.96, -37.82, 0] } })).ok).toBe(false);
    expect(validateRoads(fc(road({}, [[144.96, -37.82], [144.961, -37.82]]))).errors[0]).toMatch(/\[lon, lat, z_m\]/);
    expect(validateRoads(fc(road({}, [[-37.82, 144.96, 0], [-37.82, 144.961, 0]]))).errors[0]).toMatch(/swapped/);
    expect(validateRoads(fc(road({}, [[144.96, -37.82, 0]]))).errors[0]).toMatch(/fewer than 2/);
    expect(validateRoads(fc(road({}, [[144.96, -37.82, Number.NaN], [144.961, -37.82, 0]]))).ok).toBe(false);
  });

  it("checks positions against SIM_EXTRACT_BBOX, not the wider building ring", () => {
    // Inside the building ring (e.g. near the MCG) but outside the road extract.
    expect(SIM_EXTRACT_BBOX.east).toBeLessThan(144.985);
    const r = validateRoads(fc(road({}, [[144.96, -37.82, 0], [144.985, -37.82, 0]])));
    expect(r.ok).toBe(false);
    expect(r.errors[0]).toMatch(/1 of 2 positions outside the road extract/);
    // Line ends just inside the buffered edge are fine (6-decimal rounding allowed).
    const edge = [Number(SIM_EXTRACT_BBOX.west.toFixed(6)), Number(SIM_EXTRACT_BBOX.south.toFixed(6)), 0];
    expect(validateRoads(fc(road({}, [edge, [144.96, -37.82, 0]]))).ok).toBe(true);
  });

  it("allows motorway / motorway_link tunnels out to TUNNEL_EXTRACT_BBOX (the Burnley and Domain tunnels), nothing else", () => {
    // Burnley tunnel under the Yarra: east of SIM_EXTRACT_BBOX, inside TUNNEL_EXTRACT_BBOX.
    const under = [[144.968, -37.8258, -16], [144.98, -37.8255, -30], [144.9897, -37.8249, -30]];
    expect(SIM_EXTRACT_BBOX.east).toBeLessThan(144.98);
    for (const highway of ["motorway", "motorway_link"]) {
      const r = validateRoads(fc(road({ highway, tunnel: true, bridge: false, layer: -3 }, under)));
      expect(r.errors).toEqual([]);
    }
    // The same positions on a non-tunnel motorway, or a tunnel that isn't a motorway, stay at SIM_EXTRACT_BBOX.
    for (const props of [{ highway: "motorway", tunnel: false }, { highway: "trunk", tunnel: true }, { highway: "service", tunnel: true }]) {
      const r = validateRoads(fc(road({ ...props, bridge: false, layer: -1 }, under)));
      expect(r.errors[0]).toMatch(/2 of 3 positions outside the road extract \(SIM_EXTRACT_BBOX\)/);
    }
    // Past TUNNEL_EXTRACT_BBOX's east edge is still an error, even for a motorway tunnel.
    const past = [[144.968, -37.8258, -16], [TUNNEL_EXTRACT_BBOX.east + 0.001, -37.8255, -30]];
    const r = validateRoads(fc(road({ highway: "motorway", tunnel: true, bridge: false, layer: -3 }, past)));
    expect(r.errors[0]).toMatch(/1 of 2 positions outside the road extract \(TUNNEL_EXTRACT_BBOX\)/);
    // West, south and north edges are SIM_EXTRACT_BBOX's for tunnels too.
    const west = [[144.968, -37.8258, -16], [SIM_EXTRACT_BBOX.west - 0.001, -37.8255, -10]];
    expect(validateRoads(fc(road({ highway: "motorway", tunnel: true, bridge: false, layer: -3 }, west))).ok).toBe(false);
  });

  it("hasExtendedClip: motorway / motorway_link with tunnel=true only", () => {
    expect(hasExtendedClip({ highway: "motorway", tunnel: true })).toBe(true);
    expect(hasExtendedClip({ highway: "motorway_link", tunnel: true })).toBe(true);
    expect(hasExtendedClip({ highway: "motorway", tunnel: false })).toBe(false);
    expect(hasExtendedClip({ highway: "trunk", tunnel: true })).toBe(false);
    expect(hasExtendedClip({ highway: "motorway", tunnel: "yes" })).toBe(false);
  });

  it("lets interior points of a clipped way loop slightly out of the extract, but not line ends", () => {
    const w = SIM_EXTRACT_BBOX.west;
    // ~5 m out in the middle of the line: allowed (as in the contract test).
    expect(validateRoads(fc(road({}, [[144.94, -37.82, 0], [w - 0.00005, -37.82, 0], [144.94, -37.821, 0]]))).ok).toBe(true);
    // ~200 m out in the middle: not allowed.
    expect(validateRoads(fc(road({}, [[144.94, -37.82, 0], [w - 0.002, -37.82, 0], [144.94, -37.821, 0]]))).ok).toBe(false);
    // ~5 m out at a line end: not allowed.
    const r = validateRoads(fc(road({}, [[144.94, -37.82, 0], [w - 0.00005, -37.82, 0]])));
    expect(r.errors[0]).toMatch(/\(line end\)/);
  });

  it("checks every position, not just up to the first problem", () => {
    const r = validateRoads(fc(road({}, [[144.96, -37.82, 120], [144.961, -37.82, 0], [144.962, -37.82, -90], [144.985, -37.82, 0]])));
    expect(r.errors).toEqual(["feature 0: 1 of 4 positions outside the road extract (SIM_EXTRACT_BBOX), first [144.985, -37.82] (line end) (lon/lat swapped?)"]);
    expect(r.warnings).toEqual(["feature 0: 2 of 4 z values outside -40..40 m (min -90, max 120)"]);
  });

  it("warns (doesn't fail) on implausible heights", () => {
    const r = validateRoads(fc(road({}, [[144.96, -37.82, 0], [144.961, -37.82, 120]])));
    expect(r.ok).toBe(true);
    expect(r.warnings[0]).toMatch(/120/);
  });

  it("rejects data marked as a test fixture", () => {
    expect(validateRoads({ ...fc(road()), _test_fixture: true }).ok).toBe(false);
  });
});

const REAL_PATH = resolve(import.meta.dirname, "../../../data/processed/roads_3d.geojson");

describe.skipIf(!existsSync(REAL_PATH))("data/processed/roads_3d.geojson", () => {
  it("passes the web contract validation", () => {
    const r = validateRoads(JSON.parse(readFileSync(REAL_PATH, "utf8")));
    expect(r.errors).toEqual([]);
    expect(r.featureCount).toBeGreaterThan(1000);
  });
});
