// qa-reviewer: Phase 2 Stage A clearance-based heights (roads_3d z_source) review screenshots.
// Saves to docs/screenshots/phase-2/stage-a/heights/ (1440x900, real data, dev fps readout hidden):
//   interchange / footscray in the normal view and the dev-only ?heights=source view, the CBD, and a
//   Princes Bridge close-up (deck now near street level, measured 0.1-0.2 m in the 2018 point cloud), a Power St
//   close-up (bridges over the CityLink cutting, about 2-3 m above street) and the Bolte Bridge / CityLink viaduct
//   in Docklands (re-review finding: drawn at 1.5 m).
// Also checks that ?heights=source draws something different from the normal view (dev) and that the normal view
// shows no alert. Run like review_web.spec.ts, limited to this file:
//   NODE_PATH=$PWD/web/node_modules PLAYWRIGHT_BROWSERS_PATH=0 \
//     pnpm -C web exec playwright test --config ../tests/e2e/playwright.review.config.ts review_heights \
//     --project review-real
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Page, test } from "@playwright/test";

// The Stage A record is only rewritten with REVIEW_STAGE_A_SHOTS=1; otherwise re-shoots go to gitignored test-results.
const SHOTS = process.env.REVIEW_STAGE_A_SHOTS
  ? resolve(__dirname, "../../docs/screenshots/phase-2/stage-a/heights")
  : resolve(__dirname, "../../web/test-results/review/stage-a-rerun/heights");
const HIDE_FPS = '[data-testid="fps"] { display: none !important; }';

/** Princes Bridge (ways 52137278/9) from just west of it over the river, low, looking east along the bridge side. */
const PRINCES_BRIDGE = { center: [144.9682, -37.8193] as [number, number], zoom: 18.4, pitch: 70, bearing: 65 };

/** Power St bridges over the CityLink cutting (ways 5234831, 172516956, 5234818), low, looking west along CityLink. */
const POWER_ST = { center: [144.9661, -37.8264] as [number, number], zoom: 18.2, pitch: 68, bearing: -75 };
/** Bolte Bridge (34314083) and the CityLink viaduct north of it, from the south-east, looking north-west. */
const BOLTE = { center: [144.9345, -37.8165] as [number, number], zoom: 16.3, pitch: 68, bearing: -40 };

/** Crown car-park ramp (1132332719, ~11 % in the cloud), from the south-east side, looking north-west. */
const CROWN_RAMP = { center: [144.9592, -37.8252] as [number, number], zoom: 17.7, pitch: 55, bearing: 135 };

test.beforeEach(({}, info) => test.skip(info.project.name !== "review-real", "real data only"));

async function ready(page: Page, query = "") {
  await page.goto(`/${query}`);
  await page.waitForFunction(() => window.__twinReady === true, null, { timeout: 90_000 });
  await expect(page.locator("pre[role=alert]")).toHaveCount(0);
  await page.addStyleTag({ content: HIDE_FPS });
}

async function settled(page: Page) {
  await page.waitForFunction(() => window.__twinMap !== undefined && !window.__twinMap.isMoving(), null, { timeout: 30_000 });
  await page.evaluate(
    () =>
      new Promise<void>((done) => {
        const map = window.__twinMap!;
        if (map.loaded() && map.areTilesLoaded()) return done();
        map.once("idle", () => done());
      }),
  );
  await page.waitForTimeout(600);
}

async function preset(page: Page, label: string) {
  await page.getByRole("button", { name: label, exact: true }).click();
  await settled(page);
}

test("heights review screenshots: normal and ?heights=source", async ({ page }) => {
  mkdirSync(SHOTS, { recursive: true });
  const pngs: Record<string, Buffer> = {};
  for (const [view, query] of [
    ["normal", ""],
    ["source", "?heights=source"],
  ] as const) {
    await ready(page, query);
    for (const [id, label] of [
      ["interchange", "Interchange"],
      ["footscray", "Footscray Rd"],
    ] as const) {
      await preset(page, label);
      pngs[`${view}-${id}`] = await page.screenshot({ path: resolve(SHOTS, `${id}-${view}.png`) });
    }
    if (view === "normal") {
      await preset(page, "CBD");
      await page.screenshot({ path: resolve(SHOTS, "cbd.png") });
      await page.evaluate((v) => void window.__twinMap!.jumpTo(v), PRINCES_BRIDGE);
      await settled(page);
      await page.screenshot({ path: resolve(SHOTS, "princes-bridge.png") });
      for (const [name, view] of [
        ["power-st", POWER_ST],
        ["bolte-citylink", BOLTE],
        ["crown-ramp", CROWN_RAMP],
      ] as const) {
        await page.evaluate((v) => void window.__twinMap!.jumpTo(v), view);
        await settled(page);
        await page.screenshot({ path: resolve(SHOTS, `${name}.png`) });
      }
    }
  }
  // The dev-only source view must actually change the picture (estimated decks dimmer and dashed).
  expect(pngs["source-interchange"]!.equals(pngs["normal-interchange"]!)).toBe(false);
});
