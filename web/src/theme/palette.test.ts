import { describe, expect, it } from "vitest";
import {
  BUILDING_OPACITY,
  buildingFill,
  chroma,
  clampBuildingOpacity,
  cssRgba,
  hiddenRoofline,
  LOW_RISE_FADE,
  lowRiseFade,
  OUTER_RING_FADE,
  outerRingFade,
  GLOW,
  palette,
  type RGBA,
  rooflineOpacity,
  SPEED,
  speedClass,
  speedEvidence,
  TRAIL_SECONDS,
  XRAY,
} from "./palette";

describe("palette", () => {
  it("defaults building opacity to 25%, within the brief's 15–30% range", () => {
    expect(BUILDING_OPACITY).toEqual({ min: 0.15, max: 0.3, default: 0.25 });
    expect(buildingFill()).toEqual([200, 220, 240, 64]);
  });

  it("clamps building opacity to 15–30%", () => {
    expect(clampBuildingOpacity(0.05)).toBe(0.15);
    expect(clampBuildingOpacity(0.9)).toBe(0.3);
    expect(clampBuildingOpacity(0.2)).toBe(0.2);
    expect(clampBuildingOpacity(Number.NaN)).toBe(0.25);
    expect(buildingFill(1)[3]).toBe(Math.round(0.3 * 255));
  });

  it("keeps buildings and outlines muted (pale, low chroma) and outlines brighter than fills", () => {
    expect(chroma(palette.buildingFill)).toBeLessThan(0.25);
    expect(chroma(palette.buildingOutline)).toBeLessThan(0.25);
    const lum = (c: readonly number[]) => (c[0]! + c[1]! + c[2]!) / 3;
    expect(lum(palette.buildingOutline)).toBeGreaterThan(lum(palette.buildingFill));
  });

  it("draws hidden rooflines in the outline colour but much fainter", () => {
    const h = hiddenRoofline();
    expect(h.slice(0, 3)).toEqual(palette.buildingOutline.slice(0, 3));
    expect(h[3]).toBeLessThan(palette.buildingOutline[3] / 2);
    expect(h[3]).toBeGreaterThan(0);
  });

  it("fades rooflines when zoomed out, full strength when close", () => {
    expect(rooflineOpacity(12)).toBeCloseTo(0.3);
    expect(rooflineOpacity(13.5)).toBeCloseTo(0.3);
    expect(rooflineOpacity(14.5)).toBeCloseTo(0.65);
    expect(rooflineOpacity(15.5)).toBe(1);
    expect(rooflineOpacity(18)).toBe(1);
  });

  it("reserves saturated blue / amber / red for traffic", () => {
    const { flowing, slowing, jammed } = palette.traffic;
    expect(flowing).toEqual([56, 140, 255]);
    expect(slowing).toEqual([255, 176, 32]);
    expect(jammed).toEqual([235, 50, 50]);
    for (const c of [flowing, slowing, jammed]) expect(chroma(c)).toBeGreaterThan(0.6);
    // Hue order: blue channel dominates flowing, red dominates slowing and jammed, amber has more green than red.
    expect(flowing[2]).toBe(Math.max(...flowing));
    expect(slowing[0]).toBe(Math.max(...slowing));
    expect(jammed[0]).toBe(Math.max(...jammed));
    expect(slowing[1]).toBeGreaterThan(jammed[1]);
  });

  it("keeps the picked traffic look sane (Stage C: trail 20 s, glow through, X-ray depth)", () => {
    expect(TRAIL_SECONDS).toBe(20);
    expect(GLOW.core.alpha).toBeGreaterThan(GLOW.halo.alpha); // a bright core inside a faint glow
    expect(GLOW.halo.widthM).toBeGreaterThan(GLOW.core.widthM);
    expect(GLOW.head.minPx).toBeLessThan(GLOW.head.maxPx);
    // Cars behind buildings glow through them, but dimmer than cars in plain view.
    expect(GLOW.throughBuildings).toBeGreaterThan(0);
    expect(GLOW.throughBuildings).toBeLessThan(1);
    // X-ray tunnel cars: wider and glowing more than surface cars, with a tether and a ring on the surface.
    for (const k of ["coreWidth", "haloWidth", "headHaloScale", "headRadius", "surfaceMarkScale"] as const) expect(XRAY[k]).toBeGreaterThanOrEqual(1);
    expect(XRAY.headHaloAlpha).toBeGreaterThanOrEqual(GLOW.head.haloAlpha);
    expect(XRAY.tetherAlpha).toBeGreaterThan(0);
    expect(XRAY.tetherAlpha).toBeLessThanOrEqual(255);
    expect(XRAY.surfaceMark).toBeGreaterThan(0);
    expect(XRAY.surfaceMark).toBeLessThanOrEqual(1);
  });

  it("keeps tunnel structure ghost-toned (low chroma; only the cars are saturated), with a ceiling above the floor", () => {
    const { ceilingM, ceilingAlpha, ...colours } = palette.roadTunnel;
    for (const c of Object.values(colours)) {
      expect(chroma(c as RGBA)).toBeLessThan(0.25);
      expect((c as RGBA)[3]).toBeGreaterThan(0);
    }
    expect(ceilingM).toBeGreaterThan(0);
    expect(ceilingAlpha).toBeGreaterThan(0);
    expect(ceilingAlpha).toBeLessThan(1);
  });

  it("keeps UI chrome unsaturated (traffic colours stay the only saturated ones)", () => {
    const hex = (c: string) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16)) as [number, number, number];
    for (const c of [palette.ui.text, palette.ui.textDim, palette.ui.accent]) expect(chroma(hex(c))).toBeLessThan(0.25);
  });

  it("fades outer-ring rooflines smoothly with distance outside the simulated area", () => {
    expect(outerRingFade(0)).toBe(1);
    expect(outerRingFade(OUTER_RING_FADE.distanceM / 2)).toBeCloseTo((1 + OUTER_RING_FADE.minFactor) / 2);
    expect(outerRingFade(OUTER_RING_FADE.distanceM)).toBeCloseTo(OUTER_RING_FADE.minFactor);
    expect(outerRingFade(10_000)).toBeCloseTo(OUTER_RING_FADE.minFactor);
    expect(outerRingFade(100)).toBeLessThan(1);
    expect(outerRingFade(100)).toBeGreaterThan(outerRingFade(300));
  });

  it("draws tower edges fainter than rooflines", () => {
    expect(palette.towerEdgeAlpha).toBeLessThan(palette.buildingOutline[3]);
    expect(palette.towerEdgeAlpha).toBeGreaterThan(palette.hiddenRooflineAlpha);
  });

  it("formats CSS colours for the MapLibre style", () => {
    expect(cssRgba([1, 2, 3])).toBe("rgba(1, 2, 3, 1)");
    expect(cssRgba([1, 2, 3], 0.5)).toBe("rgba(1, 2, 3, 0.5)");
  });

  it("fades low-rise rooflines zoomed out only, never towers", () => {
    expect(lowRiseFade(5, 13)).toBeCloseTo(LOW_RISE_FADE.minFactor);
    expect(lowRiseFade(200, 13)).toBe(1);
    expect(lowRiseFade(5, 15.5)).toBe(1);
    expect(lowRiseFade(5, 14.5)).toBeGreaterThan(LOW_RISE_FADE.minFactor);
    expect(lowRiseFade(5, 14.5)).toBeLessThan(1);
    expect(lowRiseFade(25, 13)).toBeGreaterThan(lowRiseFade(12, 13));
  });
});

describe("road speed classes (SPEED)", () => {
  it("blue >= 0.7, amber 0.4–0.7, red < 0.4; ratios above 1 are still flowing; no data has no class", () => {
    expect(speedClass(0.7)).toBe("flowing");
    expect(speedClass(1.3)).toBe("flowing");
    expect(speedClass(0.6999)).toBe("slowing");
    expect(speedClass(0.4)).toBe("slowing");
    expect(speedClass(0.3999)).toBe("jammed");
    expect(speedClass(0)).toBe("jammed");
    expect(speedClass(Number.NaN)).toBeNull();
    expect(speedClass(Number.POSITIVE_INFINITY)).toBeNull();
  });

  it("reuses the traffic palette: the speed colours are exactly the car colours (the only saturated ones)", () => {
    for (const cls of ["flowing", "slowing", "jammed"] as const) expect(chroma(palette.traffic[cls])).toBeGreaterThan(0.6);
    expect(SPEED.thresholds).toEqual({ flowing: 0.7, slowing: 0.4 });
  });

  it("evidence: one car is faint, full strength from fullAt cars, nothing without cars", () => {
    expect(speedEvidence(0)).toBe(0);
    expect(speedEvidence(1)).toBe(SPEED.evidence.minAlpha);
    expect(speedEvidence(SPEED.evidence.fullAt)).toBe(1);
    expect(speedEvidence(50)).toBe(1);
    expect(speedEvidence(2)).toBeGreaterThan(speedEvidence(1));
  });

  it("speed lines stay quieter than the cars (cars carry the light); congestion is the loudest road", () => {
    expect(SPEED.classStrength.flowing).toBeLessThan(SPEED.classStrength.jammed);
    expect(SPEED.core.alpha).toBeLessThan(GLOW.core.alpha);
    expect(SPEED.halo.alpha).toBeLessThan(GLOW.halo.alpha);
    expect(SPEED.halo.widthM).toBeLessThan(GLOW.halo.widthM);
    expect(SPEED.liftM).toBeLessThan(0.5); // under the cars' CAR_LIFT_M
  });
});
