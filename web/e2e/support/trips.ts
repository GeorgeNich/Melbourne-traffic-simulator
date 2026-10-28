// Test-only helpers: serve labelled test trips to the page with page.route (the synced data is untouched), and
// pause the playback at a fixed time so screenshots are repeatable.
import { resolve } from "node:path";
import type { Page } from "@playwright/test";

const ROOT = resolve(import.meta.dirname, "../../..");

/** The labelled trips fixture (tests/fixtures/, made by scripts/make-trips-fixture.mjs). */
export const FIXTURE_TRIPS = {
  trips: resolve(ROOT, "tests/fixtures/trips_fixture.json"),
  meta: resolve(ROOT, "tests/fixtures/trips_fixture.meta.json"),
};

/** Synthetic stress trips (gitignored, made by scripts/make-stress-trips.mjs --cars <n>). */
export const stressTrips = (cars: number) => ({
  trips: resolve(import.meta.dirname, `../../perf-results/stress/trips_stress_${cars}.json`),
  meta: resolve(import.meta.dirname, `../../perf-results/stress/stress_${cars}.meta.json`),
});

type Files = { trips: string; meta: string } | { tripsJson: unknown; metaJson: unknown };

/** Point the traffic manifest at routed trips + meta (files on disk or inline JSON). Call before page.goto. */
export async function routeTrips(page: Page, files: Files, source: "real" | "fixture" = "fixture") {
  await page.route("**/data/traffic.json", (r) =>
    r.fulfill({ json: { scenario: "routed", source, trips: "routed_trips.json", meta: "routed_trips.meta.json" } }),
  );
  if ("trips" in files) {
    await page.route("**/data/routed_trips.json", (r) => r.fulfill({ path: files.trips, contentType: "application/json" }));
    await page.route("**/data/routed_trips.meta.json", (r) => r.fulfill({ path: files.meta, contentType: "application/json" }));
  } else {
    await page.route("**/data/routed_trips.json", (r) => r.fulfill({ json: files.tripsJson }));
    await page.route("**/data/routed_trips.meta.json", (r) => r.fulfill({ json: files.metaJson }));
  }
}

export async function waitForReady(page: Page) {
  await page.waitForFunction(() => window.__twinReady === true, null, { timeout: 120_000 });
}

/** Jump the camera (no fly) to a view, pause playback at `time`, and wait until the map has drawn it. */
export async function frameAt(
  page: Page,
  view: { longitude: number; latitude: number; zoom: number; pitch: number; bearing: number },
  time: number,
) {
  await page.evaluate(
    ({ view, time }) =>
      new Promise<void>((done) => {
        const pb = window.__twinPlayback;
        if (pb) {
          pb.pause();
          pb.seek(time);
        }
        const map = window.__twinMap!;
        map.jumpTo({ center: [view.longitude, view.latitude], zoom: view.zoom, pitch: view.pitch, bearing: view.bearing });
        map.once("idle", () => done());
        map.triggerRepaint();
      }),
    { view, time },
  );
  await page.waitForTimeout(400); // label fade-in
}

/** Share of screenshot pixels that are saturated traffic blue (blue-dominant, chroma > 0.35). */
export async function trafficBlueFraction(page: Page, png: Buffer) {
  return page.evaluate(async (b64) => {
    const img = new Image();
    img.src = `data:image/png;base64,${b64}`;
    await img.decode();
    const c = document.createElement("canvas");
    c.width = img.width;
    c.height = img.height;
    const ctx = c.getContext("2d")!;
    ctx.drawImage(img, 0, 0);
    const { data } = ctx.getImageData(0, 0, c.width, c.height);
    let blue = 0;
    for (let i = 0; i < data.length; i += 4) {
      const r = data[i]!, g = data[i + 1]!, b = data[i + 2]!;
      if (b > r + 60 && b > g && (Math.max(r, g, b) - Math.min(r, g, b)) / 255 > 0.35) blue++;
    }
    return blue / (data.length / 4);
  }, png.toString("base64"));
}
