// Redraw on demand: the twin draws only when something changes. Real buildings, roads and baseline traffic ("real"
// project only). Counts animation-frame callbacks, MapLibre renders and deck.gl draws (support/redraws.ts):
//   paused + still camera -> nothing at all over 2 s; play -> redraws resume; a preset button still flies the camera,
//   and drawing stops again once it lands; a hidden tab stops playback and resumes at the same sim time.
import { expect, test } from "@playwright/test";
import { getPreset } from "../src/camera/presets";
import { countOver, countRedraws, installFrameCounter, readCounters, setHidden, waitForSettled } from "./support/redraws";
import { waitForReady } from "./support/trips";

test("paused and still: no redraws; play resumes them; presets still fly; hidden stops playback", async ({ page }, info) => {
  test.skip(info.project.name !== "real", "the real-data server only");
  test.setTimeout(240_000);
  await installFrameCounter(page);
  await page.goto("/");
  await waitForReady(page);
  await countRedraws(page);
  const button = page.getByTestId("play-pause");
  const simTime = () => page.evaluate(() => window.__twinPlayback!.time);

  // Autoplay draws.
  await expect(button).toHaveAttribute("aria-label", "Pause");
  const playing = await countOver(page, 1000);
  expect(playing.renders, "playing: MapLibre renders").toBeGreaterThan(0);
  expect(playing.deckDraws, "playing: deck.gl draws").toBeGreaterThan(0);

  // Pause, let the camera settle: then nothing runs at all.
  await button.click();
  await expect(button).toHaveAttribute("aria-label", "Play");
  await waitForSettled(page);
  const t0 = await simTime();
  const paused = await countOver(page, 2000);
  expect(paused, "paused + still: no frames, no renders, no draws").toEqual({ raf: 0, renders: 0, deckDraws: 0, seconds: paused.seconds });
  expect(await simTime()).toBe(t0);
  await expect(page.getByTestId("fps")).toHaveText(/^0 fps idle/);

  // Play: redraws resume and the sim time moves on from where it was.
  await button.click();
  await expect(button).toHaveAttribute("aria-label", "Pause");
  const resumed = await countOver(page, 1000);
  expect(resumed.renders).toBeGreaterThan(0);
  expect(resumed.deckDraws).toBeGreaterThan(0);
  const t1 = await simTime();
  expect(t1).toBeGreaterThan(t0);
  expect(t1 - t0).toBeLessThan(5); // 1x: about a second, never a jump

  // Pause again; a preset still flies the camera (renders while moving), then drawing stops when it lands.
  await button.click();
  await waitForSettled(page);
  const target = getPreset("southbank").view;
  const before = await readCounters(page);
  await page.getByRole("button", { name: "Southbank", exact: true }).click();
  await page.waitForFunction(() => window.__twinMap!.isMoving(), null, { polling: 20, timeout: 5_000 });
  await waitForSettled(page);
  const flown = await readCounters(page);
  expect(flown.renders - before.renders, "the fly-to renders while the camera moves").toBeGreaterThan(2);
  expect(flown.deckDraws - before.deckDraws).toBeGreaterThan(2);
  const centre = await page.evaluate(() => {
    const m = window.__twinMap!;
    return { lng: m.getCenter().lng, lat: m.getCenter().lat, zoom: m.getZoom(), pitch: m.getPitch(), bearing: m.getBearing() };
  });
  expect(centre.lng).toBeCloseTo(target.longitude, 3);
  expect(centre.lat).toBeCloseTo(target.latitude, 3);
  expect(centre.zoom).toBeCloseTo(target.zoom, 1);
  expect(centre.pitch).toBeCloseTo(target.pitch, 0);
  const landed = await countOver(page, 1500);
  expect(landed, "after the fly-to lands: nothing drawn").toEqual({ raf: 0, renders: 0, deckDraws: 0, seconds: landed.seconds });

  // Hidden while playing: playback stops (no frames from the app); shown again, it continues from the same sim time.
  await button.click();
  await expect(button).toHaveAttribute("aria-label", "Pause");
  await setHidden(page, true);
  const tHidden = await simTime();
  await page.waitForTimeout(300); // MapLibre's last render of the playing frame, then quiet
  const hidden = await countOver(page, 1500);
  expect(hidden.renders, "hidden: no renders").toBe(0);
  expect(await simTime()).toBe(tHidden);
  await setHidden(page, false);
  const shown = await countOver(page, 1000);
  expect(shown.renders, "shown again: playing resumes").toBeGreaterThan(0);
  const tShown = await simTime();
  expect(tShown).toBeGreaterThan(tHidden);
  expect(tShown - tHidden).toBeLessThan(5); // no jump for the time spent hidden
});
