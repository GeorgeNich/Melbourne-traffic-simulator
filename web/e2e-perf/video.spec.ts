// A ~20 s video of glowing car playback at the Interchange (default look), for George's mid-phase review. HEADED Chromium (real GPU),
// real buildings and roads, the labelled trips fixture routed in (page.route), 4x playback.
//   web/screenshots/video/interchange.webm   (Playwright's recording, includes the page load)
//   web/screenshots/video/interchange.mp4    (load trimmed off; only if ffmpeg is on PATH)
// Run: pnpm -C web perf:video     (one at a time; nothing heavy alongside)
import { execFileSync } from "node:child_process";
import { mkdirSync, renameSync, rmSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "@playwright/test";
import { getPreset } from "../src/camera/presets";
import { FIXTURE_TRIPS, frameAt, routeTrips, waitForReady } from "../e2e/support/trips";

const DIR = resolve(import.meta.dirname, "../screenshots/video");
const SECONDS = Number(process.env.VIDEO_SECONDS ?? 20);

function hasFfmpeg() {
  try {
    execFileSync("ffmpeg", ["-version"], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

test("playback video at the Interchange", async ({ browser }) => {
  test.setTimeout(180_000);
  mkdirSync(DIR, { recursive: true });
  const size = { width: 1440, height: 900 };
  const context = await browser.newContext({ viewport: size, deviceScaleFactor: 1, recordVideo: { dir: DIR, size } });
  const page = await context.newPage();
  const t0 = Date.now();
  await routeTrips(page, FIXTURE_TRIPS);
  await page.goto("/");
  await waitForReady(page);
  await page.addStyleTag({ content: '[data-testid="fps"] { display: none !important; }' });
  await frameAt(page, getPreset("interchange").view, 95);
  const startS = (Date.now() - t0) / 1000;
  await page.evaluate(() => {
    window.__twinPlayback!.setSpeed(4);
    window.__twinPlayback!.play();
  });
  await page.waitForTimeout(SECONDS * 1000);
  const video = page.video()!;
  await context.close();
  const webm = resolve(DIR, "interchange.webm");
  rmSync(webm, { force: true });
  renameSync(await video.path(), webm);
  if (hasFfmpeg()) {
    const mp4 = resolve(DIR, "interchange.mp4");
    execFileSync("ffmpeg", ["-y", "-loglevel", "error", "-ss", startS.toFixed(2), "-i", webm, "-t", String(SECONDS), "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "18", mp4]);
    console.log(`[video] ${mp4} (load trimmed: first ${startS.toFixed(1)} s)`);
  }
  console.log(`[video] ${webm}`);
});
