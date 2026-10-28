import { WebMercatorViewport } from "@math.gl/web-mercator";
import { describe, expect, it } from "vitest";
import { BUILDINGS_BBOX, bboxContainsPoint, SIM_BBOX, TUNNEL_EXTRACT_BBOX } from "../config/bbox";
import { CAMERA_PRESETS, DEFAULT_PRESET, getPreset, VIEW_LIMITS } from "./presets";

describe("camera presets", () => {
  it("has CBD, Southbank, Docklands, the Kings Way / West Gate interchange, Footscray Rd, Burnley tunnel and an overview", () => {
    expect(CAMERA_PRESETS.map((p) => p.id)).toEqual(["cbd", "southbank", "docklands", "interchange", "footscray", "burnley", "overview"]);
    expect(getPreset(DEFAULT_PRESET).id).toBe("southbank");
  });

  it.each(CAMERA_PRESETS.map((p) => [p.id, p] as const))("%s centre is inside BUILDINGS_BBOX (and SIM_BBOX, except Burnley)", (_, p) => {
    expect(bboxContainsPoint(BUILDINGS_BBOX, p.view.longitude, p.view.latitude)).toBe(true);
    // The Burnley tunnel's Yarra crossing is east of the simulated area (TUNNEL_EXTRACT_BBOX reaches it).
    if (p.id !== "burnley") expect(bboxContainsPoint(SIM_BBOX, p.view.longitude, p.view.latitude)).toBe(true);
    else expect(bboxContainsPoint(TUNNEL_EXTRACT_BBOX, p.view.longitude, p.view.latitude)).toBe(true);
  });

  it.each(CAMERA_PRESETS.map((p) => [p.id, p] as const))("%s has a sensible pitch, bearing and zoom", (_, p) => {
    expect(p.view.pitch).toBeGreaterThanOrEqual(45);
    // Road close-ups (interchange, Footscray Rd, Burnley tunnel) go lower and steeper so stacked decks and tunnel
    // depth read; still under maxPitch.
    expect(p.view.pitch).toBeLessThanOrEqual(["interchange", "footscray", "burnley"].includes(p.id) ? 70 : 60);
    expect(p.view.pitch).toBeLessThan(VIEW_LIMITS.maxPitch);
    expect(p.view.bearing).toBeGreaterThanOrEqual(-180);
    expect(p.view.bearing).toBeLessThanOrEqual(180);
    expect(p.view.zoom).toBeGreaterThanOrEqual(VIEW_LIMITS.minZoom);
    expect(p.view.zoom).toBeLessThanOrEqual(VIEW_LIMITS.maxZoom);
  });

  it("Footscray Rd looks at the West Gate Tunnel viaducts at the north-west corner", () => {
    const { view } = getPreset("footscray");
    expect(view.longitude).toBeLessThanOrEqual(144.937);
    expect(view.latitude).toBeGreaterThanOrEqual(-37.808);
    expect(view.pitch).toBeGreaterThanOrEqual(60);
  });

  it("interchange looks at the Kings Way / West Gate interchange, steeply tilted", () => {
    const { view } = getPreset("interchange");
    expect(view.longitude).toBeGreaterThanOrEqual(144.955);
    expect(view.longitude).toBeLessThanOrEqual(144.962);
    expect(view.latitude).toBeGreaterThanOrEqual(-37.832);
    expect(view.latitude).toBeLessThanOrEqual(-37.826);
    expect(view.pitch).toBeGreaterThanOrEqual(55);
    expect(view.zoom).toBeGreaterThanOrEqual(16);
  });

  it("Burnley tunnel frames the tunnels' Yarra crossing, deep point included, well inside the 1440x900 frame", () => {
    const { view } = getPreset("burnley");
    expect(view.longitude).toBeGreaterThanOrEqual(144.975);
    expect(view.longitude).toBeLessThanOrEqual(144.986);
    expect(view.latitude).toBeGreaterThanOrEqual(-37.827);
    expect(view.latitude).toBeLessThanOrEqual(-37.822);
    const vp = new WebMercatorViewport({ width: 1440, height: 900, ...view });
    // Burnley (8066124) and Domain (8066126) at -30 m where they cross under the river (~144.9784 / ~144.9797).
    for (const p of [
      [144.97864, -37.82528, -30],
      [144.9797, -37.8257, -30],
    ]) {
      const [x, y] = vp.project(p);
      expect(x).toBeGreaterThan(150);
      expect(x).toBeLessThan(1290);
      expect(y).toBeGreaterThan(160); // below the control panel
      expect(y).toBeLessThan(800); // above the playback bar
    }
  });

  // The screenshot viewport (1440x900). MapLibre and deck.gl share this projection (fov 36.87 deg).
  it.each([
    ["Australia 108", 144.96396, -37.82315, 317],
    ["Eureka Tower", 144.96454, -37.82158, 298],
  ])("southbank frames the top of %s with headroom", (_, lon, lat, height) => {
    const vp = new WebMercatorViewport({ width: 1440, height: 900, ...getPreset("southbank").view });
    const [x, top] = vp.project([lon, lat, height]);
    const [, base] = vp.project([lon, lat, 0]);
    expect(x).toBeGreaterThan(200);
    expect(x).toBeLessThan(1240);
    expect(top).toBeGreaterThan(100); // clear of the top edge
    expect(base).toBeLessThan(900);
  });
});
