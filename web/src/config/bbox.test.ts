import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { bboxContainsPoint, BUILDINGS_AREA_BBOX, BUILDINGS_BBOX, SIM_BBOX, SIM_EXTRACT_BBOX, TUNNEL_EXTRACT_BBOX, type BBox } from "./bbox";

const CONFIG_PY = resolve(import.meta.dirname, "../../../pipeline/config.py");

function parseBBox(source: string, name: string): BBox {
  const m = new RegExp(`${name}\\s*=\\s*BBox\\(([^)]*)\\)`).exec(source);
  if (!m?.[1]) throw new Error(`${name} not found in pipeline/config.py`);
  const out: Record<string, number> = {};
  for (const part of m[1].split(",")) {
    const [k, v] = part.split("=").map((s) => s.trim());
    if (k && v) out[k] = Number(v);
  }
  return { west: out.west!, south: out.south!, east: out.east!, north: out.north! };
}

function parseNumber(source: string, name: string): number {
  const m = new RegExp(`^${name}\\s*=\\s*([\\d._]+)\\s*$`, "m").exec(source);
  if (!m?.[1]) throw new Error(`${name} not found in pipeline/config.py`);
  return Number(m[1].replaceAll("_", ""));
}

describe("bbox constants", () => {
  const py = readFileSync(CONFIG_PY, "utf8");

  it("SIM_BBOX matches pipeline/config.py", () => {
    expect(SIM_BBOX).toEqual(parseBBox(py, "SIM_BBOX"));
  });

  it("BUILDINGS_BBOX matches pipeline/config.py", () => {
    expect(BUILDINGS_BBOX).toEqual(parseBBox(py, "BUILDINGS_BBOX"));
  });

  it("BUILDINGS_AREA_BBOX matches pipeline/config.py and contains BUILDINGS_BBOX", () => {
    expect(BUILDINGS_AREA_BBOX).toEqual(parseBBox(py, "BUILDINGS_AREA_BBOX"));
    for (const [lon, lat] of [[BUILDINGS_BBOX.west, BUILDINGS_BBOX.south], [BUILDINGS_BBOX.east, BUILDINGS_BBOX.north]] as const) {
      expect(bboxContainsPoint(BUILDINGS_AREA_BBOX, lon, lat)).toBe(true);
    }
  });

  it("SIM_EXTRACT_BBOX matches pipeline/config.py (SIM_BBOX buffered by SIM_BUFFER_M)", () => {
    // The Python side computes it, so check the definition and recompute with the same formula (config.buffered).
    expect(py).toMatch(/^SIM_EXTRACT_BBOX\s*=\s*buffered\(SIM_BBOX,\s*SIM_BUFFER_M\)\s*$/m);
    const sim = parseBBox(py, "SIM_BBOX");
    const metres = parseNumber(py, "SIM_BUFFER_M");
    const mPerDegLat = parseNumber(py, "_M_PER_DEG_LAT");
    const midLat = (sim.south + sim.north) / 2;
    const dlat = metres / mPerDegLat;
    const dlon = metres / (mPerDegLat * Math.cos((midLat * Math.PI) / 180));
    const expected = { west: sim.west - dlon, south: sim.south - dlat, east: sim.east + dlon, north: sim.north + dlat };
    for (const k of ["west", "south", "east", "north"] as const) expect(SIM_EXTRACT_BBOX[k]).toBeCloseTo(expected[k], 10);
    // It must contain the simulated area and sit inside the building ring.
    expect(SIM_EXTRACT_BBOX.west).toBeLessThan(SIM_BBOX.west);
    expect(SIM_EXTRACT_BBOX.north).toBeGreaterThan(SIM_BBOX.north);
    expect(SIM_EXTRACT_BBOX.west).toBeGreaterThan(BUILDINGS_BBOX.west);
    expect(SIM_EXTRACT_BBOX.south).toBeGreaterThan(BUILDINGS_BBOX.south);
  });

  it("TUNNEL_EXTRACT_BBOX matches pipeline/config.py (SIM_EXTRACT_BBOX with BUILDINGS_BBOX's east edge)", () => {
    const m = /^TUNNEL_EXTRACT_BBOX\s*=\s*BBox\(([^)]*)\)/m.exec(py);
    if (!m?.[1]) throw new Error("TUNNEL_EXTRACT_BBOX not found in pipeline/config.py");
    const fields = Object.fromEntries(
      m[1]
        .split(",")
        .map((part) => part.split("=").map((x) => x.trim()))
        .filter(([k, v]) => k && v),
    );
    expect(fields).toEqual({
      west: "SIM_EXTRACT_BBOX.west",
      south: "SIM_EXTRACT_BBOX.south",
      east: "BUILDINGS_BBOX.east",
      north: "SIM_EXTRACT_BBOX.north",
    });
    expect(TUNNEL_EXTRACT_BBOX).toEqual({ ...SIM_EXTRACT_BBOX, east: parseBBox(py, "BUILDINGS_BBOX").east });
    // The extended tunnels reach under the Yarra (~144.9784 / ~144.9797), past SIM_EXTRACT_BBOX's east edge.
    expect(TUNNEL_EXTRACT_BBOX.east).toBeGreaterThan(144.98);
  });
});
