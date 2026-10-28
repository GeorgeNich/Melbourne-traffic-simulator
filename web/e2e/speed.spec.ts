// Road speeds (Phase 3B), "real" project: the real baseline edge_stats + edge_geometry, real buildings and roads.
//   - a known jammed edge renders red and a known flowing edge blue at a known time (pixels, cars hidden);
//   - the "Road speeds" toggle hides them; Show tunnels hides the X-ray speed passes;
//   - redraw on demand: paused and still = 0 frames with the layer on; the toggle wakes one redraw, then quiet again.
// Opt-in stills (SPEED_SHOTS=1): the Phase 2 gate views and every preset with road speeds, into
// web/screenshots/phase-3-speed/, plus the CBD haze metric (qa's: share of the central band with luma > 150).
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Page, test } from "@playwright/test";
import { CAMERA_PRESETS } from "../src/camera/presets";
import { GATE_VIEWS, HIDE_UI, showAt } from "./support/gate";
import { countOver, countRedraws, installFrameCounter, waitForSettled } from "./support/redraws";
import { frameAt, waitForReady } from "./support/trips";

/**
 * Picked from sim/out/edge_stats_baseline.parquet at t = 480 s (bin 480–540 s), with the app's smoothing (current
 * minute + half the previous one, vehicle-weighted): surface edges at least 90 m long, at least 4 vehicles, and no
 * other edge with data within 25 m of their midpoint (so no neighbour's glow mixes in).
 *   963120895#0  ratio 0.260 over 7 vehicles: jammed (red)
 *   207516091#0  ratio 1.012 over 5.5 vehicles: flowing (blue), above the limit (driver speed factors)
 */
const T = 480;
const KNOWN = [
  { edge: "963120895#0", cls: "jammed", at: [144.9553998764779, -37.81589024534112] },
  { edge: "207516091#0", cls: "flowing", at: [144.9567968404535, -37.818904902998185] },
] as const;

const SHOTS = resolve(import.meta.dirname, "../screenshots/phase-3-speed");

/** Hide (or show) the CAR layers only: every traffic layer that isn't a road-speed pass. */
async function setCars(page: Page, visible: boolean) {
  await page.evaluate((visible) => {
    type L = { id: string; clone: (p: Record<string, unknown>) => L };
    const deck = (window.__twinMap as unknown as { __deck: { props: { layers: L[] }; setProps: (p: { layers: L[] }) => void } }).__deck;
    const isCar = (id: string) => id.startsWith("traffic") && !/^traffic-(tunnel-)?speed-/.test(id);
    deck.setProps({ layers: deck.props.layers.map((l) => (isCar(l.id) ? l.clone({ visible }) : l)) });
    window.__twinMap!.triggerRepaint();
  }, visible);
  await page.waitForTimeout(300);
}

/** Hide every traffic layer (cars and road speeds), as qa's haze metric does. */
async function setAllTraffic(page: Page, visible: boolean) {
  await page.evaluate((visible) => {
    type L = { id: string; clone: (p: Record<string, unknown>) => L };
    const deck = (window.__twinMap as unknown as { __deck: { props: { layers: L[] }; setProps: (p: { layers: L[] }) => void } }).__deck;
    deck.setProps({ layers: deck.props.layers.map((l) => (l.id.startsWith("traffic") ? l.clone({ visible }) : l)) });
    window.__twinMap!.triggerRepaint();
  }, visible);
  await page.waitForTimeout(400);
}

type Hues = { red: number; amber: number; blue: number; other: number; n: number };

/** Saturated pixels (chroma > 0.25) by traffic hue in a `half`-px box around a screen point (CSS px). */
async function huesAround(page: Page, png: Buffer, x: number, y: number, half: number): Promise<Hues> {
  return page.evaluate(
    async ({ b64, x, y, half }) => {
      const img = new Image();
      img.src = `data:image/png;base64,${b64}`;
      await img.decode();
      const c = document.createElement("canvas");
      c.width = img.width;
      c.height = img.height;
      const ctx = c.getContext("2d")!;
      ctx.drawImage(img, 0, 0);
      const s = img.width / window.innerWidth;
      const [x0, y0, w] = [Math.round((x - half) * s), Math.round((y - half) * s), Math.round(2 * half * s)];
      const { data } = ctx.getImageData(x0, y0, w, w);
      const out = { red: 0, amber: 0, blue: 0, other: 0, n: w * w };
      for (let i = 0; i < data.length; i += 4) {
        const r = data[i]!, g = data[i + 1]!, b = data[i + 2]!;
        const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
        if (d / 255 <= 0.25) continue;
        let h = mx === r ? 60 * (((g - b) / d) % 6) : mx === g ? 60 * ((b - r) / d + 2) : 60 * ((r - g) / d + 4);
        if (h < 0) h += 360;
        if (h >= 200 && h <= 232) out.blue++;
        else if (h >= 25 && h <= 50) out.amber++;
        else if (h >= 345 || h <= 12) out.red++;
        else out.other++;
      }
      return out;
    },
    { b64: png.toString("base64"), x, y, half },
  );
}

/** qa's wash-out metric (tests/e2e/review_web.spec.ts brightFraction): central band share with luma > 150. */
async function brightFraction(page: Page, png: Buffer) {
  return page.evaluate(async (b64) => {
    const img = new Image();
    img.src = `data:image/png;base64,${b64}`;
    await img.decode();
    const c = document.createElement("canvas");
    c.width = img.width;
    c.height = img.height;
    const ctx = c.getContext("2d")!;
    ctx.drawImage(img, 0, 0);
    const { data } = ctx.getImageData(Math.round(c.width * 0.25), Math.round(c.height * 0.2), Math.round(c.width * 0.5), Math.round(c.height * 0.6));
    let bright = 0;
    for (let i = 0; i < data.length; i += 4) if (0.2126 * data[i]! + 0.7152 * data[i + 1]! + 0.0722 * data[i + 2]! > 150) bright++;
    return bright / (data.length / 4);
  }, png.toString("base64"));
}

const speedsToggle = (page: Page) => page.getByRole("checkbox", { name: "Road speeds" });

test.describe("road speeds, real baseline", () => {
  // eslint-disable-next-line no-empty-pattern -- Playwright needs a destructuring pattern for the fixtures argument
  test.beforeEach(({}, info) => test.skip(info.project.name !== "real", "the real-data server only"));

  test("loads: toggle on with a legend, manifest lists the scenario's files, no contract messages", async ({ page }) => {
    const msgs: string[] = [];
    page.on("console", (m) => {
      if ((m.type() === "warning" || m.type() === "error") && m.text().includes("[speeds]")) msgs.push(m.text());
    });
    await page.goto("/");
    await waitForReady(page);
    // the app boots into am_peak when it is synced (scenario switch, 2026-10-28); these are
    // the baseline expectations, so pin the scenario first
    await page.getByRole("button", { name: "Baseline" }).click();
    await expect(page.getByTestId("placeholder-badge")).toHaveText("Placeholder traffic");
    await expect(speedsToggle(page)).toBeChecked();
    await expect(page.getByTestId("speed-legend")).toHaveText(/flowing.*slowing.*jammed/);
    await expect(page.locator("pre[role=alert]")).toHaveCount(0);
    const manifest = await page.evaluate(async () => (await fetch("/data/scenarios.json")).json());
    const baseline = manifest.scenarios.find((s: { scenario: string }) => s.scenario === "baseline");
    expect(baseline.speeds).toEqual({ edgeGeometry: "edge_geometry.json", edgeStats: "edge_stats_baseline.parquet" });
    await expect
      .poll(() => page.evaluate(() => ({ nEdges: window.__twinSpeeds!.table.nEdges, nBins: window.__twinSpeeds!.table.nBins })))
      .toEqual({ nEdges: 3955, nBins: 15 });
    expect(msgs).toEqual([]);
    const marks = await page.evaluate(() =>
      Object.fromEntries(["speeds-fetch-start", "speeds-parsed", "speeds-prepared", "ready"].map((m) => [m, Math.round(performance.getEntriesByName(`twin:${m}`)[0]?.startTime ?? -1)])),
    );
    console.log("speeds load marks (ms, headless dev):", JSON.stringify(marks));
  });

  test("a known jammed edge renders red and a known flowing edge blue at t = 480 s; the toggle hides them", async ({ page }) => {
    await page.goto("/");
    await waitForReady(page);
    // pin the baseline scenario (the KNOWN edges' classes are baseline values)
    await page.getByRole("button", { name: "Baseline" }).click();
    await expect(page.getByTestId("placeholder-badge")).toHaveText("Placeholder traffic");
    await expect.poll(() => page.evaluate(() => window.__twinSpeeds!.table.nBins)).toBe(15);
    for (const k of KNOWN) {
      // The data says so (the app's smoothed value), then the pixels do.
      const s = await page.evaluate(({ edge, t }) => window.__twinSpeeds!.speedAt(edge, t), { edge: k.edge, t: T + 30 });
      expect(s, k.edge).not.toBeNull();
      expect(k.cls === "jammed" ? s!.ratio < 0.4 : s!.ratio >= 0.7, `${k.edge} ratio ${s!.ratio}`).toBe(true);

      await frameAt(page, { longitude: k.at[0], latitude: k.at[1], zoom: 18.5, pitch: 0, bearing: 0 }, T + 30);
      await setCars(page, false);
      const p = await page.evaluate((at) => window.__twinMap!.project(at as [number, number]), k.at);
      const on = await huesAround(page, await page.screenshot(), p.x, p.y, 6);
      console.log(k.edge, "speeds on:", JSON.stringify(on));
      if (k.cls === "jammed") {
        expect(on.red, `${k.edge}: red pixels`).toBeGreaterThan(10);
        expect(on.blue + on.amber).toBeLessThan(on.red / 4);
      } else {
        expect(on.blue, `${k.edge}: blue pixels`).toBeGreaterThan(10);
        expect(on.red + on.amber).toBeLessThan(on.blue / 4);
      }

      await speedsToggle(page).uncheck();
      await setCars(page, false); // the toggle rebuilt the frame layers
      const off = await huesAround(page, await page.screenshot(), p.x, p.y, 6);
      console.log(k.edge, "speeds off:", JSON.stringify(off));
      expect(off.red + off.amber + off.blue, `${k.edge}: nothing saturated with Road speeds off`).toBe(0);
      await speedsToggle(page).check();
    }
  });

  test("X-ray speed passes follow Show tunnels; surface passes don't", async ({ page }) => {
    await page.goto("/");
    await waitForReady(page);
    const vis = () =>
      page.evaluate(() =>
        (window.__twinMap as unknown as { __deck: { props: { layers: { id: string; props: { visible: boolean } }[] } } }).__deck.props.layers
          .filter((l) => /^traffic-(tunnel-)?speed-/.test(l.id))
          .map((l) => `${l.id}:${l.props.visible}`),
      );
    expect(await vis()).toEqual([
      "traffic-speed-thru-halo:true",
      "traffic-speed-thru-core:true",
      "traffic-speed-halo:true",
      "traffic-speed-core:true",
      "traffic-tunnel-speed-halo:true",
      "traffic-tunnel-speed-core:true",
    ]);
    await page.getByRole("checkbox", { name: "Show tunnels" }).uncheck();
    await expect.poll(vis).toEqual([
      "traffic-speed-thru-halo:true",
      "traffic-speed-thru-core:true",
      "traffic-speed-halo:true",
      "traffic-speed-core:true",
      "traffic-tunnel-speed-halo:false",
      "traffic-tunnel-speed-core:false",
    ]);
  });

  test("redraw on demand: paused and still = 0 frames with road speeds on; the toggle redraws, then quiet", async ({ page }) => {
    test.setTimeout(180_000);
    await installFrameCounter(page);
    await page.goto("/");
    await waitForReady(page);
    await countRedraws(page);
    await expect(speedsToggle(page)).toBeChecked();
    await page.getByTestId("play-pause").click();
    await expect(page.getByTestId("play-pause")).toHaveAttribute("aria-label", "Play");
    await waitForSettled(page);
    const paused = await countOver(page, 2000);
    expect(paused, "paused + still, speeds on: nothing").toEqual({ raf: 0, renders: 0, deckDraws: 0, seconds: paused.seconds });

    // The toggle is a deck prop change: it must wake deck.gl and get drawn (then everything sleeps again).
    await speedsToggle(page).uncheck();
    const toggled = await countOver(page, 600);
    expect(toggled.deckDraws, "toggle off drew").toBeGreaterThan(0);
    await waitForSettled(page);
    const after = await countOver(page, 1500);
    expect(after, "quiet again after the toggle").toEqual({ raf: 0, renders: 0, deckDraws: 0, seconds: after.seconds });

    // A seek across a bin edge while paused changes the colours: one redraw, then quiet.
    await speedsToggle(page).check();
    await waitForSettled(page);
    await page.evaluate(() => window.__twinPlayback!.seek(605));
    const seek = await countOver(page, 600);
    expect(seek.deckDraws, "seek drew").toBeGreaterThan(0);
    await waitForSettled(page);
    const quiet = await countOver(page, 1500);
    expect(quiet).toEqual({ raf: 0, renders: 0, deckDraws: 0, seconds: quiet.seconds });
  });

  test("stills: Phase 2 gate views and presets with road speeds; CBD haze (opt-in SPEED_SHOTS=1)", async ({ page }) => {
    test.skip(process.env.SPEED_SHOTS !== "1", "opt-in: SPEED_SHOTS=1");
    test.setTimeout(400_000);
    mkdirSync(SHOTS, { recursive: true });
    await page.goto("/");
    await waitForReady(page);
    await page.screenshot({ path: resolve(SHOTS, "ui-default.png") }); // the panel with the toggle and legend
    await page.addStyleTag({ content: HIDE_UI });
    for (const v of GATE_VIEWS) {
      await showAt(page, v);
      await page.screenshot({ path: resolve(SHOTS, `${v.id}.png`) });
    }
    // The panel is hidden by HIDE_UI, so the Road speeds input is clicked directly.
    const clickSpeeds = () =>
      page.evaluate(() => [...document.querySelectorAll("label")].find((l) => l.textContent?.includes("Road speeds"))!.querySelector("input")!.click());
    const haze: Record<string, Record<string, number>> = {};
    for (const p of CAMERA_PRESETS) {
      await showAt(page, { id: p.id, label: p.label, view: p.view, time: T });
      const withAll = await page.screenshot({ path: resolve(SHOTS, `preset-${p.id}.png`) });
      await setCars(page, false);
      const speedsOnly = await page.screenshot({ path: resolve(SHOTS, `preset-${p.id}-speeds-only.png`) });
      await setAllTraffic(page, false);
      const bare = await page.screenshot();
      await setAllTraffic(page, true);
      await clickSpeeds();
      await page.waitForTimeout(400);
      const carsOnly = await page.screenshot();
      await clickSpeeds();
      await page.waitForTimeout(400);
      haze[p.id] = {
        trafficHidden: await brightFraction(page, bare),
        speedsOnly: await brightFraction(page, speedsOnly),
        carsOnly: await brightFraction(page, carsOnly),
        carsAndSpeeds: await brightFraction(page, withAll),
      };
    }
    // The CBD gate view with Road speeds off, for comparison.
    await clickSpeeds();
    await showAt(page, GATE_VIEWS[0]!);
    await page.screenshot({ path: resolve(SHOTS, `${GATE_VIEWS[0]!.id}-speeds-off.png`) });
    await clickSpeeds();
    console.log("CBD haze (luma > 150 share of the central band):", JSON.stringify(haze));
    writeFileSync(resolve(SHOTS, "haze.json"), JSON.stringify(haze, null, 2) + "\n");
  });
});
