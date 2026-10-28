// Scenario switch (am_peak <-> baseline) on the real-data server. Until the real sim/out am_peak files land, am_peak is
// a labelled test fixture routed with page.route (nothing on disk): the trips fixture shifted to 07:30 on a 06:30 sim
// clock (timestamps 3600..3900, like am_peak's 3600..5400) with a start_local meta. Baseline is the real synced file.
import { readFileSync } from "node:fs";
import { expect, test } from "@playwright/test";
import { FIXTURE_TRIPS, waitForReady } from "./support/trips";

const SHIFT_S = 3600;
const START_LOCAL = "2026-10-23T06:30:00+11:00";

test("switches am_peak <-> baseline: trips' own time range, local clock, badge, road speeds", async ({ page }, info) => {
  test.skip(info.project.name !== "real", "the real-data server only");
  const trips = (JSON.parse(readFileSync(FIXTURE_TRIPS.trips, "utf8")) as { timestamps: number[] }[]).map((t) => ({
    ...t,
    timestamps: t.timestamps.map((s) => s + SHIFT_S),
  }));
  const meta = {
    ...(JSON.parse(readFileSync(FIXTURE_TRIPS.meta, "utf8")) as Record<string, unknown>),
    scenario: "am_peak",
    duration_s: 5400,
    start_local: START_LOCAL,
    _notes: ["TEST FIXTURE: the trips fixture shifted by 3600 s for the scenario-switch e2e (page.route only)."],
  };
  const amPeak = { scenario: "am_peak", source: "fixture", trips: "sw_am_peak.json", meta: "sw_am_peak.meta.json" };
  const baseline = (await (await page.request.get("/data/scenarios.json")).json()) as { scenarios: { scenario: string }[] };
  const realBaseline = baseline.scenarios.find((s) => s.scenario === "baseline");
  expect(realBaseline, "the real baseline is synced").toBeTruthy();
  await page.route("**/data/traffic.json", (r) => r.fulfill({ json: amPeak }));
  await page.route("**/data/scenarios.json", (r) => r.fulfill({ json: { default: "am_peak", scenarios: [amPeak, realBaseline] } }));
  await page.route("**/data/sw_am_peak.json", (r) => r.fulfill({ json: trips }));
  await page.route("**/data/sw_am_peak.meta.json", (r) => r.fulfill({ json: meta }));

  await page.goto("/");
  await waitForReady(page);
  const am = page.getByRole("button", { name: "AM peak" });
  const base = page.getByRole("button", { name: "Baseline" });
  const clockText = page.getByTestId("sim-clock");
  const slider = page.getByRole("slider", { name: "Playback position" });

  // Default am_peak: replays 3600..3900 (07:30-07:35), Melbourne time on the clock.
  await expect(am).toHaveAttribute("aria-pressed", "true");
  await expect(slider).toHaveAttribute("min", "3600");
  await expect(slider).toHaveAttribute("max", "3900");
  await page.evaluate(() => {
    window.__twinPlayback!.pause();
    window.__twinPlayback!.seek(3720);
  });
  await expect(clockText).toHaveText("Fri 23 Oct 2026 · 07:32");
  await page.evaluate(() => window.__twinPlayback!.seek(0)); // clamps to the range start
  await expect(clockText).toHaveText("Fri 23 Oct 2026 · 07:30");
  await expect(page.getByTestId("placeholder-badge")).toHaveText("Synthetic baseline"); // a fixture, not real counts
  await expect(page.getByTestId("cars")).not.toHaveText(/^0 cars$/);

  // Baseline: 0..900, mm:ss clock, placeholder badge, real road speeds.
  await base.click();
  await expect(base).toHaveAttribute("aria-pressed", "true");
  await expect(clockText).toHaveText(/^\d\d:\d\d \/ 15:00$/);
  await expect(slider).toHaveAttribute("min", "0");
  await expect(slider).toHaveAttribute("max", "900");
  expect(await page.evaluate(() => window.__twinPlayback!.time)).toBeLessThan(900);
  await expect(page.getByTestId("placeholder-badge")).toHaveText("Synthetic baseline");
  await expect(page.getByRole("checkbox", { name: "Road speeds" })).toBeEnabled();
  await expect(page.getByTestId("fixture-banner")).toHaveCount(0);

  // And back: the clock moves to the am_peak range start.
  await am.click();
  await expect(slider).toHaveAttribute("min", "3600");
  expect(await page.evaluate(() => window.__twinPlayback!.time)).toBeGreaterThanOrEqual(3600);
  await expect(clockText).toHaveText(/^Fri 23 Oct 2026 · 07:3\d$/);
  await expect(page.locator("pre[role=alert]")).toHaveCount(0);
});
