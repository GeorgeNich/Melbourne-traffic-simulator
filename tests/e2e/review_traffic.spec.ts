// qa-reviewer: Phase 2 Stage C glowing-car playback (frontend-engineer's web work), checked independently.
// See playwright.review.config.ts for how to run it. Projects used here:
//   review-trips -> TWIN_USE_FIXTURE=trips: the labelled trips fixture over the REAL buildings and roads. Look,
//                   layers, bucketing, playback, car count; screenshots to web/test-results/review/stage-c-rerun/
//                   (the Stage C record in docs/screenshots/phase-2/stage-c/ is only rewritten with REVIEW_STAGE_C_SHOTS=1).
//   review-real  -> the real baseline export (sim/out/trips_baseline.json, placeholder demand): loads labelled, tunnel
//                   cars classified by the roads_3d tunnel flag (CLAUDE.md), final-look screenshots to
//                   web/test-results/review/phase-2-final-rerun/ (the Phase 2 record in docs/screenshots/phase-2/final/
//                   is only rewritten with REVIEW_PHASE2_FINAL_SHOTS=1; since Phase 3 the default look also shows the
//                   road speeds); plus the bucket-boundary pixel test with routed, labelled
//                   synthetic test trips (page.route only, nothing on disk); production checks with PROD_URL.
// The look is George's Stage C pick (cfb379f): trail 20 s, glow "through" (cars behind buildings at 40%), X-ray tunnel
// cars full strength, 1.6x wider, hollow ring heads, a tether up to a ring on the surface. No look URL params any more.
// Expected values are computed here from tests/fixtures/trips_fixture.json and the brief, not taken from the app.
import { mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, type Page, test } from "@playwright/test";
import { expectAttributionUnobstructed, hideFps, saturationBreakdown } from "./support/pixels";

const ROOT = resolve(__dirname, "../..");
const SHOTS = process.env.REVIEW_STAGE_C_SHOTS
  ? resolve(ROOT, "docs/screenshots/phase-2/stage-c")
  : resolve(ROOT, "web/test-results/review/stage-c-rerun");
/** Final Phase 2 look with the real baseline traffic (qa final pass). The record is not rewritten by default. */
const FINAL_SHOTS = process.env.REVIEW_PHASE2_FINAL_SHOTS
  ? resolve(ROOT, "docs/screenshots/phase-2/final")
  : resolve(ROOT, "web/test-results/review/phase-2-final-rerun");
const FIXTURE = resolve(ROOT, "tests/fixtures/trips_fixture.json");
const FIXTURE_META = resolve(ROOT, "tests/fixtures/trips_fixture.meta.json");

type Trip = { id: string; type: string; path: [number, number, number][]; timestamps: number[] };
const TRIPS = JSON.parse(readFileSync(FIXTURE, "utf8")) as Trip[];
const META = JSON.parse(readFileSync(FIXTURE_META, "utf8")) as { duration_s: number; vehicles: number };

/** Independent: cars on the road at t (first timestamp <= t < last timestamp). */
const activeAt = (t: number) => TRIPS.filter((x) => x.timestamps[0]! <= t && t < x.timestamps.at(-1)!).length;

/** Presets and a playback time with fixture cars in frame (computed from the fixture: Bolte cars at t = 254 s). */
const PRESET_VIEWS = {
  interchange: { label: "Interchange", t: 130 },
  cbd: { label: "CBD", t: 130 },
  footscray: { label: "Footscray Rd", t: 254 },
} as const;
/** Domain Tunnel portal area, looking along the tunnel (fixture tunnel cars underground around t = 45 s). */
const TUNNEL_VIEW = { center: [144.9715, -37.8262] as [number, number], zoom: 16.4, pitch: 62, bearing: -15 };
/** Close-up on fixture-domain-tunnel-west-1, at -12 m (FAKE fixture depth) around t = 40 s. */
const TUNNEL_CLOSE = { center: [144.9722, -37.8256] as [number, number], zoom: 17.6, pitch: 60, bearing: -60 };
/** Palette (web/src/theme/palette.ts): flowing blue, per CLAUDE.md / PLAN.md. */
const FLOWING = [56, 140, 255];
/** George's Stage C look picks (the brief for cfb379f): trail, glow-through share, X-ray width factor. */
const LOOK = { trailS: 20, through: 0.4, xrayWidth: 1.6 } as const;
/** Every look URL param the look review used; all removed in cfb379f, so none may change anything. */
const OLD_LOOK_QUERY = "?look=b&trail=long&glow=soft&roads=faint&xray=X1&edges=all";
const STATUS_REAL = /^\d{1,3}(,\d{3})+ building pieces · \d{1,3}(,\d{3})* elevated roads$/;
const MAX_OTHER_SATURATED = 0.002;

const onlyIn = (name: string) => test.beforeEach(({}, info) => test.skip(info.project.name !== name, `runs in ${name} only`));

async function ready(page: Page, query = "") {
  await page.goto(`/${query}`);
  await page.waitForFunction(() => window.__twinReady === true, null, { timeout: 120_000 });
}

/** Pause at `time`, jump the camera (no fly), wait for the map to go idle and the labels to fade in. */
async function frameAt(page: Page, view: { center: [number, number]; zoom: number; pitch: number; bearing: number } | null, time: number) {
  await page.evaluate(
    ({ view, time }) =>
      new Promise<void>((done) => {
        const pb = window.__twinPlayback!;
        pb.pause();
        pb.seek(time);
        const map = window.__twinMap!;
        if (view) map.jumpTo(view);
        map.once("idle", () => done());
        map.triggerRepaint();
      }),
    { view, time },
  );
  await page.waitForTimeout(500);
}

async function goPreset(page: Page, label: string) {
  const btn = page.getByRole("button", { name: label, exact: true });
  await btn.click();
  await expect(btn).toHaveAttribute("aria-pressed", "true");
  await page.waitForFunction(() => !window.__twinMap!.isMoving(), null, { timeout: 30_000 });
  await page.evaluate(
    () =>
      new Promise<void>((done) => {
        const map = window.__twinMap!;
        if (map.loaded() && map.areTilesLoaded()) return done();
        map.once("idle", () => done());
      }),
  );
}

type LayerInfo = { id: string; visible: boolean; currentTime: number; props: Record<string, unknown> };

/** The traffic layers on the interleaved deck (id, visibility, time and a few props). */
async function trafficLayers(page: Page): Promise<LayerInfo[]> {
  return page.evaluate(() => {
    type L = { id: string; props: Record<string, unknown> };
    const deck = (window.__twinMap as unknown as { __deck: { props: { layers: L[] } } }).__deck;
    return deck.props.layers
      .filter((l) => l.id.startsWith("traffic"))
      .map((l) => ({
        id: l.id,
        visible: l.props.visible !== false,
        currentTime: l.props.currentTime as number,
        props: {
          getColor: l.props.getColor,
          color: l.props.color,
          coreColor: l.props.coreColor,
          parameters: l.props.parameters,
          trailLength: l.props.trailLength,
          fadeTrail: l.props.fadeTrail,
          widthUnits: l.props.widthUnits,
          billboard: l.props.billboard,
          getWidth: l.props.getWidth,
          widthMinPixels: l.props.widthMinPixels,
          radiusMeters: l.props.radiusMeters,
          ring: l.props.ring,
          widthPixels: l.props.widthPixels,
        },
      }));
  });
}

/** Every head instance (segment) of every traffic bucket: [t0, t1] pairs, surface and tunnel separately. */
async function headIntervals(page: Page) {
  return page.evaluate(() => {
    type L = { id: string; props: { data: { length: number; attributes: { getTimes: { value: Float32Array } } } } };
    const deck = (window.__twinMap as unknown as { __deck: { props: { layers: L[] } } }).__deck;
    const out: { tunnel: boolean; bucket: number; times: number[] }[] = [];
    for (const l of deck.props.layers) {
      const m = /^traffic(-tunnel)?-heads-b(\d+)$/.exec(l.id);
      if (!m) continue;
      out.push({ tunnel: !!m[1], bucket: Number(m[2]), times: Array.from(l.props.data.attributes.getTimes.value) });
    }
    return out;
  });
}

async function shot(page: Page, name: string | null) {
  await expectAttributionUnobstructed(page);
  await expect(page.getByTestId("playback")).toBeVisible();
  const png = await page.screenshot(name ? { path: resolve(SHOTS, `${name}.png`) } : {});
  const banner = await page.getByTestId("fixture-banner").boundingBox();
  const sat = await saturationBreakdown(page, png, banner ? [banner] : []);
  return { png, sat };
}

// ---------------------------------------------------------------------------------------------------------------
test.describe("trips fixture over the real buildings (TWIN_USE_FIXTURE=trips)", () => {
  onlyIn("review-trips");

  test("loads labelled: real buildings, fixture cars, badge, no contract messages", async ({ page }) => {
    const msgs: string[] = [];
    page.on("console", (m) => {
      if ((m.type() === "warning" || m.type() === "error") && m.text().includes("[traffic]")) msgs.push(m.text());
    });
    await ready(page);
    await expect(page.getByTestId("status")).toHaveText(STATUS_REAL);
    const banner = page.getByTestId("fixture-banner");
    await expect(banner).toContainText("hand-made cars, not simulated traffic");
    await expect(banner).not.toContainText("rectangles");
    await expect(page.getByTestId("placeholder-badge")).toHaveText("Synthetic baseline");
    await expect(page.getByTestId("placeholder-badge")).toHaveAttribute("title", /not simulated traffic/);
    await expect(page.getByTestId("sim-clock")).toHaveText(/^\d\d:\d\d \/ 05:00$/);
    await expect(page.locator("pre[role=alert]")).toHaveCount(0);
    const manifest = await page.evaluate(async () => (await fetch("/data/traffic.json")).json());
    expect(manifest).toEqual({ scenario: "fixture", source: "fixture", trips: "trips_fixture.json", meta: "trips_fixture.meta.json" });
    expect(msgs).toEqual([]);
  });

  test("layers: George's look per 60 s bucket: glow-through passes, X-ray tunnel cars full strength, wider, ring heads, tethers", async ({ page }) => {
    await ready(page);
    await frameAt(page, null, 130);
    const layers = await trafficLayers(page);
    const ids = layers.map((l) => l.id);
    const byId = Object.fromEntries(layers.map((l) => [l.id, l.props]));
    // 0-300 s fixture: surface buckets b0..b4, each with the see-through passes first, then halo / core / heads.
    const SURFACE = ["thru-halo", "thru-core", "thru-heads", "halo", "core", "heads"];
    for (const b of [0, 1, 2, 3, 4]) {
      const i = SURFACE.map((p) => ids.indexOf(`traffic-${p}-b${b}`));
      expect(i.every((x) => x >= 0), `surface bucket ${b}`).toBe(true);
      expect(i.every((x, k) => k === 0 || x > i[k - 1]!), `surface bucket ${b} order`).toBe(true);
    }
    // Tunnel buckets: tethers, the surface ring, then halo / core / heads.
    const TUNNEL = ["tethers", "mark", "halo", "core", "heads"];
    const tunnelBuckets = [...new Set(ids.filter((id) => id.startsWith("traffic-tunnel-")).map((id) => Number(id.split("-b")[1])))];
    expect(tunnelBuckets.length).toBeGreaterThan(0);
    for (const b of tunnelBuckets) {
      const i = TUNNEL.map((p) => ids.indexOf(`traffic-tunnel-${p}-b${b}`));
      expect(i.every((x) => x >= 0), `tunnel bucket ${b}`).toBe(true);
      expect(i.every((x, k) => k === 0 || x > i[k - 1]!), `tunnel bucket ${b} order`).toBe(true);
    }
    expect(ids.every((id) => /^traffic-(thru-(halo|core|heads)|halo|core|heads|tunnel-(tethers|mark|halo|core|heads))-b\d+$/.test(id))).toBe(true);
    const hue = (c: number[]) => {
      const [r, g, b] = c as [number, number, number];
      const mx = Math.max(r, g, b), mn = Math.min(r, g, b);
      return 60 * ((r - g) / (mx - mn) + 4);
    };
    for (const l of layers) {
      const tunnel = l.id.startsWith("traffic-tunnel");
      const thru = l.id.startsWith("traffic-thru-");
      const p = l.props.parameters as Record<string, unknown>;
      expect(p, l.id).toMatchObject({
        blend: true,
        blendColorOperation: "add",
        blendColorDstFactor: "one",
        depthWriteEnabled: false,
        depthCompare: tunnel ? "always" : thru ? "greater" : "less-equal",
      });
      const rgba = (l.props.getColor ?? l.props.color) as number[];
      expect(rgba.slice(0, 3), `${l.id} colour`).toEqual(FLOWING);
      if (/-(heads|mark)-/.test(l.id)) {
        // Lifted toward white, same hue as the flowing blue.
        expect(Math.abs(hue(l.props.coreColor as number[]) - hue(FLOWING)), `${l.id} core hue`).toBeLessThan(1);
        expect(l.props.ring, `${l.id} ring`).toBe(tunnel ? 1 : 0);
      } else if (/-(halo|core)-/.test(l.id)) {
        expect(l.props.widthUnits).toBe("meters");
        expect(l.props.billboard).toBe(false);
        expect(l.props.fadeTrail).toBe(true);
        expect(l.props.trailLength).toBe(LOOK.trailS);
      }
    }
    const alpha = (id: string) => ((byId[id]!.getColor ?? byId[id]!.color) as number[])[3]!;
    const tb = tunnelBuckets[0]!;
    // Glow through: the see-through passes are the normal ones at 40%.
    for (const pass of ["halo", "core"]) expect(alpha(`traffic-thru-${pass}-b${tb}`)).toBe(Math.round(alpha(`traffic-${pass}-b${tb}`) * LOOK.through));
    expect(alpha(`traffic-thru-heads-b${tb}`)).toBe(Math.round(255 * LOOK.through));
    expect(alpha(`traffic-heads-b${tb}`)).toBe(255);
    // X-ray tunnel cars: full strength (no longer dimmed), 1.6x wider trails and heads.
    for (const pass of ["halo", "core", "heads"]) expect(alpha(`traffic-tunnel-${pass}-b${tb}`), pass).toBe(alpha(`traffic-${pass}-b${tb}`));
    for (const pass of ["halo", "core"]) {
      expect(byId[`traffic-tunnel-${pass}-b${tb}`]!.getWidth as number).toBeCloseTo((byId[`traffic-${pass}-b${tb}`]!.getWidth as number) * LOOK.xrayWidth, 6);
    }
    expect(byId[`traffic-tunnel-heads-b${tb}`]!.radiusMeters as number).toBeCloseTo((byId[`traffic-heads-b${tb}`]!.radiusMeters as number) * LOOK.xrayWidth, 6);
    // Tethers: thin, translucent; the surface ring dimmer than the car.
    expect(byId[`traffic-tunnel-tethers-b${tb}`]!.widthPixels as number).toBeLessThanOrEqual(2);
    expect(alpha(`traffic-tunnel-tethers-b${tb}`)).toBeLessThan(255);
    expect(alpha(`traffic-tunnel-mark-b${tb}`)).toBeLessThan(255);
  });

  test("bucketing: each car has exactly one head at any time, no segment twice, no gap; visibility by trail window", async ({ page }) => {
    await ready(page);
    const heads = await headIntervals(page);
    const all = heads.flatMap((h) => {
      const out: [number, number][] = [];
      for (let k = 0; k < h.times.length; k += 2) out.push([h.times[k]!, h.times[k + 1]!]);
      return out;
    });
    // Every segment sits in the bucket of its start time.
    for (const h of heads) {
      for (let k = 0; k < h.times.length; k += 2) expect(Math.floor(h.times[k]! / 60)).toBe(h.bucket);
    }
    // Sample every 0.25 s plus every bucket boundary and every vertex time of the fixture.
    const times = new Set<number>();
    for (let t = 0; t <= META.duration_s; t += 0.25) times.add(t);
    for (const tr of TRIPS) for (const s of tr.timestamps) times.add(s);
    let bad = 0;
    const examples: string[] = [];
    for (const t of times) {
      // The GPU compares Float32 times (head times and the currentTime uniform): do the same.
      const tf = Math.fround(t);
      const drawn = all.filter(([a, b]) => a <= tf && tf < b).length;
      const want = activeAt(t);
      if (drawn !== want) {
        bad++;
        if (examples.length < 5) examples.push(`t=${t}: ${drawn} heads, ${want} cars`);
      }
    }
    expect(bad, examples.join("; ")).toBe(0);

    // Visible buckets at a time = those whose span overlaps [t - trail, t] (trail 20 s); at most a few drawn.
    for (const t of [0, 59.9, 60, 61, 75, 130, 179.99, 180, 299]) {
      await frameAt(page, null, t);
      const layers = (await trafficLayers(page)).filter((l) => l.id.startsWith("traffic-core-"));
      for (const l of layers) {
        const b = Number(l.id.split("-b")[1]);
        const h = heads.find((x) => !x.tunnel && x.bucket === b)!;
        const lo = Math.min(...h.times.filter((_, k) => k % 2 === 0));
        const hi = Math.max(...h.times.filter((_, k) => k % 2 === 1));
        expect(l.visible, `t=${t} ${l.id} span ${lo}-${hi}`).toBe(lo <= t && hi >= t - 20);
        if (l.visible) expect(l.currentTime).toBe(t);
      }
      expect(layers.filter((l) => l.visible).length).toBeLessThanOrEqual(4);
    }
  });

  test("car count matches the active trips; scrub and mm:ss clock", async ({ page }) => {
    await ready(page);
    const slider = page.getByRole("slider", { name: "Playback position" });
    await expect(slider).toHaveAttribute("max", String(META.duration_s));
    for (const t of [0, 15, 59.5, 60, 100, 150.5, 222, 299.5, 300]) {
      await frameAt(page, null, t);
      const want = activeAt(t);
      await expect(page.getByTestId("cars")).toHaveText(`${want.toLocaleString("en-US")} ${want === 1 ? "car" : "cars"}`);
      const s = Math.floor(t);
      const mmss = `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
      await expect(page.getByTestId("sim-clock")).toHaveText(`${mmss} / 05:00`);
    }
    // "Show tunnels" off: cars underground aren't drawn, so they aren't counted; "+N in tunnels" says so. Independent:
    // at t = 40 s exactly one fixture car (fixture-domain-tunnel-west-1) is below z -0.5 m.
    const underAt = (t: number) =>
      TRIPS.filter((x) => {
        const k = x.timestamps.findIndex((s) => s > t);
        return k > 0 && (x.path[k - 1]![2] + x.path[k]![2]) / 2 < -0.5;
      }).length;
    expect(underAt(40)).toBe(1);
    const toggle = page.getByRole("checkbox", { name: "Show tunnels" });
    await toggle.uncheck();
    await frameAt(page, null, 40);
    await expect(page.getByTestId("cars")).toHaveText(`${activeAt(40) - 1} cars`);
    await expect(page.getByTestId("cars-hidden")).toHaveText("+1 in tunnels");
    await toggle.check();
    await expect(page.getByTestId("cars")).toHaveText(`${activeAt(40)} cars`);
    await expect(page.getByTestId("cars-hidden")).toHaveCount(0);
    // Scrub with the slider itself (keyboard + fill), paused.
    await slider.fill("187");
    await expect(page.getByTestId("sim-clock")).toHaveText("03:07 / 05:00");
    expect(await page.evaluate(() => window.__twinPlayback!.time)).toBe(187);
    await slider.press("End");
    await expect(page.getByTestId("sim-clock")).toHaveText("05:00 / 05:00");
    await slider.press("Home");
    await expect(page.getByTestId("sim-clock")).toHaveText("00:00 / 05:00");
  });

  test("speeds: at 30x the 300 s fixture loops in ~10 s of wall clock; 4x and 1x rates", async ({ page }) => {
    await ready(page);
    const group = page.getByRole("group", { name: "Playback speed" });
    expect(await group.getByRole("button").allInnerTexts()).toEqual(["1×", "4×", "10×", "30×"]);
    // Measure the sim-time rate over the wall clock in the page (rAF timestamps), per speed.
    const rate = async (speed: string, wallS: number) => {
      await page.evaluate(() => {
        window.__twinPlayback!.pause();
        window.__twinPlayback!.seek(0);
      });
      await group.getByRole("button", { name: speed }).click();
      await expect(group.getByRole("button", { name: speed })).toHaveAttribute("aria-pressed", "true");
      return page.evaluate(
        (wallS) =>
          new Promise<{ simS: number; wallS: number }>((done) => {
            const pb = window.__twinPlayback!;
            pb.play();
            const t0 = performance.now();
            const s0 = pb.time;
            const step = (now: number) => {
              if (now - t0 >= wallS * 1000) done({ simS: pb.time - s0, wallS: (now - t0) / 1000 });
              else requestAnimationFrame(step);
            };
            requestAnimationFrame(step);
          }),
        wallS,
      );
    };
    const r1 = await rate("1×", 3);
    const r4 = await rate("4×", 3);
    console.log(`1x: ${(r1.simS / r1.wallS).toFixed(2)} sim s / s; 4x: ${(r4.simS / r4.wallS).toFixed(2)}`);
    expect(r1.simS / r1.wallS).toBeGreaterThan(0.9);
    expect(r1.simS / r1.wallS).toBeLessThan(1.1);
    expect(r4.simS / r4.wallS).toBeGreaterThan(3.6);
    expect(r4.simS / r4.wallS).toBeLessThan(4.4);

    // 30x: wall time from 0 until the clock wraps back to the start.
    await page.evaluate(() => {
      window.__twinPlayback!.pause();
      window.__twinPlayback!.seek(0);
    });
    await group.getByRole("button", { name: "30×" }).click();
    const loop = await page.evaluate(
      () =>
        new Promise<{ wallS: number; frames: number; maxGapMs: number }>((done) => {
          const pb = window.__twinPlayback!;
          pb.play();
          const t0 = performance.now();
          let last = pb.time;
          let lastNow = t0;
          let frames = 0;
          let maxGapMs = 0;
          const step = (now: number) => {
            frames++;
            maxGapMs = Math.max(maxGapMs, now - lastNow);
            lastNow = now;
            const t = pb.time;
            if (t < last) return done({ wallS: (now - t0) / 1000, frames, maxGapMs });
            last = t;
            if (now - t0 > 30_000) return done({ wallS: Infinity, frames, maxGapMs });
            requestAnimationFrame(step);
          };
          requestAnimationFrame(step);
        }),
    );
    console.log(`30x loop of the 300 s fixture: ${loop.wallS.toFixed(2)} s wall clock, ${loop.frames} frames, max gap ${loop.maxGapMs.toFixed(0)} ms`);
    expect(loop.wallS).toBeGreaterThan(9.5);
    expect(loop.wallS).toBeLessThan(10.8);
    // Pause stops the clock.
    await page.getByTestId("play-pause").click();
    await expect(page.getByTestId("play-pause")).toHaveAttribute("aria-label", "Play");
    const a = await page.evaluate(() => window.__twinPlayback!.time);
    await page.waitForTimeout(500);
    expect(await page.evaluate(() => window.__twinPlayback!.time)).toBe(a);
  });

  test("Stage C screenshots: interchange, cbd, footscray; tunnel X-ray on / off; only traffic blue is saturated", async ({ page }) => {
    mkdirSync(SHOTS, { recursive: true });
    await ready(page);
    await hideFps(page);
    const metrics: Record<string, { trafficBlue: number; other: number; otherHues: Record<string, number> }> = {};
    for (const [id, { label, t }] of Object.entries(PRESET_VIEWS)) {
      await goPreset(page, label);
      await frameAt(page, null, t);
      const { sat } = await shot(page, id);
      metrics[id] = sat;
    }
    // Tunnel X-ray: toggle on, then off, same frame.
    const toggle = page.getByRole("checkbox", { name: "Show tunnels" });
    await expect(toggle).toBeChecked();
    await frameAt(page, TUNNEL_VIEW, 45);
    const on = await shot(page, "tunnel-xray-on");
    metrics["tunnel-xray-on"] = on.sat;
    await toggle.uncheck();
    await frameAt(page, null, 45);
    const off = await shot(page, "tunnel-xray-off");
    metrics["tunnel-xray-off"] = off.sat;
    await toggle.check();
    await frameAt(page, TUNNEL_CLOSE, 40);
    metrics["tunnel-xray-closeup-on"] = (await shot(page, "tunnel-xray-closeup-on")).sat;
    await toggle.uncheck();
    await frameAt(page, null, 40);
    metrics["tunnel-xray-closeup-off"] = (await shot(page, "tunnel-xray-closeup-off")).sat;
    await toggle.check();
    console.log("stage C look metrics:", JSON.stringify(metrics));
    for (const [id, m] of Object.entries(metrics)) {
      expect(m.other, `${id}: saturated pixels outside the traffic palette`).toBeLessThan(MAX_OTHER_SATURATED);
    }
    for (const id of ["interchange", "cbd", "footscray", "tunnel-xray-closeup-on"]) {
      expect(metrics[id]!.trafficBlue, `${id}: cars drawn`).toBeGreaterThan(0.0001);
    }
    expect(metrics["tunnel-xray-on"]!.trafficBlue).toBeGreaterThan(metrics["tunnel-xray-off"]!.trafficBlue * 1.2);
    expect(metrics["tunnel-xray-closeup-on"]!.trafficBlue).toBeGreaterThan(metrics["tunnel-xray-closeup-off"]!.trafficBlue * 1.2);
  });

  test("occlusion: buildings dim cars behind them (glow through); elevated-road decks do not hide cars on them", async ({ page }) => {
    await ready(page);
    await hideFps(page);
    const shotWith = async (hidePrefixes: string[]) => {
      await page.evaluate((pre) => {
        type L = { id: string; clone: (p: Record<string, unknown>) => L };
        const deck = (window.__twinMap as unknown as { __deck: { props: { layers: L[] }; setProps: (p: { layers: L[] }) => void } }).__deck;
        deck.setProps({ layers: deck.props.layers.map((l) => (pre.some((p) => l.id.startsWith(p)) ? l.clone({ visible: false }) : l)) });
        window.__twinMap!.triggerRepaint();
      }, hidePrefixes);
      await page.waitForTimeout(500);
      const png = await page.screenshot();
      return { png, blue: (await saturationBreakdown(page, png, [(await page.getByTestId("fixture-banner").boundingBox())!])).trafficBlue };
    };
    const blueWith = async (hidePrefixes: string[]) => (await shotWith(hidePrefixes)).blue;
    // Paused, so the traffic layers are not re-set by the clock while a pass is hidden. Re-framing restores them.
    await goPreset(page, "CBD");
    await frameAt(page, null, 130);
    const cbdShot = await shotWith([]);
    const cbd = cbdShot.blue;
    const cbdNoBuildings = await blueWith(["buildings-"]);
    const noThroughShot = await shotWith(["traffic-thru-"]);
    const cbdNoThrough = noThroughShot.blue;
    // Glow through adds light behind buildings (additive pile-ups can push saturated blue to cyan-white, so the
    // saturated-blue share is not a monotone measure here): count changed pixels instead.
    const through = await imageDiff(page, noThroughShot.png, cbdShot.png);
    await goPreset(page, "Footscray Rd");
    await frameAt(page, null, PRESET_VIEWS.footscray.t);
    const bolte = await blueWith([]);
    const bolteNoRoads = await blueWith(["roads-"]);
    console.log(
      `traffic blue: CBD ${cbd.toFixed(5)} -> ${cbdNoBuildings.toFixed(5)} without buildings, ${cbdNoThrough.toFixed(5)} without the glow-through passes; ` +
        `Footscray ${bolte.toFixed(5)} -> ${bolteNoRoads.toFixed(5)} without road decks`,
    );
    expect(cbdNoBuildings, "cars behind buildings are dimmer than in plain view").toBeGreaterThan(cbd * 1.05);
    console.log("glow-through pixels changed (> 8):", through.over8);
    expect(through.over8, "cars behind buildings still glow through them").toBeGreaterThan(200);
    // The translucent deck tints the glow a little (16% fewer saturated pixels with the strong glow, cfb379f); a deck
    // that hid the cars would remove most of them.
    expect(Math.abs(bolteNoRoads - bolte) / bolte, "decks don't hide cars on them").toBeLessThan(0.25);
  });

  test("the old look URL params change nothing in dev (George's picks are the only look)", async ({ page }) => {
    const props = async (q: string) => {
      await ready(page, q);
      await frameAt(page, null, 130);
      const layers = await trafficLayers(page);
      const roads = await page.evaluate(() => ["road_major", "road_motorway"].map((id) => String(window.__twinMap!.getPaintProperty(id, "line-color"))));
      const edges = await page.evaluate(() => {
        type L = { id: string; props: { data: unknown[] } };
        const deck = (window.__twinMap as unknown as { __deck: { props: { layers: L[] } } }).__deck;
        return deck.props.layers.filter((l) => l.id.startsWith("buildings-") || l.id.startsWith("tunnels-")).map((l) => [l.id, l.props.data.length]);
      });
      return { traffic: layers.map((l) => [l.id, JSON.stringify(l.props)]), roads, edges };
    };
    const plain = await props("");
    const old = await props(OLD_LOOK_QUERY);
    expect(old).toEqual(plain);
    expect(plain.traffic.length).toBeGreaterThan(0);
  });
});

// ---------------------------------------------------------------------------------------------------------------
// Bucket boundary in pixels (round 1 finding: double-drawn round caps where two trail pieces met at a 60 s bucket edge
// left a phantom car-like dot). A labelled synthetic test car (routed with page.route, never on disk) at 10 m/s,
// sampled every 0.5 s like the SUMO export, flying at z = 30 m so no building hides it. "test": a frame whose 20 s
// trail spans the t = 60 s bucket edge. "control": the same car 30 s later, rendered 30 s later, so geometry, camera,
// background and trail age are identical but its trail spans no bucket edge. Any double-drawn or missing fragment at
// the edge shows as a pixel difference. Shapes: straight; a 90 degree bend exactly at the edge vertex (t = 60); a
// bend one sample before it (t = 59.5). Times: t = 75 (edge 15 s back in the trail) and t = 60.25 (edge just behind
// the head).
const BOUNDARY = { lon0: 144.9405, lat: -37.8215, z: 30, speed: 10, startS: 20, steps: 160 };
type Shape = "straight" | "bend-at-edge" | "bend-before-edge";
const BEND_STEP: Record<Shape, number | null> = { straight: null, "bend-at-edge": 80, "bend-before-edge": 79 };
const M_PER_DEG_LAT = 110_574;
const M_PER_DEG_LON = 111_320 * Math.cos((BOUNDARY.lat * Math.PI) / 180);

/** Position after k samples (5 m each): east, then north after the bend step. */
function boundaryPos(k: number, shape: Shape): [number, number] {
  const bend = BEND_STEP[shape];
  const step = 0.5 * BOUNDARY.speed;
  const east = bend === null ? k : Math.min(k, bend);
  const north = bend === null ? 0 : Math.max(0, k - bend);
  return [BOUNDARY.lon0 + (east * step) / M_PER_DEG_LON, BOUNDARY.lat + (north * step) / M_PER_DEG_LAT];
}

function syntheticTrips(shiftS: number, shape: Shape = "straight") {
  const path: [number, number, number][] = [];
  const timestamps: number[] = [];
  for (let k = 0; k <= BOUNDARY.steps; k++) {
    const [lon, lat] = boundaryPos(k, shape);
    path.push([Number(lon.toFixed(7)), Number(lat.toFixed(7)), BOUNDARY.z]);
    timestamps.push(BOUNDARY.startS + shiftS + k * 0.5);
  }
  return {
    trips: [{ id: "fixture-qa-boundary", type: "passenger", path, timestamps }],
    meta: {
      scenario: "fixture",
      demand: "test_fixture",
      duration_s: 300,
      seed: 0,
      sumo_version: "none: qa-reviewer synthetic test car",
      vehicles: 1,
      teleports: 0,
      tunnel_vehicles: 0,
      generated_at: "2026-10-27T00:00:00Z",
      _test_fixture: true,
      _notes: ["TEST FIXTURE: qa-reviewer synthetic car for the bucket-boundary pixel test (page.route only)."],
    },
  };
}

async function routeSynthetic(page: Page, shiftS: number, shape: Shape = "straight") {
  const { trips, meta } = syntheticTrips(shiftS, shape);
  await page.unrouteAll();
  await page.route("**/data/traffic.json", (r) =>
    r.fulfill({ json: { scenario: "fixture", source: "fixture", trips: "qa_trips.json", meta: "qa_trips.meta.json" } }),
  );
  await page.route("**/data/qa_trips.json", (r) => r.fulfill({ json: trips }));
  await page.route("**/data/qa_trips.meta.json", (r) => r.fulfill({ json: meta }));
}

/** Render the synthetic car at sim time `t` (+ shift), camera centred on the t = 60 s edge vertex, optionally butt caps. */
async function renderBoundary(page: Page, o: { shape: Shape; t: number; shift: number; butt?: boolean }) {
  await routeSynthetic(page, o.shift, o.shape);
  await ready(page);
  await hideFps(page);
  const edge = boundaryPos(80, o.shape); // t = 60 s
  await frameAt(page, { center: edge, zoom: 18, pitch: 0, bearing: 0 }, o.t + o.shift);
  if (o.butt) {
    await page.evaluate(() => {
      type L = { id: string; clone: (p: Record<string, unknown>) => L };
      const deck = (window.__twinMap as unknown as { __deck: { props: { layers: L[] }; setProps: (p: { layers: L[] }) => void } }).__deck;
      deck.setProps({ layers: deck.props.layers.map((l) => (/^traffic-(halo|core)-b/.test(l.id) ? l.clone({ capRounded: false }) : l)) });
      window.__twinMap!.triggerRepaint();
    });
    await page.waitForTimeout(600);
  }
  // The playback panel shows the (different) clock: mask it, the trail never passes under it in this framing.
  return page.screenshot({ mask: [page.getByTestId("playback")], maskColor: "#000000" });
}

/** Whole-image test vs control: max channel difference, pixels differing by > 8, and the brightest trail channel. */
async function imageDiff(page: Page, a: Buffer, b: Buffer) {
  return page.evaluate(
    async ({ a, b }) => {
      const load = async (s: string) => {
        const img = new Image();
        img.src = `data:image/png;base64,${s}`;
        await img.decode();
        const c = document.createElement("canvas");
        c.width = img.width;
        c.height = img.height;
        const ctx = c.getContext("2d")!;
        ctx.drawImage(img, 0, 0);
        return ctx.getImageData(0, 0, c.width, c.height).data;
      };
      const [A, B] = [await load(a), await load(b)];
      let maxDiff = 0;
      let over8 = 0;
      let maxBlue = 0;
      const where: number[][] = [];
      const w = Math.round(Math.sqrt((A.length / 4) * 1.6)); // 1440 x 900 viewport
      for (let i = 0; i < A.length; i += 4) {
        const d = Math.max(Math.abs(A[i]! - B[i]!), Math.abs(A[i + 1]! - B[i + 1]!), Math.abs(A[i + 2]! - B[i + 2]!));
        maxDiff = Math.max(maxDiff, d);
        if (d > 8) {
          over8++;
          if (where.length < 3) where.push([(i / 4) % w, Math.floor(i / 4 / w), d]);
        }
        if (B[i + 2]! - B[i]! > 60) maxBlue = Math.max(maxBlue, B[i + 2]!);
      }
      return { maxDiff, over8, maxBlue, where };
    },
    { a: a.toString("base64"), b: b.toString("base64") },
  );
}

test.describe("bucket boundary, pixels", () => {
  onlyIn("review-real");

  for (const shape of ["straight", "bend-at-edge", "bend-before-edge"] as const) {
    test(`${shape}: nothing drawn twice or missing where a trail crosses a 60 s bucket edge`, async ({ page }) => {
      const results: Record<string, { maxDiff: number; over8: number; maxBlue: number; where: number[][] }> = {};
      for (const t of [75, 60.25]) {
        const test_ = await renderBoundary(page, { shape, t, shift: 0 });
        const control = await renderBoundary(page, { shape, t, shift: 30 });
        results[`t=${t}`] = await imageDiff(page, test_, control);
      }
      console.log(`bucket edge, ${shape}:`, JSON.stringify(results));
      // Round 1 (round caps): peak +69 / +82 over hundreds of pixels. Round 2 measured 0-1 everywhere except a
      // deterministic residual of 9 px <= 15/255 next to the bend vertex for bend-before-edge, strong glow, t = 60.25
      // (known issue, invisible; strong glow is the default look since cfb379f). The limits sit just above that so any
      // return of the knot fails.
      for (const [k, r] of Object.entries(results)) {
        expect(r.maxBlue, `${k}: the trail is on screen`).toBeGreaterThan(60);
        expect(r.over8, `${k}: pixels differing by > 8 between test and control`).toBeLessThanOrEqual(12);
        expect(r.maxDiff, `${k}: largest test-control difference`).toBeLessThan(20);
      }
    });
  }

  // The clip window is a fragment discard: it must apply in the picking pass too (nothing picked ahead of the car or
  // outside the trail), and the trail must still be pickable inside it. Layers made pickable at runtime (the app has
  // no picking yet; Phase 5 will).
  test("picking still works with the clip window: trail and edge pickable, nothing ahead of the car", async ({ page }) => {
    await renderBoundary(page, { shape: "straight", t: 75, shift: 0 });
    // Interleaved deck.gl applies new layer props on the next map render: make the trails pickable, repaint, then pick.
    await page.evaluate(() => {
      type L = { id: string; clone: (p: Record<string, unknown>) => L };
      const deck = (window.__twinMap as unknown as { __deck: { props: { layers: L[] }; setProps: (p: { layers: L[] }) => void } }).__deck;
      deck.setProps({ layers: deck.props.layers.map((l) => (/^traffic-/.test(l.id) ? l.clone({ pickable: true }) : l)) });
      window.__twinMap!.triggerRepaint();
    });
    await page.waitForTimeout(800);
    const r = await page.evaluate(
      ({ pts, z }) => {
        type Deck = {
          getViewports: () => { project: (p: number[]) => number[] }[];
          pickObject: (o: { x: number; y: number; radius: number }) => { layer?: { id: string } } | null;
          pickMultipleObjects: (o: { x: number; y: number; radius: number; depth: number }) => { layer?: { id: string } }[];
        };
        const deck = (window.__twinMap as unknown as { __deck: Deck }).__deck;
        const vp = deck.getViewports()[0]!;
        const out: Record<string, string[]> = {};
        for (const [name, lonlat] of Object.entries(pts)) {
          const [x, y] = vp.project([lonlat[0]!, lonlat[1]!, z]);
          out[name] = deck.pickMultipleObjects({ x: x!, y: y!, radius: 1, depth: 10 }).map((o) => o.layer?.id ?? "?");
        }
        return out;
      },
      {
        // t = 70 s (trail, bucket 1), t = 60 s (the edge), t = 56 s (bucket 0), t = 85 s (10 s ahead of the car).
        pts: { trail70: boundaryPos(100, "straight"), edge60: boundaryPos(80, "straight"), trail56: boundaryPos(72, "straight"), ahead85: boundaryPos(130, "straight") },
        z: BOUNDARY.z + 0.5,
      },
    );
    console.log("picking with the clip window:", JSON.stringify(r));
    const trails = (ids: string[]) => ids.filter((id) => /^traffic-(halo|core)-b\d+$/.test(id));
    expect(trails(r.trail70!).length).toBeGreaterThan(0);
    expect(trails(r.trail70!).every((id) => id.endsWith("-b1"))).toBe(true);
    expect(trails(r.trail56!).every((id) => id.endsWith("-b0"))).toBe(true);
    expect(trails(r.trail56!).length).toBeGreaterThan(0);
    expect(trails(r.edge60!).length).toBeGreaterThan(0);
    expect(r.ahead85!.filter((id) => id.startsWith("traffic"))).toEqual([]);
  });

  // Round 1 diagnostic, inverted after the fix: butt caps (runtime clone, app code untouched) must not change it either.
  test("straight, butt caps cloned at runtime: still no extra or missing light at the edge", async ({ page }) => {
    const test_ = await renderBoundary(page, { shape: "straight", t: 75, shift: 0, butt: true });
    const control = await renderBoundary(page, { shape: "straight", t: 75, shift: 30, butt: true });
    const r = await imageDiff(page, test_, control);
    console.log("bucket edge, straight, butt caps:", JSON.stringify(r));
    expect(r.maxBlue).toBeGreaterThan(60);
    expect(r.over8).toBeLessThanOrEqual(3);
    expect(r.maxDiff).toBeLessThan(16);
  });
});

// ---------------------------------------------------------------------------------------------------------------
test.describe("real baseline export (sim/out/trips_baseline.json)", () => {
  onlyIn("review-real");

  test("loads labelled: placeholder badge, no fixture banner, no note, no contract messages; attribution uncovered", async ({ page }) => {
    const msgs: string[] = [];
    page.on("console", (m) => {
      if ((m.type() === "warning" || m.type() === "error") && m.text().includes("[traffic]")) msgs.push(m.text());
    });
    await ready(page);
    const manifest = await page.evaluate(async () => (await fetch("/data/traffic.json")).json());
    // Phase 3B: real traffic also lists its road speeds (edge_geometry.json + edge_stats_<scenario>.parquet).
    expect(manifest).toEqual({
      scenario: "baseline",
      source: "real",
      trips: "trips_baseline.json",
      meta: "baseline.meta.json",
      speeds: { edgeGeometry: "edge_geometry.json", edgeStats: "edge_stats_baseline.parquet" },
    });
    await expect(page.getByTestId("placeholder-badge")).toHaveText("Synthetic baseline");
    await expect(page.getByTestId("traffic-note")).toHaveCount(0);
    await expect(page.getByTestId("fixture-banner")).toHaveCount(0);
    await expect(page.locator("pre[role=alert]")).toHaveCount(0);
    await expect(page.getByTestId("sim-clock")).toHaveText(/^\d\d:\d\d \/ 15:00$/);
    await expectAttributionUnobstructed(page);
    expect(msgs.filter((m) => !m.includes("tunnel_vehicles"))).toEqual([]);
  });

  // CLAUDE.md (trips contract, ed70512): "The web classifies a car segment as 'in a tunnel' (X-ray, hidden by the Show
  // tunnels toggle) by the tunnel flag of the nearest below-ground roads_3d way, not by z alone, so cars in the open
  // CityLink cutting stay solid." Since cfb379f every X-ray car also hangs from a tether to a ring on the surface.
  // Independent check: every X-ray (tunnel-group) head segment whose midpoint is below ground must lie on or next to a
  // roads_3d way tagged tunnel; a segment whose nearest below-ground way is NOT a tunnel and which is more than 30 m
  // from any tunnel way (so portal transitions are never counted) is a car in the open cutting drawn as X-ray.
  test("X-ray tunnel cars (ring heads, tethers, hidden by Show tunnels) are only cars on roads_3d tunnel ways, not in the open cutting", async ({ page }) => {
    await ready(page);
    const r = await page.evaluate(async () => {
      type L = { id: string; props: { data: { length: number; attributes: { getFrom: { value: Float64Array }; getTo: { value: Float64Array } } } } };
      const deck = (window.__twinMap as unknown as { __deck: { props: { layers: L[] } } }).__deck;
      type F = { properties: { way_id: number; name: string | null; tunnel: boolean }; geometry: { coordinates: [number, number, number][] } };
      const fc = (await (await fetch("/data/roads_3d.geojson")).json()) as { features: F[] };
      const KY = 110_574, KX = 111_320 * Math.cos((-37.82 * Math.PI) / 180);
      type Seg = { a: number[]; b: number[]; tunnel: boolean; way: number; name: string | null };
      const segs: Seg[] = [];
      for (const f of fc.features) {
        const c = f.geometry.coordinates;
        for (let i = 1; i < c.length; i++) {
          if ((c[i - 1]![2] + c[i]![2]) / 2 < 0) segs.push({ a: c[i - 1]!, b: c[i]!, tunnel: f.properties.tunnel, way: f.properties.way_id, name: f.properties.name });
        }
      }
      const G = 0.001;
      const grid = new Map<string, number[]>();
      segs.forEach((s, i) => {
        for (let x = Math.floor(Math.min(s.a[0]!, s.b[0]!) / G); x <= Math.floor(Math.max(s.a[0]!, s.b[0]!) / G); x++)
          for (let y = Math.floor(Math.min(s.a[1]!, s.b[1]!) / G); y <= Math.floor(Math.max(s.a[1]!, s.b[1]!) / G); y++)
            (grid.get(`${x},${y}`) ?? grid.set(`${x},${y}`, []).get(`${x},${y}`)!).push(i);
      });
      const dist = (p: number[], s: Seg) => {
        const ax = (s.a[0]! - p[0]!) * KX, ay = (s.a[1]! - p[1]!) * KY, bx = (s.b[0]! - p[0]!) * KX, by = (s.b[1]! - p[1]!) * KY;
        const dx = bx - ax, dy = by - ay, l2 = dx * dx + dy * dy;
        const t = l2 > 0 ? Math.min(1, Math.max(0, -(ax * dx + ay * dy) / l2)) : 0;
        return Math.hypot(ax + t * dx, ay + t * dy);
      };
      const nearby = (p: number[]) => {
        const out: { d: number; s: Seg }[] = [];
        const gx = Math.floor(p[0]! / G), gy = Math.floor(p[1]! / G);
        for (let x = gx - 1; x <= gx + 1; x++) for (let y = gy - 1; y <= gy + 1; y++) for (const i of grid.get(`${x},${y}`) ?? []) out.push({ d: dist(p, segs[i]!), s: segs[i]! });
        return out;
      };
      let xray = 0, cutting = 0, farthest = 0;
      const ways: Record<string, number> = {};
      for (const l of deck.props.layers) {
        if (!/^traffic-tunnel-heads-b\d+$/.test(l.id)) continue;
        const { getFrom, getTo } = l.props.data.attributes;
        for (let k = 0; k < l.props.data.length; k++) {
          // Heads are lifted CAR_LIFT_M (0.5 m) above the road; the classification uses road z < -0.5 m.
          const mid = [0, 1, 2].map((j) => (getFrom.value[k * 3 + j]! + getTo.value[k * 3 + j]!) / 2);
          if (mid[2]! - 0.5 >= -0.5) continue;
          xray++;
          const near = nearby(mid);
          if (!near.length) continue;
          const nearest = near.reduce((m, x) => (x.d < m.d ? x : m));
          farthest = Math.max(farthest, nearest.d);
          const tunnelD = Math.min(Infinity, ...near.filter((x) => x.s.tunnel).map((x) => x.d));
          if (!nearest.s.tunnel && tunnelD > 30) {
            cutting++;
            const key = `${nearest.s.way} ${nearest.s.name}`;
            ways[key] = (ways[key] ?? 0) + 1;
          }
        }
      }
      return { xray, cutting, ways, farthest };
    });
    console.log("X-ray head segments below ground:", r.xray, "on open (non-tunnel) below-ground ways > 30 m from any tunnel:", r.cutting, JSON.stringify(r.ways));
    expect(r.xray).toBeGreaterThan(1000);
    // a908a3b falls back to depth alone when no below-ground road is within its lookup reach (>= ~44 m). Every X-ray
    // car is on or next to a below-ground road (8 m at most, 2026-10 baseline), so the fallback never decides a real car.
    console.log("farthest X-ray head segment from a below-ground road (m):", r.farthest.toFixed(1));
    expect(r.farthest).toBeLessThan(20);
    expect(r.cutting, `X-ray head segments in the open cutting / underpasses: ${JSON.stringify(r.ways)}`).toBe(0);
  });

  test("final look with the real baseline: presets, X-ray tunnels, the open CityLink cutting; only the traffic palette saturated", async ({ page }) => {
    mkdirSync(FINAL_SHOTS, { recursive: true });
    await ready(page);
    await hideFps(page);
    const metrics: Record<string, { trafficBlue: number; other: number; cyanPileUp: number }> = {};
    const snap = async (name: string) => {
      await expectAttributionUnobstructed(page);
      await expect(page.getByTestId("placeholder-badge")).toBeVisible();
      const png = await page.screenshot({ path: resolve(FINAL_SHOTS, `${name}.png`) });
      const sat = await saturationBreakdown(page, png);
      // Additive pile-up of flowing blue rgb(56, 140, 255): blue clamps first, so overlapping glows turn cyan (hue
      // ~180-199) on the way to white. Counted separately (traffic light, but a hue outside the palette band).
      const cyan = Object.entries(sat.otherHues).filter(([h]) => Number(h) >= 170 && Number(h) < 200).reduce((n, [, c]) => n + c, 0) / sat.counted;
      metrics[name] = { trafficBlue: sat.trafficBlue, other: sat.other - cyan, cyanPileUp: cyan, otherHues: sat.otherHues } as never;
    };
    for (const [id, label] of [["cbd", "CBD"], ["southbank", "Southbank"], ["docklands", "Docklands"], ["interchange", "Interchange"], ["burnley", "Burnley tunnel"], ["overview", "Overview"]] as const) {
      await goPreset(page, label);
      await frameAt(page, null, 480);
      await snap(id);
    }
    // Car rt734 on the Kings Way - CityLink out ramp (way 5234953, tunnel=no, an open ramp down into the cutting) at
    // t = 466 s, about 5 m below ground: Show tunnels on, then off (an open-air car must stay drawn).
    const CUTTING = { center: [144.9642, -37.8264] as [number, number], zoom: 17.6, pitch: 60, bearing: -20 };
    const toggle = page.getByRole("checkbox", { name: "Show tunnels" });
    await frameAt(page, CUTTING, 466);
    await snap("cutting-kings-ramp-tunnels-on");
    await toggle.uncheck();
    await frameAt(page, null, 466);
    await snap("cutting-kings-ramp-tunnels-off");
    await toggle.check();
    console.log("final look metrics:", JSON.stringify(metrics));
    // Phase 3B: amber / red (road speeds) count as traffic; anything else saturated is not allowed.
    for (const [id, m] of Object.entries(metrics)) expect(m.other, `${id}: saturated pixels outside the traffic palette`).toBeLessThan(MAX_OTHER_SATURATED);
    for (const id of ["cbd", "southbank", "interchange", "burnley"]) expect(metrics[id]!.trafficBlue, `${id}: cars drawn`).toBeGreaterThan(0.0001);
  });

  test("playback panel never covers the attribution at smaller windows", async ({ page }) => {
    await page.route("**/data/traffic.json", (r) =>
      r.fulfill({ json: { scenario: "fixture", source: "fixture", trips: "qa_fx.json", meta: "qa_fx.meta.json" } }),
    );
    await page.route("**/data/qa_fx.json", (r) => r.fulfill({ path: FIXTURE, contentType: "application/json" }));
    await page.route("**/data/qa_fx.meta.json", (r) => r.fulfill({ path: FIXTURE_META, contentType: "application/json" }));
    const sizes: Record<string, string> = {};
    for (const [w, h] of [
      [1440, 900],
      [1280, 720],
      [1024, 768],
    ] as const) {
      await page.setViewportSize({ width: w, height: h });
      await ready(page);
      await expect(page.getByTestId("playback")).toBeVisible();
      try {
        await expectAttributionUnobstructed(page);
        sizes[`${w}x${h}`] = "ok";
      } catch (e) {
        sizes[`${w}x${h}`] = String(e).split("\n")[0]!;
      }
    }
    console.log("attribution with the playback panel:", JSON.stringify(sizes));
    expect(sizes["1440x900"]).toBe("ok");
    expect(sizes["1280x720"]).toBe("ok");
    test.info().annotations.push({ type: "1024x768", description: sizes["1024x768"]! });
  });
});

// ---------------------------------------------------------------------------------------------------------------
// Production: PROD_URL = a `vite preview` of `pnpm build` (real data and the real baseline export). The fixture trips
// are routed in the browser only (never in the build) to exercise the playback UI and the look-option guard.
test.describe("production build, traffic", () => {
  onlyIn("review-real");
  test.skip(!process.env.PROD_URL, "PROD_URL not set");

  const prodAt = async (page: Page, q: string) => {
    await page.unrouteAll();
    await page.route("**/data/traffic.json", (r) =>
      r.fulfill({ json: { scenario: "fixture", source: "fixture", trips: "qa_fx.json", meta: "qa_fx.meta.json" } }),
    );
    await page.route("**/data/qa_fx.json", (r) => r.fulfill({ path: FIXTURE, contentType: "application/json" }));
    await page.route("**/data/qa_fx.meta.json", (r) => r.fulfill({ path: FIXTURE_META, contentType: "application/json" }));
    await page.goto(`${process.env.PROD_URL!}/${q}`);
    await expect(page.getByTestId("playback")).toBeVisible({ timeout: 90_000 });
    await page.getByTestId("play-pause").click();
    await expect(page.getByTestId("play-pause")).toHaveAttribute("aria-label", "Play");
    await page.getByRole("button", { name: "Interchange", exact: true }).click();
    await page.waitForTimeout(3500);
    await page.waitForLoadState("networkidle");
    await page.getByRole("slider", { name: "Playback position" }).fill("130");
    await expect(page.getByTestId("sim-clock")).toHaveText("02:10 / 05:00");
    await page.waitForTimeout(800);
    return page.screenshot();
  };

  test("no dev hooks; the real baseline ships labelled; look-option queries ignored", async ({ page }) => {
    await page.goto(process.env.PROD_URL!);
    await expect(page.getByTestId("placeholder-badge")).toHaveText("Synthetic baseline", { timeout: 90_000 });
    // __twinReady (a readiness flag) has been set in builds since Phase 1; the Stage C dev hook is __twinPlayback.
    expect(await page.evaluate(() => [window.__twinPlayback, window.__twinMap])).toEqual([undefined, undefined]);
    for (const f of ["data/traffic.json"]) {
      const text = await page.evaluate(async (p) => (await fetch(p)).text(), f);
      expect(JSON.parse(text)).toEqual({
        scenario: "baseline",
        source: "real",
        trips: "trips_baseline.json",
        meta: "baseline.meta.json",
        speeds: { edgeGeometry: "edge_geometry.json", edgeStats: "edge_stats_baseline.parquet" }, // Phase 3B
      });
    }
    const plain = await prodAt(page, "");
    const opt = await prodAt(page, OLD_LOOK_QUERY);
    const again = await prodAt(page, "");
    const [p, o, a] = [await saturationBreakdown(page, plain), await saturationBreakdown(page, opt), await saturationBreakdown(page, again)];
    console.log(`prod traffic blue: plain ${p.trafficBlue.toFixed(5)}, ${OLD_LOOK_QUERY} ${o.trafficBlue.toFixed(5)}, plain again ${a.trafficBlue.toFixed(5)}`);
    expect(p.trafficBlue).toBeGreaterThan(0.0002);
    const noise = Math.abs(p.trafficBlue - a.trafficBlue);
    expect(Math.abs(o.trafficBlue - p.trafficBlue)).toBeLessThan(Math.max(noise * 3, p.trafficBlue * 0.1));
  });
});

// ---------------------------------------------------------------------------------------------------------------
// Optional, informational: SIM_BASELINE_DIR = a directory holding a real sim export (trips_baseline.json +
// baseline.meta.json, e.g. a read-only copy of another branch's sim/out). Routed in the browser only. Checks the web
// validator accepts it and shoots CBD frames whose 20 s trail does / does not span a 60 s bucket boundary.
test.describe("real sim export, routed (SIM_BASELINE_DIR)", () => {
  onlyIn("review-real");
  test.skip(!process.env.SIM_BASELINE_DIR, "SIM_BASELINE_DIR not set");

  test("validator accepts it; bucket-boundary frames at scale", async ({ page }) => {
    const dir = process.env.SIM_BASELINE_DIR!;
    const msgs: string[] = [];
    page.on("console", (m) => {
      if (m.text().includes("[traffic]")) msgs.push(`${m.type()}: ${m.text()}`);
    });
    await page.route("**/data/traffic.json", (r) =>
      r.fulfill({ json: { scenario: "baseline", source: "real", trips: "qa_base.json", meta: "qa_base.meta.json" } }),
    );
    await page.route("**/data/qa_base.json", (r) => r.fulfill({ path: resolve(dir, "trips_baseline.json"), contentType: "application/json" }));
    await page.route("**/data/qa_base.meta.json", (r) => r.fulfill({ path: resolve(dir, "baseline.meta.json"), contentType: "application/json" }));
    await ready(page);
    await hideFps(page);
    await expect(page.locator("pre[role=alert]")).toHaveCount(0);
    await expect(page.getByTestId("placeholder-badge")).toHaveText("Synthetic baseline");
    await expect(page.getByTestId("fixture-banner")).toHaveCount(0);
    console.log("console [traffic] messages:", JSON.stringify(msgs));
    const art = resolve(ROOT, "web/test-results/review/sim-baseline");
    mkdirSync(art, { recursive: true });
    const view = { center: [144.9605, -37.8175] as [number, number], zoom: 17.2, pitch: 0, bearing: 0 };
    const out: Record<string, number> = {};
    for (const t of [372, 378, 399]) {
      // 372 / 378: trail [352, 372] / [358, 378] spans the 360 s boundary; 399: trail [379, 399] spans none.
      await frameAt(page, view, t);
      const png = await page.screenshot({ path: resolve(art, `cbd-topdown-t${t}.png`) });
      out[`t${t}`] = (await saturationBreakdown(page, png)).trafficBlue;
    }
    console.log("sim baseline traffic blue:", JSON.stringify(out));
  });
});

// ---------------------------------------------------------------------------------------------------------------
// The web's dev validator (web/src/data/tripsContract.ts) vs tests/contracts/test_trips_contract.py. Round 2: the web
// claims the same rules (strict timestamps, ~10 m bbox slack, duration_s bound, meta.vehicles and tunnel_vehicles <=
// vehicles as errors), so every case the contract rejects must be rejected here too.
test.describe("web trips validator vs the Python contract", () => {
  onlyIn("review-real");

  test("same verdicts as the contract test on every case it rejects", async ({ page }) => {
    await ready(page);
    const r = await page.evaluate(async () => {
      const m = (await import("/src/data/tripsContract.ts" as string)) as {
        validateTrips: (d: unknown, o?: { durationS?: number }) => { ok: boolean; errors: string[]; warnings: string[] };
        validateTripsMeta: (d: unknown) => { ok: boolean; errors: string[] };
        crossCheckTrips: (t: unknown, m: unknown) => { errors: string[]; info: string[] };
        TRIP_KEYS: readonly string[];
        META_KEYS: readonly string[];
      };
      const trip = (over: Record<string, unknown> = {}) => ({
        id: "veh0",
        type: "passenger",
        path: [
          [144.96, -37.818, 0],
          [144.961, -37.8177, 0],
          [144.962, -37.8174, 0],
        ],
        timestamps: [0, 4.5, 9],
        ...over,
      });
      const meta = (over: Record<string, unknown> = {}) => ({
        scenario: "baseline",
        demand: "placeholder_random",
        duration_s: 900,
        seed: 42,
        sumo_version: "1.28.0",
        vehicles: 1,
        teleports: 0,
        tunnel_vehicles: 0,
        generated_at: "2026-10-26T14:16:33Z",
        ...over,
      });
      const ok = (d: unknown, durationS?: number) => m.validateTrips(d, { durationS }).ok;
      const crossOk = (t: unknown, mm: unknown) => m.crossCheckTrips(t, mm).errors.length === 0;
      const mok = (d: unknown) => m.validateTripsMeta(d).ok;
      // 50 m east of SIM_EXTRACT_BBOX (east 144.97841): outside the contract's ~10 m slack, inside the web's ~100 m.
      const outEast = trip({ path: [[144.96, -37.818, 0], [144.9790, -37.8177, 0], [144.962, -37.8174, 0]] });
      return {
        keys: { trip: [...m.TRIP_KEYS].sort(), meta: [...m.META_KEYS].sort() },
        mustReject: {
          extraKey: ok([{ ...trip(), speed: 1 }]),
          twoD: ok([trip({ path: [[144.96, -37.818], [144.961, -37.8177], [144.962, -37.8174]] })]),
          swapped: ok([trip({ path: trip().path.map((p) => [p[1], p[0], p[2]]) })]),
          backwards: ok([trip({ timestamps: [0, 9, 4.5] })]),
          lengthMismatch: ok([trip({ timestamps: [0, 4.5] })]),
          dupId: ok([trip(), trip()]),
          fixtureIdInReal: ok([trip({ id: "fixture-x" })]),
          badDemand: mok(meta({ demand: "guessed" })),
          naiveDate: mok(meta({ generated_at: "2026-10-26T14:16:33" })),
          floatSeed: mok(meta({ seed: 4.2 })),
          extraMetaKey: mok(meta({ note: "x" })),
          fixtureLabelInReal: mok(meta({ _test_fixture: true })),
          equalTimestamps: ok([trip({ timestamps: [0, 4.5, 4.5] })]),
          fiftyMetresOutsideExtract: ok([outEast]),
          pastDuration: ok([trip({ timestamps: [0, 4.5, 950] })], 900),
          vehiclesMismatch: crossOk([trip()], meta({ vehicles: 2 })),
          tunnelMoreThanVehicles: crossOk([trip()], meta({ tunnel_vehicles: 2 })),
        },
        accept: {
          valid: ok([trip()], 900) && mok(meta()) && crossOk([trip()], meta()),
          lastSampleHalfSecondPast: ok([trip({ timestamps: [0, 4.5, 900.5] })], 900),
          // tunnel_vehicles counts OSM tunnel ways (CLAUDE.md); a difference from z < -0.5 is only a note.
          tunnelCountVsDepthIsInfo: crossOk([trip()], meta({ tunnel_vehicles: 1 })),
          tunnelInfo: m.crossCheckTrips([trip()], meta({ tunnel_vehicles: 1 })).info.length,
        },
      };
    });
    console.log("web validator vs contract:", JSON.stringify(r));
    expect(r.keys.trip).toEqual(["id", "path", "timestamps", "type"]);
    expect(r.keys.meta).toEqual(
      ["demand", "duration_s", "generated_at", "scenario", "seed", "sumo_version", "teleports", "tunnel_vehicles", "vehicles"].sort(),
    );
    for (const [k, v] of Object.entries(r.mustReject)) expect(v, `web accepts ${k}`).toBe(false);
    expect(r.accept).toEqual({ valid: true, lastSampleHalfSecondPast: true, tunnelCountVsDepthIsInfo: true, tunnelInfo: 1 });
  });
});
