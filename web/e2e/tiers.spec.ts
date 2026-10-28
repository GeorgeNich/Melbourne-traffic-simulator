// Screenshots of the stacked-tier buildings (real data): CBD preset, a close-up of 101/120 Collins, Southbank with
// Eureka, and a low side view. Saved to web/screenshots/buildings-tiers/. Run: pnpm -C web e2e --project=real tiers
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "@playwright/test";

const DIR = resolve(import.meta.dirname, "../screenshots/buildings-tiers");
type View = { longitude: number; latitude: number; zoom: number; pitch: number; bearing: number };
const VIEWS: Record<string, View> = {
  "cbd": { longitude: 144.9628, latitude: -37.8145, zoom: 15.3, pitch: 55, bearing: -20 },
  "collins-closeup": { longitude: 144.9701, latitude: -37.8152, zoom: 16.7, pitch: 68, bearing: 0 },
  "southbank-eureka": { longitude: 144.9645, latitude: -37.8219, zoom: 16, pitch: 55, bearing: 30 },
  "side-view": { longitude: 144.9651, latitude: -37.8208, zoom: 16.4, pitch: 74, bearing: 300 },
};

test("tier screenshots", async ({ page }, info) => {
  test.setTimeout(300_000);
  test.skip(info.project.name !== "real", "real data only");
  mkdirSync(DIR, { recursive: true });
  await page.goto("/");
  await page.waitForFunction(() => window.__twinReady === true, null, { timeout: 90_000 });
  await page.addStyleTag({ content: '[data-testid="fps"] { display: none !important; }' });
  const only = process.env.TIERS_ONLY?.split(",");
  for (const [name, view] of Object.entries(VIEWS)) {
    if (only && !only.includes(name)) continue;
    await page.evaluate((v) => void window.__twinMap!.jumpTo({ center: [v.longitude, v.latitude], zoom: v.zoom, pitch: v.pitch, bearing: v.bearing }), view);
    await page.waitForFunction(() => !window.__twinMap!.isMoving());
    await page.evaluate(() => new Promise<void>((done) => { const m = window.__twinMap!; if (m.loaded() && m.areTilesLoaded()) return done(); m.once("idle", () => done()); }));
    await page.waitForTimeout(800);
    await page.screenshot({ path: resolve(DIR, `${name}.png`) });
  }
});
