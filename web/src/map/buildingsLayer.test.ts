import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { SIM_BBOX } from "../config/bbox";
import { type BuildingPolygon, type BuildingsCollection, type Position, toPolygons } from "../data/buildingsContract";
import { OUTER_RING_FADE } from "../theme/palette";
import { buildingsLayers, distanceOutsideBBoxM, stackLinks, toRooflines, TOWER_EDGES, toTowerEdges } from "./buildingsLayer";

// Hand-written shapes in local metres around the CBD, converted to lon/lat.
const LAT0 = -37.815;
const LON0 = 144.963;
const kx = 111_320 * Math.cos((LAT0 * Math.PI) / 180);
const ll = ([x, y]: [number, number]): Position => [LON0 + x / kx, LAT0 + y / 110_574];
const building = (ringM: [number, number][], height: number, id = "b", base = 0): BuildingPolygon => {
  const ring = ringM.map(ll);
  return { id, height, base, polygon: [[...ring, ring[0]!]] };
};
const square = (h: number) => building([[0, 0], [30, 0], [30, 30], [0, 30]], h);

describe("tower edges", () => {
  it("adds one vertical edge per real corner of a tall tower, from the ground to its roof", () => {
    const edges = toTowerEdges([square(200)]);
    expect(edges).toHaveLength(4);
    for (const e of edges) {
      expect(e.path[0][2]).toBe(0);
      expect(e.path[1][2]).toBe(200);
      expect(e.path[0].slice(0, 2)).toEqual(e.path[1].slice(0, 2));
    }
  });

  it("starts a stacked piece's corner edges at its base, not the ground", () => {
    const edges = toTowerEdges([building([[0, 0], [20, 0], [20, 20], [0, 20]], 200, "t", 40)]);
    expect(edges).toHaveLength(4);
    for (const e of edges) expect([e.path[0][2], e.path[1][2]]).toEqual([40, 200]);
  });

  it("skips buildings under the height threshold", () => {
    expect(toTowerEdges([square(TOWER_EDGES.minHeightM - 1)])).toHaveLength(0);
    expect(toTowerEdges([square(TOWER_EDGES.minHeightM)])).toHaveLength(4);
  });

  it("puts no edges along a curved facade (many small turns), only at its sharp corners", () => {
    // A half-disc: 24 small-turn vertices on the arc, two 90-degree corners where the arc meets the diameter.
    const arc: [number, number][] = [];
    for (let i = 0; i <= 24; i++) {
      const a = (Math.PI * i) / 24;
      arc.push([20 * Math.cos(a), 20 * Math.sin(a)]);
    }
    expect(toTowerEdges([building(arc, 150)])).toHaveLength(2);
  });

  it("drops near-duplicate corners at tiny notches and caps edges per building", () => {
    // A square with a 1 m notch at one corner: the notch's corners are within minSpacingM of each other.
    const notched = building([[0, 0], [30, 0], [30, 29], [29, 29], [29, 30], [0, 30]], 100);
    expect(toTowerEdges([notched])).toHaveLength(4);
    // A 40-gon star: every vertex is a sharp corner, but the cap applies.
    const star: [number, number][] = [];
    for (let i = 0; i < 40; i++) {
      const r = i % 2 ? 10 : 25;
      const a = (2 * Math.PI * i) / 40;
      star.push([r * Math.cos(a), r * Math.sin(a)]);
    }
    expect(toTowerEdges([building(star, 100)])).toHaveLength(TOWER_EDGES.maxPerBuilding);
  });
});

describe("stacked tiers (silhouette edges)", () => {
  // A podium (0-30 m, 40x40), a shaft on it flush with its south and west sides (30-150 m, 30x30), and a set-back crown
  // (150-190 m, 10x10 in the middle of the shaft).
  const podium = building([[0, 0], [40, 0], [40, 40], [0, 40]], 30, "s_podium");
  const shaft = building([[0, 0], [30, 0], [30, 30], [0, 30]], 150, "s_shaft", 30);
  const crown = building([[10, 10], [20, 10], [20, 20], [10, 20]], 190, "s_crown", 150);
  const tower = [podium, shaft, crown];

  it("links each tier to the tiers stacked on it and under it", () => {
    const links = stackLinks(tower);
    expect(links.above(0)).toEqual([1]);
    expect(links.above(1)).toEqual([2]);
    expect(links.below(2)).toEqual([1]);
    expect(links.below(1)).toEqual([0]);
    expect(links.above(2)).toEqual([]);
  });

  it("does not link tiers that touch in height but not in plan", () => {
    const apart = building([[100, 100], [120, 100], [120, 120], [100, 120]], 200, "other", 150);
    expect(stackLinks([shaft, apart]).above(0)).toEqual([]);
  });

  it("runs a corner line through every tier that shares it, and keeps set-back corners", () => {
    const edges = toTowerEdges(tower);
    const spans = edges.map((e) => [e.path[0][2], e.path[1][2]]);
    // Three shaft corners lie on the podium's outline, so their lines run from the ground to the shaft's top; the
    // fourth (30, 30) is inside the podium's roof and starts there; the crown keeps its own four corners.
    expect(spans.filter(([a, b]) => a === 0 && b === 150)).toHaveLength(3);
    expect(spans.filter(([a, b]) => a === 30 && b === 150)).toHaveLength(1);
    expect(spans.filter(([a, b]) => a === 150 && b === 190)).toHaveLength(4);
    expect(edges).toHaveLength(8);
  });

  it("merges identical stacked tiers into one line per corner and skips re-entrant corners", () => {
    const a = building([[0, 0], [30, 0], [30, 30], [0, 30]], 100, "a");
    const b = building([[0, 0], [30, 0], [30, 30], [0, 30]], 200, "b", 100);
    const merged = toTowerEdges([a, b]);
    expect(merged).toHaveLength(4); // not 8 (one set per tier)
    for (const e of merged) expect([e.path[0][2], e.path[1][2]]).toEqual([0, 200]);
    // An L-shape: 5 convex corners on the hull, 1 re-entrant corner that is never part of the outline.
    const l = building([[0, 0], [30, 0], [30, 15], [15, 15], [15, 30], [0, 30]], 120, "l");
    expect(toTowerEdges([l])).toHaveLength(5);
  });

  it("terminates on thin tiers at one level that each sit on the other (no endless walk)", () => {
    // qa (cfb379f): two 0.5 m tiers at 100 m are each within STACK_TOLERANCE_M of the other's top.
    const tower = building([[0, 0], [30, 0], [30, 30], [0, 30]], 100, "tower");
    const thin1 = building([[0, 0], [30, 0], [30, 30], [0, 30]], 100.5, "thin1", 100);
    const thin2 = building([[0, 0], [30, 0], [30, 30], [0, 30]], 100.5, "thin2", 100);
    const edges = toTowerEdges([tower, thin1, thin2]);
    expect(edges).toHaveLength(4);
    for (const e of edges) expect([e.path[0][2], e.path[1][2]]).toEqual([0, 100.5]);
    // Stacked thin tiers that go up in steps still carry the line all the way.
    const steps = [0, 1, 2, 3].map((k) => building([[0, 0], [30, 0], [30, 30], [0, 30]], 100.5 + 0.5 * (k + 1), `s${k}`, 100 + 0.5 * k));
    for (const e of toTowerEdges([tower, ...steps])) expect(e.path[1][2]).toBe(102.5);
  });

  it("starts no lines on tiers shorter than 15 m (their rooflines outline them)", () => {
    const thin = building([[0, 0], [30, 0], [30, 30], [0, 30]], 80, "thin", 70);
    expect(toTowerEdges([thin])).toHaveLength(0);
  });
});

describe("outer-ring fade", () => {
  it("measures distance outside SIM_BBOX in metres (0 inside)", () => {
    expect(distanceOutsideBBoxM(SIM_BBOX, 144.96, -37.815)).toBe(0);
    // 0.01 degrees of latitude south of the bbox is about 1.1 km.
    expect(distanceOutsideBBoxM(SIM_BBOX, 144.96, SIM_BBOX.south - 0.01)).toBeCloseTo(1105.7, 0);
  });

  it("keeps rooflines full strength inside the simulated area and faded far outside it", () => {
    const inside = toRooflines([square(20)])[0]!;
    expect(inside.fade).toBe(1);
    const far: BuildingPolygon = { id: "far", height: 10, base: 0, polygon: [[[144.927, -37.839], [144.9271, -37.839], [144.9271, -37.8389], [144.927, -37.839]]] };
    expect(toRooflines([far])[0]!.fade).toBeCloseTo(OUTER_RING_FADE.minFactor);
  });
});

describe("buildingsLayers", () => {
  it("extrudes a stacked piece from its base: vertex z = base, elevation = height - base", () => {
    const data = [building([[0, 0], [20, 0], [20, 20], [0, 20]], 200, "t", 40)];
    const fill = buildingsLayers(data, toRooflines(data), toTowerEdges(data), 0.25, 15)[0]! as unknown as {
      props: { getPolygon: (d: BuildingPolygon) => Position[][]; getElevation: (d: BuildingPolygon) => number };
    };
    expect(fill.props.getElevation(data[0]!)).toBe(160);
    expect(fill.props.getPolygon(data[0]!)[0]!.every((p) => p[2] === 40)).toBe(true);
    const flat = square(30);
    expect(fill.props.getElevation(flat)).toBe(30);
  });

  it("draws fills, hidden rooflines, visible rooflines and tower edges", () => {
    const data = [square(200)];
    const layers = buildingsLayers(data, toRooflines(data), toTowerEdges(data), 0.25, 15);
    expect(layers.map((l) => l.id)).toEqual(["buildings-fill", "buildings-roofline-hidden", "buildings-roofline", "buildings-tower-edges"]);
  });
});

const REAL_PATH = resolve(import.meta.dirname, "../../../data/processed/buildings.geojson");

describe.skipIf(!existsSync(REAL_PATH))("tower edges on the real buildings", () => {
  const polygons = toPolygons(JSON.parse(readFileSync(REAL_PATH, "utf8")) as BuildingsCollection);
  const edges = toTowerEdges(polygons);

  it("gives Australia 108 and Eureka Tower solid forms", () => {
    // Ids are now stacked pieces: c<structure>_<n>. Australia 108 is structure c900483, Eureka c815210.
    for (const structure of ["c900483", "c815210"]) {
      const pieces = polygons.filter((p) => String(p.id).startsWith(`${structure}_`));
      expect(pieces.length).toBeGreaterThanOrEqual(2); // stacked, not one slab
      const n = toTowerEdges(pieces).length;
      expect(n).toBeGreaterThanOrEqual(3);
      expect(n).toBeLessThanOrEqual(TOWER_EDGES.maxPerBuilding * pieces.length);
    }
  });

  it("draws a stacked tower as long silhouette lines, not one set per tier", () => {
    // Eureka (c815210): 6 stacked pieces. 11 lines (2026-10 data), most running from the ground past 200 m.
    const eureka = polygons.filter((p) => String(p.id).startsWith("c815210_"));
    const lines = toTowerEdges(eureka);
    expect(lines.length).toBeLessThanOrEqual(TOWER_EDGES.maxPerBuilding);
    expect(lines.filter((e) => e.path[0][2] === 0 && (e.path[1][2] ?? 0) > 200).length).toBeGreaterThanOrEqual(3);
  });

  it("stays a light touch: a few thousand lines at most, not a haze", () => {
    const towers = polygons.filter((p) => p.height >= TOWER_EDGES.minHeightM).length;
    expect(edges.length).toBeLessThan(towers * 4);
    expect(edges.length).toBeLessThan(3000); // 2,236 with the 2026-10 data (6,151 before silhouette lines)
  });
});
