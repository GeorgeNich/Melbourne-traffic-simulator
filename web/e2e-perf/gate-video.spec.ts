// The Phase 2 gate video: a slow fly-through from the CBD across the Yarra into Southbank with the REAL baseline
// traffic playing at 4x, in the default look. Rendered frame by frame, so it is smooth whatever the machine's fps: each
// frame sets the camera and the simulation time, waits for the map to draw (tiles loaded), and screenshots it
// (Chromium, 1440x900 at DPR 2, downscaled to 1440x900). The playback bar shows the 4x the cars move at; its
// play / pause button is hidden, because playback is paused and stepped by seeking. The frames are piped to the
// ffmpeg build that ships with Playwright, inside node_modules (VP9 WebM only; nothing outside the project is used).
//   web/screenshots/phase-2-gate/cbd-to-southbank.webm
// Run: pnpm -C web gate-video   (10+ minutes)
import { spawn } from "node:child_process";
import { existsSync, mkdirSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "@playwright/test";
import type { CameraView } from "../src/camera/presets";
import type { Speed } from "../src/traffic/playback";
import { GATE_DIR, HIDE_UI } from "../e2e/support/gate";
import { waitForReady } from "../e2e/support/trips";

test.use({ headless: true, channel: "chromium" });

const SECONDS = Number(process.env.VIDEO_SECONDS ?? 20);
const FPS = 30;
/** Simulation seconds per video second (4x: cars move visibly at city scale); one of the app's playback speeds. */
const SPEED: Speed = 4;
const SIM_START = 400;
const OUT = { width: 1440, height: 900 };

/** Start over the west CBD looking up the grid; end over Southbank looking at Eureka and Australia 108. */
const FROM: CameraView = { longitude: 144.9598, latitude: -37.8138, zoom: 15.7, pitch: 60, bearing: 55 };
const TO: CameraView = { longitude: 144.9645, latitude: -37.8226, zoom: 16.35, pitch: 66, bearing: 165 };

const ease = (u: number) => u * u * (3 - 2 * u);
const lerp = (a: number, b: number, u: number) => a + (b - a) * u;
function cameraAt(u: number): CameraView {
  const e = ease(u);
  return {
    longitude: lerp(FROM.longitude, TO.longitude, e),
    latitude: lerp(FROM.latitude, TO.latitude, e),
    zoom: lerp(FROM.zoom, TO.zoom, e),
    pitch: lerp(FROM.pitch, TO.pitch, e),
    bearing: lerp(FROM.bearing, TO.bearing, e),
  };
}

/** The Playwright-managed ffmpeg inside node_modules (PLAYWRIGHT_BROWSERS_PATH=0). */
function playwrightFfmpeg(): string {
  const pnpmDir = resolve(import.meta.dirname, "../../node_modules/.pnpm");
  for (const pkg of readdirSync(pnpmDir).filter((d) => d.startsWith("playwright-core@"))) {
    const browsers = resolve(pnpmDir, pkg, "node_modules/playwright-core/.local-browsers");
    if (!existsSync(browsers)) continue;
    for (const d of readdirSync(browsers).filter((x) => x.startsWith("ffmpeg-"))) {
      for (const bin of ["ffmpeg-mac", "ffmpeg-linux", "ffmpeg-win64.exe"]) if (existsSync(resolve(browsers, d, bin))) return resolve(browsers, d, bin);
    }
  }
  throw new Error("Playwright's ffmpeg not found in node_modules: run pnpm -C web e2e:install (or playwright install ffmpeg)");
}

test("phase 2 gate: fly-through video, CBD to Southbank, default look", async ({ browser }) => {
  test.skip(process.env.GATE !== "1", "opt-in: GATE=1 (pnpm -C web gate-video)");
  test.setTimeout(1_800_000);
  mkdirSync(GATE_DIR, { recursive: true });
  const out = resolve(GATE_DIR, "cbd-to-southbank.webm");
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, deviceScaleFactor: 2 });
  const page = await context.newPage();
  await page.goto("/");
  await waitForReady(page);
  await page.addStyleTag({ content: `${HIDE_UI} [data-testid="play-pause"] { display: none !important; }` });
  // The bar's speed reads the video's real playback rate (frames are stepped by seeking, so the clock stays paused).
  await page.evaluate((speed) => window.__twinPlayback!.setSpeed(speed), SPEED);

  const ffmpeg = spawn(
    playwrightFfmpeg(),
    ["-y", "-loglevel", "error", "-f", "image2pipe", "-framerate", String(FPS), "-c:v", "mjpeg", "-i", "pipe:0",
      "-vf", `scale=${OUT.width}:${OUT.height}:flags=lanczos`, "-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "38",
      "-pix_fmt", "yuv420p", "-row-mt", "1", "-deadline", "good", "-cpu-used", "2", out],
    { stdio: ["pipe", "inherit", "inherit"] },
  );
  let exited = false;
  const done = new Promise<number>((ok) =>
    ffmpeg.on("close", (code) => {
      exited = true;
      ok(code ?? 1);
    }),
  );

  const frames = SECONDS * FPS;
  const t0 = Date.now();
  for (let i = 0; i < frames; i++) {
    const view = cameraAt(i / (frames - 1));
    const time = SIM_START + (i / FPS) * SPEED;
    await page.evaluate(
      ({ view, time }) =>
        new Promise<void>((ok) => {
          const pb = window.__twinPlayback!;
          pb.pause();
          pb.seek(time);
          const map = window.__twinMap!;
          map.jumpTo({ center: [view.longitude, view.latitude], zoom: view.zoom, pitch: view.pitch, bearing: view.bearing });
          const settle = () => (map.areTilesLoaded() ? ok() : map.once("idle", () => ok()));
          map.once("render", () => setTimeout(settle, 0));
          map.triggerRepaint();
        }),
      { view, time },
    );
    const jpg = await page.screenshot({ type: "jpeg", quality: 92 });
    if (exited) throw new Error(`ffmpeg exited early (code ${await done})`);
    if (!ffmpeg.stdin.write(jpg)) await Promise.race([new Promise((ok) => ffmpeg.stdin.once("drain", ok)), done]);
    if (i % 60 === 0) console.log(`[video] frame ${i}/${frames} (${((Date.now() - t0) / 1000).toFixed(0)} s)`);
  }
  ffmpeg.stdin.end();
  const code = await done;
  await context.close();
  if (code !== 0) throw new Error(`ffmpeg exited with ${code}`);
  console.log(`[video] ${out}: ${SECONDS} s at ${FPS} fps, sim ${SIM_START}–${SIM_START + SECONDS * SPEED} s at ${SPEED}x`);
});
