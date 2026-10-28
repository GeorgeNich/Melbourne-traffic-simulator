// Roads (roads_3d.geojson) in dev: contract errors show in the shared alert panel and block __twinReady; the dev-only
// `?heights=source` view loads cleanly. Screenshots (real data): web/screenshots/heights/<normal|source>-<preset>.png.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Page, test } from "@playwright/test";
import { CAMERA_PRESETS } from "../src/camera/presets";

const DIR = resolve(import.meta.dirname, "../screenshots/heights");
const SHOT_PRESETS = ["interchange", "footscray", "cbd", "southbank"];

async function waitForIdle(page: Page) {
  await page.waitForFunction(() => window.__twinMap !== undefined && !window.__twinMap.isMoving(), null, { timeout: 30_000 });
  await page.evaluate(
    () =>
      new Promise<void>((done) => {
        const map = window.__twinMap!;
        if (map.loaded() && map.areTilesLoaded()) return done();
        map.once("idle", () => done());
      }),
  );
}

test("a roads contract error shows in the alert panel and the app never reports ready", async ({ page }) => {
  // A small hand-written broken collection (test-only, inline): swapped [lat, lon] and an unknown z_source.
  const broken = {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        properties: { way_id: 1, highway: "primary", layer: 1, bridge: true, tunnel: false, name: "Bad", z_source: "guessed" },
        geometry: { type: "LineString", coordinates: [[-37.82, 144.96, 0], [-37.82, 144.961, 6]] },
      },
    ],
  };
  await page.route("**/data/roads_3d.geojson", (route) => route.fulfill({ json: broken }));
  await page.goto("/");
  const alert = page.locator("pre[role=alert]");
  await expect(alert).toBeVisible({ timeout: 60_000 });
  await expect(alert).toContainText("[roads] feature 0: z_source");
  await expect(alert).toContainText("lon/lat swapped?");
  await expect(page.getByTestId("status")).toContainText("Roads failed");
  // Buildings still load and the map goes idle, but the ready flag stays unset.
  await expect(page.getByTestId("status")).toHaveText(/buildings/);
  await waitForIdle(page);
  await page.waitForTimeout(500);
  expect(await page.evaluate(() => window.__twinReady)).toBeUndefined();
});

test("a roads load failure also shows in the alert panel", async ({ page }) => {
  await page.route("**/data/roads_3d.geojson", (route) => route.fulfill({ status: 404, body: "" }));
  await page.goto("/");
  await expect(page.locator("pre[role=alert]")).toContainText("[roads] Could not load", { timeout: 60_000 });
  expect(await page.evaluate(() => window.__twinReady)).toBeUndefined();
});

for (const view of ["source", "normal"] as const) {
  test(`${view === "source" ? "?heights=source" : "normal view"} loads with no contract errors; screenshots`, async ({ page }, info) => {
    await page.goto(view === "source" ? "/?heights=source" : "/");
    await page.waitForFunction(() => window.__twinReady === true, null, { timeout: 90_000 });
    await expect(page.locator("pre[role=alert]")).toHaveCount(0);
    await expect(page.getByTestId("status")).toHaveText(/[\d,]+ elevated roads$/);
    if (info.project.name !== "real") return;
    mkdirSync(DIR, { recursive: true });
    await page.addStyleTag({ content: '[data-testid="fps"] { display: none !important; }' });
    for (const preset of CAMERA_PRESETS.filter((p) => SHOT_PRESETS.includes(p.id))) {
      await page.getByRole("button", { name: preset.label, exact: true }).click();
      await waitForIdle(page);
      await page.waitForTimeout(400); // label fade-in
      await page.screenshot({ path: resolve(DIR, `${view}-${preset.id}.png`) });
    }
  });
}
