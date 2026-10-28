// Runs against two dev servers (playwright.config.ts): "real" (data/processed/buildings.geojson) and
// "fixture" (TWIN_USE_FIXTURE=1). Screenshots: web/screenshots/<preset>.png (real), fixture-<preset>.png.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Page, test } from "@playwright/test";
import { CAMERA_PRESETS } from "../src/camera/presets";

const SCREENSHOT_DIR = resolve(import.meta.dirname, "../screenshots");
const COM_CREDIT = "Building and road heights: Contains City of Melbourne Open Data (CC BY 4.0), modified";

async function waitForReady(page: Page) {
  await page.waitForFunction(() => window.__twinReady === true, null, { timeout: 90_000 });
}

/** Wait until a fly-to has finished and the map has drawn the new view (tiles loaded). */
async function waitForSettled(page: Page) {
  await page.waitForFunction(() => window.__twinMap !== undefined && !window.__twinMap.isMoving(), null, {
    timeout: 30_000,
  });
  await page.evaluate(
    () =>
      new Promise<void>((done) => {
        const map = window.__twinMap!;
        if (map.loaded() && map.areTilesLoaded()) return done();
        map.once("idle", () => done());
      }),
  );
}

test.beforeEach(async ({ page }) => {
  await page.goto("/");
  await waitForReady(page);
});

test("app loads and the map canvas renders", async ({ page }) => {
  await expect(page.getByRole("heading", { name: "Melbourne Traffic Twin" })).toBeVisible();
  const canvas = page.locator("canvas.maplibregl-canvas");
  await expect(canvas).toBeVisible();
  const box = await canvas.boundingBox();
  expect(box?.width).toBeGreaterThan(800);
  // deck.gl layers are interleaved into MapLibre's style as a custom "deck-layer-group-*" layer.
  const deckGroups = await page.evaluate(() =>
    window
      .__twinMap!.getLayersOrder()
      .filter((id) => id.startsWith("deck-layer-group"))
      .map((id) => window.__twinMap!.getLayer(id)?.type),
  );
  expect(deckGroups).toEqual(["custom"]);
  // Labels are drawn UNDER the ghost buildings: every symbol layer comes before the deck.gl group.
  const order = await page.evaluate(() => {
    const m = window.__twinMap!;
    const ids = m.getLayersOrder();
    return {
      symbols: ids.filter((id) => m.getLayer(id)?.type === "symbol").map((id) => ids.indexOf(id)),
      deck: ids.findIndex((id) => id.startsWith("deck-layer-group")),
    };
  });
  expect(order.symbols.length).toBeGreaterThan(0);
  expect(Math.max(...order.symbols)).toBeLessThan(order.deck);
  await expect(page.getByTestId("status")).toHaveText(/\d+ buildings/);
});

test("attribution is visible on the map, including the City of Melbourne credit", async ({ page }) => {
  const attrib = page.locator(".maplibregl-ctrl-attrib");
  await expect(attrib).toBeVisible();
  await expect(attrib).toContainText("© OpenStreetMap contributors");
  await expect(attrib).toContainText("OpenFreeMap");
  await expect(attrib).toContainText("© OpenMapTiles");
  await expect(attrib).toContainText(COM_CREDIT);
  await expect(attrib.getByRole("link", { name: "© OpenStreetMap contributors" })).toBeVisible();
  const com = attrib.getByRole("link", { name: COM_CREDIT });
  await expect(com).toBeVisible();
  await expect(com).toHaveAttribute("href", "https://creativecommons.org/licenses/by/4.0/");
  // Exact on-screen text, base-map credits in their fixed order after the City of Melbourne credit.
  await expect(attrib.locator(".maplibregl-ctrl-attrib-inner")).toHaveText(
    `${COM_CREDIT} | OpenFreeMap © OpenMapTiles © OpenStreetMap contributors`,
  );
  // One line at 1440 px wide, and nothing drawn over any of the links.
  const viewport = page.viewportSize();
  if (viewport?.width === 1440) {
    const lines = await attrib.locator(".maplibregl-ctrl-attrib-inner").evaluate((el) => {
      const lh = parseFloat(getComputedStyle(el).lineHeight) || 1.2 * parseFloat(getComputedStyle(el).fontSize);
      return Math.round(el.getBoundingClientRect().height / lh);
    });
    expect(lines).toBe(1);
  }
  const covered = await attrib.locator("a").evaluateAll((links) =>
    links
      .filter((a) => {
        const r = a.getBoundingClientRect();
        const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
        return !(hit && (hit === a || a.contains(hit)));
      })
      .map((a) => a.textContent),
  );
  expect(covered).toEqual([]);
  // Near-solid background so the credits stay legible over bright buildings.
  const bg = await attrib.evaluate((el) => getComputedStyle(el).backgroundColor);
  const alpha = Number(/rgba?\(([^)]+)\)/.exec(bg)?.[1]?.split(",")[3] ?? 1);
  expect(alpha).toBeGreaterThanOrEqual(0.9);
});

test("base map uses OpenFreeMap tiles only, and no icons", async ({ page }) => {
  const style = await page.evaluate(() => window.__twinMap!.getStyle());
  expect(Object.keys(style.sources)).toEqual(["openmaptiles"]);
  expect(JSON.stringify(style.sources)).toContain("https://tiles.openfreemap.org/planet");
  expect(style.sprite ?? []).toEqual([]);
  const tiles = await page.evaluate(() =>
    performance
      .getEntriesByType("resource")
      .map((r) => new URL(r.name))
      .filter((u) => u.host !== location.host)
      .map((u) => u.host),
  );
  expect(tiles.length).toBeGreaterThan(0);
  expect(new Set(tiles)).toEqual(new Set(["tiles.openfreemap.org"]));
});

test("fixture banner shows only for the fixture", async ({ page }, testInfo) => {
  const banner = page.getByTestId("fixture-banner");
  if (testInfo.project.name === "fixture") {
    await expect(banner).toBeVisible();
    await expect(banner).toContainText("TEST FIXTURE DATA");
    await expect(page.getByTestId("status")).toHaveText(/^9 buildings · [\d,]+ elevated roads$/);
  } else {
    await expect(banner).toHaveCount(0);
    await expect(page.getByTestId("status")).toHaveText(/^\d{1,3}(,\d{3})+ buildings · [\d,]+ elevated roads$/); // real data: thousands
  }
});

test("dev fps readout is shown", async ({ page }) => {
  await expect(page.getByTestId("fps")).toHaveText(/\d+ fps/, { timeout: 5_000 });
});

test("camera presets fly to each view; screenshots", async ({ page }, testInfo) => {
  mkdirSync(SCREENSHOT_DIR, { recursive: true });
  const prefix = testInfo.project.name === "fixture" ? "fixture-" : "";
  for (const preset of CAMERA_PRESETS) {
    await page.getByRole("button", { name: preset.label, exact: true }).click();
    await expect(page.getByRole("button", { name: preset.label, exact: true })).toHaveAttribute("aria-pressed", "true");
    await waitForSettled(page);
    const cam = await page.evaluate(() => {
      const m = window.__twinMap!;
      return { lng: m.getCenter().lng, lat: m.getCenter().lat, zoom: m.getZoom(), pitch: m.getPitch(), bearing: m.getBearing() };
    });
    expect(cam.lng).toBeCloseTo(preset.view.longitude, 4);
    expect(cam.lat).toBeCloseTo(preset.view.latitude, 4);
    expect(cam.zoom).toBeCloseTo(preset.view.zoom, 2);
    expect(cam.pitch).toBeCloseTo(preset.view.pitch, 1);
    expect(cam.bearing).toBeCloseTo(preset.view.bearing, 1);
    await page.screenshot({ path: resolve(SCREENSHOT_DIR, `${prefix}${preset.id}.png`) });
  }
});
