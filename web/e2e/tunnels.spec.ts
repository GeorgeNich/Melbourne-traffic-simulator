// Stage B: X-ray tunnels (real data). Checks that
//   - the far plane reaches below the deepest tunnel at every pitch (MapLibre's own far plane stops a few metres under
//     the ground at pitch 0 and high zoom; src/map/farPlane.ts lowers it), at the deep Burnley point under the Yarra;
//   - the X-ray tunnel ribbons are drawn and "Show tunnels" hides them together with the tunnel cars.
// Opt-in screenshots for the Stage B review (STAGE_B_SHOTS=1): web/screenshots/stage-b/*.png, 1440x900, fps hidden.
//   STAGE_B_SHOTS=1 pnpm -C web e2e --project=real tunnels
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Page, test } from "@playwright/test";
import { CAMERA_PRESETS, getPreset } from "../src/camera/presets";
import { frameAt, waitForReady } from "./support/trips";

/** The Burnley tunnel (way 8066124) at -30 m, under the Yarra. */
const DEEP_BURNLEY: [number, number, number] = [144.97864, -37.82528, -30];

type View = { longitude: number; latitude: number; zoom: number; pitch: number; bearing: number };

/**
 * Clip-space depth (z / w, visible in [-1, 1]) of a [lon, lat, z_m] point under MapLibre's current projection: the same
 * near / far planes deck.gl draws with (interleaved overlay). Also returns w > 0 (in front of the camera).
 */
async function clipDepth(page: Page, p: [number, number, number]) {
  return page.evaluate(([lon, lat, z]) => {
    const t = (window.__twinMap as unknown as { transform: { worldSize: number; modelViewProjectionMatrix: ArrayLike<number> } }).transform;
    const x = ((lon + 180) / 360) * t.worldSize;
    const y = ((180 - (180 / Math.PI) * Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360))) / 360) * t.worldSize;
    const m = t.modelViewProjectionMatrix; // column-major; input (x px, y px, z m, 1)
    const cz = m[2]! * x + m[6]! * y + m[10]! * z + m[14]!;
    const cw = m[3]! * x + m[7]! * y + m[11]! * z + m[15]!;
    return { ndcZ: cz / cw, w: cw };
  }, p);
}

/** Visible-flag of the deck.gl layers whose id starts with `prefix` (map.__deck is deck.gl's handle on the map). */
async function layerVisibility(page: Page, prefix: string) {
  return page.evaluate((prefix) => {
    const deck = (window.__twinMap as unknown as { __deck: { props: { layers: { id: string; props: { visible?: boolean } }[] } } }).__deck;
    return deck.props.layers.filter((l) => l.id.startsWith(prefix)).map((l) => l.props.visible !== false);
  }, prefix);
}

test.describe("X-ray tunnels (real data)", () => {
  test.beforeEach(async ({ page }, info) => {
    test.skip(info.project.name !== "real", "real roads_3d only");
    await page.goto("/");
    await waitForReady(page);
  });

  test("the deep Burnley point is inside the far plane at pitch 0, 30 and 60, from zoom 15 to 19", async ({ page }) => {
    const [lon, lat] = DEEP_BURNLEY;
    const results: string[] = [];
    for (const pitch of [0, 30, 60]) {
      for (const zoom of [15, 17, 19]) {
        await frameAt(page, { longitude: lon, latitude: lat, zoom, pitch, bearing: 25 }, 0);
        const { ndcZ, w } = await clipDepth(page, DEEP_BURNLEY);
        // A car on the deck floats CAR_LIFT_M above it; the tunnel floor is the deepest thing drawn.
        results.push(`pitch ${pitch} zoom ${zoom}: ndc z ${ndcZ.toFixed(4)}`);
        expect(w, `pitch ${pitch} zoom ${zoom}`).toBeGreaterThan(0);
        expect(ndcZ, `pitch ${pitch} zoom ${zoom}: behind the far plane`).toBeLessThan(1);
        expect(ndcZ, `pitch ${pitch} zoom ${zoom}: in front of the near plane`).toBeGreaterThan(-1);
      }
    }
    console.log("deep Burnley point, clip depth:", results.join("; "));
  });

  test("the far plane also holds at every camera preset (centre of view pushed to -30 m)", async ({ page }) => {
    for (const p of CAMERA_PRESETS) {
      await frameAt(page, p.view, 0);
      const { ndcZ } = await clipDepth(page, [p.view.longitude, p.view.latitude, -30]);
      expect(ndcZ, p.id).toBeLessThan(1);
    }
  });

  test("Show tunnels hides the X-ray tunnel ribbons (and tunnel cars); the cutting and bridges stay", async ({ page }) => {
    await frameAt(page, getPreset("burnley").view, 0);
    expect(await layerVisibility(page, "tunnels-")).toEqual([true, true, true, true, true, true]);
    const toggle = page.getByRole("checkbox", { name: "Show tunnels" });
    await toggle.uncheck();
    await expect.poll(() => layerVisibility(page, "tunnels-")).toEqual([false, false, false, false, false, false]);
    // Ordinary ribbons (bridges, ramps and the open CityLink cutting) don't follow the toggle.
    expect((await layerVisibility(page, "roads-")).every(Boolean)).toBe(true);
    expect((await layerVisibility(page, "traffic-tunnel-")).every((v) => !v)).toBe(true);
    await toggle.check();
    await expect.poll(() => layerVisibility(page, "tunnels-")).toEqual([true, true, true, true, true, true]);
  });

  test("tunnel ribbons are drawn before the buildings (the city tints over them)", async ({ page }) => {
    const ids = await page.evaluate(() =>
      (window.__twinMap as unknown as { __deck: { props: { layers: { id: string }[] } } }).__deck.props.layers.map((l) => l.id),
    );
    const firstBuilding = ids.findIndex((id) => id.startsWith("buildings"));
    expect(firstBuilding).toBeGreaterThan(0);
    expect(ids.slice(0, firstBuilding)).toEqual(["tunnels-curtain", "tunnels-trace", "tunnels-deck", "tunnels-portal", "tunnels-edge", "tunnels-ceiling"]);
  });
});

const SHOTS = resolve(import.meta.dirname, "../screenshots/stage-b");
/** Playback paused here for every shot, so they show the same moment (seconds from scenario start). */
const SHOT_TIME_S = 420;
const VIEWS: Record<string, View> = {
  // The Burnley preset's view at a lower pitch: the tunnels must still read as underground, not as bridges.
  "burnley-xray-low": { ...getPreset("burnley").view, pitch: 40 },
  // Side-on, near the horizon, looking along the Yarra where both tunnels cross under it: the depth below the
  // river reads directly (the river surface at 0 m, the tunnels at -30 m beneath it).
  "burnley-xray-side": { longitude: 144.9786, latitude: -37.8252, zoom: 17.8, pitch: 72, bearing: 160 },
  // The CityLink cutting under Power St (open to the sky: solid ribbons) where the tunnels' X-ray portals start,
  // looking north-north-east across it.
  "power-st-cutting": { longitude: 144.9655, latitude: -37.8268, zoom: 17.8, pitch: 50, bearing: 20 },
  // City Road underpass under St Kilda Road (X-ray) and its open approach ramps, looking east.
  "city-road-underpass": { longitude: 144.969, latitude: -37.8211, zoom: 17.6, pitch: 45, bearing: 100 },
};

async function shoot(page: Page, name: string, view: View) {
  await frameAt(page, view, SHOT_TIME_S);
  await page.evaluate(
    () =>
      new Promise<void>((done) => {
        const map = window.__twinMap!;
        if (map.loaded() && map.areTilesLoaded()) return done();
        map.once("idle", () => done());
      }),
  );
  await page.waitForTimeout(500); // label fade-in
  await page.screenshot({ path: resolve(SHOTS, `${name}.png`) });
}

test("Stage B review screenshots", async ({ page }, info) => {
  test.skip(info.project.name !== "real" || process.env.STAGE_B_SHOTS !== "1", "opt-in: STAGE_B_SHOTS=1, real project");
  test.setTimeout(300_000);
  mkdirSync(SHOTS, { recursive: true });
  await page.goto("/");
  await waitForReady(page);
  await page.addStyleTag({ content: '[data-testid="fps"] { display: none !important; }' });
  const toggle = page.getByRole("checkbox", { name: "Show tunnels" });
  // Burnley through its preset button (fly-to), so the button shows as active.
  await page.getByRole("button", { name: "Burnley tunnel", exact: true }).click();
  await page.waitForFunction(() => !window.__twinMap!.isMoving(), null, { timeout: 30_000 });
  await shoot(page, "burnley-xray", getPreset("burnley").view);
  await toggle.uncheck();
  await shoot(page, "burnley-xray-off", getPreset("burnley").view);
  await toggle.check();
  // A small real drag clears the active preset button (only user gestures do), for the views that aren't presets.
  await page.mouse.move(900, 500);
  await page.mouse.down();
  await page.mouse.move(905, 505, { steps: 3 });
  await page.mouse.up();
  for (const [name, view] of Object.entries(VIEWS)) await shoot(page, name, view);
  await page.getByRole("button", { name: "CBD", exact: true }).click();
  await page.waitForFunction(() => !window.__twinMap!.isMoving(), null, { timeout: 30_000 });
  await shoot(page, "cbd", getPreset("cbd").view);
});
