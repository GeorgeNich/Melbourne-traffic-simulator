import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  assertNoFixtureIn,
  assertRealDataPresent,
  DEFAULT_PATHS,
  fixtureRequested,
  fixtureSelection,
  isTestFixtureText,
  publicDir,
  resolveDataSource,
  scenarioName,
  syncData,
} from "./data-guard.mjs";

const FIXTURE_TEXT = readFileSync(DEFAULT_PATHS.fixturePath, "utf8");
const REAL_TEXT = JSON.stringify({
  type: "FeatureCollection",
  features: [
    {
      type: "Feature",
      properties: { id: "way/1", height_m: 30, levels: null, height_source: "type_default" },
      geometry: { type: "Polygon", coordinates: [[[144.96, -37.82], [144.961, -37.82], [144.961, -37.819], [144.96, -37.82]]] },
    },
  ],
});

const ROADS_TEXT = JSON.stringify({
  type: "FeatureCollection",
  features: [
    {
      type: "Feature",
      properties: { way_id: 1, highway: "primary", layer: 0, bridge: false, tunnel: false, name: "Test Road" },
      geometry: { type: "LineString", coordinates: [[144.96, -37.82, 0], [144.961, -37.82, 0]] },
    },
  ],
});

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "twin-guard-"));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

const TRIPS_FIXTURE_TEXT = readFileSync(DEFAULT_PATHS.tripsFixturePath, "utf8");
const TRIPS_META_FIXTURE_TEXT = readFileSync(DEFAULT_PATHS.tripsFixtureMetaPath, "utf8");
/** A tiny real-shaped trips pair (hand-written, inline). */
const TRIPS_TEXT = JSON.stringify([{ id: "veh0", type: "passenger", path: [[144.96, -37.82, 0], [144.961, -37.82, 0]], timestamps: [0, 8] }]);
/** Stand-ins for the road-speed files: the sync only copies them (the app validates their contents). */
const GEOMETRY_TEXT = JSON.stringify([{ edge_id: "e1", way_id: 1, path: [[144.96, -37.82, 0], [144.961, -37.82, 0]] }]);
const STATS_BYTES = Buffer.from("PAR1 stand-in bytes, not a real parquet file PAR1");
const META_TEXT = JSON.stringify({ scenario: "baseline", demand: "placeholder_random", duration_s: 900, seed: 1, sumo_version: "1.28.0", vehicles: 1, teleports: 0, tunnel_vehicles: 0, generated_at: "2026-10-27T00:00:00Z" });

function paths(withReal: string | null, withRoads: string | null = ROADS_TEXT, trips: { trips?: string; meta?: string } = {}) {
  const realPath = join(dir, "processed", "buildings.geojson");
  const roadsPath = join(dir, "processed", "roads_3d.geojson");
  const simOutDir = join(dir, "sim", "out");
  mkdirSync(join(dir, "processed"), { recursive: true });
  rmSync(simOutDir, { recursive: true, force: true });
  mkdirSync(simOutDir, { recursive: true });
  if (withReal !== null) writeFileSync(realPath, withReal);
  if (withRoads !== null) writeFileSync(roadsPath, withRoads);
  if (trips.trips !== undefined) writeFileSync(join(simOutDir, "trips_baseline.json"), trips.trips);
  if (trips.meta !== undefined) writeFileSync(join(simOutDir, "baseline.meta.json"), trips.meta);
  return { realPath, roadsPath, simOutDir, fixturePath: DEFAULT_PATHS.fixturePath, outDir: join(dir, "public", "data") };
}

const manifest = (outDir: string) => JSON.parse(readFileSync(join(outDir, "traffic.json"), "utf8")) as Record<string, unknown>;
const scenariosManifest = (outDir: string) => JSON.parse(readFileSync(join(outDir, "scenarios.json"), "utf8")) as Record<string, unknown>;

describe("isTestFixtureText", () => {
  it("flags the fixture and leaves real-shaped data alone", () => {
    expect(isTestFixtureText(FIXTURE_TEXT)).toBe(true);
    expect(isTestFixtureText(REAL_TEXT)).toBe(false);
    expect(isTestFixtureText(TRIPS_FIXTURE_TEXT)).toBe(true);
    expect(isTestFixtureText(TRIPS_META_FIXTURE_TEXT)).toBe(true);
    expect(isTestFixtureText(TRIPS_TEXT)).toBe(false);
    expect(isTestFixtureText(META_TEXT)).toBe(false);
    // Fixture trips are caught by their ids even with the label stripped from the meta.
    expect(isTestFixtureText('[{"id": "fixture-x", "type": "passenger"}]')).toBe(true);
    expect(isTestFixtureText('{"features":[{"properties":{"height_source": "test_fixture"}}]}')).toBe(true);
  });
});

describe("resolveDataSource", () => {
  it("uses real data whenever it exists", () => {
    expect(resolveDataSource({ mode: "dev", realExists: true })).toBe("real");
    expect(resolveDataSource({ mode: "build", realExists: true })).toBe("real");
  });
  it("never falls back to the fixture by default: a missing real file is an error in dev and build", () => {
    expect(() => resolveDataSource({ mode: "dev", realExists: false })).toThrow(/missing/);
    expect(() => resolveDataSource({ mode: "build", realExists: false })).toThrow(/Build refused/);
  });
  it("serves the fixture only with the explicit opt-in, and only in dev", () => {
    expect(resolveDataSource({ mode: "dev", realExists: true, useFixture: true })).toBe("fixture");
    expect(resolveDataSource({ mode: "dev", realExists: false, useFixture: true })).toBe("fixture");
    expect(() => resolveDataSource({ mode: "build", realExists: true, useFixture: true })).toThrow(/Build refused/);
  });
  it("reads the opt-in and public dir from the environment", () => {
    expect(fixtureRequested({})).toBe(false);
    expect(fixtureRequested({ TWIN_USE_FIXTURE: "0" })).toBe(false);
    expect(fixtureRequested({ TWIN_USE_FIXTURE: "1" })).toBe(true);
    expect(fixtureRequested({ TWIN_USE_FIXTURE: "trips" })).toBe(true);
    expect(fixtureSelection({})).toEqual({ buildings: false, trips: false });
    expect(fixtureSelection({ TWIN_USE_FIXTURE: "0" })).toEqual({ buildings: false, trips: false });
    expect(fixtureSelection({ TWIN_USE_FIXTURE: "1" })).toEqual({ buildings: true, trips: true });
    expect(fixtureSelection({ TWIN_USE_FIXTURE: "all" })).toEqual({ buildings: true, trips: true });
    expect(fixtureSelection({ TWIN_USE_FIXTURE: "trips" })).toEqual({ buildings: false, trips: true });
    expect(fixtureSelection({ TWIN_USE_FIXTURE: "buildings" })).toEqual({ buildings: true, trips: false });
    expect(fixtureSelection({ TWIN_USE_FIXTURE: "trips,buildings" })).toEqual({ buildings: true, trips: true });
    expect(() => fixtureSelection({ TWIN_USE_FIXTURE: "yes" })).toThrow(/expected 1, all, trips or buildings/);
    expect(scenarioName({})).toBeUndefined(); // the first scenario with files (SCENARIOS order)
    expect(scenarioName({ TWIN_SCENARIO: "am_peak" })).toBe("am_peak");
    expect(() => scenarioName({ TWIN_SCENARIO: "../x" })).toThrow(/snake_case/);
    expect(publicDir({})).toMatch(/web[\\/]public$/);
    expect(publicDir({ TWIN_PUBLIC_DIR: "public-fixture" })).toMatch(/web[\\/]public-fixture$/);
  });
  it("refuses a 'real' file that is marked as test data, in any mode", () => {
    expect(() => resolveDataSource({ mode: "dev", realExists: true, realIsFixture: true })).toThrow(/test fixture/);
    expect(() => resolveDataSource({ mode: "build", realExists: true, realIsFixture: true })).toThrow(/test fixture/);
  });
});

describe("syncData + build guard", () => {
  it("build fails when the real file is absent, and leaves no data behind", () => {
    const p = paths(null);
    expect(() => syncData({ mode: "build", ...p })).toThrow(/Build refused/);
    expect(() => assertRealDataPresent(p.outDir)).toThrow(/Build refused/);
  });

  it("build fails when the real file carries _test_fixture", () => {
    const p = paths(FIXTURE_TEXT);
    expect(() => syncData({ mode: "build", ...p })).toThrow(/test fixture/);
  });

  it("build copies the real file and the guard passes", () => {
    const p = paths(REAL_TEXT);
    expect(syncData({ mode: "build", ...p }).source).toBe("real");
    expect(readFileSync(join(p.outDir, "buildings.geojson"), "utf8")).toBe(REAL_TEXT);
    expect(readFileSync(join(p.outDir, "roads_3d.geojson"), "utf8")).toBe(ROADS_TEXT);
    expect(() => assertRealDataPresent(p.outDir)).not.toThrow();
    expect(assertNoFixtureIn(resolve(p.outDir, ".."))).toHaveLength(4); // + the traffic and scenarios manifests
  });

  it("dev without real data fails unless the fixture is requested", () => {
    const p = paths(null);
    expect(() => syncData({ mode: "dev", ...p })).toThrow(/missing/);
  });

  it("dev with the opt-in serves the fixture, which the build guard then refuses", () => {
    const p = paths(REAL_TEXT);
    expect(syncData({ mode: "dev", useFixture: true, ...p }).source).toBe("fixture");
    expect(readFileSync(join(p.outDir, "buildings.geojson"), "utf8")).toBe(FIXTURE_TEXT);
    expect(() => assertNoFixtureIn(resolve(p.outDir, ".."))).toThrow(/Build refused/);
  });

  it("the build guard scans every file, whatever its name or extension", () => {
    const p = paths(REAL_TEXT);
    syncData({ mode: "build", ...p });
    const pub = resolve(p.outDir, "..");
    writeFileSync(join(pub, "notes.txt"), "plain text");
    expect(assertNoFixtureIn(pub)).toHaveLength(5);
    mkdirSync(join(pub, "assets"));
    writeFileSync(join(pub, "assets", "renamed.bin"), FIXTURE_TEXT);
    expect(() => assertNoFixtureIn(pub)).toThrow(/renamed\.bin is test fixture data/);
  });

  it("dev uses real data when present", () => {
    const p = paths(REAL_TEXT);
    expect(syncData({ mode: "dev", ...p }).source).toBe("real");
  });

  it("roads_3d.geojson is required in every mode and leaves no data behind when missing", () => {
    for (const mode of ["dev", "build"] as const) {
      const p = paths(REAL_TEXT, null);
      expect(() => syncData({ mode, ...p })).toThrow(/roads_3d\.geojson is missing/);
      expect(() => assertRealDataPresent(p.outDir)).toThrow(/Build refused/);
    }
  });

  it("refuses a roads_3d.geojson marked as test data", () => {
    const p = paths(REAL_TEXT, '{"_test_fixture": true, "features": []}');
    expect(() => syncData({ mode: "dev", ...p })).toThrow(/test fixture/);
  });

  it("the fixture server gets the real roads beside the fixture buildings", () => {
    const p = paths(REAL_TEXT);
    expect(syncData({ mode: "dev", useFixture: true, ...p }).source).toBe("fixture");
    expect(readFileSync(join(p.outDir, "roads_3d.geojson"), "utf8")).toBe(ROADS_TEXT);
  });
});

describe("traffic sync (sim/out trips + meta)", () => {
  it("no trips yet: the app gets a manifest with no traffic, in dev and build", () => {
    for (const mode of ["dev", "build"] as const) {
      const p = paths(REAL_TEXT);
      expect(syncData({ mode, ...p }).traffic.source).toBe("none");
      expect(manifest(p.outDir)).toEqual({ scenario: "baseline", source: "none", trips: null, meta: null });
      expect(() => assertRealDataPresent(p.outDir)).not.toThrow();
    }
  });

  it("copies the real trips and meta and lists them in the manifest; the build guard passes", () => {
    const p = paths(REAL_TEXT, ROADS_TEXT, { trips: TRIPS_TEXT, meta: META_TEXT });
    expect(syncData({ mode: "build", ...p }).traffic.source).toBe("real");
    expect(manifest(p.outDir)).toEqual({ scenario: "baseline", source: "real", trips: "trips_baseline.json", meta: "baseline.meta.json" });
    expect(readFileSync(join(p.outDir, "trips_baseline.json"), "utf8")).toBe(TRIPS_TEXT);
    expect(readFileSync(join(p.outDir, "baseline.meta.json"), "utf8")).toBe(META_TEXT);
    expect(assertNoFixtureIn(resolve(p.outDir, ".."))).toHaveLength(6);
  });

  it("uses the scenario name for the file names", () => {
    const p = paths(REAL_TEXT);
    writeFileSync(join(p.simOutDir, "trips_am_peak.json"), TRIPS_TEXT);
    writeFileSync(join(p.simOutDir, "am_peak.meta.json"), META_TEXT);
    expect(syncData({ mode: "dev", scenario: "am_peak", ...p }).traffic.source).toBe("real");
    expect(manifest(p.outDir)).toMatchObject({ scenario: "am_peak", trips: "trips_am_peak.json", meta: "am_peak.meta.json" });
  });

  it("half a pair (trips without meta, or meta without trips) is an error and leaves no data behind", () => {
    for (const half of [{ trips: TRIPS_TEXT }, { meta: META_TEXT }]) {
      const p = paths(REAL_TEXT, ROADS_TEXT, half);
      expect(() => syncData({ mode: "dev", ...p })).toThrow(/needs both/);
      expect(() => assertRealDataPresent(p.outDir)).toThrow(/Build refused/);
    }
  });

  it("refuses sim/out files marked as test data", () => {
    const p = paths(REAL_TEXT, ROADS_TEXT, { trips: TRIPS_FIXTURE_TEXT, meta: META_TEXT });
    expect(() => syncData({ mode: "dev", ...p })).toThrow(/test fixture/);
  });

  it("syncs the scenario's road speeds (edge stats + edge geometry) beside real traffic and lists them", () => {
    const p = paths(REAL_TEXT, ROADS_TEXT, { trips: TRIPS_TEXT, meta: META_TEXT });
    writeFileSync(join(p.simOutDir, "edge_geometry.json"), GEOMETRY_TEXT);
    writeFileSync(join(p.simOutDir, "edge_stats_baseline.parquet"), STATS_BYTES);
    writeFileSync(join(p.simOutDir, "edge_stats_am_peak.parquet"), STATS_BYTES);
    expect(syncData({ mode: "build", ...p }).speeds).not.toBeNull();
    expect(manifest(p.outDir)).toEqual({
      scenario: "baseline",
      source: "real",
      trips: "trips_baseline.json",
      meta: "baseline.meta.json",
      speeds: { edgeGeometry: "edge_geometry.json", edgeStats: "edge_stats_baseline.parquet" },
    });
    expect(readFileSync(join(p.outDir, "edge_stats_baseline.parquet"))).toEqual(STATS_BYTES);
    expect(existsSync(join(p.outDir, "edge_stats_am_peak.parquet"))).toBe(false); // only the scenario being replayed
    expect(assertNoFixtureIn(resolve(p.outDir, ".."))).toHaveLength(8);
  });

  it("no edge stats for the scenario: no road speeds (geometry alone is not enough)", () => {
    const p = paths(REAL_TEXT, ROADS_TEXT, { trips: TRIPS_TEXT, meta: META_TEXT });
    writeFileSync(join(p.simOutDir, "edge_geometry.json"), GEOMETRY_TEXT);
    expect(syncData({ mode: "dev", ...p }).speeds).toBeNull();
    expect(manifest(p.outDir)).not.toHaveProperty("speeds");
  });

  it("edge stats without edge_geometry.json is an error and leaves no data behind", () => {
    const p = paths(REAL_TEXT, ROADS_TEXT, { trips: TRIPS_TEXT, meta: META_TEXT });
    writeFileSync(join(p.simOutDir, "edge_stats_baseline.parquet"), STATS_BYTES);
    expect(() => syncData({ mode: "dev", ...p })).toThrow(/no edge_geometry\.json/);
    expect(() => assertRealDataPresent(p.outDir)).toThrow(/Build refused/);
  });

  it("fixture trips never get real road speeds", () => {
    const p = paths(REAL_TEXT, ROADS_TEXT, { trips: TRIPS_TEXT, meta: META_TEXT });
    writeFileSync(join(p.simOutDir, "edge_geometry.json"), GEOMETRY_TEXT);
    writeFileSync(join(p.simOutDir, "edge_stats_baseline.parquet"), STATS_BYTES);
    expect(syncData({ mode: "dev", useTripsFixture: true, ...p }).speeds).toBeNull();
    expect(manifest(p.outDir)).not.toHaveProperty("speeds");
  });

  it("serves the trips fixture only with the opt-in, in dev, and the build guard then refuses it", () => {
    const p = paths(REAL_TEXT, ROADS_TEXT, { trips: TRIPS_TEXT, meta: META_TEXT });
    const r = syncData({ mode: "dev", useTripsFixture: true, ...p });
    expect(r.source).toBe("real"); // buildings stay real with TWIN_USE_FIXTURE=trips
    expect(r.traffic.source).toBe("fixture");
    expect(manifest(p.outDir)).toEqual({ scenario: "fixture", source: "fixture", trips: "trips_fixture.json", meta: "trips_fixture.meta.json" });
    expect(readFileSync(join(p.outDir, "trips_fixture.json"), "utf8")).toBe(TRIPS_FIXTURE_TEXT);
    expect(() => assertNoFixtureIn(resolve(p.outDir, ".."))).toThrow(/Build refused/);
    expect(() => syncData({ mode: "build", useTripsFixture: true, ...p })).toThrow(/Build refused/);
  });
});

describe("scenario switch (am_peak + baseline)", () => {
  const AM_META = META_TEXT.replace('"baseline"', '"am_peak"').replace('"placeholder_random"', '"calibrated_counts"');
  const withAmPeak = (p: ReturnType<typeof paths>, stats = true) => {
    writeFileSync(join(p.simOutDir, "trips_am_peak.json"), TRIPS_TEXT);
    writeFileSync(join(p.simOutDir, "am_peak.meta.json"), AM_META);
    writeFileSync(join(p.simOutDir, "edge_geometry.json"), GEOMETRY_TEXT);
    writeFileSync(join(p.simOutDir, "edge_stats_baseline.parquet"), STATS_BYTES);
    if (stats) writeFileSync(join(p.simOutDir, "edge_stats_am_peak.parquet"), STATS_BYTES);
  };
  const entry = (scenario: string, speeds = true) => ({
    scenario,
    source: "real",
    trips: `trips_${scenario}.json`,
    meta: `${scenario}.meta.json`,
    ...(speeds ? { speeds: { edgeGeometry: "edge_geometry.json", edgeStats: `edge_stats_${scenario}.parquet` } } : {}),
  });

  it("both present: am_peak is the default (traffic.json); scenarios.json lists both, each with its files", () => {
    const p = paths(REAL_TEXT, ROADS_TEXT, { trips: TRIPS_TEXT, meta: META_TEXT });
    withAmPeak(p);
    const r = syncData({ mode: "build", ...p });
    expect(r.traffic.scenario).toBe("am_peak");
    expect(manifest(p.outDir)).toEqual(entry("am_peak"));
    expect(scenariosManifest(p.outDir)).toEqual({ default: "am_peak", scenarios: [entry("am_peak"), entry("baseline")] });
    for (const f of ["trips_am_peak.json", "am_peak.meta.json", "edge_stats_am_peak.parquet", "trips_baseline.json", "edge_stats_baseline.parquet"]) {
      expect(existsSync(join(p.outDir, f)), f).toBe(true);
    }
    expect(() => assertRealDataPresent(p.outDir)).not.toThrow();
  });

  it("am_peak missing: baseline is the default and the only scenario (no switch); the app still works", () => {
    const p = paths(REAL_TEXT, ROADS_TEXT, { trips: TRIPS_TEXT, meta: META_TEXT });
    expect(syncData({ mode: "dev", ...p }).traffic.scenario).toBe("baseline");
    expect(scenariosManifest(p.outDir)).toEqual({ default: "baseline", scenarios: [entry("baseline", false)] });
  });

  it("am_peak without edge stats: listed without road speeds", () => {
    const p = paths(REAL_TEXT, ROADS_TEXT, { trips: TRIPS_TEXT, meta: META_TEXT });
    withAmPeak(p, false);
    syncData({ mode: "dev", ...p });
    expect(scenariosManifest(p.outDir)).toMatchObject({ scenarios: [entry("am_peak", false), entry("baseline")] });
  });

  it("TWIN_SCENARIO picks the default; the others stay switchable", () => {
    const p = paths(REAL_TEXT, ROADS_TEXT, { trips: TRIPS_TEXT, meta: META_TEXT });
    withAmPeak(p);
    expect(syncData({ mode: "dev", scenario: "baseline", ...p }).traffic.scenario).toBe("baseline");
    expect(manifest(p.outDir)).toEqual(entry("baseline"));
    expect(scenariosManifest(p.outDir)).toEqual({ default: "baseline", scenarios: [entry("baseline"), entry("am_peak")] });
  });

  it("half an am_peak pair is an error, like baseline", () => {
    const p = paths(REAL_TEXT, ROADS_TEXT, { trips: TRIPS_TEXT, meta: META_TEXT });
    writeFileSync(join(p.simOutDir, "trips_am_peak.json"), TRIPS_TEXT);
    expect(() => syncData({ mode: "dev", ...p })).toThrow(/needs both/);
  });

  it("no traffic at all: an empty switch; fixture trips: only the fixture", () => {
    const p = paths(REAL_TEXT);
    syncData({ mode: "dev", ...p });
    expect(scenariosManifest(p.outDir)).toEqual({ default: "baseline", scenarios: [] });
    const q = paths(REAL_TEXT, ROADS_TEXT, { trips: TRIPS_TEXT, meta: META_TEXT });
    withAmPeak(q);
    syncData({ mode: "dev", useTripsFixture: true, ...q });
    expect(scenariosManifest(q.outDir)).toMatchObject({ default: "fixture", scenarios: [{ scenario: "fixture", source: "fixture" }] });
  });
});
