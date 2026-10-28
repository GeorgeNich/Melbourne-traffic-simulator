// Decides which buildings data the app gets, copies the road heights (roads_3d.geojson), the traffic replay
// (sim/out/trips_<scenario>.json + <scenario>.meta.json, when they exist) and its road speeds
// (sim/out/edge_stats_<scenario>.parquet + edge_geometry.json, when they exist) beside it, and stops the test fixtures
// from ever shipping in a build.
//
// Used by scripts/sync-data.mjs (before `dev` / `build`) and by the Vite build guard plugin in vite.config.ts.
// Plain ESM JavaScript so Node runs it without a build step; types come from JSDoc.

import { copyFileSync, existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { basename, dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const WEB_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");

/**
 * The public folder Vite serves and bundles. TWIN_PUBLIC_DIR (relative to web/) lets the fixture e2e server use
 * its own gitignored folder (public-fixture/) so it can run beside a real-data server without either overwriting
 * the other's data.
 * @param {NodeJS.ProcessEnv} [env]
 */
export function publicDir(env = process.env) {
  return resolve(WEB_DIR, env.TWIN_PUBLIC_DIR || "public");
}

/** Default locations, relative to the repo layout in CLAUDE.md. */
export const DEFAULT_PATHS = {
  /** The data-engineer's contract output. */
  realPath: resolve(WEB_DIR, "../data/processed/buildings.geojson"),
  /** Hand-made, labelled test data. Only with TWIN_USE_FIXTURE=1 in dev; never bundled. */
  fixturePath: resolve(WEB_DIR, "../tests/fixtures/buildings_fixture.geojson"),
  /** The data-engineer's 3D road contract output. Real data only: there is no roads fixture file. */
  roadsPath: resolve(WEB_DIR, "../data/processed/roads_3d.geojson"),
  /** The sim-engineer's exported runs: trips_<scenario>.json and <scenario>.meta.json. */
  simOutDir: resolve(WEB_DIR, "../sim/out"),
  /** Hand-made, labelled trips test data (web/scripts/make-trips-fixture.mjs). Only with the opt-in in dev. */
  tripsFixturePath: resolve(WEB_DIR, "../tests/fixtures/trips_fixture.json"),
  tripsFixtureMetaPath: resolve(WEB_DIR, "../tests/fixtures/trips_fixture.meta.json"),
  /** Gitignored; Vite serves it at /data/ in dev and copies it into dist/ on build. */
  outDir: join(publicDir(), "data"),
};

export const OUTPUT_FILENAME = "buildings.geojson";
export const ROADS_FILENAME = "roads_3d.geojson";
/** Small index the app reads first: which traffic files (if any) were synced, and from where. */
export const TRAFFIC_MANIFEST_FILENAME = "traffic.json";
/** Every scenario the app can switch between, in default order: the first one whose files exist is replayed first. */
export const SCENARIOS = ["am_peak", "baseline"];
/** The scenario named in the manifest when no scenario has traffic yet. */
export const DEFAULT_SCENARIO = "baseline";
/** All synced scenarios, for the scenario switch: { default, scenarios: TrafficManifest[] }. traffic.json stays the default's. */
export const SCENARIOS_MANIFEST_FILENAME = "scenarios.json";

/**
 * Which test fixtures the explicit opt-in asks for (tests / fixture e2e / look review only):
 *   TWIN_USE_FIXTURE=1 (or "all")  both: fixture buildings and fixture trips
 *   TWIN_USE_FIXTURE=trips         fixture trips over the real buildings (look review)
 *   TWIN_USE_FIXTURE=buildings     fixture buildings only
 * Unset, empty or "0" = none. Anything else is an error, so a typo never silently serves real data.
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {{ buildings: boolean, trips: boolean }}
 */
export function fixtureSelection(env = process.env) {
  const v = (env.TWIN_USE_FIXTURE ?? "").trim();
  if (v === "" || v === "0") return { buildings: false, trips: false };
  if (v === "1" || v === "all") return { buildings: true, trips: true };
  const parts = v.split(",").map((x) => x.trim());
  const bad = parts.filter((x) => x !== "trips" && x !== "buildings");
  if (bad.length) throw new Error(`TWIN_USE_FIXTURE=${v}: expected 1, all, trips or buildings (comma-separated)`);
  return { buildings: parts.includes("buildings"), trips: parts.includes("trips") };
}

/** True when any fixture opt-in is set (tests / fixture e2e only). Builds refuse it. */
export function fixtureRequested(env = process.env) {
  const v = (env.TWIN_USE_FIXTURE ?? "").trim();
  return v !== "" && v !== "0";
}

/**
 * The scenario to replay first: TWIN_SCENARIO, or undefined (the first of SCENARIOS with files). Lowercase snake_case
 * only (it becomes a file name).
 * @param {NodeJS.ProcessEnv} [env]
 * @returns {string | undefined}
 */
export function scenarioName(env = process.env) {
  const s = env.TWIN_SCENARIO;
  if (!s) return undefined;
  if (!/^[a-z][a-z0-9_]*$/.test(s)) throw new Error(`TWIN_SCENARIO=${s} is not a lowercase snake_case scenario name`);
  return s;
}

/**
 * True if a text is (or contains) test fixture data: the top-level `_test_fixture` foreign member, or any feature
 * whose `height_source` is "test_fixture".
 * @param {string} text
 * @returns {boolean}
 */
export function isTestFixtureText(text) {
  // Deliberately a string check, not a parse: conservative, fast on large files, and it also catches files
  // that fail to parse. A false positive only blocks a build, which is the safe direction.
  // Trips files are a bare array (no room for a `_test_fixture` member), so fixture trips are caught by their
  // "fixture-" vehicle ids.
  return text.includes('"_test_fixture"') || text.includes('"test_fixture"') || /"id"\s*:\s*"fixture-/.test(text);
}

/**
 * Pick the data source. Real data is the only default; the fixture needs the explicit opt-in and never builds.
 * @param {{ mode: "dev" | "build", realExists: boolean, realIsFixture?: boolean, useFixture?: boolean }} input
 * @returns {"real" | "fixture"}
 */
export function resolveDataSource({ mode, realExists, realIsFixture = false, useFixture = false }) {
  if (mode === "build" && useFixture) {
    throw new Error("Build refused: TWIN_USE_FIXTURE=1 is set. The test fixture is never bundled into a build.");
  }
  if (useFixture) return "fixture";
  if (realExists && realIsFixture) {
    throw new Error(
      "data/processed/buildings.geojson is marked as test fixture data (_test_fixture / height_source " +
        '"test_fixture"). Test data must never appear in data/processed/. Stop and report to the orchestrator.',
    );
  }
  if (realExists) return "real";
  throw new Error(
    (mode === "build" ? "Build refused: " : "") +
      "data/processed/buildings.geojson is missing. Regenerate it with the data pipeline (see data/SOURCES.md). " +
      "For tests only, TWIN_USE_FIXTURE=1 serves the labelled test fixture in dev.",
  );
}

/**
 * @typedef {object} TrafficManifest
 * @property {string} scenario       the scenario name the files belong to ("fixture" for the test fixture)
 * @property {"real" | "fixture" | "none"} source
 * @property {string | null} trips   file name in the data folder, or null when there is no traffic yet
 * @property {string | null} meta
 * @property {SpeedsManifest} [speeds] road speeds for this scenario; only present when both files were synced
 */

/**
 * @typedef {object} SpeedsManifest
 * @property {string} edgeGeometry   edge_geometry.json (one shape per SUMO edge; scenario-independent)
 * @property {string} edgeStats      edge_stats_<scenario>.parquet (speed per edge per 60 s bin)
 */

/** The sim-engineer's per-edge shapes (CLAUDE.md contract, Phase 3A): scenario-independent. */
export const EDGE_GEOMETRY_FILENAME = "edge_geometry.json";
/** @param {string} scenario */
export const edgeStatsFilename = (scenario) => `edge_stats_${scenario}.parquet`;

/**
 * Road speeds for a real scenario: sim/out/edge_stats_<scenario>.parquet + edge_geometry.json when both exist. Stats
 * without the geometry is an error (the web needs a shape for every coloured edge); geometry without stats for this
 * scenario just means no road speeds yet. Test fixture traffic never gets real road speeds (they would not match).
 * @param {{ traffic: { source: "real" | "fixture" | "none", scenario: string }, simOutDir: string }} input
 * @returns {{ geometryFrom: string, statsFrom: string } | null}
 */
export function resolveSpeeds({ traffic, simOutDir }) {
  if (traffic.source !== "real") return null;
  const geometryFrom = join(simOutDir, EDGE_GEOMETRY_FILENAME);
  const statsFrom = join(simOutDir, edgeStatsFilename(traffic.scenario));
  if (!existsSync(statsFrom)) return null;
  if (!existsSync(geometryFrom)) {
    throw new Error(
      `sim/out has ${edgeStatsFilename(traffic.scenario)} but no ${EDGE_GEOMETRY_FILENAME}: the web needs a shape for every ` +
        "coloured edge. Stop and report to the orchestrator.",
    );
  }
  for (const p of [geometryFrom, statsFrom]) {
    if (isTestFixtureText(readFileSync(p, "utf8"))) {
      throw new Error(`${p} is marked as test fixture data. Test data must never appear in sim/out/. Stop and report to the orchestrator.`);
    }
  }
  return { geometryFrom, statsFrom };
}

/**
 * Pick the traffic replay: the trips fixture with the opt-in (dev only), else sim/out/trips_<scenario>.json + its
 * meta when both exist, else none (the app runs without cars and says so). Exactly one of the pair is an error.
 * @param {{ mode: "dev" | "build", scenario: string, useTripsFixture: boolean, simOutDir: string, tripsFixturePath: string, tripsFixtureMetaPath: string }} input
 * @returns {{ source: "real" | "fixture" | "none", scenario: string, tripsFrom: string | null, metaFrom: string | null }}
 */
export function resolveTraffic({ mode, scenario, useTripsFixture, simOutDir, tripsFixturePath, tripsFixtureMetaPath }) {
  if (useTripsFixture) {
    if (mode === "build") throw new Error("Build refused: the trips test fixture is never bundled into a build.");
    for (const p of [tripsFixturePath, tripsFixtureMetaPath]) if (!existsSync(p)) throw new Error(`Trips fixture not found: ${p}`);
    return { source: "fixture", scenario: "fixture", tripsFrom: tripsFixturePath, metaFrom: tripsFixtureMetaPath };
  }
  const tripsFrom = join(simOutDir, `trips_${scenario}.json`);
  const metaFrom = join(simOutDir, `${scenario}.meta.json`);
  const [hasTrips, hasMeta] = [existsSync(tripsFrom), existsSync(metaFrom)];
  if (!hasTrips && !hasMeta) return { source: "none", scenario, tripsFrom: null, metaFrom: null };
  if (hasTrips !== hasMeta) {
    throw new Error(
      `sim/out has ${hasTrips ? `trips_${scenario}.json but no ${scenario}.meta.json` : `${scenario}.meta.json but no trips_${scenario}.json`}. ` +
        "The traffic contract needs both; stop and report to the orchestrator.",
    );
  }
  for (const p of [tripsFrom, metaFrom]) {
    if (isTestFixtureText(readFileSync(p, "utf8"))) {
      throw new Error(`${p} is marked as test fixture data. Test data must never appear in sim/out/. Stop and report to the orchestrator.`);
    }
  }
  return { source: "real", scenario, tripsFrom, metaFrom };
}

/**
 * The scenarios to sync: with the trips fixture, just the fixture; otherwise every scenario in SCENARIOS (plus an
 * explicit `scenario` not in it) whose trips + meta exist, in default order (`scenario` first when given). When none
 * has traffic, one "none" entry (the app runs without cars and says so). The first entry is the default.
 * @param {{ mode: "dev" | "build", scenario?: string, useTripsFixture: boolean, simOutDir: string, tripsFixturePath: string, tripsFixtureMetaPath: string }} input
 * @returns {{ source: "real" | "fixture" | "none", scenario: string, tripsFrom: string | null, metaFrom: string | null }[]}
 */
export function resolveScenarios({ scenario, ...rest }) {
  if (rest.useTripsFixture) return [resolveTraffic({ ...rest, scenario: "fixture" })];
  const names = scenario ? [scenario, ...SCENARIOS.filter((s) => s !== scenario)] : SCENARIOS;
  const found = names.map((name) => resolveTraffic({ ...rest, scenario: name })).filter((t) => t.source !== "none");
  return found.length ? found : [{ source: "none", scenario: scenario ?? DEFAULT_SCENARIO, tripsFrom: null, metaFrom: null }];
}

/**
 * Copy the chosen buildings file, the real roads_3d.geojson and the traffic replays (if any) into outDir, and write
 * the traffic manifests: traffic.json for the default scenario and scenarios.json listing every synced scenario (the
 * scenario switch). Removes any stale data first. Roads are required (missing = error) in every mode; there is
 * no roads fixture, so the fixture server gets the real roads beside the fixture buildings. Traffic is optional, and
 * so are its road speeds (resolveSpeeds; listed in the manifest only when synced).
 * @param {{ mode: "dev" | "build", useFixture?: boolean, useTripsFixture?: boolean, scenario?: string, realPath?: string, fixturePath?: string, roadsPath?: string, simOutDir?: string, tripsFixturePath?: string, tripsFixtureMetaPath?: string, outDir?: string }} options
 * @returns {{ source: "real" | "fixture", from: string, to: string, roadsFrom: string, roadsTo: string, traffic: { source: "real" | "fixture" | "none", scenario: string, tripsFrom: string | null, metaFrom: string | null }, speeds: { geometryFrom: string, statsFrom: string } | null, scenarios: TrafficManifest[] }}
 */
export function syncData({
  mode,
  useFixture = false,
  useTripsFixture = false,
  scenario,
  realPath = DEFAULT_PATHS.realPath,
  fixturePath = DEFAULT_PATHS.fixturePath,
  roadsPath = DEFAULT_PATHS.roadsPath,
  simOutDir = DEFAULT_PATHS.simOutDir,
  tripsFixturePath = DEFAULT_PATHS.tripsFixturePath,
  tripsFixtureMetaPath = DEFAULT_PATHS.tripsFixtureMetaPath,
  outDir = DEFAULT_PATHS.outDir,
}) {
  const realExists = existsSync(realPath);
  const realIsFixture = realExists && !useFixture && isTestFixtureText(readFileSync(realPath, "utf8"));
  rmSync(outDir, { recursive: true, force: true });
  const source = resolveDataSource({ mode, realExists, realIsFixture, useFixture });
  const from = source === "real" ? realPath : fixturePath;
  if (!existsSync(from)) throw new Error(`Buildings data not found: ${from}`);
  mkdirSync(outDir, { recursive: true });
  const to = join(outDir, OUTPUT_FILENAME);
  copyFileSync(from, to);

  if (!existsSync(roadsPath)) {
    rmSync(outDir, { recursive: true, force: true });
    throw new Error(
      (mode === "build" ? "Build refused: " : "") +
        "data/processed/roads_3d.geojson is missing. It is the data-engineer's road-height contract output; " +
        "stop and report to the orchestrator.",
    );
  }
  if (isTestFixtureText(readFileSync(roadsPath, "utf8"))) {
    rmSync(outDir, { recursive: true, force: true });
    throw new Error("data/processed/roads_3d.geojson is marked as test fixture data. Stop and report to the orchestrator.");
  }
  const roadsTo = join(outDir, ROADS_FILENAME);
  copyFileSync(roadsPath, roadsTo);

  let resolved;
  try {
    resolved = resolveScenarios({ mode, scenario, useTripsFixture, simOutDir, tripsFixturePath, tripsFixtureMetaPath }).map((traffic) => ({
      traffic,
      speeds: resolveSpeeds({ traffic, simOutDir }),
    }));
  } catch (err) {
    rmSync(outDir, { recursive: true, force: true });
    throw err;
  }
  const scenarios = resolved.map(({ traffic, speeds }) => {
    /** @type {TrafficManifest} */
    const manifest = { scenario: traffic.scenario, source: traffic.source, trips: null, meta: null };
    if (traffic.tripsFrom && traffic.metaFrom) {
      manifest.trips = basename(traffic.tripsFrom);
      manifest.meta = basename(traffic.metaFrom);
      copyFileSync(traffic.tripsFrom, join(outDir, manifest.trips));
      copyFileSync(traffic.metaFrom, join(outDir, manifest.meta));
    }
    if (speeds) {
      manifest.speeds = { edgeGeometry: basename(speeds.geometryFrom), edgeStats: basename(speeds.statsFrom) };
      copyFileSync(speeds.geometryFrom, join(outDir, manifest.speeds.edgeGeometry));
      copyFileSync(speeds.statsFrom, join(outDir, manifest.speeds.edgeStats));
    }
    return manifest;
  });
  const [first] = resolved;
  writeFileSync(join(outDir, TRAFFIC_MANIFEST_FILENAME), JSON.stringify(scenarios[0], null, 2) + "\n");
  writeFileSync(
    join(outDir, SCENARIOS_MANIFEST_FILENAME),
    JSON.stringify({ default: scenarios[0]?.scenario, scenarios: scenarios.filter((m) => m.source !== "none") }, null, 2) + "\n",
  );
  return { source, from, to, roadsFrom: roadsPath, roadsTo, traffic: first.traffic, speeds: first.speeds, scenarios };
}

/**
 * Build guard: throws if ANY file under `dir` (any name or extension) contains test fixture data. Used on the
 * public folder before `vite build`, so the fixture can't ship even if `dev` copied it there (or it was renamed)
 * and someone runs `vite build` directly.
 * @param {string} dir
 * @returns {string[]} the files checked
 */
export function assertNoFixtureIn(dir) {
  /** @type {string[]} */
  const checked = [];
  if (!existsSync(dir)) return checked;
  /** @param {string} d */
  const walk = (d) => {
    for (const name of readdirSync(d)) {
      const p = join(d, name);
      if (statSync(p).isDirectory()) {
        walk(p);
      } else {
        checked.push(p);
        if (isTestFixtureText(readFileSync(p, "utf8"))) {
          throw new Error(
            `Build refused: ${p} is test fixture data. Fixtures never ship; run \`pnpm build\` (it re-syncs the real ` +
              "data/processed/ and sim/out/ files) without TWIN_USE_FIXTURE.",
          );
        }
      }
    }
  };
  walk(dir);
  return checked;
}

/**
 * Build guard: throws if the real buildings file, the roads or the traffic manifest is not in the public data folder.
 * @param {string} outDir
 */
export function assertRealDataPresent(outDir) {
  for (const name of [OUTPUT_FILENAME, ROADS_FILENAME, TRAFFIC_MANIFEST_FILENAME, SCENARIOS_MANIFEST_FILENAME]) {
    const p = join(outDir, name);
    if (!existsSync(p)) {
      throw new Error(`Build refused: ${p} is missing. Run \`pnpm build\` (it syncs data/processed/${name}).`);
    }
  }
}
