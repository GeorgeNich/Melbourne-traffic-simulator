// Frame rate with glowing car playback ON (trails on), per camera preset, in HEADED Chromium (real GPU).
// The trips come from test data, served to the page with page.route (the real buildings and roads are untouched):
//   PERF_TRIPS=fixture (default)  tests/fixtures/trips_fixture.json (40 labelled hand-made cars)
//   PERF_TRIPS=real               the synced real sim/out/trips_baseline.json (not routed)
//   PERF_TRIPS=stress             web/perf-results/stress/trips_stress_<PERF_STRESS_CARS>.json (default 2000), made by
//                                 `node scripts/make-stress-trips.mjs --cars 2000` (synthetic, never committed)
// Per preset (PERF_PRESETS=cbd,burnley for some only): PERF_SECONDS (default 8) of playback at 1x with a still camera, then the same while orbiting.
// Writes web/perf-results/traffic-<mode>-<timestamp>.json and prints it.
// Run: pnpm -C web perf:traffic          (or PERF_TRIPS=stress pnpm -C web perf:traffic after pnpm -C web stress-trips)
// Run fps measurements one at a time, with nothing heavy alongside.
import { existsSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Page, test } from "@playwright/test";
import { CAMERA_PRESETS } from "../src/camera/presets";

const MODE = process.env.PERF_TRIPS === "stress" ? "stress" : process.env.PERF_TRIPS === "real" ? "real" : "fixture";
const STRESS_CARS = Number(process.env.PERF_STRESS_CARS ?? 2000);
const SECONDS = Number(process.env.PERF_SECONDS ?? 8);
/** PERF_PRESETS=cbd,burnley measures only those presets (default: all). */
const ONLY_PRESETS = process.env.PERF_PRESETS?.split(",");
const ROOT = resolve(import.meta.dirname, "../..");
const FILES =
  MODE === "real"
    ? { trips: resolve(ROOT, "sim/out/trips_baseline.json"), meta: resolve(ROOT, "sim/out/baseline.meta.json") }
    : MODE === "stress"
    ? {
        trips: resolve(import.meta.dirname, `../perf-results/stress/trips_stress_${STRESS_CARS}.json`),
        meta: resolve(import.meta.dirname, `../perf-results/stress/stress_${STRESS_CARS}.meta.json`),
      }
    : { trips: resolve(ROOT, "tests/fixtures/trips_fixture.json"), meta: resolve(ROOT, "tests/fixtures/trips_fixture.meta.json") };
/** Playback starts here: the fixture is busiest around 1.5–2.5 min; stress traffic is constant. */
const START_S = MODE === "stress" ? 30 : MODE === "real" ? 420 : 100;

/** Serve test trips to the page instead of the synced traffic (the manifest points at the routed files). */
export async function routeTrips(page: Page, files: { trips: string; meta: string }) {
  await page.route("**/data/traffic.json", (r) =>
    r.fulfill({ json: { scenario: "perf", source: "fixture", trips: "perf_trips.json", meta: "perf_trips.meta.json" } }),
  );
  await page.route("**/data/perf_trips.json", (r) => r.fulfill({ path: files.trips, contentType: "application/json" }));
  await page.route("**/data/perf_trips.meta.json", (r) => r.fulfill({ path: files.meta, contentType: "application/json" }));
}

interface FrameStats {
  frames: number;
  renders: number;
  seconds: number;
  frameTimes: number[];
}

function summarise(s: FrameStats) {
  const sorted = [...s.frameTimes].sort((a, b) => a - b);
  const pct = (p: number) => sorted[Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))] ?? 0;
  return {
    avgFps: Number((s.frames / s.seconds).toFixed(1)),
    mapRendersPerSecond: Number((s.renders / s.seconds).toFixed(1)),
    frameMs: { p50: Number(pct(50).toFixed(1)), p95: Number(pct(95).toFixed(1)), max: Number(pct(100).toFixed(1)) },
    framesOver20ms: s.frameTimes.filter((t) => t > 20).length,
  };
}

/** Count browser frames and MapLibre renders (each also draws the interleaved deck.gl layers) for `seconds`. */
function measure(page: Page, seconds: number, orbit: boolean, startBearing: number) {
  return page.evaluate(
    ({ seconds, orbit, startBearing }) =>
      new Promise<FrameStats>((done) => {
        const map = window.__twinMap!;
        let renders = 0;
        const onRender = () => (renders += 1);
        map.on("render", onRender);
        const frameTimes: number[] = [];
        const t0 = performance.now();
        let last = t0;
        const step = (now: number) => {
          frameTimes.push(now - last);
          last = now;
          const t = (now - t0) / 1000;
          if (orbit) map.jumpTo({ bearing: startBearing + (360 * t) / seconds });
          if (t < seconds) requestAnimationFrame(step);
          else {
            map.off("render", onRender);
            done({ frames: frameTimes.length, renders, seconds: t, frameTimes: frameTimes.slice(1) });
          }
        };
        requestAnimationFrame(step);
      }),
    { seconds, orbit, startBearing },
  );
}

test(`playback fps per preset, ${MODE} trips (headed)`, async ({ page, browserName }) => {
  test.skip(!existsSync(FILES.trips), `missing ${FILES.trips}: run node scripts/make-stress-trips.mjs --cars ${STRESS_CARS}`);
  test.setTimeout(600_000);
  if (MODE !== "real") await routeTrips(page, FILES);
  await page.goto("/");
  await page.waitForFunction(() => window.__twinReady === true, null, { timeout: 180_000 });
  await expect(page.getByTestId("playback")).toBeVisible();
  await expect(page.getByRole("checkbox", { name: "Show tunnels" })).toBeChecked();

  const load = await page.evaluate(() => {
    const t = (name: string) => {
      const e = performance.getEntriesByName(`twin:${name}`)[0];
      return e ? Math.round(e.startTime) : null;
    };
    const trips = performance.getEntriesByType("resource").find((r) => /perf_trips\.json|trips_baseline\.json/.test(r.name)) as PerformanceResourceTiming | undefined;
    return {
      tripsDecodedBytes: trips?.decodedBodySize ?? null,
      marksMs: { trafficFetchStart: t("traffic-fetch-start"), trafficParsed: t("traffic-parsed"), trafficPrepared: t("traffic-prepared"), ready: t("ready") },
    };
  });
  const gpu = await page.evaluate(() => {
    const gl = document.createElement("canvas").getContext("webgl2");
    const ext = gl?.getExtension("WEBGL_debug_renderer_info");
    return ext && gl ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)) : "unknown";
  });

  const presets: Record<string, unknown> = {};
  for (const preset of CAMERA_PRESETS.filter((p) => !ONLY_PRESETS || ONLY_PRESETS.includes(p.id))) {
    const v = preset.view;
    await page.evaluate(
      ({ view, start }) =>
        new Promise<void>((done) => {
          const pb = window.__twinPlayback!;
          pb.pause();
          pb.setSpeed(1);
          pb.seek(start);
          const map = window.__twinMap!;
          map.jumpTo({ center: [view.longitude, view.latitude], zoom: view.zoom, pitch: view.pitch, bearing: view.bearing });
          map.once("idle", () => done());
        }),
      { view: v, start: START_S },
    );
    await page.evaluate(() => window.__twinPlayback!.play());
    const still = await measure(page, SECONDS, false, v.bearing);
    const cars = await page.getByTestId("cars").textContent();
    const orbit = await measure(page, SECONDS, true, v.bearing);
    presets[preset.id] = { carsOnRoad: cars, playing: summarise(still), playingWhileOrbiting: summarise(orbit) };
    expect(still.frames).toBeGreaterThan(0);
  }

  const result = {
    when: new Date().toISOString(),
    browser: `${browserName} headed`,
    gpu,
    viewport: page.viewportSize(),
    deviceScaleFactor: 2,
    trips: MODE,
    meta: (({ vehicles, duration_s, _notes }) => ({ vehicles, duration_s, notes: _notes }))(JSON.parse(readFileSync(FILES.meta, "utf8"))),
    look: "defaults (trail 20 s, glow through, ground roads medium, X-ray depth, silhouette tower edges), Show tunnels on",
    load,
    presets,
  };
  const outDir = resolve(import.meta.dirname, "../perf-results");
  mkdirSync(outDir, { recursive: true });
  writeFileSync(resolve(outDir, `traffic-${MODE}-${Date.now()}.json`), JSON.stringify(result, null, 2) + "\n");
  console.log(JSON.stringify(result, null, 2));
});
