// Measures first-load time, main-thread freezes and frame rate while orbiting each camera preset, in HEADED
// Chromium (real GPU). Writes web/perf-results/orbit-<timestamp>.json and prints it. Run: pnpm -C web perf
import { mkdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { CAMERA_PRESETS } from "../src/camera/presets";

const ORBIT_SECONDS = 10;

declare global {
  interface Window {
    __longTasks?: { start: number; duration: number }[];
  }
}

test("load + orbit fps per preset (headed)", async ({ page, browserName }) => {
  test.setTimeout(240_000);
  // Record every main-thread task over 50 ms from the very start of the page.
  await page.addInitScript(() => {
    window.__longTasks = [];
    new PerformanceObserver((list) => {
      for (const e of list.getEntries()) window.__longTasks!.push({ start: e.startTime, duration: e.duration });
    }).observe({ type: "longtask", buffered: true });
  });

  await page.goto("/");
  await page.waitForFunction(() => window.__twinReady === true, null, { timeout: 90_000 });

  const load = await page.evaluate(() => {
    const t = (name: string) => {
      const e = performance.getEntriesByName(`twin:${name}`)[0];
      return e ? Math.round(e.startTime) : null;
    };
    const nav = performance.getEntriesByType("navigation")[0] as PerformanceNavigationTiming | undefined;
    const data = performance.getEntriesByType("resource").find((r) => r.name.includes("buildings.geojson")) as
      | PerformanceResourceTiming
      | undefined;
    const roads = performance.getEntriesByType("resource").find((r) => r.name.includes("roads_3d.geojson")) as
      | PerformanceResourceTiming
      | undefined;
    const tasks = window.__longTasks ?? [];
    return {
      domContentLoadedMs: nav ? Math.round(nav.domContentLoadedEventEnd) : null,
      marksMs: {
        fetchStart: t("fetch-start"),
        fetched: t("fetched"),
        parsed: t("parsed"),
        validated: t("validated"),
        prepared: t("prepared"),
        roadsFetchStart: t("roads-fetch-start"),
        roadsParsed: t("roads-parsed"),
        roadsPrepared: t("roads-prepared"),
        ready: t("ready"), // buildings + roads drawn, first full map idle
      },
      dataTransferBytes: data?.transferSize ?? null,
      dataDecodedBytes: data?.decodedBodySize ?? null,
      roadsDecodedBytes: roads?.decodedBodySize ?? null,
      longestTaskMs: Math.round(Math.max(0, ...tasks.map((x) => x.duration))),
      longTasksOver200Ms: tasks.filter((x) => x.duration > 200).map((x) => Math.round(x.duration)),
      totalLongTaskMs: Math.round(tasks.reduce((s, x) => s + x.duration, 0)),
    };
  });

  const gpu = await page.evaluate(() => {
    const gl = document.createElement("canvas").getContext("webgl2");
    const ext = gl?.getExtension("WEBGL_debug_renderer_info");
    return ext && gl ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)) : "unknown";
  });

  const orbits: Record<string, unknown> = {};
  for (const preset of CAMERA_PRESETS) {
    const v = preset.view;
    // Jump to the preset and let tiles settle.
    await page.evaluate(
      (view) =>
        new Promise<void>((done) => {
          const map = window.__twinMap!;
          map.jumpTo({ center: [view.longitude, view.latitude], zoom: view.zoom, pitch: view.pitch, bearing: view.bearing });
          map.once("idle", () => done());
        }),
      v,
    );
    // Orbit: a full 360° around the centre over ORBIT_SECONDS, one jumpTo per animation frame, counting browser
    // frames and MapLibre render events (each render also draws the interleaved deck.gl layers).
    const stats = await page.evaluate(
      ({ seconds, startBearing }) =>
        new Promise<{ frames: number; renders: number; seconds: number; frameTimes: number[] }>((done) => {
          const map = window.__twinMap!;
          let renders = 0;
          const onRender = () => (renders += 1);
          map.on("render", onRender);
          const frameTimes: number[] = [];
          const t0 = performance.now();
          let last = t0;
          const step = (now: number) => {
            frameTimes.push(now - last);
            last = now;
            const t = (now - t0) / 1000;
            map.jumpTo({ bearing: startBearing + (360 * t) / seconds });
            if (t < seconds) {
              requestAnimationFrame(step);
            } else {
              map.off("render", onRender);
              done({ frames: frameTimes.length, renders, seconds: t, frameTimes: frameTimes.slice(1) });
            }
          };
          requestAnimationFrame(step);
        }),
      { seconds: ORBIT_SECONDS, startBearing: v.bearing },
    );
    const sorted = [...stats.frameTimes].sort((a, b) => a - b);
    const pct = (p: number) => sorted[Math.min(sorted.length - 1, Math.floor((p / 100) * sorted.length))] ?? 0;
    orbits[preset.id] = {
      seconds: Number(stats.seconds.toFixed(2)),
      avgFps: Number((stats.frames / stats.seconds).toFixed(1)),
      mapRendersPerSecond: Number((stats.renders / stats.seconds).toFixed(1)),
      frameMs: { p50: Number(pct(50).toFixed(1)), p95: Number(pct(95).toFixed(1)), max: Number(pct(100).toFixed(1)) },
      framesOver20ms: stats.frameTimes.filter((t) => t > 20).length,
    };
    expect(stats.frames).toBeGreaterThan(0);
  }

  const result = {
    when: new Date().toISOString(),
    browser: `${browserName} headed`,
    gpu,
    viewport: page.viewportSize(),
    deviceScaleFactor: 2,
    data: await page.getByTestId("status").textContent(),
    load,
    orbits,
  };
  const outDir = resolve(import.meta.dirname, "../perf-results");
  mkdirSync(outDir, { recursive: true });
  writeFileSync(resolve(outDir, `orbit-${Date.now()}.json`), JSON.stringify(result, null, 2) + "\n");
  console.log(JSON.stringify(result, null, 2));
});
