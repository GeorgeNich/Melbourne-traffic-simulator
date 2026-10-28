import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { crossCheckTrips, isPlaceholderDemand, type Trip, type TripsMeta, validateTrips, validateTripsMeta } from "./tripsContract";

// Small hand-written trips (test-only, inline) on Flinders St.
const trip = (over: Partial<Trip> = {}): Trip => ({
  id: "veh0",
  type: "passenger",
  path: [
    [144.96, -37.818, 0],
    [144.961, -37.8177, 0],
  ],
  timestamps: [0, 9],
  ...over,
});
const meta = (over: Partial<TripsMeta> = {}): TripsMeta => ({
  scenario: "baseline",
  demand: "placeholder_random",
  duration_s: 900,
  seed: 42,
  sumo_version: "1.28.0",
  vehicles: 1,
  teleports: 0,
  tunnel_vehicles: 0,
  generated_at: "2026-10-27T09:00:00+11:00",
  ...over,
});

const FIXTURE_DIR = resolve(import.meta.dirname, "../../../tests/fixtures");
const fixtureTrips = JSON.parse(readFileSync(resolve(FIXTURE_DIR, "trips_fixture.json"), "utf8")) as Trip[];
const fixtureMeta = JSON.parse(readFileSync(resolve(FIXTURE_DIR, "trips_fixture.meta.json"), "utf8")) as TripsMeta;

describe("validateTrips", () => {
  it("accepts contract-shaped trips", () => {
    const v = validateTrips([trip(), trip({ id: "veh1" })]);
    expect(v.errors).toEqual([]);
    expect(v.ok).toBe(true);
    expect(v.tripCount).toBe(2);
  });

  it("rejects a non-array, missing or extra keys, and duplicate ids", () => {
    expect(validateTrips({ trips: [] }).errors[0]).toMatch(/not an array/);
    const noType: Partial<Trip> = trip();
    delete noType.type;
    expect(validateTrips([noType]).errors.join()).toMatch(/expected exactly \[id, path, timestamps, type\]/);
    expect(validateTrips([{ ...trip(), speed: 3 }]).errors.join()).toMatch(/keys are/);
    expect(validateTrips([trip(), trip()]).errors.join()).toMatch(/duplicate id "veh0"/);
  });

  it("rejects 2D positions, swapped lon/lat and mismatched timestamps", () => {
    const flat = trip({ path: [[144.96, -37.818], [144.961, -37.8177]] as unknown as Trip["path"] });
    expect(validateTrips([flat]).errors.join()).toMatch(/not \[lon, lat, z_m\]/);
    const swapped = trip({ path: [[-37.818, 144.96, 0], [-37.8177, 144.961, 0]] });
    expect(validateTrips([swapped]).errors.join()).toMatch(/lon\/lat swapped\?/);
    expect(validateTrips([trip({ timestamps: [0] })]).errors.join()).toMatch(/2 positions but timestamps has 1/);
  });

  it("rejects timestamps that are not strictly increasing, below zero, or past duration_s", () => {
    const path = [...trip().path, [144.962, -37.8174, 0] as [number, number, number]];
    expect(validateTrips([trip({ path, timestamps: [0, 9, 5] })]).errors.join()).toMatch(/not strictly increasing at index 2/);
    // Equal consecutive timestamps break the contract too (a stopped car still gets a new time per sample).
    expect(validateTrips([trip({ path, timestamps: [0, 9, 9] })]).errors.join()).toMatch(/not strictly increasing at index 2/);
    expect(validateTrips([trip({ timestamps: [-1, 9] })]).errors.join()).toMatch(/negative timestamp/);
    expect(validateTrips([trip({ timestamps: [0, 900.5] })], { durationS: 900 }).ok).toBe(true); // last sample slack
    expect(validateTrips([trip({ timestamps: [0, 901] })], { durationS: 900 }).errors.join()).toMatch(/past meta.duration_s 900/);
  });

  it("allows underground samples out to TUNNEL_EXTRACT_BBOX (Burnley tunnel), not surface ones", () => {
    const under = trip({ path: [[144.98975, -37.8255, -25], [144.978, -37.8255, -20]] });
    const portal = trip({ path: [[144.984237, -37.8264, -0.3], [144.978, -37.8255, -20]] });
    expect(validateTrips([portal]).errors).toEqual([]);
    const surface = trip({ path: [[144.98975, -37.8255, 0], [144.978, -37.8255, 0]] });
    const past = trip({ path: [[144.9915, -37.8255, -25], [144.978, -37.8255, -20]] });
    expect(validateTrips([under]).errors).toEqual([]);
    expect(validateTrips([surface]).errors.join()).toMatch(/outside the road extract/);
    expect(validateTrips([past]).errors.join()).toMatch(/outside the road extract/);
  });

  it("allows ~10 m outside the road extract, not more", () => {
    // SIM_EXTRACT_BBOX.east is 144.9784115: +8 m is fine, +30 m is not.
    const near = trip({ path: [[144.9784115 + 0.00009, -37.818, 0], [144.978, -37.818, 0]] });
    const far = trip({ path: [[144.9784115 + 0.00035, -37.818, 0], [144.978, -37.818, 0]] });
    expect(validateTrips([near]).ok).toBe(true);
    expect(validateTrips([far]).errors.join()).toMatch(/outside the road extract/);
  });

  it("warns (not errors) on implausible heights", () => {
    const v = validateTrips([trip({ path: [[144.96, -37.818, 0], [144.961, -37.8177, 120]] })]);
    expect(v.ok).toBe(true);
    expect(v.warnings.join()).toMatch(/z values outside/);
  });

  it("keeps fixture ids and real ids apart", () => {
    expect(validateTrips([trip({ id: "fixture-x" })]).errors.join()).toMatch(/looks like test fixture data/);
    expect(validateTrips([trip()], { isFixture: true }).errors.join()).toMatch(/does not start with "fixture-"/);
  });
});

describe("validateTripsMeta", () => {
  it("accepts contract-shaped meta for both demand labels", () => {
    expect(validateTripsMeta(meta()).errors).toEqual([]);
    expect(validateTripsMeta(meta({ demand: "calibrated_counts", generated_at: "2026-10-27T00:00:00Z" })).ok).toBe(true);
  });

  it("rejects unknown demand, missing or extra keys, bad numbers and dates", () => {
    expect(validateTripsMeta(meta({ demand: "made_up" as TripsMeta["demand"] })).errors.join()).toMatch(/demand "made_up"/);
    const noSeed: Partial<TripsMeta> = meta();
    delete noSeed.seed;
    expect(validateTripsMeta(noSeed).errors.join()).toMatch(/missing \[seed\]/);
    expect(validateTripsMeta({ ...meta(), extra: 1 }).errors.join()).toMatch(/unexpected \[extra\]/);
    expect(validateTripsMeta(meta({ vehicles: -1 })).errors.join()).toMatch(/vehicles is not a non-negative integer/);
    expect(validateTripsMeta(meta({ duration_s: 0 })).errors.join()).toMatch(/duration_s/);
    expect(validateTripsMeta(meta({ generated_at: "yesterday" })).errors.join()).toMatch(/ISO 8601/);
    expect(validateTripsMeta(meta({ scenario: "AM Peak" })).errors.join()).toMatch(/snake_case/);
  });

  it("allows the test_fixture demand and labels only in the fixture", () => {
    const f = meta({ demand: "test_fixture", _test_fixture: true, _notes: ["hand-made"] });
    expect(validateTripsMeta(f).ok).toBe(false);
    expect(validateTripsMeta(f, { isFixture: true }).errors).toEqual([]);
    expect(validateTripsMeta(meta(), { isFixture: true }).errors.join()).toMatch(/must have demand "test_fixture"/);
  });

  it("warns about teleports", () => {
    expect(validateTripsMeta(meta({ teleports: 3 })).warnings.join()).toMatch(/3 teleports/);
  });
});

describe("crossCheckTrips", () => {
  it("errors on a wrong vehicle count or tunnel_vehicles above it", () => {
    expect(crossCheckTrips([trip()], meta())).toEqual({ errors: [], info: [] });
    expect(crossCheckTrips([trip(), trip({ id: "veh1" })], meta()).errors.join()).toMatch(/meta.vehicles is 1 but the file has 2 distinct vehicles/);
    // a teleport-split vehicle ("<id>", "<id>#1", …) counts once (the qa contract, 2026-10-28)
    expect(crossCheckTrips([trip(), trip({ id: `${trip().id}#1` })], meta()).errors).toEqual([]);
    expect(crossCheckTrips([trip()], meta({ tunnel_vehicles: 2 })).errors.join()).toMatch(/tunnel_vehicles 2 is more than meta.vehicles 1/);
  });

  it("only notes (no error, no warning) that tunnel_vehicles counts OSM tunnel ways while X-ray needs z < 0", () => {
    // Stage A/C data: a car on an OSM tunnel way still at z 0 (tunnels not lowered yet).
    const flatTunnel = crossCheckTrips([trip()], meta({ tunnel_vehicles: 1 }));
    expect(flatTunnel.errors).toEqual([]);
    expect(flatTunnel.info.join()).toMatch(/tunnel_vehicles 1 \(on OSM tunnel ways\); 0 trips go below z -0.5 m/);
    const deep = trip({ path: [[144.96, -37.818, -8], [144.961, -37.8177, -9]] });
    expect(crossCheckTrips([deep], meta({ tunnel_vehicles: 1 }))).toEqual({ errors: [], info: [] });
  });
});

describe("the labelled trips fixture (tests/fixtures/trips_fixture.*)", () => {
  it("passes the contract as a fixture, and is refused as real data", () => {
    expect(validateTripsMeta(fixtureMeta, { isFixture: true }).errors).toEqual([]);
    expect(validateTrips(fixtureTrips, { isFixture: true }).errors).toEqual([]);
    expect(crossCheckTrips(fixtureTrips, fixtureMeta)).toEqual({ errors: [], info: [] });
    expect(validateTrips(fixtureTrips, { isFixture: true, durationS: fixtureMeta.duration_s }).errors).toEqual([]);
    expect(validateTripsMeta(fixtureMeta).ok).toBe(false);
    expect(validateTrips(fixtureTrips).ok).toBe(false);
  });

  it("is clearly labelled and sized as asked (20-40 cars over ~5 minutes, one or more tunnel cars, elevated decks)", () => {
    expect(fixtureMeta.demand).toBe("test_fixture");
    expect(fixtureMeta._test_fixture).toBe(true);
    expect(fixtureTrips.length).toBeGreaterThanOrEqual(20);
    expect(fixtureTrips.length).toBeLessThanOrEqual(40);
    expect(fixtureTrips.every((t) => t.id.startsWith("fixture-"))).toBe(true);
    expect(fixtureMeta.duration_s).toBe(300);
    expect(fixtureMeta.tunnel_vehicles).toBeGreaterThanOrEqual(1);
    const maxZ = (id: string) => Math.max(...fixtureTrips.filter((t) => t.id.startsWith(id)).flatMap((t) => t.path.map((p) => p[2])));
    expect(maxZ("fixture-kings-way-")).toBeGreaterThan(9); // Kings Way flyover deck
    expect(maxZ("fixture-bolte-")).toBeGreaterThan(30); // Bolte Bridge
  });

  it("drives at plausible speeds (no segment faster than 30 m/s)", () => {
    const M = 110_574;
    let worst = 0;
    for (const t of fixtureTrips) {
      for (let k = 1; k < t.path.length; k++) {
        const [a, b] = [t.path[k - 1]!, t.path[k]!];
        const d = Math.hypot((b[0] - a[0]) * 111_320 * Math.cos((a[1] * Math.PI) / 180), (b[1] - a[1]) * M);
        const dt = t.timestamps[k]! - t.timestamps[k - 1]!;
        if (dt > 0) worst = Math.max(worst, d / dt);
      }
    }
    expect(worst).toBeLessThan(30);
  });
});

describe("isPlaceholderDemand", () => {
  it("labels placeholder and fixture demand, not calibrated demand", () => {
    expect(isPlaceholderDemand("placeholder_random")).toBe(true);
    expect(isPlaceholderDemand("test_fixture")).toBe(true);
    expect(isPlaceholderDemand("calibrated_counts")).toBe(false);
  });
});

describe("meta.start_local (optional, 2026-10-27)", () => {
  const base = { scenario: "am_peak", demand: "calibrated_counts", duration_s: 5400, seed: 1, sumo_version: "1.28.0", vehicles: 1, teleports: 0, tunnel_vehicles: 0, generated_at: "2026-10-27T00:00:00Z" };
  it("is allowed when an ISO date-time with an offset, and optional", () => {
    expect(validateTripsMeta({ ...base, start_local: "2026-10-23T06:30:00+11:00" }).errors).toEqual([]);
    expect(validateTripsMeta(base).errors).toEqual([]);
  });
  it("is an error without an offset or when not a string", () => {
    expect(validateTripsMeta({ ...base, start_local: "2026-10-23T06:30:00" }).errors.join()).toMatch(/start_local/);
    expect(validateTripsMeta({ ...base, start_local: 0 }).errors.join()).toMatch(/start_local/);
  });
});
