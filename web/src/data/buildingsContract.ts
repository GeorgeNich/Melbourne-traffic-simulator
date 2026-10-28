// Runtime validation of data/processed/buildings.geojson against the contract in CLAUDE.md:
//   Building PIECES (stacked tiers), WGS84 polygons; properties `id`, `height_m`, `levels`, `height_source`, `min_height_m`.
// Errors mean the file breaks the contract (stop and report). Warnings are worth a look but don't block.

import { BUILDINGS_AREA_BBOX, bboxContainsPoint } from "../config/bbox";

export type Position = [number, number] | [number, number, number];
export type PolygonCoords = Position[][];

export interface BuildingProperties {
  id: string | number;
  height_m: number;
  levels: number | null;
  height_source: string;
  /** Base of the piece above ground (0 when it stands on the ground). */
  min_height_m: number;
}

export interface BuildingFeature {
  type: "Feature";
  properties: BuildingProperties;
  geometry: { type: "Polygon"; coordinates: PolygonCoords } | { type: "MultiPolygon"; coordinates: PolygonCoords[] };
}

export interface BuildingsCollection {
  type: "FeatureCollection";
  features: BuildingFeature[];
  _test_fixture?: boolean;
  description?: string;
}

export const CONTRACT_PROPERTIES = ["id", "height_m", "levels", "height_source", "min_height_m"] as const;

/** height_source values named in CLAUDE.md. Others are a warning (the list there is "e.g."), not an error. */
export const KNOWN_HEIGHT_SOURCES = ["osm_height", "osm_levels", "com_open_data", "com_pointcloud_2018", "type_default"] as const;
export const TEST_FIXTURE_HEIGHT_SOURCE = "test_fixture";

/** Tallest plausible building in the study area, with headroom (Australia 108 is ~317 m). */
export const MAX_PLAUSIBLE_HEIGHT_M = 400;

export interface ValidationResult {
  ok: boolean;
  errors: string[];
  warnings: string[];
  featureCount: number;
  isTestFixture: boolean;
}

const MAX_MESSAGES = 50;

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function checkPosition(p: unknown): string | null {
  if (!Array.isArray(p) || (p.length !== 2 && p.length !== 3)) return "position is not [lon, lat]";
  const [lon, lat] = p as unknown[];
  if (typeof lon !== "number" || typeof lat !== "number" || !Number.isFinite(lon) || !Number.isFinite(lat)) {
    return "position has non-numeric lon/lat";
  }
  if (lon < -180 || lon > 180 || lat < -90 || lat > 90) return `position [${lon}, ${lat}] is not WGS84 lon/lat`;
  return null;
}

function checkPolygon(poly: unknown, where: string, errors: string[], warnings: string[]): void {
  if (!Array.isArray(poly) || poly.length === 0) {
    errors.push(`${where}: polygon has no rings`);
    return;
  }
  poly.forEach((ring, r) => {
    if (!Array.isArray(ring) || ring.length < 4) {
      errors.push(`${where}: ring ${r} needs at least 4 positions`);
      return;
    }
    for (const p of ring) {
      const err = checkPosition(p);
      if (err) {
        errors.push(`${where}: ring ${r} ${err}`);
        return;
      }
    }
    const first = ring[0] as number[];
    const last = ring[ring.length - 1] as number[];
    if (first[0] !== last[0] || first[1] !== last[1]) errors.push(`${where}: ring ${r} is not closed`);
    // The building area is the City of Melbourne boundary UNION BUILDINGS_BBOX; its envelope is BUILDINGS_AREA_BBOX.
    if (r === 0 && !ring.some((p: number[]) => bboxContainsPoint(BUILDINGS_AREA_BBOX, p[0]!, p[1]!))) {
      warnings.push(`${where}: outer ring lies entirely outside the building area (BUILDINGS_AREA_BBOX)`);
    }
  });
}

/** Validate parsed GeoJSON against the buildings contract. */
export function validateBuildings(data: unknown): ValidationResult {
  const errors: string[] = [];
  const warnings: string[] = [];
  const done = (featureCount: number, isTestFixture: boolean): ValidationResult => ({
    ok: errors.length === 0,
    errors: errors.slice(0, MAX_MESSAGES),
    warnings: warnings.slice(0, MAX_MESSAGES),
    featureCount,
    isTestFixture,
  });

  if (!isObject(data) || data.type !== "FeatureCollection") {
    errors.push("top level is not a GeoJSON FeatureCollection");
    return done(0, false);
  }
  if (!Array.isArray(data.features)) {
    errors.push("FeatureCollection has no features array");
    return done(0, false);
  }
  const declaredFixture = data._test_fixture === true;
  let fixtureFeatures = 0;
  let realSourceFeatures = 0;
  const ids = new Set<unknown>();

  data.features.forEach((f: unknown, i: number) => {
    if (errors.length > MAX_MESSAGES) return;
    const where = `feature ${i}`;
    if (!isObject(f) || f.type !== "Feature") {
      errors.push(`${where}: not a GeoJSON Feature`);
      return;
    }
    const props = f.properties;
    if (!isObject(props)) {
      errors.push(`${where}: missing properties`);
    } else {
      for (const key of CONTRACT_PROPERTIES) {
        if (!(key in props)) errors.push(`${where}: missing property "${key}"`);
      }
      for (const key of Object.keys(props)) {
        if (!(CONTRACT_PROPERTIES as readonly string[]).includes(key)) {
          errors.push(`${where}: unexpected property "${key}" (not in the contract)`);
        }
      }
      const { id, height_m, levels, height_source } = props;
      if ("id" in props) {
        if (!(typeof id === "string" && id.length > 0) && !(typeof id === "number" && Number.isFinite(id))) {
          errors.push(`${where}: "id" must be a non-empty string or a number`);
        } else if (ids.has(id)) {
          errors.push(`${where}: duplicate id ${JSON.stringify(id)}`);
        } else {
          ids.add(id);
        }
      }
      if ("height_m" in props) {
        if (typeof height_m !== "number" || !Number.isFinite(height_m) || height_m <= 0) {
          errors.push(`${where}: "height_m" must be a positive number of metres`);
        } else if (height_m > MAX_PLAUSIBLE_HEIGHT_M) {
          warnings.push(`${where}: height_m ${height_m} is taller than any building in Melbourne`);
        }
      }
      if ("min_height_m" in props) {
        const mh = props.min_height_m;
        if (typeof mh !== "number" || !Number.isFinite(mh) || mh < 0) {
          errors.push(`${where}: "min_height_m" must be a number of metres >= 0`);
        } else if (typeof height_m === "number" && mh >= height_m) {
          errors.push(`${where}: "min_height_m" ${mh} must be below height_m ${height_m}`);
        }
      }
      if ("levels" in props && levels !== null) {
        if (typeof levels !== "number" || !Number.isInteger(levels) || levels < 0) {
          errors.push(`${where}: "levels" must be a non-negative integer or null`);
        }
      }
      if ("height_source" in props) {
        if (typeof height_source !== "string" || height_source.length === 0) {
          errors.push(`${where}: "height_source" must be a non-empty string`);
        } else if (height_source === TEST_FIXTURE_HEIGHT_SOURCE) {
          fixtureFeatures += 1;
        } else {
          realSourceFeatures += 1;
          if (!(KNOWN_HEIGHT_SOURCES as readonly string[]).includes(height_source)) {
            warnings.push(`${where}: unfamiliar height_source "${height_source}"`);
          }
        }
      }
    }

    const geom = f.geometry;
    if (!isObject(geom)) {
      errors.push(`${where}: missing geometry`);
    } else if (geom.type === "Polygon") {
      checkPolygon(geom.coordinates, where, errors, warnings);
    } else if (geom.type === "MultiPolygon") {
      if (!Array.isArray(geom.coordinates) || geom.coordinates.length === 0) {
        errors.push(`${where}: MultiPolygon has no polygons`);
      } else {
        geom.coordinates.forEach((poly: unknown, k: number) => checkPolygon(poly, `${where} part ${k}`, errors, warnings));
      }
    } else {
      errors.push(`${where}: geometry type ${String(geom.type)} is not Polygon or MultiPolygon`);
    }
  });

  // Test data must be labelled as a whole and never mixed with real data (qa-reviewer, Phase 1).
  if (fixtureFeatures > 0 && !declaredFixture) {
    errors.push(
      `${fixtureFeatures} feature(s) have height_source "test_fixture" but the collection lacks top-level "_test_fixture": true`,
    );
  }
  if (fixtureFeatures > 0 && realSourceFeatures > 0) {
    errors.push(`test fixture features are mixed with ${realSourceFeatures} real-data feature(s)`);
  }
  if (declaredFixture && realSourceFeatures > 0) {
    errors.push(`"_test_fixture": true but ${realSourceFeatures} feature(s) carry real height_source values`);
  }

  if (data.features.length === 0) warnings.push("FeatureCollection is empty");
  return done(data.features.length, declaredFixture || fixtureFeatures > 0);
}

/** One extrudable footprint for deck.gl's PolygonLayer (MultiPolygons are split into parts). */
export interface BuildingPolygon {
  id: string | number;
  polygon: PolygonCoords;
  /** Top of the piece above ground (height_m). */
  height: number;
  /** Base of the piece above ground (min_height_m). */
  base: number;
}

export function toPolygons(fc: BuildingsCollection): BuildingPolygon[] {
  const out: BuildingPolygon[] = [];
  for (const f of fc.features) {
    const { id, height_m, min_height_m } = f.properties;
    if (f.geometry.type === "Polygon") {
      out.push({ id, polygon: f.geometry.coordinates, height: height_m, base: min_height_m });
    } else {
      for (const polygon of f.geometry.coordinates) out.push({ id, polygon, height: height_m, base: min_height_m });
    }
  }
  return out;
}
