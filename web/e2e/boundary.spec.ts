// Pixel checks where trail pieces meet (prepare.ts: time buckets and the surface / tunnel split). The glow is
// additive, so anything drawn twice where two pieces meet shows as a bright knot, and anything missing as a gap.
// Labelled synthetic test cars (page.route only, never on disk), sampled every 0.5 s like the SUMO export.
//   bend:   a 90-degree turn with a 60 s bucket boundary exactly at the corner, and one just after it. "test" frame:
//           the 20 s trail spans the boundary; "control": the same car 30 s later (no boundary in the trail), so
//           geometry, background and trail age are identical. Max per-pixel difference near the corner, both ways.
//   portal: a car dipping into a (fake) tunnel: no brighter knot at the portal than the trail on either side. The X-ray
//           tunnel trail is wider than the surface one by design (palette XRAY), so the check is on light per screen
//           column across the trail: at the portal it must not exceed the brighter of the two sides.
// Results: web/test-results/boundary/*.png and the console. Real project only.
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Page, test } from "@playwright/test";
import { frameAt, waitForReady } from "./support/trips";

const LAT = -37.8215;
const LON0 = 144.9405;
const M_LON = 111_320 * Math.cos((LAT * Math.PI) / 180);
const M_LAT = 110_574;
const OUT = resolve(import.meta.dirname, "../test-results/boundary");

/** East at 10 m/s from startS; turns north at cornerS. z from zAt(metres along). */
function car(startS: number, cornerS: number, zAt: (alongM: number) => number = () => 30, lon0 = LON0, lat = LAT) {
  const path: [number, number, number][] = [];
  const timestamps: number[] = [];
  for (let k = 0; k <= 200; k++) {
    const t = startS + k * 0.5;
    const east = Math.min(t, cornerS) - startS;
    const north = Math.max(0, t - cornerS);
    const along = (east + north) * 10;
    path.push([Number((lon0 + (east * 10) / M_LON).toFixed(7)), Number((lat + (north * 10) / M_LAT).toFixed(7)), zAt(along)]);
    timestamps.push(t);
  }
  return path.map((p, i) => ({ p, t: timestamps[i]! }));
}

async function route(page: Page, pts: { p: [number, number, number]; t: number }[], tunnelVehicles = 0) {
  const trips = [{ id: "fixture-boundary", type: "passenger", path: pts.map((x) => x.p), timestamps: pts.map((x) => x.t) }];
  const meta = {
    scenario: "fixture",
    demand: "test_fixture",
    duration_s: 300,
    seed: 0,
    sumo_version: "none: synthetic test car (web/e2e/boundary.spec.ts)",
    vehicles: 1,
    teleports: 0,
    tunnel_vehicles: tunnelVehicles,
    generated_at: "2026-10-27T00:00:00Z",
    _test_fixture: true,
    _notes: ["TEST FIXTURE: synthetic car for the piece-boundary pixel tests (page.route only)."],
  };
  await page.unrouteAll();
  await page.route("**/data/traffic.json", (r) =>
    r.fulfill({ json: { scenario: "fixture", source: "fixture", trips: "b_trips.json", meta: "b_trips.meta.json" } }),
  );
  await page.route("**/data/b_trips.json", (r) => r.fulfill({ json: trips }));
  await page.route("**/data/b_trips.meta.json", (r) => r.fulfill({ json: meta }));
}

async function shot(page: Page, center: [number, number], t: number, pitch = 0) {
  await page.goto("/");
  await waitForReady(page);
  await page.addStyleTag({ content: '[data-testid="fps"], [data-testid="playback"], [data-testid="fixture-banner"] { display: none !important; }' });
  await frameAt(page, { longitude: center[0], latitude: center[1], zoom: 18.5, pitch, bearing: 0 }, t);
  return page.screenshot();
}

/** Max per-pixel channel difference a - b (extra) and b - a (missing) in a box around (x, y). */
function diffBox(page: Page, a: Buffer, b: Buffer, x: number, y: number, r: number) {
  return page.evaluate(
    async ({ a, b, x, y, r }) => {
      const load = async (s: string) => {
        const img = new Image();
        img.src = `data:image/png;base64,${s}`;
        await img.decode();
        const c = document.createElement("canvas");
        [c.width, c.height] = [img.width, img.height];
        const ctx = c.getContext("2d")!;
        ctx.drawImage(img, 0, 0);
        return { d: ctx.getImageData(0, 0, c.width, c.height).data, w: c.width };
      };
      const [A, B] = [await load(a), await load(b)];
      let extra = 0;
      let missing = 0;
      for (let dy = -r; dy <= r; dy++) {
        for (let dx = -r; dx <= r; dx++) {
          const i = ((Math.round(y) + dy) * A.w + Math.round(x) + dx) * 4;
          for (let c = 0; c < 3; c++) {
            extra = Math.max(extra, A.d[i + c]! - B.d[i + c]!);
            missing = Math.max(missing, B.d[i + c]! - A.d[i + c]!);
          }
        }
      }
      return { extra, missing };
    },
    { a: a.toString("base64"), b: b.toString("base64"), x, y, r },
  );
}

/** Light (sum of RGB) per screen column x - r .. x + r, summed over rows y - r .. y + r. */
function columnLight(page: Page, a: Buffer, x: number, y: number, r: number) {
  return page.evaluate(
    async ({ a, x, y, r }) => {
      const img = new Image();
      img.src = `data:image/png;base64,${a}`;
      await img.decode();
      const c = document.createElement("canvas");
      [c.width, c.height] = [img.width, img.height];
      const ctx = c.getContext("2d")!;
      ctx.drawImage(img, 0, 0);
      const d = ctx.getImageData(0, 0, c.width, c.height).data;
      const cols: number[] = [];
      for (let dx = -r; dx <= r; dx++) {
        let sum = 0;
        for (let dy = -r; dy <= r; dy++) {
          const i = ((Math.round(y) + dy) * c.width + Math.round(x) + dx) * 4;
          sum += d[i]! + d[i + 1]! + d[i + 2]!;
        }
        cols.push(sum);
      }
      return cols;
    },
    { a: a.toString("base64"), x, y, r },
  );
}

test.describe("piece boundaries in pixels", () => {
  test.beforeEach(({ page }, info) => {
    void page;
    test.skip(info.project.name !== "real", "the real-data server only");
  });

  test("bend: no knot, wedge or gap where a 60 s bucket boundary falls at or near a corner", async ({ page }) => {
    mkdirSync(OUT, { recursive: true });
    const results: Record<string, unknown> = {};
    for (const offset of [0, 0.25]) {
      // Corner at 60 s (+ offset: the boundary falls on the corner, or inside the segment just after it).
      const corner = 60 - offset;
      const cornerLonLat: [number, number] = [LON0 + ((corner - 20) * 10) / M_LON, LAT];
      const frames: Buffer[] = [];
      for (const shift of [0, 30]) {
        await route(page, car(20 + shift, corner + shift));
        frames.push(await shot(page, [cornerLonLat[0], cornerLonLat[1] + 30 / M_LAT], 72 + shift));
      }
      const px = await page.evaluate((p) => window.__twinMap!.project(p), cornerLonLat);
      writeFileSync(resolve(OUT, `bend-${offset}-test.png`), frames[0]!);
      writeFileSync(resolve(OUT, `bend-${offset}-control.png`), frames[1]!);
      const d = await diffBox(page, frames[0]!, frames[1]!, px.x, px.y, 60);
      results[`boundary ${offset ? "0.25 s after" : "at"} the corner`] = d;
    }
    console.log("bend boundary (test - control, max channel):", JSON.stringify(results));
    for (const [k, d] of Object.entries(results) as [string, { extra: number; missing: number }][]) {
      expect.soft(d.extra, `${k}: extra light (knot / overlap)`).toBeLessThan(8);
      expect.soft(d.missing, `${k}: missing light (gap / wedge)`).toBeLessThan(8);
    }
  });

  test("tunnel portal: no brighter knot where the surface piece meets the X-ray tunnel piece", async ({ page }) => {
    mkdirSync(OUT, { recursive: true });
    // Over Victoria Harbour (open water, no buildings in the way): straight east at ground level, then 1.2 m below it
    // from 200 m along (a shallow fake tunnel: X-ray, but close enough to the ground that a top-down camera neither
    // clips it at the far plane nor shifts it by parallax near the screen centre). The split vertex is at 195 m (the
    // 195-200 m segment's mid-height is -0.6 m).
    const lat = -37.8222;
    const lon0 = 144.9382;
    const portalLon = lon0 + 195 / M_LON;
    const ramp = (along: number) => (along < 200 ? 0 : -1.2);
    await route(page, car(20, 1e6, ramp, lon0, lat), 1);
    // At t = 52 the head is 125 m past the portal (reached at 39.5 s): the 20 s trail spans it; the head, its tether
    // and its surface ring are far outside the measured box.
    const frame = await shot(page, [portalLon, lat], 52);
    writeFileSync(resolve(OUT, "portal-test.png"), frame);
    const px = await page.evaluate((p) => window.__twinMap!.project(p), [portalLon, lat] as [number, number]);
    const R = 60;
    const cols = await columnLight(page, frame, px.x, px.y, R);
    const band = (from: number, to: number) => Math.max(...cols.slice(R + from, R + to + 1));
    const portal = band(-8, 8);
    const surface = band(-R, -20);
    const tunnel = band(20, R);
    console.log(`tunnel portal, light per column: portal ${portal}, surface side ${surface}, tunnel side ${tunnel}`);
    expect(surface, "the surface trail is drawn").toBeGreaterThan(1000);
    expect(tunnel, "the X-ray tunnel trail is drawn").toBeGreaterThan(1000);
    expect.soft(portal, "no knot at the portal (more light than either side)").toBeLessThan(Math.max(surface, tunnel) * 1.1);
  });
});
