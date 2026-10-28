#!/usr/bin/env node
// SYNTHETIC STRESS-TEST TRIPS for fps measurement only (never committed, never shipped): random walks over the real
// roads_3d.geojson network, `--cars` cars on the road at all times for `--duration` seconds. Labelled as test data
// ("fixture-stress-" ids, demand "test_fixture", `_test_fixture: true`) so the app shows the fixture banner and a
// build would refuse it. Written to web/perf-results/stress/ (gitignored); e2e-perf/traffic.spec.ts serves it to
// the page with page.route.
//
// Point density matches a SUMO FCD export at 0.5 s: roads_3d is densified to ~8 m, i.e. one point every ~0.5–0.8 s
// at city speeds, plus stopped cars at signals.
//
// Run: pnpm -C web stress-trips --cars 2000 --duration 900   (or node scripts/make-stress-trips.mjs ... from web/)
// A literal "--" before the options (pnpm 11/12 passes `pnpm stress-trips -- --cars 2000` through as-is) is ignored.
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { parseArgs } from "node:util";
import { buildGraph, mulberry32, randomWalk, readOsmExtras, roundStrict, timeRoute } from "./lib/road-graph.mjs";

const WEB_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const ROOT = resolve(WEB_DIR, "..");
export const STRESS_DIR = resolve(WEB_DIR, "perf-results/stress");

const argv = process.argv.slice(2);
if (argv[0] === "--") argv.shift();
const { values } = parseArgs({
  args: argv,
  options: {
    cars: { type: "string", default: "2000" },
    duration: { type: "string", default: "300" },
    seed: { type: "string", default: "7" },
  },
});
const CARS = Number(values.cars);
const DURATION_S = Number(values.duration);
const SEED = Number(values.seed);

const roads = JSON.parse(readFileSync(resolve(ROOT, "data/processed/roads_3d.geojson"), "utf8"));
const { oneway, signalKeys } = readOsmExtras(resolve(ROOT, "data/processed/osm_sim_area.osm.gz"));
const g = buildGraph(roads.features, oneway, signalKeys);
const rng = mulberry32(SEED);
// Start anywhere on a non-service road.
const starts = [];
for (let i = 0; i < g.coords.length; i++) if (g.out[i].some((e) => e.highway !== "service" && e.highway !== "living_street")) starts.push(i);

const trips = [];
let n = 0;
let points = 0;
for (let slot = 0; slot < CARS; slot++) {
  // Each slot keeps one car on the road for the whole duration: when a walk ends (dead end at the network edge),
  // a new car starts somewhere else at that moment.
  let t = rng() * 2;
  while (t < DURATION_S) {
    const start = starts[Math.floor(rng() * starts.length)];
    const speedFactor = 0.6 + rng() * 0.45;
    const walk = randomWalk(g, start, (DURATION_S - t) * 18 * speedFactor + 50, rng);
    if (walk.edges.length < 2) continue;
    const stops = new Map();
    walk.vertices.forEach((v, k) => {
      if (k > 0 && g.signals.has(v) && rng() < 0.3) stops.set(k, 5 + rng() * 25);
    });
    const timed = timeRoute(g, walk, { startS: t, speedFactor, stops });
    let end = timed.timestamps.findIndex((s) => s > DURATION_S);
    if (end === -1) end = timed.timestamps.length;
    if (end >= 2) {
      trips.push({
        id: `fixture-stress-${n++}`,
        type: "passenger",
        path: timed.path.slice(0, end).map(([lon, lat, z]) => [lon, lat, Math.round(z * 100) / 100]),
        timestamps: roundStrict(timed.timestamps.slice(0, end)),
      });
      points += end;
    }
    // Next car in this slot starts where this one finished (plus a short gap).
    t = (timed.timestamps[Math.max(0, end - 1)] ?? t) + 1 + rng() * 3;
  }
}

const tunnelVehicles = trips.filter((tr) => tr.path.some((p) => p[2] < -0.5)).length;
const meta = {
  scenario: "stress_test",
  demand: "test_fixture",
  duration_s: DURATION_S,
  seed: SEED,
  sumo_version: "none: synthetic stress test (web/scripts/make-stress-trips.mjs), not a SUMO run",
  vehicles: trips.length,
  teleports: 0,
  tunnel_vehicles: tunnelVehicles,
  generated_at: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
  _test_fixture: true,
  _notes: [`SYNTHETIC STRESS TEST: ${CARS} random-walk cars on the road at all times over real roads_3d ways. fps measurement only.`],
};
mkdirSync(STRESS_DIR, { recursive: true });
const name = `stress_${CARS}`;
writeFileSync(resolve(STRESS_DIR, `trips_${name}.json`), JSON.stringify(trips));
writeFileSync(resolve(STRESS_DIR, `${name}.meta.json`), JSON.stringify(meta, null, 2));
console.log(`[stress-trips] ${trips.length} trips (${CARS} on the road at once), ${points.toLocaleString()} points, ${DURATION_S} s -> ${STRESS_DIR}/trips_${name}.json`);
