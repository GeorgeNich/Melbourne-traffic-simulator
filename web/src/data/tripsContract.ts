// Runtime validation (dev) of the traffic replay against the contract in CLAUDE.md:
//   sim/out/trips_<scenario>.json  array of {id, type, path: [[lon, lat, z_m], …], timestamps: [s, …]}
//                                  (seconds from scenario start, strictly increasing, within duration_s; z from
//                                  roads_3d.geojson; positions inside SIM_EXTRACT_BBOX give or take ~10 m, or inside TUNNEL_EXTRACT_BBOX when below ground, z < 0: tunnel samples)
//   sim/out/<scenario>.meta.json   scenario, demand, duration_s, seed, sumo_version, vehicles (= distinct vehicles;
//                                  a teleport splits one vehicle's replay into trips "<id>", "<id>#1", …),
//                                  teleports, tunnel_vehicles (vehicles on OSM tunnel-tagged ways, excluding
//                                  building_passage: CLAUDE.md), generated_at
// Errors mean the files break the contract (stop and report). Warnings are worth a look but don't block. The same
// rules as qa-reviewer's tests/contracts/test_trips_contract.py.
// The labelled test fixture (tests/fixtures/trips_fixture.*) is the one allowed exception: demand "test_fixture",
// `_test_fixture: true` and `_notes` in its meta, and "fixture-" vehicle ids.

import { type BBox, bboxContainsPoint, SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX } from "../config/bbox";
import { PLAUSIBLE_Z_M } from "./roadsContract";

export type TripPosition = [number, number, number];

export interface Trip {
  id: string;
  type: string;
  path: TripPosition[];
  timestamps: number[];
}

/** Demand labels. `test_fixture` is only valid in the labelled fixture (with `_test_fixture: true`). */
export const DEMANDS = ["placeholder_random", "calibrated_counts"] as const;
export type Demand = (typeof DEMANDS)[number] | "test_fixture";

export interface TripsMeta {
  scenario: string;
  demand: Demand;
  duration_s: number;
  seed: number;
  sumo_version: string;
  vehicles: number;
  teleports: number;
  tunnel_vehicles: number;
  generated_at: string;
  /** Optional: wall-clock time of sim clock 0, ISO 8601 with an offset ("2026-10-23T06:30:00+11:00"). Baseline has none. */
  start_local?: string;
  /** Test fixture only. */
  _test_fixture?: true;
  _notes?: string[];
}

export const TRIP_KEYS = ["id", "type", "path", "timestamps"] as const;
export const META_KEYS = [
  "scenario",
  "demand",
  "duration_s",
  "seed",
  "sumo_version",
  "vehicles",
  "teleports",
  "tunnel_vehicles",
  "generated_at",
] as const;
/** Optional meta members any scenario may carry (contract addition 2026-10-27, PLAN.md). */
export const OPTIONAL_META_KEYS = ["start_local"] as const;
/** Extra meta members the labelled test fixture may carry (and nothing else may). */
export const FIXTURE_META_KEYS = ["_test_fixture", "_notes"] as const;
/** Fixture vehicle ids start with this. */
export const FIXTURE_ID_PREFIX = "fixture-";

/** A car below this z (m) is in a tunnel: drawn X-ray style, behind the "Show tunnels" toggle. */
export const TUNNEL_Z_M = -0.5;

export interface TripsValidation {
  ok: boolean;
  errors: string[];
  warnings: string[];
  tripCount: number;
}

const MAX_MESSAGES = 50;
const isObject = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);
const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);
const isNonNegInt = (v: unknown): v is number => Number.isInteger(v) && (v as number) >= 0;
const grow = (b: BBox, d: number): BBox => ({ west: b.west - d, south: b.south - d, east: b.east + d, north: b.north + d });
/** Cars drive on roads_3d ways, which loop out of the road extract by at most ~5 m: allow ~10 m (1e-4 deg). */
export const TRIPS_BBOX_SLACK_DEG = 1e-4;
const TRIPS_BBOX = grow(SIM_EXTRACT_BBOX, TRIPS_BBOX_SLACK_DEG);
/**
 * CityLink motorway tunnel samples may reach TUNNEL_EXTRACT_BBOX (the Burnley tunnel east of the sim area). Strict
 * and cheap: outside TRIPS_BBOX a sample is accepted only inside this box and below ground (z < 0: the real baseline has a portal sample at z -0.3, shallower than TUNNEL_Z_M).
 */
const TRIPS_TUNNEL_BBOX = grow(TUNNEL_EXTRACT_BBOX, TRIPS_BBOX_SLACK_DEG);
/** A trip may end this far past meta.duration_s (s): the last FCD sample of the run. */
export const DURATION_SLACK_S = 0.5;
/** ISO 8601 date-time with a timezone (Z or ±hh:mm). */
const ISO_DATETIME = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:\d{2})$/;

/**
 * Validate a parsed trips_<scenario>.json. `isFixture` allows (and requires) "fixture-" ids; `durationS` (from a valid
 * meta) bounds the timestamps.
 */
export function validateTrips(
  data: unknown,
  { isFixture = false, durationS }: { isFixture?: boolean; durationS?: number } = {},
): TripsValidation {
  const errors: string[] = [];
  const warnings: string[] = [];
  const done = (tripCount: number): TripsValidation => ({
    ok: errors.length === 0,
    errors: errors.slice(0, MAX_MESSAGES),
    warnings: warnings.slice(0, MAX_MESSAGES),
    tripCount,
  });
  if (!Array.isArray(data)) {
    errors.push("top level is not an array of trips");
    return done(0);
  }
  const ids = new Set<string>();
  const expected = [...TRIP_KEYS].sort().join(", ");
  data.forEach((t: unknown, i: number) => {
    if (errors.length > MAX_MESSAGES) return;
    const where = `trip ${i}`;
    if (!isObject(t)) {
      errors.push(`${where}: not an object`);
      return;
    }
    const keys = Object.keys(t).sort();
    if (keys.join(", ") !== expected) errors.push(`${where}: keys are [${keys.join(", ")}], expected exactly [${expected}]`);
    const id = t.id;
    if (typeof id !== "string" || id === "") errors.push(`${where}: id is not a non-empty string`);
    else {
      if (ids.has(id)) errors.push(`${where}: duplicate id ${JSON.stringify(id)}`);
      ids.add(id);
      if (isFixture !== id.startsWith(FIXTURE_ID_PREFIX)) {
        errors.push(
          isFixture
            ? `${where}: fixture trip id ${JSON.stringify(id)} does not start with "${FIXTURE_ID_PREFIX}"`
            : `${where}: id ${JSON.stringify(id)} looks like test fixture data ("${FIXTURE_ID_PREFIX}" prefix)`,
        );
      }
    }
    if (typeof t.type !== "string" || t.type === "") errors.push(`${where}: type is not a non-empty string`);

    const path = t.path;
    const ts = t.timestamps;
    if (!Array.isArray(path)) {
      errors.push(`${where}: path is not an array`);
      return;
    }
    if (!Array.isArray(ts)) {
      errors.push(`${where}: timestamps is not an array`);
      return;
    }
    if (path.length < 2) errors.push(`${where}: path has fewer than 2 positions`);
    if (path.length !== ts.length) errors.push(`${where}: path has ${path.length} positions but timestamps has ${ts.length}`);

    let malformed = 0;
    let outside = 0;
    let firstOutside = "";
    const badZ: number[] = [];
    for (const p of path) {
      if (!Array.isArray(p) || p.length !== 3 || !p.every(isNum)) {
        malformed++;
        continue;
      }
      const [lon, lat, z] = p as TripPosition;
      if (!bboxContainsPoint(TRIPS_BBOX, lon, lat) && !(z < 0 && bboxContainsPoint(TRIPS_TUNNEL_BBOX, lon, lat))) {
        if (!outside) firstOutside = `[${lon}, ${lat}]`;
        outside++;
      }
      if (z < PLAUSIBLE_Z_M.min || z > PLAUSIBLE_Z_M.max) badZ.push(z);
    }
    const n = path.length;
    if (malformed) errors.push(`${where}: ${malformed} of ${n} positions are not [lon, lat, z_m] with finite numbers`);
    if (outside) errors.push(`${where}: ${outside} of ${n} positions outside the road extract, first ${firstOutside} (lon/lat swapped?)`);
    if (badZ.length) {
      warnings.push(`${where}: ${badZ.length} of ${n} z values outside ${PLAUSIBLE_Z_M.min}..${PLAUSIBLE_Z_M.max} m (min ${Math.min(...badZ)}, max ${Math.max(...badZ)})`);
    }

    if (!ts.every(isNum)) errors.push(`${where}: timestamps are not all finite numbers`);
    else {
      const t = ts as number[];
      if (t.some((s) => s < 0)) errors.push(`${where}: negative timestamp (seconds from scenario start)`);
      const bad = t.findIndex((s, k) => k > 0 && s <= t[k - 1]!);
      if (bad > 0) errors.push(`${where}: timestamps not strictly increasing at index ${bad} (${t[bad - 1]} then ${t[bad]})`);
      const last = t.at(-1);
      if (durationS !== undefined && last !== undefined && last > durationS + DURATION_SLACK_S) {
        errors.push(`${where}: last timestamp ${last} is past meta.duration_s ${durationS}`);
      }
    }
  });
  return done(data.length);
}

/** Validate a parsed <scenario>.meta.json. `isFixture` allows (and requires) the fixture labels. */
export function validateTripsMeta(data: unknown, { isFixture = false }: { isFixture?: boolean } = {}): TripsValidation {
  const errors: string[] = [];
  const warnings: string[] = [];
  const done = (): TripsValidation => ({ ok: errors.length === 0, errors, warnings, tripCount: 0 });
  if (!isObject(data)) {
    errors.push("meta is not a JSON object");
    return done();
  }
  const keys = Object.keys(data);
  const allowed = new Set<string>([...META_KEYS, ...OPTIONAL_META_KEYS, ...(isFixture ? FIXTURE_META_KEYS : [])]);
  const missing = META_KEYS.filter((k) => !(k in data));
  const extra = keys.filter((k) => !allowed.has(k));
  if (missing.length) errors.push(`meta: missing [${missing.join(", ")}]`);
  if (extra.length) errors.push(`meta: unexpected [${extra.join(", ")}]`);

  if (typeof data.scenario !== "string" || !/^[a-z][a-z0-9_]*$/.test(data.scenario)) errors.push("meta: scenario is not a lowercase snake_case name");
  if (isFixture) {
    if (data.demand !== "test_fixture") errors.push('meta: the trips fixture must have demand "test_fixture"');
    if (data._test_fixture !== true) errors.push("meta: the trips fixture must have _test_fixture: true");
  } else if (!(DEMANDS as readonly unknown[]).includes(data.demand)) {
    errors.push(`meta: demand ${JSON.stringify(data.demand)} is not one of ${DEMANDS.join(", ")}`);
  }
  if (!isNum(data.duration_s) || data.duration_s <= 0) errors.push("meta: duration_s is not a positive number");
  if (!Number.isInteger(data.seed)) errors.push("meta: seed is not an integer");
  if (typeof data.sumo_version !== "string" || data.sumo_version === "") errors.push("meta: sumo_version is not a non-empty string");
  for (const k of ["vehicles", "teleports", "tunnel_vehicles"] as const) {
    if (!isNonNegInt(data[k])) errors.push(`meta: ${k} is not a non-negative integer`);
  }
  if (typeof data.generated_at !== "string" || !ISO_DATETIME.test(data.generated_at)) {
    errors.push("meta: generated_at is not an ISO 8601 date-time with a timezone");
  }
  if ("start_local" in data && (typeof data.start_local !== "string" || !ISO_DATETIME.test(data.start_local))) {
    errors.push("meta: start_local is not an ISO 8601 date-time with a timezone");
  }
  if (isNonNegInt(data.teleports) && data.teleports > 0) warnings.push(`meta: ${data.teleports} teleports in the run`);
  return done();
}

/**
 * Cross-checks between a valid meta and valid trips. Errors: meta.vehicles must equal the number of distinct
 * vehicles (trip ids with any teleport-split "#N" suffix folded: the qa contract, 2026-10-28), and
 * tunnel_vehicles must lie in [0, vehicles]. Info only: tunnel_vehicles counts vehicles on OSM tunnel-tagged ways
 * (CLAUDE.md), while this counts trips that go below TUNNEL_Z_M, open cuttings included (the web draws X-ray only the
 * below-ground segments on roads_3d tunnel ways, traffic/tunnelWays.ts), so a difference is a note, not a problem.
 */
export function crossCheckTrips(trips: Trip[], meta: TripsMeta): { errors: string[]; info: string[] } {
  const errors: string[] = [];
  const info: string[] = [];
  const vehicles = new Set(trips.map((t) => t.id.replace(/#\d+$/, ""))).size;
  if (meta.vehicles !== vehicles)
    errors.push(`meta.vehicles is ${meta.vehicles} but the file has ${vehicles} distinct vehicles (${trips.length} trips)`);
  if (meta.tunnel_vehicles > meta.vehicles) errors.push(`meta.tunnel_vehicles ${meta.tunnel_vehicles} is more than meta.vehicles ${meta.vehicles}`);
  const below = trips.filter((t) => t.path.some((p) => p[2] < TUNNEL_Z_M)).length;
  if (meta.tunnel_vehicles !== below) {
    info.push(
      `meta.tunnel_vehicles ${meta.tunnel_vehicles} (on OSM tunnel ways); ${below} trips go below z ${TUNNEL_Z_M} m ` +
        "(open cuttings included; only cars on roads_3d tunnel ways are drawn X-ray)",
    );
  }
  return { errors, info };
}

/** True if this meta says the traffic is not calibrated to counts (the "Placeholder traffic" badge). */
export function isPlaceholderDemand(demand: Demand): boolean {
  return demand !== "calibrated_counts";
}
