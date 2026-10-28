// qa-reviewer helpers shared by the review specs: pixel metrics on screenshots (decoded in the page, so no image
// dependency is needed) and the attribution check.
import { expect, type Page } from "@playwright/test";

export type Box = { x: number; y: number; width: number; height: number };

/** "Strongly saturated": chroma (max - min channel) above this share of 255. */
export const SATURATED_CHROMA = 0.35;
/**
 * Hue band of the traffic "flowing" blue, palette.traffic.flowing = rgb(56, 140, 255) -> hue 214.7 deg; its lifted head
 * core rgb(165, 203, 255) has the same hue. Additive blending over the dark base and translucent buildings shifts it
 * a little; anything saturated outside this band is not traffic blue.
 */
export const TRAFFIC_BLUE_HUE = { min: 200, max: 232 } as const;
/**
 * Phase 3 road speeds (and the car palette): amber "slowing" rgb(255, 176, 32) -> hue 38.7 deg, red "jammed"
 * rgb(235, 50, 50) -> hue 0 deg. Additive pile-up clamps red first, pushing amber toward yellow and keeping red at 0,
 * so the bands lean that way. Saturated pixels in these bands count as traffic, not as "other".
 */
export const TRAFFIC_AMBER_HUE = { min: 25, max: 58 } as const;
export const TRAFFIC_RED_HUE = { min: 348, max: 12 } as const; // wraps through 0

export interface SaturationBreakdown {
  /** Share of counted pixels that are saturated traffic blue. */
  trafficBlue: number;
  /** Share of counted pixels that are saturated traffic amber (road speeds "slowing"). */
  trafficAmber: number;
  /** Share of counted pixels that are saturated traffic red (road speeds "jammed"). */
  trafficRed: number;
  /**
   * Share of counted pixels that are saturated and in NONE of the traffic palette bands (must stay ~0: only traffic
   * is saturated). Before Phase 3 this meant "not traffic blue"; amber and red are now traffic too.
   */
  other: number;
  /** Dominant hues (10 deg bins) of the "other" saturated pixels, for the report. */
  otherHues: Record<string, number>;
  counted: number;
}

/** Saturated-pixel breakdown of a screenshot by hue, ignoring the given rectangles (e.g. the fixture banner). */
export async function saturationBreakdown(page: Page, png: Buffer, ignore: Box[] = [], region?: Box): Promise<SaturationBreakdown> {
  return page.evaluate(
    async ({ b64, ignore, region, chromaMin, hue, amberHue, redHue }) => {
      const img = new Image();
      img.src = `data:image/png;base64,${b64}`;
      await img.decode();
      const c = document.createElement("canvas");
      c.width = img.width;
      c.height = img.height;
      const ctx = c.getContext("2d")!;
      ctx.drawImage(img, 0, 0);
      const { data } = ctx.getImageData(0, 0, c.width, c.height);
      // Screenshots may be taken at a device scale factor: boxes are in CSS pixels.
      const s = img.width / window.innerWidth;
      const scale = (b: { x: number; y: number; width: number; height: number }) => ({ x: b.x * s, y: b.y * s, w: b.width * s, h: b.height * s });
      const ig = ignore.map(scale);
      const r0 = region ? scale(region) : { x: 0, y: 0, w: c.width, h: c.height };
      let blue = 0;
      let amber = 0;
      let red = 0;
      let other = 0;
      let counted = 0;
      const hues: Record<string, number> = {};
      for (let y = Math.max(0, Math.floor(r0.y)); y < Math.min(c.height, r0.y + r0.h); y++) {
        for (let x = Math.max(0, Math.floor(r0.x)); x < Math.min(c.width, r0.x + r0.w); x++) {
          if (ig.some((r) => x >= r.x && x < r.x + r.w && y >= r.y && y < r.y + r.h)) continue;
          counted++;
          const i = (y * c.width + x) * 4;
          const r = data[i]!, g = data[i + 1]!, b = data[i + 2]!;
          const mx = Math.max(r, g, b), mn = Math.min(r, g, b), d = mx - mn;
          if (d / 255 <= chromaMin) continue;
          let h: number;
          if (mx === r) h = 60 * (((g - b) / d) % 6);
          else if (mx === g) h = 60 * ((b - r) / d + 2);
          else h = 60 * ((r - g) / d + 4);
          if (h < 0) h += 360;
          if (h >= hue.min && h <= hue.max) blue++;
          else if (h >= amberHue.min && h <= amberHue.max) amber++;
          else if (h >= redHue.min || h <= redHue.max) red++;
          else {
            other++;
            const bin = String(Math.floor(h / 10) * 10);
            hues[bin] = (hues[bin] ?? 0) + 1;
          }
        }
      }
      return { trafficBlue: blue / counted, trafficAmber: amber / counted, trafficRed: red / counted, other: other / counted, otherHues: hues, counted };
    },
    {
      b64: png.toString("base64"),
      ignore,
      region,
      chromaMin: SATURATED_CHROMA,
      hue: TRAFFIC_BLUE_HUE,
      amberHue: TRAFFIC_AMBER_HUE,
      redHue: TRAFFIC_RED_HUE,
    },
  );
}

export const COM_CREDIT = "Building and road heights: Contains City of Melbourne Open Data (CC BY 4.0), modified";
export const OSM_CREDITS = "OpenFreeMap © OpenMapTiles © OpenStreetMap contributors";
export const ATTRIBUTION_TEXT = `${COM_CREDIT} | ${OSM_CREDITS}`;

/** The attribution must be on screen, expanded, exactly worded and not covered by anything (incl. the playback panel). */
export async function expectAttributionUnobstructed(page: Page) {
  const attrib = page.locator(".maplibregl-ctrl-attrib");
  await expect(attrib).toBeVisible();
  await expect(attrib).not.toHaveClass(/maplibregl-compact(?!-show)/);
  const text = (await attrib.innerText()).replace(/\s+/g, " ").trim();
  expect(text).toBe(ATTRIBUTION_TEXT);
  await expect(attrib.getByRole("link", { name: COM_CREDIT })).toHaveAttribute("href", "https://creativecommons.org/licenses/by/4.0/");
  await expect(attrib.getByRole("link", { name: "© OpenStreetMap contributors" })).toHaveAttribute(
    "href",
    "https://www.openstreetmap.org/copyright",
  );
  const bg = await attrib.evaluate((el) => getComputedStyle(el).backgroundColor);
  expect(bg, "attribution panel colour").toBe("rgba(5, 7, 10, 0.94)");
  const box = (await attrib.boundingBox())!;
  const vp = page.viewportSize()!;
  expect(box.x).toBeGreaterThanOrEqual(0);
  expect(box.y + box.height).toBeLessThanOrEqual(vp.height);
  expect(box.x + box.width).toBeLessThanOrEqual(vp.width);
  expect(box.height, "attribution fits on one line").toBeLessThan(30);
  // Sample a grid over the whole box: every point must hit the attribution itself, not an overlay.
  const hits = await page.evaluate((b) => {
    const el = document.querySelector(".maplibregl-ctrl-attrib")!;
    const out: boolean[] = [];
    for (const fy of [0.25, 0.5, 0.75]) {
      for (let i = 0; i <= 10; i++) {
        out.push(el.contains(document.elementFromPoint(b.x + 3 + ((b.width - 6) * i) / 10, b.y + b.height * fy)));
      }
    }
    return out;
  }, box);
  expect(hits.every(Boolean), `attribution covered at some points: ${JSON.stringify(hits)}`).toBe(true);
  // And no other panel's box overlaps it (a transparent overlay would not show in elementFromPoint).
  const overlaps = await page.evaluate((b) => {
    const ids = ["playback", "traffic-note", "fixture-banner", "fps"];
    return ids.flatMap((id) => {
      const el = document.querySelector(`[data-testid="${id}"]`);
      if (!el) return [];
      const r = el.getBoundingClientRect();
      const hit = r.left < b.x + b.width && r.right > b.x && r.top < b.y + b.height && r.bottom > b.y;
      return hit ? [id] : [];
    });
  }, box);
  expect(overlaps, "panels overlapping the attribution").toEqual([]);
}

/** Hide the dev fps readout (screenshots must not show it). */
export const hideFps = (page: Page) => page.addStyleTag({ content: '[data-testid="fps"] { display: none !important; }' });
