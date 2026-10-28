#!/usr/bin/env node
// Copies buildings data, roads_3d.geojson and the traffic replay into the public folder (gitignored) before `dev`
// and `build`.
//   node scripts/sync-data.mjs --mode=dev    data/processed/buildings.geojson, or fail
//   node scripts/sync-data.mjs --mode=build  the same; fixtures can never ship
// Traffic: sim/out/trips_<scenario>.json + <scenario>.meta.json for every scenario that has them (am_peak, baseline);
// the first is the default (TWIN_SCENARIO picks another); with none the app runs without cars and says so. Road speeds: sim/out/edge_stats_<scenario>.parquet +
// edge_geometry.json with real traffic, when they exist (never with fixture trips).
// TWIN_USE_FIXTURE (dev only, tests / look review): 1 or all = fixture buildings + fixture trips; trips = fixture
// trips over the real buildings; buildings = fixture buildings only.
// TWIN_PUBLIC_DIR=<dir> (relative to web/) syncs into another public folder (default: public).
import { fixtureSelection, scenarioName, syncData } from "./data-guard.mjs";

const arg = process.argv.find((a) => a.startsWith("--mode="));
const mode = arg?.slice("--mode=".length);
if (mode !== "dev" && mode !== "build") {
  console.error("usage: node scripts/sync-data.mjs --mode=dev|build");
  process.exit(2);
}

try {
  const fixtures = fixtureSelection();
  const { source, from, to, roadsFrom, roadsTo, traffic, speeds, scenarios } = syncData({
    mode,
    useFixture: fixtures.buildings,
    useTripsFixture: fixtures.trips,
    scenario: scenarioName(),
  });
  if (source === "fixture") {
    console.warn(
      `\n\x1b[33m[sync-data] TWIN_USE_FIXTURE=1: serving the TEST FIXTURE (${from}) -> ${to}.\n` +
        "[sync-data] Hand-made rectangles, not real buildings. Dev/test only; a build will refuse it.\x1b[0m\n",
    );
  } else {
    console.log(`[sync-data] buildings: ${from} -> ${to}`);
  }
  console.log(`[sync-data] roads: ${roadsFrom} -> ${roadsTo}`);
  if (traffic.source === "fixture") {
    console.warn(
      `\x1b[33m[sync-data] TWIN_USE_FIXTURE: serving the TRIPS TEST FIXTURE (${traffic.tripsFrom}).\n` +
        "[sync-data] Hand-made cars on real roads, not simulated traffic. Dev/test only; a build will refuse it.\x1b[0m",
    );
  } else if (traffic.source === "real") {
    console.log(`[sync-data] traffic: ${traffic.tripsFrom} + ${traffic.metaFrom}`);
  } else {
    console.log(`[sync-data] traffic: none yet (no sim/out/trips_${traffic.scenario}.json); the app runs without cars.`);
  }
  if (speeds) console.log(`[sync-data] road speeds: ${speeds.statsFrom} + ${speeds.geometryFrom}`);
  if (scenarios.length > 1) console.log(`[sync-data] scenarios: ${scenarios.map((s) => s.scenario).join(", ")} (default ${traffic.scenario})`);
  else if (traffic.source === "real") console.log(`[sync-data] road speeds: none yet (no sim/out/edge_stats_${traffic.scenario}.parquet).`);
} catch (err) {
  console.error(`\n\x1b[31m[sync-data] ${err instanceof Error ? err.message : String(err)}\x1b[0m\n`);
  process.exit(1);
}
