// qa-reviewer: Phase 3B scenario switch / local clock / demand badge (frontend-engineer's phase-3b/am-peak-web),
// checked independently against the REAL attempt-5 am_peak export (sim/out/am_peak.meta.json: duration_s 10800,
// start_local 2026-10-23T06:30:00+11:00, vehicles 9622 DISTINCT over 9754 trip pieces; trips 3600..5400 on the
// 06:30 clock). Runs on review-real only; the am_peak files must be in sim/out (copied from phase-3b/am-peak).
//
// Test 1 is the contract: the app must accept the real meta as committed. It FAILS on this branch:
// web/src/data/tripsContract.ts:238 still reads meta.vehicles as the number of trips, but the contract (settled
// 2026-10-28, commit 92b7995: exporter + tests/contracts) counts DISTINCT vehicles, folding teleport splits
// "<id>#N". Until the web folds them too, the real am_peak export is rejected with
// "[traffic] meta.vehicles is 9622 but the file has 9754 trips" and the app never gets ready.
//
// Test 2 routes the same meta with `vehicles` set to the trip-piece count (labelled workaround for the bug above,
// nothing else changed) so the switch/clock/badge behaviour and the look can be reviewed and screenshotted.
// Screenshots: REVIEW_PHASE3_SHOTS_DIR (absolute), else web/test-results/review/phase-3b-web-rerun/.
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Page, test } from "@playwright/test";
import { expectAttributionUnobstructed, hideFps } from "./support/pixels";

const ROOT = resolve(__dirname, "../..");
const SHOTS = process.env.REVIEW_PHASE3_SHOTS_DIR ?? resolve(ROOT, "web/test-results/review/phase-3b-web-rerun");
const META_PATH = resolve(ROOT, "sim/out/am_peak.meta.json");
const REAL_META = existsSync(META_PATH) ? (JSON.parse(readFileSync(META_PATH, "utf8")) as Record<string, unknown>) : null;

// Computed from the committed attempt-5 contract files, not from the app:
// start_local 06:30 +11:00 Fri 23 Oct 2026; trips span 3600..5400 -> playback range [3600, 5400] (whole minutes).
const AM_RANGE = { min: "3600", max: "5400" };
const AM_BADGE = "Real counts · Fri 23 Oct 2026 · calibrated 7–8 am";
const AM_CLOCK_AT_4320 = "Fri 23 Oct 2026 · 07:42"; // 06:30 + 4320 s

test.beforeEach(({}, info) => {
  test.skip(info.project.name !== "review-real", "runs in review-real only");
  test.skip(REAL_META === null, "sim/out/am_peak.meta.json not present in this worktree (merge/rebase phase-3b/am-peak first)");
});

async function ready(page: Page) {
  await page.goto("/");
  await page.waitForFunction(() => window.__twinReady === true, null, { timeout: 150_000 });
}

async function still(page: Page, time: number) {
  await page.evaluate(
    (t) =>
      new Promise<void>((done) => {
        const pb = window.__twinPlayback!;
        pb.pause();
        pb.seek(t);
        const map = window.__twinMap!;
        map.once("idle", () => done());
        map.triggerRepaint();
      }),
    time,
  );
  await page.waitForTimeout(500);
}

test("the app accepts the real am_peak meta as committed (vehicles = distinct vehicles)", async ({ page }) => {
  await page.goto("/");
  // Either the app gets ready or a [traffic] contract alert appears; the alert is a failure.
  await page.waitForFunction(
    () => window.__twinReady === true || !!document.querySelector('[role="alert"]'),
    null,
    { timeout: 150_000 },
  );
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(await page.evaluate(() => window.__twinReady)).toBe(true);
});

test("am_peak default: real-counts badge, Melbourne clock, trips' own range, attribution (meta.vehicles patched: see header)", async ({ page }) => {
  await page.route("**/data/am_peak.meta.json", (r) =>
    r.fulfill({ json: { ...REAL_META, vehicles: 9754 } }), // trip-piece count; ONLY to get past tripsContract.ts:238
  );
  await ready(page);
  mkdirSync(SHOTS, { recursive: true });
  const am = page.getByRole("button", { name: "AM peak" });
  const base = page.getByRole("button", { name: "Baseline" });
  const slider = page.getByRole("slider", { name: "Playback position" });
  const clock = page.getByTestId("sim-clock");

  // Default scenario and its range come from the real export, not from 0.
  await expect(am).toHaveAttribute("aria-pressed", "true");
  await expect(slider).toHaveAttribute("min", AM_RANGE.min);
  await expect(slider).toHaveAttribute("max", AM_RANGE.max);
  // Calibrated demand: the badge names the counts day and the calibrated hours; never "Placeholder".
  await expect(page.getByTestId("demand-badge")).toHaveText(AM_BADGE);
  await expect(page.getByTestId("placeholder-badge")).toHaveCount(0);
  // The clock is Melbourne wall-clock time derived from meta.start_local.
  await still(page, 4320);
  await expect(clock).toHaveText(AM_CLOCK_AT_4320);
  // Cars on the road mid-peak, and the attribution stays visible over the map.
  await expect(page.getByTestId("cars")).not.toHaveText(/^0 cars/);
  await expectAttributionUnobstructed(page);
  await hideFps(page);
  await page.screenshot({ path: resolve(SHOTS, "am-peak-0742.png") });

  // Switch to baseline: mm:ss clock from 0, placeholder badge; and back again.
  await base.click();
  await expect(base).toHaveAttribute("aria-pressed", "true");
  await expect(slider).toHaveAttribute("min", "0");
  await expect(clock).toHaveText(/^\d\d:\d\d \/ 15:00$/);
  await expect(page.getByTestId("placeholder-badge")).toHaveText("Placeholder traffic");
  await page.waitForTimeout(800);
  await hideFps(page);
  await page.screenshot({ path: resolve(SHOTS, "baseline-after-switch.png") });

  await am.click();
  await expect(slider).toHaveAttribute("min", AM_RANGE.min);
  await expect(page.getByTestId("demand-badge")).toHaveText(AM_BADGE);
});
