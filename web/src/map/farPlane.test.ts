import { describe, expect, it } from "vitest";
import { PLAUSIBLE_Z_M } from "../data/roadsContract";
import { FAR_PLANE_DEPTH_M, lowerMinElevation } from "./farPlane";

/** Stand-in for MapLibre's MercatorTransform: a prototype getter over a value the map resets to 0 every frame. */
class FakeTransform {
  helperMin = 0;
  get minElevationForCurrentTile(): number {
    return this.helperMin;
  }
  setMinElevationForCurrentTile(v: number) {
    this.helperMin = v;
  }
}

describe("lowerMinElevation", () => {
  it("reaches below the deepest plausible road", () => {
    expect(FAR_PLANE_DEPTH_M).toBeGreaterThan(-PLAUSIBLE_Z_M.min);
  });

  it("makes the transform report at most -depth, even after the map resets it to 0", () => {
    const t = new FakeTransform();
    expect(lowerMinElevation(t, 45)).toBe(true);
    expect(t.minElevationForCurrentTile).toBe(-45);
    t.setMinElevationForCurrentTile(0);
    expect(t.minElevationForCurrentTile).toBe(-45);
    // Real terrain lower than that still wins.
    t.setMinElevationForCurrentTile(-120);
    expect(t.minElevationForCurrentTile).toBe(-120);
    // Other instances (and the prototype) are untouched.
    expect(new FakeTransform().minElevationForCurrentTile).toBe(0);
  });

  it("is idempotent and reports a transform it can't patch", () => {
    const t = new FakeTransform();
    lowerMinElevation(t, 45);
    lowerMinElevation(t, 45);
    t.setMinElevationForCurrentTile(-10);
    expect(t.minElevationForCurrentTile).toBe(-45);
    expect(lowerMinElevation({}, 45)).toBe(false);
  });
});
