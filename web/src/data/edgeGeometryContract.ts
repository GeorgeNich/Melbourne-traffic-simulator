// Runtime validation (dev) of sim/out/edge_geometry.json against the contract in CLAUDE.md (Phase 3A, 2026-10-27):
//   an array of {edge_id, way_id, path: [[lon, lat, z_m], …]} for every non-internal car edge in roads.net.xml, in
//   driving direction (each direction of a two-way road has its own line, offset to its side); z from roads_3d.geojson
//   (the trips export's height model); way_id is one of the edge's OSM ways (--output.original-names).
// Errors mean the file breaks the contract (stop and report). Warnings are worth a look but don't block. The rules
// follow qa-reviewer's tests/contracts/test_edge_geometry_contract.py where the browser can check them cheaply (the
// network itself isn't loaded here, so coverage of roads.net.xml stays with the Python test).

import { type BBox, bboxContainsPoint, SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX } from "../config/bbox";
import { PLAUSIBLE_Z_M } from "./roadsContract";
import { TRIPS_BBOX_SLACK_DEG } from "./tripsContract";

export type EdgePosition = [number, number, number];

export interface EdgeGeometry {
  edge_id: string;
  way_id: number;
  path: EdgePosition[];
}

export const EDGE_GEOMETRY_KEYS = ["edge_id", "way_id", "path"] as const;

export interface EdgeGeometryValidation {
  ok: boolean;
  errors: string[];
  warnings: string[];
  edgeCount: number;
}

const MAX_MESSAGES = 50;
const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const grow = (b: BBox, d: number): BBox => ({ west: b.west - d, south: b.south - d, east: b.east + d, north: b.north + d });
/**
 * Position bounds: inside SIM_EXTRACT_BBOX give or take ~10 m (lane offsets past the clip, as the trips), or inside
 * TUNNEL_EXTRACT_BBOX on an edge that goes below ground (a CityLink tunnel edge east of the sim area, including its
 * at-grade portal end, e.g. the Burnley tunnel's east portal). Swapped [lat, lon] would be ~180 km out.
 */
const EDGE_BBOX = grow(SIM_EXTRACT_BBOX, TRIPS_BBOX_SLACK_DEG);
const EDGE_TUNNEL_BBOX = grow(TUNNEL_EXTRACT_BBOX, TRIPS_BBOX_SLACK_DEG);

/** Validate a parsed edge_geometry.json. */
export function validateEdgeGeometry(data: unknown): EdgeGeometryValidation {
  const errors: string[] = [];
  const warnings: string[] = [];
  const done = (edgeCount: number): EdgeGeometryValidation => ({
    ok: errors.length === 0,
    errors: errors.slice(0, MAX_MESSAGES),
    warnings: warnings.slice(0, MAX_MESSAGES),
    edgeCount,
  });
  if (!Array.isArray(data)) {
    errors.push("top level is not an array of edges");
    return done(0);
  }
  if (data.length === 0) errors.push("no edges");
  const ids = new Set<string>();
  const expected = [...EDGE_GEOMETRY_KEYS].sort().join();
  data.forEach((e: unknown, i: number) => {
    if (errors.length > MAX_MESSAGES) return;
    if (!isObject(e)) {
      errors.push(`edge ${i}: not an object`);
      return;
    }
    const where = `edge ${i} (${typeof e.edge_id === "string" ? e.edge_id : "?"})`;
    const keys = Object.keys(e).sort();
    if (keys.join() !== expected) errors.push(`${where}: keys are [${keys.join(", ")}], expected exactly [${expected.split(",").join(", ")}]`);
    if (typeof e.edge_id !== "string" || e.edge_id === "") errors.push(`${where}: edge_id is not a non-empty string`);
    else if (e.edge_id.startsWith(":")) errors.push(`${where}: internal (junction) edge; the contract lists non-internal edges only`);
    else if (ids.has(e.edge_id)) errors.push(`${where}: duplicate edge_id`);
    else ids.add(e.edge_id);
    if (!Number.isInteger(e.way_id) || (e.way_id as number) <= 0) errors.push(`${where}: way_id is not a positive integer`);
    if (!Array.isArray(e.path)) {
      errors.push(`${where}: path is not an array`);
      return;
    }
    if (e.path.length < 2) errors.push(`${where}: path has fewer than 2 positions`);
    let malformed = 0;
    const outside: string[] = [];
    const badZ: number[] = [];
    const goesBelow = (e.path as unknown[]).some((p) => Array.isArray(p) && isNum(p[2]) && p[2] < 0);
    for (const p of e.path as unknown[]) {
      if (!Array.isArray(p) || p.length !== 3 || !p.every(isNum)) {
        malformed++;
        continue;
      }
      const [lon, lat, z] = p as EdgePosition;
      if (!bboxContainsPoint(EDGE_BBOX, lon, lat) && !(goesBelow && bboxContainsPoint(EDGE_TUNNEL_BBOX, lon, lat))) outside.push(`[${lon}, ${lat}, ${z}]`);
      if (z < PLAUSIBLE_Z_M.min || z > PLAUSIBLE_Z_M.max) badZ.push(z);
    }
    const n = e.path.length;
    if (malformed) errors.push(`${where}: ${malformed} of ${n} positions are not [lon, lat, z_m] with finite numbers`);
    if (outside.length) errors.push(`${where}: ${outside.length} of ${n} positions outside the road extract, first ${outside[0]} (lon/lat swapped?)`);
    if (badZ.length) warnings.push(`${where}: ${badZ.length} of ${n} z values outside ${PLAUSIBLE_Z_M.min}..${PLAUSIBLE_Z_M.max} m`);
  });
  return done(data.length);
}
