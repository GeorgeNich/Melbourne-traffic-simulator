// Idle cost: how much the twin draws, and how much CPU the browser burns, when nothing is changing. HEADED Chromium
// (real GPU), real buildings, roads and baseline traffic. Four windows of PERF_SECONDS (default 8) each, camera still:
//   paused        playback paused, map settled (should draw nothing)
//   playing-1x    playback running at 1x
//   playing-30x   playback running at 30x (same frame rate as 1x: only the sim time step changes)
//   hidden        playing, tab hidden (emulated: see below; nothing of the app's should run)
// then the tab is shown again and the sim time is checked to resume from where it was.
// Counted per second: requestAnimationFrame callbacks run by the page (anyone's: MapLibre, deck.gl, the playback
// clock), MapLibre renders, and deck.gl layer draws. CPU: `ps` CPU time of this worktree's Chromium processes
// (browser, GPU, renderer) over the window, as % of one core; plus the page's main-thread busy time (CDP TaskDuration).
// PERF_WINDOW_LEFT=<px> first moves the browser window there, e.g. onto a second display with another refresh rate
// (on George's desk: 3600 = the MacBook's 120 Hz built-in screen; the main display is 60 Hz). The display's refresh
// rate is probed once (1 s of animation frames) before anything is measured.
// Writes web/perf-results/idle-<timestamp>.json and prints it.
// Run: pnpm -C web perf:idle          (one at a time; nothing heavy alongside)
import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { type CDPSession, expect, test } from "@playwright/test";
import { getPreset } from "../src/camera/presets";
import { countRedraws, installFrameCounter, setHidden, waitForSettled } from "../e2e/support/redraws";

const SECONDS = Number(process.env.PERF_SECONDS ?? 8);
const WINDOW_LEFT = process.env.PERF_WINDOW_LEFT === undefined ? null : Number(process.env.PERF_WINDOW_LEFT);
const WORKTREE = resolve(import.meta.dirname, "../..");

/** Cumulative CPU seconds per Chromium process type, for the browser this worktree's Playwright launched. */
function chromiumCpu(): Record<string, number> {
  const out = execFileSync("ps", ["-A", "-o", "pid=,time=,command="], { encoding: "utf8", maxBuffer: 64 << 20 });
  const totals: Record<string, number> = {};
  for (const line of out.split("\n")) {
    const m = /^\s*(\d+)\s+(\S+)\s+(.*)$/.exec(line);
    if (!m) continue;
    const [, , time, cmd] = m;
    if (!cmd!.includes(WORKTREE) || !/chromium-\d+/.test(cmd!) || !/Chrom/.test(cmd!)) continue;
    const type = /--type=([\w-]+)/.exec(cmd!)?.[1] ?? "browser";
    const key = type === "gpu-process" ? "gpu" : type === "renderer" ? "renderer" : type === "browser" ? "browser" : "other";
    totals[key] = (totals[key] ?? 0) + cpuSeconds(time!);
  }
  return totals;
}

/** ps TIME ([[dd-]hh:]mm:ss.cc) in seconds. */
function cpuSeconds(t: string): number {
  const [days, rest] = t.includes("-") ? t.split("-") : ["0", t];
  return rest!.split(":").reduce((s, part) => s * 60 + Number(part), 0) + Number(days) * 86400;
}

async function taskSeconds(cdp: CDPSession): Promise<number> {
  const { metrics } = await cdp.send("Performance.getMetrics");
  return metrics.find((m) => m.name === "TaskDuration")?.value ?? 0;
}

test("idle cost: paused, playing, hidden (headed)", async ({ page, browserName }) => {
  test.setTimeout(300_000);
  await installFrameCounter(page); // counts the page's own animation frames; schedules none
  await page.goto("/");
  await page.waitForFunction(() => window.__twinReady === true, null, { timeout: 180_000 });
  await expect(page.getByTestId("playback")).toBeVisible();
  await countRedraws(page);
  const gpu = await page.evaluate(() => {
    const gl = document.createElement("canvas").getContext("webgl2");
    const ext = gl?.getExtension("WEBGL_debug_renderer_info");
    return ext && gl ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)) : "unknown";
  });
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("Performance.enable");
  if (WINDOW_LEFT !== null) {
    const { windowId } = (await cdp.send("Browser.getWindowForTarget")) as { windowId: number };
    await cdp.send("Browser.setWindowBounds", { windowId, bounds: { left: WINDOW_LEFT, top: 0 } });
    await page.waitForTimeout(1000);
  }
  // The display's refresh rate: 1 s of animation frames (before any measurement window).
  const displayHz = await page.evaluate(
    () =>
      new Promise<number>((done) => {
        let n = 0;
        const t0 = performance.now();
        const f = (t: number) => {
          n += 1;
          if (t - t0 < 1000) requestAnimationFrame(f);
          else done(Math.round((n * 1000) / (t - t0)));
        };
        requestAnimationFrame(f);
      }),
  );

  // A still CBD camera, settled.
  const v = getPreset("cbd").view;
  await page.evaluate(
    (view) =>
      new Promise<void>((done) => {
        const pb = window.__twinPlayback!;
        pb.pause();
        pb.setSpeed(1);
        pb.seek(420);
        const map = window.__twinMap!;
        map.jumpTo({ center: [view.longitude, view.latitude], zoom: view.zoom, pitch: view.pitch, bearing: view.bearing });
        map.once("idle", () => done());
      }),
    v,
  );

  const measure = async (name: string) => {
    const counters0 = await page.evaluate(() => ({ ...window.__redrawCounters! }));
    const cpu0 = chromiumCpu();
    const task0 = await taskSeconds(cdp);
    const t0 = Date.now();
    await new Promise((r) => setTimeout(r, SECONDS * 1000));
    const s = (Date.now() - t0) / 1000;
    const counters1 = await page.evaluate(() => ({ ...window.__redrawCounters! }));
    const cpu1 = chromiumCpu();
    const task1 = await taskSeconds(cdp);
    const perS = (k: keyof typeof counters0) => Number(((counters1[k] - counters0[k]) / s).toFixed(1));
    const cpu: Record<string, number> = {};
    for (const k of Object.keys(cpu1)) cpu[k] = Number(((100 * ((cpu1[k] ?? 0) - (cpu0[k] ?? 0))) / s).toFixed(1));
    cpu.total = Number(Object.values(cpu).reduce((a, b) => a + b, 0).toFixed(1));
    return {
      window: name,
      seconds: Number(s.toFixed(2)),
      rafCallbacksPerS: perS("raf"),
      mapRendersPerS: perS("renders"),
      deckDrawsPerS: perS("deckDraws"),
      cpuPercentOfOneCore: cpu,
      mainThreadBusyPercent: Number(((100 * (task1 - task0)) / s).toFixed(1)),
      fpsMeter: await page.getByTestId("fps").textContent().catch(() => null),
    };
  };

  // Let the camera settle fully (label fade-in, tile placement) before the paused window.
  await waitForSettled(page);
  await page.waitForTimeout(1000);
  const paused = await measure("paused");
  await page.evaluate(() => window.__twinPlayback!.play());
  const playing = await measure("playing-1x");
  await page.evaluate(() => window.__twinPlayback!.setSpeed(30));
  const playing30 = await measure("playing-30x");
  await page.evaluate(() => window.__twinPlayback!.setSpeed(1));

  // Hidden while playing, emulated (support/redraws.ts setHidden: Playwright's Chromium never reports a page hidden).
  // A real hidden tab also stops the browser's own animation frames, so this measures what the app itself keeps running.
  await setHidden(page, true);
  const before = await page.evaluate(() => window.__twinPlayback!.time);
  const hidden = await measure("hidden (emulated)");
  const whileHidden = await page.evaluate(() => window.__twinPlayback!.time);
  await setHidden(page, false);
  await page.waitForTimeout(1000);
  const after = await page.evaluate(() => window.__twinPlayback!.time);

  const result = {
    when: new Date().toISOString(),
    browser: `${browserName} headed`,
    gpu,
    viewport: page.viewportSize(),
    deviceScaleFactor: 2,
    displayHz,
    camera: "cbd preset, still",
    windows: [paused, playing, playing30, hidden],
    hiddenResume: {
      simTimeBeforeHide: Number(before.toFixed(2)),
      simTimeAdvancedWhileHidden: Number((whileHidden - before).toFixed(2)),
      simTimeAdvancedInFirstSecondAfterShow: Number((after - whileHidden).toFixed(2)),
    },
  };
  const outDir = resolve(import.meta.dirname, "../perf-results");
  mkdirSync(outDir, { recursive: true });
  writeFileSync(resolve(outDir, `idle-${Date.now()}.json`), JSON.stringify(result, null, 2) + "\n");
  console.log(JSON.stringify(result, null, 2));
});
