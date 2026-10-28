// Test-only redraw instrumentation, shared by e2e/redraw.spec.ts and e2e-perf/idle.spec.ts. Nothing here schedules
// frames of its own: the counters only wrap what the page already does.
//   raf        animation-frame callbacks run by the page (anyone's: MapLibre, deck.gl, the playback clock)
//   renders    MapLibre renders (each also draws the interleaved deck.gl layers)
//   deckDraws  deck.gl layer draws
// Waits during a measurement must not poll with requestAnimationFrame (Playwright's default for waitForFunction):
// use page.waitForTimeout or `polling: <ms>`.
import type { Page } from "@playwright/test";

export interface RedrawCounters {
  raf: number;
  renders: number;
  deckDraws: number;
}

declare global {
  interface Window {
    __redrawCounters?: RedrawCounters;
  }
}

/** Count every animation-frame callback from the very start of the page. Call before page.goto. */
export async function installFrameCounter(page: Page) {
  await page.addInitScript(() => {
    const c = { raf: 0, renders: 0, deckDraws: 0 };
    window.__redrawCounters = c;
    const raf = window.requestAnimationFrame.bind(window);
    window.requestAnimationFrame = (cb) =>
      raf((t) => {
        c.raf += 1;
        cb(t);
      });
  });
}

/** Count MapLibre renders and deck.gl draws (needs the dev hook window.__twinMap, so call once the app is ready). */
export async function countRedraws(page: Page) {
  await page.evaluate(() => {
    const map = window.__twinMap!;
    const c = window.__redrawCounters!;
    map.on("render", () => (c.renders += 1));
    const deck = (map as unknown as { __deck?: { _drawLayers: (...a: unknown[]) => unknown } }).__deck;
    if (!deck) throw new Error("no interleaved deck.gl instance on the map");
    const draw = deck._drawLayers.bind(deck);
    deck._drawLayers = (...a: unknown[]) => {
      c.deckDraws += 1;
      return draw(...a);
    };
  });
}

export const readCounters = (page: Page): Promise<RedrawCounters> => page.evaluate(() => ({ ...window.__redrawCounters! }));

/** Counter increases over `ms` of wall time (a Node-side wait, so it adds no frames). */
export async function countOver(page: Page, ms: number): Promise<RedrawCounters & { seconds: number }> {
  const a = await readCounters(page);
  const t0 = Date.now();
  await page.waitForTimeout(ms);
  const b = await readCounters(page);
  return { raf: b.raf - a.raf, renders: b.renders - a.renders, deckDraws: b.deckDraws - a.deckDraws, seconds: (Date.now() - t0) / 1000 };
}

/** No MapLibre render for this long counts as settled. */
const QUIET_MS = 500;
/** Settling tail after the last render: deck.gl's WAKE_FRAMES frames, with margin. */
const TAIL_MS = 250;

/** Wait until the camera has stopped and MapLibre is idle (tiles loaded, nothing animating), polling by timer. */
export async function waitForSettled(page: Page) {
  await page.waitForFunction(() => !window.__twinMap!.isMoving(), null, { polling: 100, timeout: 30_000 });
  // Settled = 'idle' fired, or no render at all for QUIET_MS (MapLibre fires 'idle' only after a render).
  await page.evaluate(
    (quietMs) =>
      new Promise<void>((done) => {
        const map = window.__twinMap!;
        let timer = setTimeout(finish, quietMs);
        function onRender() {
          clearTimeout(timer);
          timer = setTimeout(finish, quietMs);
        }
        function finish() {
          clearTimeout(timer);
          map.off("render", onRender);
          map.off("idle", finish);
          done();
        }
        map.on("render", onRender);
        map.on("idle", finish);
      }),
    QUIET_MS,
  );
  // deck.gl's loop runs a couple of frames past the last render before it sleeps (src/map/onDemandFrames.ts).
  await page.waitForTimeout(TAIL_MS);
}

/**
 * Emulate the tab being hidden or shown. Playwright's Chromium keeps every page "visible" (minimising the window or
 * switching tabs doesn't change document.visibilityState), so visibilityState / hidden are overridden and
 * visibilitychange fired. The browser still delivers animation frames, so this checks what the APP keeps running;
 * a real hidden tab also stops MapLibre's and deck.gl's frames.
 */
export async function setHidden(page: Page, hidden: boolean) {
  await page.evaluate((h) => {
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => (h ? "hidden" : "visible") });
    Object.defineProperty(document, "hidden", { configurable: true, get: () => h });
    document.dispatchEvent(new Event("visibilitychange"));
  }, hidden);
}
