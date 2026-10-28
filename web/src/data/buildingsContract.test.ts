import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { BUILDINGS_BBOX, bboxContainsPoint, SIM_BBOX } from "../config/bbox";
import { type BuildingsCollection, toPolygons, validateBuildings } from "./buildingsContract";

const FIXTURE_PATH = resolve(import.meta.dirname, "../../../tests/fixtures/buildings_fixture.geojson");
const loadFixture = (): BuildingsCollection => JSON.parse(readFileSync(FIXTURE_PATH, "utf8")) as BuildingsCollection;

/** A deep copy of the fixture with one feature's properties edited. */
function withProps(edit: (p: Record<string, unknown>) => void): unknown {
  const fc = loadFixture();
  edit(fc.features[0]!.properties as unknown as Record<string, unknown>);
  return fc;
}

describe("buildings contract: the fixture", () => {
  it("passes validation and is flagged as test data", () => {
    const r = validateBuildings(loadFixture());
    expect(r.errors).toEqual([]);
    expect(r.warnings).toEqual([]);
    expect(r.ok).toBe(true);
    expect(r.isTestFixture).toBe(true);
    expect(r.featureCount).toBeGreaterThanOrEqual(5);
    expect(r.featureCount).toBeLessThanOrEqual(10);
  });

  it("is labelled as test data in all three places", () => {
    const fc = loadFixture();
    expect(fc._test_fixture).toBe(true);
    expect(fc.description).toMatch(/test/i);
    expect(fc.description).toMatch(/not real buildings/i);
    fc.features.forEach((f, i) => {
      expect(f.properties.id).toBe(`fixture-${i + 1}`);
      expect(f.properties.height_source).toBe("test_fixture");
      expect(Object.keys(f.properties).sort()).toEqual(["height_m", "height_source", "id", "levels", "min_height_m"]);
    });
  });

  it("sits in Southbank, inside SIM_BBOX and BUILDINGS_BBOX, as plain rectangles", () => {
    for (const f of loadFixture().features) {
      expect(f.geometry.type).toBe("Polygon");
      const ring = (f.geometry.coordinates as number[][][])[0]!;
      expect(ring).toHaveLength(5); // 4 corners + closing point
      for (const [lon, lat] of ring) {
        expect(bboxContainsPoint(SIM_BBOX, lon!, lat!)).toBe(true);
        expect(bboxContainsPoint(BUILDINGS_BBOX, lon!, lat!)).toBe(true);
        expect(lon).toBeGreaterThanOrEqual(144.96);
        expect(lon).toBeLessThanOrEqual(144.968);
        expect(lat).toBeGreaterThanOrEqual(-37.826);
        expect(lat).toBeLessThanOrEqual(-37.821);
      }
      const lons = new Set(ring.map((p) => p[0]));
      const lats = new Set(ring.map((p) => p[1]));
      expect(lons.size).toBe(2);
      expect(lats.size).toBe(2);
    }
  });
});

describe("buildings contract: rejects bad data", () => {
  it.each([
    ["missing id", (p: Record<string, unknown>) => delete p.id, /missing property "id"/],
    ["missing height_m", (p: Record<string, unknown>) => delete p.height_m, /missing property "height_m"/],
    ["missing levels", (p: Record<string, unknown>) => delete p.levels, /missing property "levels"/],
    ["missing height_source", (p: Record<string, unknown>) => delete p.height_source, /missing property "height_source"/],
    ["string height", (p: Record<string, unknown>) => (p.height_m = "120"), /"height_m" must be/],
    ["zero height", (p: Record<string, unknown>) => (p.height_m = 0), /"height_m" must be/],
    ["fractional levels", (p: Record<string, unknown>) => (p.levels = 2.5), /"levels" must be/],
    ["empty height_source", (p: Record<string, unknown>) => (p.height_source = ""), /"height_source" must be/],
    ["missing min_height_m", (p: Record<string, unknown>) => delete p.min_height_m, /missing property "min_height_m"/],
    ["negative min_height_m", (p: Record<string, unknown>) => (p.min_height_m = -1), /"min_height_m" must be a number/],
    ["string min_height_m", (p: Record<string, unknown>) => (p.min_height_m = "0"), /"min_height_m" must be a number/],
    ["min_height_m at height_m", (p: Record<string, unknown>) => (p.min_height_m = p.height_m), /must be below height_m/],
    ["renamed property", (p: Record<string, unknown>) => ((p.height = p.height_m), delete p.height_m), /unexpected property "height"/],
    ["empty id", (p: Record<string, unknown>) => (p.id = ""), /"id" must be/],
  ])("%s", (_, edit, message) => {
    const r = validateBuildings(withProps(edit));
    expect(r.ok).toBe(false);
    expect(r.errors.join("\n")).toMatch(message);
  });

  it("rejects non-FeatureCollections", () => {
    expect(validateBuildings(null).ok).toBe(false);
    expect(validateBuildings({ type: "Feature" }).ok).toBe(false);
    expect(validateBuildings({ type: "FeatureCollection" }).ok).toBe(false);
  });

  it("rejects duplicate ids, non-polygons, unclosed rings and lat/lon swaps", () => {
    const dup = loadFixture();
    dup.features[1]!.properties.id = dup.features[0]!.properties.id;
    expect(validateBuildings(dup).errors.join()).toMatch(/duplicate id/);

    const point = loadFixture() as unknown as { features: { geometry: unknown }[] };
    point.features[0]!.geometry = { type: "Point", coordinates: [144.96, -37.82] };
    expect(validateBuildings(point).errors.join()).toMatch(/not Polygon or MultiPolygon/);

    const open = loadFixture();
    (open.features[0]!.geometry.coordinates as number[][][])[0]!.pop();
    expect(validateBuildings(open).errors.join()).toMatch(/not closed|at least 4/);

    const swapped = loadFixture();
    const ring = (swapped.features[0]!.geometry.coordinates as number[][][])[0]!;
    for (const p of ring) p.reverse(); // [lat, lon]: lat 144.96 is out of range
    expect(validateBuildings(swapped).errors.join()).toMatch(/not WGS84/);
  });

  it("accepts real-data height sources and numeric ids, and warns on unfamiliar ones", () => {
    const real = loadFixture() as unknown as Record<string, unknown> & BuildingsCollection;
    delete real._test_fixture;
    real.features.forEach((f, i) => {
      f.properties.height_source = "osm_height";
      f.properties.id = i + 1;
    });
    const r = validateBuildings(real);
    expect(r.ok).toBe(true);
    expect(r.isTestFixture).toBe(false);

    real.features[1]!.properties.height_source = "com_pointcloud_2018";
    expect(validateBuildings(real).warnings).toEqual([]);

    // The building area is the City of Melbourne UNION BUILDINGS_BBOX: a piece in Kensington (west of BUILDINGS_BBOX,
    // inside the City of Melbourne) is fine; one in Footscray, outside the area's envelope, still warns.
    const shift = (dLon: number) => {
      const c = structuredClone(real);
      for (const p of (c.features[0]!.geometry.coordinates as number[][][])[0]!) p[0] = p[0]! + dLon;
      return validateBuildings(c).warnings;
    };
    const lon0 = (real.features[0]!.geometry.coordinates as number[][][])[0]![0]![0]!;
    expect(shift(144.915 - lon0)).toEqual([]);
    expect(shift(144.885 - lon0).join()).toMatch(/outside the building area/);

    real.features[0]!.properties.height_source = "lidar";
    const w = validateBuildings(real);
    expect(w.ok).toBe(true);
    expect(w.warnings.join()).toMatch(/unfamiliar height_source "lidar"/);
  });
});

describe("buildings contract: test-fixture labelling", () => {
  it("errors when test_fixture features lack the top-level _test_fixture flag", () => {
    const fc = loadFixture() as unknown as Record<string, unknown> & BuildingsCollection;
    delete fc._test_fixture;
    const r = validateBuildings(fc);
    expect(r.ok).toBe(false);
    expect(r.isTestFixture).toBe(true);
    expect(r.errors.join()).toMatch(/lacks top-level "_test_fixture": true/);
  });

  it("errors when test_fixture features are mixed with real height sources", () => {
    const fc = loadFixture();
    fc.features[0]!.properties.height_source = "osm_height";
    const r = validateBuildings(fc);
    expect(r.ok).toBe(false);
    expect(r.errors.join()).toMatch(/mixed with 1 real-data feature/);
  });

  it("errors when one test_fixture feature hides in otherwise real data", () => {
    const fc = loadFixture() as unknown as Record<string, unknown> & BuildingsCollection;
    delete fc._test_fixture;
    fc.features.forEach((f) => (f.properties.height_source = "osm_levels"));
    fc.features[3]!.properties.height_source = "test_fixture";
    const r = validateBuildings(fc);
    expect(r.ok).toBe(false);
    expect(r.errors.join()).toMatch(/lacks top-level/);
    expect(r.errors.join()).toMatch(/mixed with/);
  });
});

describe("toPolygons", () => {
  it("emits one polygon per Polygon and one per MultiPolygon part", () => {
    const fc = loadFixture();
    expect(toPolygons(fc)).toHaveLength(fc.features.length);
    const ring = (fc.features[0]!.geometry.coordinates as number[][][])[0]!;
    fc.features[0]!.geometry = { type: "MultiPolygon", coordinates: [[ring as [number, number][]], [ring as [number, number][]]] };
    const polys = toPolygons(fc);
    expect(polys).toHaveLength(fc.features.length + 1);
    expect(polys[0]).toMatchObject({ id: "fixture-1", height: 120, base: 0 });
  });
});
