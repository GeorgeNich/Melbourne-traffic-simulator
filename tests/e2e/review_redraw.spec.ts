// qa-reviewer: redraw on demand (phase-2d, c201c50). The twin must draw only when something changes, and must never
// leave a stale or half-finished frame on screen when it stops.
//
// Counters are this spec's own (not web/e2e/support/redraws.ts): every requestAnimationFrame callback the page runs,
// and every MapLibre 'render'. Waits during a measurement never poll with requestAnimationFrame (Playwright's default).
//
// "Not frozen" check: once the map is quiet, the canvas on screen must equal a forced synchronous redraw of the same
// state (forceCurrentFrame: pending deck.gl layer updates applied, then map.redraw()). If a change (slider, toggle,
// seek, resize, late data, tile or label fade) was made but never drawn, or a fade stopped half-way, they differ.
// Mutation-checked: a deck.setProps with no wake (deck's loop asleep) leaves the old frame, and this check fails.
//
// The renderCap check runs against the REAL MapLibre: web/src/map/renderCap.ts relies on MapLibre clearing
// `_frameRequest` before it calls `_render`, so a deferred still-camera render can book the next frame with
// triggerRepaint(). If a MapLibre update changes that, deferred renders are silently dropped; this test fails first.
//
// Projects: review-real (dev server, real data). The production idle check runs only with PROD_URL (a `vite preview`
// of `pnpm build`). See playwright.review.config.ts for how to run.
import { expect, type Page, test } from "@playwright/test";

interface Counters {
  raf: number;
  renders: number;
}

declare global {
  interface Window {
    __qaCounters?: Counters;
  }
}

const CBD = { center: [144.9631, -37.8136] as [number, number], zoom: 15.5, pitch: 55, bearing: -20 };
/** Pixels whose largest channel difference is above this count as different (PNG noise is 0; this is margin). */
const DIFF_CHANNEL = 8;

async function installCounters(page: Page) {
  await page.addInitScript(() => {
    const c = { raf: 0, renders: 0 };
    window.__qaCounters = c;
    const raf = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = (cb) =>
      raf((t) => {
        c.raf += 1;
        cb(t);
      });
  });
}

async function readyDev(page: Page) {
  await installCounters(page);
  await page.goto("/");
  await page.waitForFunction(() => window.__twinReady === true, null, { timeout: 120_000, polling: 200 });
  await page.evaluate(() => void window.__twinMap!.on("render", () => (window.__qaCounters!.renders += 1)));
  await page.addStyleTag({ content: '[data-testid="fps"] { display: none !important; }' });
}

const counters = (page: Page): Promise<Counters> => page.evaluate(() => ({ ...window.__qaCounters! }));

async function countOver(page: Page, ms: number): Promise<Counters> {
  const a = await counters(page);
  await page.waitForTimeout(ms);
  const b = await counters(page);
  return { raf: b.raf - a.raf, renders: b.renders - a.renders };
}

/** Camera still and no MapLibre render for `quietMs`, plus a tail for deck.gl's wake frames. */
async function settle(page: Page, quietMs = 600) {
  await page.waitForFunction(() => !window.__twinMap!.isMoving(), null, { polling: 100, timeout: 30_000 });
  await page.evaluate(
    (q) =>
      new Promise<void>((done) => {
        const map = window.__twinMap!;
        let timer = setTimeout(finish, q);
        function onRender() {
          clearTimeout(timer);
          timer = setTimeout(finish, q);
        }
        function finish() {
          map.off("render", onRender);
          done();
        }
        map.on("render", onRender);
      }),
    quietMs,
  );
  await page.waitForTimeout(250);
}

async function pause(page: Page) {
  const button = page.getByTestId("play-pause");
  if ((await button.getAttribute("aria-label")) === "Pause") await button.click();
  await expect(button).toHaveAttribute("aria-label", "Play");
}

/** Number of pixels that differ between two PNG screenshots (decoded in the page; no image dependency). */
async function diffPixels(page: Page, a: Buffer, b: Buffer): Promise<number> {
  return page.evaluate(
    async ({ a, b, th }) => {
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
      const [x, y] = [await load(a), await load(b)];
      if (x.length !== y.length) return Number.POSITIVE_INFINITY;
      let n = 0;
      for (let i = 0; i < x.length; i += 4) {
        const d = Math.max(Math.abs(x[i]! - y[i]!), Math.abs(x[i + 1]! - y[i + 1]!), Math.abs(x[i + 2]! - y[i + 2]!));
        if (d > th) n++;
      }
      return n;
    },
    { a: a.toString("base64"), b: b.toString("base64"), th: DIFF_CHANNEL },
  );
}

/**
 * Draw the true current state synchronously: first apply any deck.gl props still waiting for deck's frame loop (new
 * layers are only applied there, in layerManager.updateLayers), then a synchronous MapLibre render (which draws the
 * interleaved deck.gl layers). A missed MapLibre repaint AND a missed deck.gl wake both show up as a difference.
 */
function forceCurrentFrame() {
  const map = window.__twinMap!;
  const deck = (map as unknown as { __deck?: { layerManager?: { updateLayers: () => void } } }).__deck;
  if (!deck?.layerManager) throw new Error("no interleaved deck.gl instance on the map");
  deck.layerManager.updateLayers();
  map.redraw();
}

/**
 * The map is quiet (no frames, no renders over 1 s) and what is on screen equals a forced redraw of the same state.
 * Returns the on-screen PNG.
 */
async function expectQuietAndCurrent(page: Page, what: string): Promise<Buffer> {
  await settle(page);
  const quiet = await countOver(page, 1000);
  expect(quiet, `${what}: quiet once settled`).toEqual({ raf: 0, renders: 0 });
  const shown = await page.screenshot();
  await page.evaluate(forceCurrentFrame);
  const forced = await page.screenshot();
  const diff = await diffPixels(page, shown, forced);
  console.log(`${what}: ${diff} pixels differ between the settled frame and a forced redraw`);
  expect(diff, `${what}: the settled frame is current (equals a forced redraw)`).toBe(0);
  await settle(page);
  return shown;
}

test.describe("redraw on demand (dev, real data)", () => {
  test.beforeEach(({}, info) => test.skip(info.project.name !== "review-real", "runs in review-real only"));

  test("paused and still: nothing runs; a still frame is current", async ({ page }) => {
    test.setTimeout(180_000);
    await readyDev(page);
    await pause(page);
    await page.evaluate((v) => void window.__twinMap!.jumpTo(v), CBD);
    await settle(page);
    const t0 = await page.evaluate(() => window.__twinPlayback!.time);
    const idle = await countOver(page, 3000);
    console.log("paused idle over 3 s:", JSON.stringify(idle));
    expect(idle, "paused + still: no animation frames, no renders over 3 s").toEqual({ raf: 0, renders: 0 });
    expect(await page.evaluate(() => window.__twinPlayback!.time)).toBe(t0);
    expect(await page.evaluate(() => window.__twinPlayback!.running)).toBe(false);
    await expectQuietAndCurrent(page, "paused cbd");
  });

  test("while paused: Buildings slider, Show tunnels, time slider each redraw once, then stop, with a current frame", async ({ page }) => {
    test.setTimeout(240_000);
    await readyDev(page);
    await pause(page);
    await page.evaluate((v) => void window.__twinMap!.jumpTo(v), CBD);
    const base = await expectQuietAndCurrent(page, "before changes");

    // Buildings opacity 25% -> 15%.
    let before = await counters(page);
    await page.getByRole("slider", { name: "Building opacity" }).fill("0.15");
    const dimmer = await expectQuietAndCurrent(page, "buildings slider");
    expect((await counters(page)).renders - before.renders, "the slider change rendered").toBeGreaterThan(0);
    expect(await diffPixels(page, base, dimmer), "the slider change is visible").toBeGreaterThan(1000);

    // Time slider seek: the cars move.
    before = await counters(page);
    const t = await page.evaluate(() => window.__twinPlayback!.time);
    const target = t < 400 ? 600 : 200; // on the slider's 0.5 s step
    await page.getByRole("slider", { name: "Playback position" }).fill(String(target));
    expect(await page.evaluate(() => window.__twinPlayback!.time)).toBeCloseTo(target, 0);
    const seeked = await expectQuietAndCurrent(page, "time slider seek");
    expect((await counters(page)).renders - before.renders, "the seek rendered").toBeGreaterThan(0);
    expect(await diffPixels(page, dimmer, seeked), "the seek is visible (cars moved)").toBeGreaterThan(50);
    expect(await page.evaluate(() => window.__twinPlayback!.running)).toBe(false);

    // Phase 3B: the Road speeds toggle (off, then on again) redraws while paused and leaves a current frame.
    const speeds = page.getByRole("checkbox", { name: "Road speeds" });
    if (await speeds.count()) {
      // Neutral UI for the comparison shots: no focus ring or hover highlight (the mouse rests on plain status text).
      const neutral = async () => {
        await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
        await page.getByTestId("status").hover();
      };
      await neutral();
      const withSpeeds = await expectQuietAndCurrent(page, "road speeds on");
      before = await counters(page);
      await speeds.uncheck();
      const noSpeeds = await expectQuietAndCurrent(page, "road speeds off");
      expect((await counters(page)).renders - before.renders, "the speeds toggle rendered").toBeGreaterThan(0);
      // > the checkbox and legend alone (a few hundred pixels): the road lines went.
      expect(await diffPixels(page, withSpeeds, noSpeeds), "the speeds toggle is visible").toBeGreaterThan(2000);
      await speeds.check();
      await neutral();
      const speedsBack = await expectQuietAndCurrent(page, "road speeds on again");
      // A handful of pixels may differ (seen: 1; GPU rounding of the additive passes), never a road.
      expect(await diffPixels(page, withSpeeds, speedsBack), "speeds back as before").toBeLessThan(20);
    }

    // Show tunnels off at the Burnley tunnel (X-ray ribbons and tunnel cars hidden).
    await page.getByRole("button", { name: "Burnley tunnel", exact: true }).click();
    await page.waitForFunction(() => window.__twinMap!.isMoving(), null, { polling: 20, timeout: 5_000 });
    const withTunnels = await expectQuietAndCurrent(page, "burnley, tunnels shown");
    before = await counters(page);
    await page.getByRole("checkbox", { name: "Show tunnels" }).uncheck();
    const noTunnels = await expectQuietAndCurrent(page, "show tunnels off");
    expect((await counters(page)).renders - before.renders, "the toggle rendered").toBeGreaterThan(0);
    expect(await diffPixels(page, withTunnels, noTunnels), "the toggle is visible").toBeGreaterThan(500);
  });

  test("while paused: a jumpTo to a new area loads its tiles and finishes the label fade, then stops", async ({ page }) => {
    test.setTimeout(180_000);
    await readyDev(page);
    await pause(page);
    for (const [name, view] of [
      ["docklands", { center: [144.9405, -37.8155], zoom: 16.2, pitch: 50, bearing: 30 }],
      ["southbank", { center: [144.9640, -37.8240], zoom: 16.5, pitch: 0, bearing: 0 }],
    ] as const) {
      await page.evaluate((v) => void window.__twinMap!.jumpTo(v as never), view);
      await expectQuietAndCurrent(page, `jumpTo ${name}`);
      expect(await page.evaluate(() => window.__twinMap!.areTilesLoaded()), `${name}: tiles loaded`).toBe(true);
    }
  });

  test("while paused: a window resize redraws at the new size, then stops", async ({ page }) => {
    test.setTimeout(180_000);
    await readyDev(page);
    await pause(page);
    await page.evaluate((v) => void window.__twinMap!.jumpTo(v), CBD);
    await settle(page);
    const before = await counters(page);
    await page.setViewportSize({ width: 1100, height: 760 });
    await expectQuietAndCurrent(page, "resized");
    expect((await counters(page)).renders - before.renders).toBeGreaterThan(0);
    const size = await page.evaluate(() => {
      const c = window.__twinMap!.getCanvas();
      return { w: c.clientWidth, h: c.clientHeight, bw: c.width, dpr: window.devicePixelRatio };
    });
    expect(size.w).toBe(1100);
    expect(size.h).toBe(760);
    expect(size.bw).toBe(Math.round(1100 * size.dpr));
  });

  test("data that arrives while paused is drawn (buildings held back until after pause)", async ({ page }) => {
    test.setTimeout(240_000);
    await installCounters(page);
    let release!: () => void;
    const released = new Promise<void>((r) => (release = r));
    await page.route("**/data/buildings.geojson", async (route) => {
      await released;
      await route.continue();
    });
    await page.goto("/");
    // Traffic (and its controls) arrive while the buildings are still held.
    await expect(page.getByTestId("play-pause")).toBeVisible({ timeout: 120_000 });
    await expect(page.getByTestId("status")).toHaveText(/^Loading buildings/);
    // The map's 'load' (dev hook) can come after the traffic controls.
    await page.waitForFunction(() => window.__twinMap !== undefined, null, { polling: 100, timeout: 60_000 });
    await page.evaluate(() => void window.__twinMap!.on("render", () => (window.__qaCounters!.renders += 1)));
    await page.addStyleTag({ content: '[data-testid="fps"] { display: none !important; }' });
    await pause(page);
    await page.evaluate(() => window.__twinPlayback!.seek(480));
    await page.evaluate((v) => void window.__twinMap!.jumpTo(v), CBD);
    const noBuildings = await expectQuietAndCurrent(page, "paused, buildings not loaded yet");

    const before = await counters(page);
    release();
    await expect(page.getByTestId("status")).toHaveText(/\d building pieces/, { timeout: 120_000 });
    await page.waitForFunction(() => window.__twinReady === true, null, { timeout: 60_000, polling: 200 });
    const withBuildings = await expectQuietAndCurrent(page, "paused, buildings arrived");
    expect((await counters(page)).renders - before.renders, "the late data rendered").toBeGreaterThan(0);
    expect(await diffPixels(page, noBuildings, withBuildings), "the buildings are on screen").toBeGreaterThan(5000);
    expect(await page.evaluate(() => window.__twinPlayback!.time)).toBe(480);
  });

  test("renderCap against the real MapLibre: installed, _frameRequest cleared before _render, a deferred render re-books", async ({ page }) => {
    test.setTimeout(180_000);
    await readyDev(page);
    await pause(page);
    await settle(page);
    const r = await page.evaluate(
      () =>
        new Promise<Record<string, unknown>>((done, fail) => {
          type M = { _render: (n: number) => unknown; _frameRequest: unknown; __twinRenderCap?: true };
          const map = window.__twinMap!;
          const m = map as unknown as M;
          const out: Record<string, unknown> = { installed: m.__twinRenderCap === true && typeof m._render === "function" };
          // Wrap the (capped) _render once more, to see the state MapLibre calls it in.
          const capped = m._render;
          let lastNow = 0;
          let frameRequestAtCall: unknown = "unset";
          m._render = function (this: unknown, now: number) {
            lastNow = now;
            frameRequestAtCall = m._frameRequest;
            return capped.call(map, now);
          };
          const restore = () => (m._render = capped);
          setTimeout(() => (restore(), fail(new Error("no render after triggerRepaint"))), 3000);
          map.once("render", () => {
            // Out of the render (as the next display frame would be): a frame 1 ms after this one comes before its
            // 1/60 s slot, so the cap must defer it AND book the next frame.
            setTimeout(() => {
              out.frameRequestNullWhenRendering = frameRequestAtCall === null;
              out.firstNow = lastNow;
              let rendered = 0;
              const count = () => (rendered += 1);
              map.on("render", count);
              m._render(lastNow + 1);
              out.renderedSynchronously = rendered;
              out.bookedAfterDeferral = m._frameRequest !== null && m._frameRequest !== undefined;
              const t0 = performance.now();
              map.once("render", () => {
                out.followUpRenderMs = Math.round(performance.now() - t0);
                map.off("render", count);
                restore();
                done(out);
              });
            }, 0);
          });
          map.triggerRepaint();
        }),
    );
    console.log("renderCap vs MapLibre:", JSON.stringify(r));
    expect(r.installed, "renderCap wrapped MapLibre's _render (no fallback warning)").toBe(true);
    expect(r.frameRequestNullWhenRendering, "MapLibre clears _frameRequest before calling _render").toBe(true);
    expect(r.renderedSynchronously, "a too-early still-camera render is deferred").toBe(0);
    expect(r.bookedAfterDeferral, "the deferred render booked the next frame").toBe(true);
    expect(r.followUpRenderMs as number, "the deferred render happens on a following frame").toBeLessThan(200);
    await expectQuietAndCurrent(page, "after the renderCap probe");
  });

  test("play/pause races, end-of-scenario loop, hidden tab: correct sim time and no leftover frames", async ({ page }) => {
    test.setTimeout(180_000);
    await readyDev(page);
    const clock = () => page.evaluate(() => ({ t: window.__twinPlayback!.time, running: window.__twinPlayback!.running }));
    // Quick toggles (StrictMode dev build): ends paused, nothing left running.
    const button = page.getByTestId("play-pause");
    await pause(page);
    for (let i = 0; i < 9; i++) await button.click({ delay: 0 });
    await expect(button).toHaveAttribute("aria-label", "Pause");
    await button.click();
    await expect(button).toHaveAttribute("aria-label", "Play");
    expect((await clock()).running).toBe(false);
    await page.evaluate((v) => void window.__twinMap!.jumpTo(v), CBD);
    await expectQuietAndCurrent(page, "after 10 quick toggles");

    // End of scenario: playing over the end wraps to the start and keeps drawing.
    const duration = await page.evaluate(() => window.__twinPlayback!.getSnapshot().duration);
    await page.evaluate((d) => {
      const pb = window.__twinPlayback!;
      pb.setSpeed(4);
      pb.seek(d - 2);
      pb.play();
    }, duration);
    await page.waitForTimeout(1500);
    const wrapped = await clock();
    expect(wrapped.running).toBe(true);
    expect(wrapped.t, "looped past the end to the start").toBeLessThan(10);
    const looping = await countOver(page, 1000);
    expect(looping.renders, "still drawing after the loop").toBeGreaterThan(20);

    // Hidden (emulated: Playwright's Chromium never reports hidden) stops the clock; shown resumes from the same time.
    const setHidden = (h: boolean) =>
      page.evaluate((h) => {
        Object.defineProperty(document, "visibilityState", { configurable: true, get: () => (h ? "hidden" : "visible") });
        Object.defineProperty(document, "hidden", { configurable: true, get: () => h });
        document.dispatchEvent(new Event("visibilitychange"));
      }, h);
    await page.evaluate(() => window.__twinPlayback!.setSpeed(1));
    await setHidden(true);
    const hiddenAt = await clock();
    expect(hiddenAt.running).toBe(false);
    await page.waitForTimeout(400);
    const whileHidden = await countOver(page, 1500);
    expect(whileHidden.renders, "hidden: no renders").toBe(0);
    expect((await clock()).t).toBe(hiddenAt.t);
    await setHidden(false);
    await page.waitForTimeout(1000);
    const shown = await clock();
    expect(shown.running).toBe(true);
    expect(shown.t - hiddenAt.t, "resumes from the same sim time (about 1 s on, no jump)").toBeGreaterThan(0.3);
    expect(shown.t - hiddenAt.t).toBeLessThan(2);
    await pause(page);
    await expectQuietAndCurrent(page, "paused after the hidden spell");
  });
});

// Production build: the same idle result without any dev hooks (only the page's animation frames can be counted).
test.describe("redraw on demand (production build)", () => {
  test.skip(!process.env.PROD_URL, "PROD_URL not set");
  test.beforeEach(({}, info) => test.skip(info.project.name !== "review-real", "runs in review-real only"));

  test("paused in a production build: no animation frames once loaded", async ({ page }) => {
    test.setTimeout(180_000);
    await installCounters(page);
    await page.goto(process.env.PROD_URL!);
    await expect(page.getByTestId("play-pause")).toBeVisible({ timeout: 120_000 });
    await expect(page.getByTestId("status")).toHaveText(/\d building pieces · \d/, { timeout: 120_000 });
    await pause(page);
    // Settled = a whole second without a frame (tiles and label fades done), within 30 s.
    let settledAfter = -1;
    for (let i = 0; i < 30; i++) {
      if ((await countOver(page, 1000)).raf === 0) {
        settledAfter = i + 1;
        break;
      }
    }
    expect(settledAfter, "the paused production build stops asking for frames").toBeGreaterThan(0);
    const idle = await countOver(page, 3000);
    console.log(`prod paused idle over 3 s (settled after ${settledAfter} s):`, JSON.stringify(idle));
    expect(idle.raf).toBe(0);
    await page.getByTestId("play-pause").click();
    const playing = await countOver(page, 1000);
    expect(playing.raf, "playing again draws").toBeGreaterThan(20);
  });
});
