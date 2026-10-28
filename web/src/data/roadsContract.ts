// Runtime validation of data/processed/roads_3d.geojson against the contract in CLAUDE.md:
//   drivable OSM ways (sim area + 300 m buffer) as 3D LineStrings [lon, lat, z_m]; properties exactly `way_id`,
//   `highway`, `layer`, `bridge`, `tunnel`, `name`, `z_source`. The single source of road heights.
// Errors mean the file breaks the contract (stop and report). Warnings are worth a look but don't block.

import { type BBox, bboxContainsPoint, EXTENDED_CLIP_HIGHWAYS, SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX } from "../config/bbox";

export type Position3 = [number, number, number];

/** Where a way's heights come from (PLAN.md Decisions log, 2026-10-26): estimated heights are never shown as measured. */
export const Z_SOURCES = ["ground", "estimated", "measured"] as const;
export type ZSource = (typeof Z_SOURCES)[number];

export interface RoadProperties {
  way_id: number;
  highway: string;
  layer: number;
  bridge: boolean;
  tunnel: boolean;
  name: string | null;
  z_source: ZSource;
}

export interface RoadFeature {
  type: "Feature";
  properties: RoadProperties;
  geometry: { type: "LineString"; coordinates: Position3[] };
}

export interface RoadsCollection {
  type: "FeatureCollection";
  features: RoadFeature[];
}

export const ROAD_PROPERTIES = ["way_id", "highway", "layer", "bridge", "tunnel", "name", "z_source"] as const;

/**
 * Position bounds, as in tests/contracts/test_roads_3d_contract.py: every line starts and ends inside its clip box
 * (give or take the 6-decimal rounding of stored lon/lat); interior points of a clipped way may loop slightly out
 * (seen: 4.8 m), up to 0.001 deg (~100 m). Anything further is wrong data, e.g. swapped [lat, lon].
 * The clip box is SIM_EXTRACT_BBOX, except for motorway / motorway_link ways with tunnel=true (Stage B: the CityLink
 * Burnley and Domain tunnels under the Yarra), which are clipped at TUNNEL_EXTRACT_BBOX.
 */
export const EXTRACT_BBOX_SLACK_DEG = { ends: 1e-6, loopOut: 0.001 } as const;

const grow = (b: BBox, d: number): BBox => ({ west: b.west - d, south: b.south - d, east: b.east + d, north: b.north + d });
const clipBoxes = (b: BBox) => ({ ends: grow(b, EXTRACT_BBOX_SLACK_DEG.ends), loopOut: grow(b, EXTRACT_BBOX_SLACK_DEG.loopOut) });
const SIM_CLIP = { name: "SIM_EXTRACT_BBOX", ...clipBoxes(SIM_EXTRACT_BBOX) };
const TUNNEL_CLIP = { name: "TUNNEL_EXTRACT_BBOX", ...clipBoxes(TUNNEL_EXTRACT_BBOX) };

/** True if a way is clipped at TUNNEL_EXTRACT_BBOX: a motorway or motorway_link tunnel (pipeline/config.py). */
export function hasExtendedClip(props: { highway?: unknown; tunnel?: unknown }): boolean {
  return props.tunnel === true && typeof props.highway === "string" && EXTENDED_CLIP_HIGHWAYS.includes(props.highway);
}

/** Plausible road heights (m) for warnings only: bridges top out at 26 m; Stage B tunnels go to about -30 m. */
export const PLAUSIBLE_Z_M = { min: -40, max: 40 } as const;

export interface RoadsValidation {
  ok: boolean;
  errors: string[];
  warnings: string[];
  featureCount: number;
}

const MAX_MESSAGES = 50;

const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const sameKeys = (keys: string[], expected: readonly string[]) => keys.join() === [...expected].sort().join();

/** Validate parsed GeoJSON against the roads_3d contract. */
export function validateRoads(data: unknown): RoadsValidation {
  const errors: string[] = [];
  const warnings: string[] = [];
  const done = (featureCount: number): RoadsValidation => ({
    ok: errors.length === 0,
    errors: errors.slice(0, MAX_MESSAGES),
    warnings: warnings.slice(0, MAX_MESSAGES),
    featureCount,
  });

  if (!isObject(data) || data.type !== "FeatureCollection") {
    errors.push("top level is not a GeoJSON FeatureCollection");
    return done(0);
  }
  if (!Array.isArray(data.features)) {
    errors.push("FeatureCollection has no features array");
    return done(0);
  }
  if (data._test_fixture === true) errors.push("roads_3d.geojson is marked as test fixture data");
  const ids = new Set<number>();

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
      const keys = Object.keys(props).sort();
      if (!sameKeys(keys, ROAD_PROPERTIES)) errors.push(`${where}: properties are [${keys.join(", ")}], expected exactly [${[...ROAD_PROPERTIES].sort().join(", ")}]`);
      if (!Number.isInteger(props.way_id) || (props.way_id as number) <= 0) errors.push(`${where}: way_id is not a positive integer`);
      else if (ids.has(props.way_id as number)) errors.push(`${where}: duplicate way_id ${String(props.way_id)}`);
      else ids.add(props.way_id as number);
      if (typeof props.highway !== "string" || props.highway === "") errors.push(`${where}: highway is not a string`);
      if (!Number.isInteger(props.layer)) errors.push(`${where}: layer is not an integer`);
      if (typeof props.bridge !== "boolean") errors.push(`${where}: bridge is not a boolean`);
      if (typeof props.tunnel !== "boolean") errors.push(`${where}: tunnel is not a boolean`);
      if (props.name !== null && (typeof props.name !== "string" || props.name === "")) {
        errors.push(`${where}: name is not a non-empty string or null`);
      }
      if (!(Z_SOURCES as readonly unknown[]).includes(props.z_source)) {
        errors.push(`${where}: z_source ${JSON.stringify(props.z_source)} is not one of ${Z_SOURCES.join(", ")}`);
      }
    }
    const g = f.geometry;
    if (!isObject(g) || g.type !== "LineString" || !Array.isArray(g.coordinates)) {
      errors.push(`${where}: geometry is not a LineString`);
      return;
    }
    if (g.coordinates.length < 2) errors.push(`${where}: LineString has fewer than 2 positions`);
    const clip = isObject(props) && hasExtendedClip(props) ? TUNNEL_CLIP : SIM_CLIP;
    // Check every position; report each kind of problem once per feature (with a count), not just the first.
    let malformed = 0;
    const outside: string[] = [];
    const badZ: number[] = [];
    const coords = g.coordinates as unknown[];
    coords.forEach((p, k) => {
      if (!Array.isArray(p) || p.length !== 3 || !p.every(isNum)) {
        malformed++;
        return;
      }
      const [lon, lat, z] = p as Position3;
      const isEnd = k === 0 || k === coords.length - 1;
      if (!bboxContainsPoint(isEnd ? clip.ends : clip.loopOut, lon, lat)) outside.push(`[${lon}, ${lat}]${isEnd ? " (line end)" : ""}`);
      if (z < PLAUSIBLE_Z_M.min || z > PLAUSIBLE_Z_M.max) badZ.push(z);
    });
    const n = coords.length;
    if (malformed) errors.push(`${where}: ${malformed} of ${n} positions are not [lon, lat, z_m] with finite numbers`);
    if (outside.length) {
      errors.push(
        `${where}: ${outside.length} of ${n} positions outside the road extract (${clip.name}), first ${outside[0]} (lon/lat swapped?)`,
      );
    }
    if (badZ.length) {
      warnings.push(
        `${where}: ${badZ.length} of ${n} z values outside ${PLAUSIBLE_Z_M.min}..${PLAUSIBLE_Z_M.max} m ` +
          `(min ${Math.min(...badZ)}, max ${Math.max(...badZ)})`,
      );
    }
  });
  return done(data.features.length);
}
