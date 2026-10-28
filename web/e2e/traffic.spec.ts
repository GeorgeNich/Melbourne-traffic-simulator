// Traffic playback. Two dev servers (playwright.config.ts):
//   "fixture" (TWIN_USE_FIXTURE=1): the labelled trips fixture synced by sync-data; badge, controls, toggle, glow.
//   "real": the real sim/out/trips_baseline.json; a routed "none" manifest checks the no-traffic note, routed trips the rest.
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { getPreset } from "../src/camera/presets";
import { FIXTURE_TRIPS, frameAt, routeTrips, trafficBlueFraction, waitForReady } from "./support/trips";

const SHOTS = resolve(import.meta.dirname, "../screenshots/traffic");
/** Domain Tunnel close-up (fixture tunnel car fixture-domain-tunnel-west-1 is underground around t = 45 s). */
const TUNNEL_VIEW = { longitude: 144.9715, latitude: -37.8262, zoom: 16.4, pitch: 62, bearing: -15 };

/**
 * deck.gl layer props from the interleaved deck instance (map.__deck is deck.gl's own handle on the map), for one
 * traffic pass across its time buckets, e.g. "traffic-core" -> traffic-core-b0, -b1, ... Returns the VISIBLE ones.
 */
async function visibleLayers(page: import("@playwright/test").Page, pass: string) {
  return page.evaluate((pass) => {
    const deck = (window.__twinMap as unknown as { __deck: { props: { layers: { id: string; props: Record<string, unknown> }[] } } }).__deck;
    return deck.props.layers
      .filter((l) => new RegExp(`^${pass}-b\\d+$`).test(l.id) && l.props.visible)
      .map((l) => ({ id: l.id, currentTime: l.props.currentTime as number }));
  }, pass);
}

test.describe("fixture server: the labelled trips fixture", () => {
  test.beforeEach(async ({ page }, info) => {
    test.skip(info.project.name !== "fixture", "the fixture server only");
    await page.goto("/");
    await waitForReady(page);
  });

  test("placeholder badge, fixture banner and sim clock", async ({ page }) => {
    const playback = page.getByTestId("playback");
    await expect(playback).toBeVisible();
    const badge = page.getByTestId("placeholder-badge");
    await expect(badge).toHaveText("Synthetic baseline");
    await expect(badge).toHaveAttribute("title", /not simulated traffic/);
    await expect(page.getByTestId("fixture-banner")).toContainText("hand-made cars, not simulated traffic");
    await expect(page.getByTestId("fixture-banner")).toContainText("hand-made rectangles, not real buildings");
    await expect(page.getByTestId("sim-clock")).toHaveText(/^\d\d:\d\d \/ 05:00$/);
    // The badge is muted UI chrome, never a traffic colour.
    const color = await badge.evaluate((el) => getComputedStyle(el).color);
    const [r, g, b] = (/rgba?\(([^)]+)\)/.exec(color)?.[1] ?? "").split(",").map(Number);
    expect((Math.max(r!, g!, b!) - Math.min(r!, g!, b!)) / 255).toBeLessThan(0.25);
  });

  test("play / pause, scrub and speed controls drive the clock", async ({ page }) => {
    const button = page.getByTestId("play-pause");
    await expect(button).toHaveAttribute("aria-label", "Pause"); // autoplay
    await button.click();
    await expect(button).toHaveAttribute("aria-label", "Play");
    const t0 = await page.evaluate(() => window.__twinPlayback!.time);
    await page.waitForTimeout(600);
    expect(await page.evaluate(() => window.__twinPlayback!.time)).toBe(t0); // paused

    // Scrub: the range input sets the time; the clock shows it.
    await page.getByRole("slider", { name: "Playback position" }).fill("150");
    await expect(page.getByTestId("sim-clock")).toHaveText("02:30 / 05:00");
    const at150 = await visibleLayers(page, "traffic-core");
    expect(at150.length).toBeGreaterThan(0);
    expect(at150.every((l) => l.currentTime === 150)).toBe(true);
    expect(at150.map((l) => l.id)).toContain("traffic-core-b2"); // 150 s is in bucket 2 (120-180 s)

    // Speeds 1x / 4x / 10x / 30x.
    const group = page.getByRole("group", { name: "Playback speed" });
    expect(await group.getByRole("button").allInnerTexts()).toEqual(["1×", "4×", "10×", "30×"]);
    await expect(group.getByRole("button", { name: "1×" })).toHaveAttribute("aria-pressed", "true");
    await group.getByRole("button", { name: "30×" }).click();
    await expect(group.getByRole("button", { name: "30×" })).toHaveAttribute("aria-pressed", "true");
    await button.click(); // play at 30x
    await page.waitForTimeout(1000);
    const t1 = await page.evaluate(() => window.__twinPlayback!.time);
    expect(t1 - 150).toBeGreaterThan(10); // ~30 s of sim time per real second (minus frame caps)
    const later = await visibleLayers(page, "traffic-core");
    expect(Math.max(...later.map((l) => l.currentTime))).toBeGreaterThan(150);
  });

  test("Show tunnels hides and shows the X-ray tunnel cars only", async ({ page }) => {
    const toggle = page.getByRole("checkbox", { name: "Show tunnels" });
    await expect(toggle).toBeChecked();
    await frameAt(page, TUNNEL_VIEW, 45);
    expect((await visibleLayers(page, "traffic-tunnel-core")).length).toBeGreaterThan(0);
    const on = await page.screenshot();
    await toggle.uncheck();
    await expect(toggle).not.toBeChecked();
    expect(await visibleLayers(page, "traffic-tunnel-core")).toEqual([]);
    expect((await visibleLayers(page, "traffic-core")).length).toBeGreaterThan(0);
    await frameAt(page, TUNNEL_VIEW, 45);
    const off = await page.screenshot();
    // The underground car's glow is gone with the toggle off.
    expect(await trafficBlueFraction(page, on)).toBeGreaterThan(await trafficBlueFraction(page, off));
    // The car count leaves out the hidden tunnel cars and says how many there are.
    await expect(page.getByTestId("cars-hidden")).toHaveText("+1 in tunnels");
    const hiddenCount = Number((await page.getByTestId("cars").textContent())!.split(" ")[0]);
    await toggle.check();
    await expect(page.getByTestId("cars-hidden")).toHaveCount(0);
    await expect(page.getByTestId("cars")).toHaveText(`${hiddenCount + 1} cars`);
  });

  test("the playback panel is one line at 1440 px", async ({ page }) => {
    const panel = page.getByTestId("playback");
    const rows = await panel.evaluate((el) => new Set(Array.from(el.children).map((c) => Math.round(c.getBoundingClientRect().top / 8))).size);
    const box = (await panel.boundingBox())!;
    expect(rows, "all controls on one row").toBe(1);
    expect(box.height).toBeLessThan(50);
  });
});

test.describe("real server: no trips exported (routed), routed trips", () => {
  test.beforeEach(({ page }, info) => {
    void page;
    test.skip(info.project.name !== "real", "the real-data server only");
  });

  test("the app runs without cars and says so", async ({ page }) => {
    // sim/out now has real trips, so serve the manifest sync-data writes when none are exported yet.
    await page.route("**/data/traffic.json", (r) => r.fulfill({ json: { scenario: "baseline", source: "none", trips: null, meta: null } }));
    await page.goto("/");
    await waitForReady(page);
    await expect(page.getByTestId("traffic-note")).toHaveText("No traffic yet: sim/out/trips_baseline.json has not been exported.");
    await expect(page.getByTestId("playback")).toHaveCount(0);
    await expect(page.getByTestId("placeholder-badge")).toHaveCount(0);
    await expect(page.locator("pre[role=alert]")).toHaveCount(0);
  });

  test("glowing cars draw over the ghost city at the Interchange and CBD; attribution stays visible", async ({ page }) => {
    await routeTrips(page, FIXTURE_TRIPS);
    await page.goto("/");
    await waitForReady(page);
    await page.addStyleTag({ content: '[data-testid="fps"] { display: none !important; }' });
    mkdirSync(SHOTS, { recursive: true });
    for (const id of ["interchange", "cbd"] as const) {
      await frameAt(page, getPreset(id).view, 130);
      const png = await page.screenshot({ path: resolve(SHOTS, `fixture-${id}.png`) });
      // The fixture is sparse (a handful of cars per view): this only checks that cars are drawn at all.
      expect(await trafficBlueFraction(page, png), `${id}: some traffic blue on screen`).toBeGreaterThan(0.00005);
    }
    const attrib = page.locator(".maplibregl-ctrl-attrib");
    await expect(attrib).toContainText("© OpenStreetMap contributors");
    await expect(attrib).toContainText("OpenFreeMap");
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
    // The playback panel sits above the attribution bar.
    const panel = (await page.getByTestId("playback").boundingBox())!;
    const bar = (await attrib.boundingBox())!;
    expect(panel.y + panel.height).toBeLessThanOrEqual(bar.y);
  });

  test("calibrated demand shows the real-counts badge, not the placeholder one", async ({ page }) => {
    // Small hand-written real-shaped trips (test-only, inline).
    const tripsJson = [{ id: "veh0", type: "passenger", path: [[144.96, -37.818, 0], [144.961, -37.8177, 0]], timestamps: [0, 9] }];
    const metaJson = {
      scenario: "am_peak",
      demand: "calibrated_counts",
      duration_s: 900,
      seed: 1,
      sumo_version: "1.28.0",
      vehicles: 1,
      teleports: 0,
      tunnel_vehicles: 0,
      generated_at: "2026-10-27T00:00:00Z",
    };
    await routeTrips(page, { tripsJson, metaJson }, "real");
    await page.goto("/");
    await waitForReady(page);
    await expect(page.getByTestId("playback")).toBeVisible();
    await expect(page.getByTestId("placeholder-badge")).toHaveCount(0);
    await expect(page.getByTestId("demand-badge")).toHaveText("Real counts · calibrated 7–8 am"); // no start_local: no date
    await expect(page.getByTestId("fixture-banner")).toHaveCount(0);
    // Playback runs over the trips' own range (0..9 s, rounded out to a whole minute), not the meta duration.
    await expect(page.getByTestId("sim-clock")).toHaveText(/\/ 01:00$/);
  });

  test("a traffic contract error shows in the alert panel and the app never reports ready", async ({ page }) => {
    // 2D positions (the pre-Phase-2 shape) and an unknown demand label: both break the contract.
    const tripsJson = [{ id: "veh0", type: "passenger", path: [[144.96, -37.818], [144.961, -37.8177]], timestamps: [0, 9] }];
    const metaJson = { scenario: "baseline", demand: "guessed", duration_s: 900, seed: 1, sumo_version: "1.28.0", vehicles: 1, teleports: 0, tunnel_vehicles: 0, generated_at: "2026-10-27T00:00:00Z" };
    await routeTrips(page, { tripsJson, metaJson }, "real");
    await page.goto("/");
    const alert = page.locator("pre[role=alert]");
    await expect(alert).toContainText("[traffic] routed_trips.meta.json: meta: demand", { timeout: 60_000 });
    await expect(alert).toContainText("not [lon, lat, z_m]");
    await expect(page.getByTestId("playback")).toHaveCount(0);
    await page.waitForTimeout(1500);
    expect(await page.evaluate(() => window.__twinReady)).toBeUndefined();
  });
});
