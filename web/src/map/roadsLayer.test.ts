import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import type { Position3, RoadFeature, RoadsCollection } from "../data/roadsContract";
import { chroma, palette } from "../theme/palette";
import {
  belowGroundRuns,
  curtainQuads,
  dashLine,
  depthAlpha,
  dropLines,
  elevatedRuns,
  heightConfidence,
  heightsViewFromQuery,
  offsetPath,
  RIBBONS,
  ribbonAlpha,
  ribbonColors,
  ROAD_WIDTH_M,
  roadsLayers,
  toRoadGeometry,
  TUNNELS,
  tunnelLayers,
  XRAY_PARAMETERS,
} from "./roadsLayer";

const LAT0 = -37.82;
const LON0 = 144.96;
const kx = 111_320 * Math.cos((LAT0 * Math.PI) / 180);
/** Local metres (x east, y north, z up) to [lon, lat, z]. */
const p3 = (x: number, y: number, z: number): Position3 => [LON0 + x / kx, LAT0 + y / 110_574, z];
const toM = (p: Position3) => [(p[0] - LON0) * kx, (p[1] - LAT0) * 110_574, p[2]] as const;

const feature = (coords: Position3[], props: Partial<RoadFeature["properties"]> = {}): RoadFeature => ({
  type: "Feature",
  properties: { way_id: 1, highway: "motorway", layer: 1, bridge: true, tunnel: false, name: "Test", z_source: "estimated", ...props },
  geometry: { type: "LineString", coordinates: coords },
});

// A ground road that ramps up to a 6.5 m deck and back down: ground, ramp, deck, ramp, ground.
const zs = [0, 0, 0, 1, 3, 6.5, 6.5, 6.5, 3, 1, 0, 0];
const way = zs.map((z, i) => p3(i * 20, 0, z));

describe("elevatedRuns", () => {
  it("keeps elevated runs plus one ground vertex at each end", () => {
    const runs = elevatedRuns(way);
    expect(runs).toHaveLength(1);
    expect(runs[0]!.map((p) => p[2])).toEqual([0, 1, 3, 6.5, 6.5, 6.5, 3, 1, 0]);
  });

  it("splits separate elevated stretches and ignores all-ground ways", () => {
    const two = [0, 2, 0, 0, 0, 2, 0].map((z, i) => p3(i * 10, 0, z));
    expect(elevatedRuns(two)).toHaveLength(2);
    expect(elevatedRuns([p3(0, 0, 0), p3(10, 0, 0.2)])).toEqual([]);
  });

  it("handles runs that start or end the way", () => {
    expect(elevatedRuns([p3(0, 0, 5), p3(10, 0, 5), p3(20, 0, 0)])[0]).toHaveLength(3);
    expect(elevatedRuns([p3(0, 0, 0), p3(10, 0, 5)])[0]).toHaveLength(2);
  });
});

// A road that ramps down into a 12 m deep tunnel and back up: ground, ramp, tunnel, ramp, ground.
const tunnelZs = [0, 0, -1, -4, -12, -12, -12, -12, -4, -1, 0, 0];
const tunnelWay = tunnelZs.map((z, i) => p3(i * 30, 0, z));

describe("belowGroundRuns", () => {
  it("keeps below-ground runs plus one ground vertex at each end; elevated and ground parts are left out", () => {
    const runs = belowGroundRuns(tunnelWay);
    expect(runs).toHaveLength(1);
    expect(runs[0]!.map((p) => p[2])).toEqual([0, -1, -4, -12, -12, -12, -12, -4, -1, 0]);
    expect(belowGroundRuns(way)).toEqual([]);
    expect(belowGroundRuns([p3(0, 0, 0), p3(10, 0, -0.2)])).toEqual([]);
    // A way that rises onto a bridge and then dips into a cutting has one run of each kind.
    const both = [0, 3, 6, 3, 0, -3, -6, -3, 0].map((z, i) => p3(i * 20, 0, z));
    expect(elevatedRuns(both)).toHaveLength(1);
    expect(belowGroundRuns(both)).toHaveLength(1);
  });

  it("fades with depth like ribbonAlpha does with height", () => {
    expect(depthAlpha(0)).toBe(0);
    expect(depthAlpha(3)).toBe(0);
    expect(depthAlpha(-RIBBONS.fullAlphaZ)).toBe(1);
    expect(depthAlpha(-30)).toBe(1);
    expect(depthAlpha(-1)).toBeCloseTo(ribbonAlpha(1), 12);
  });
});

describe("ribbonAlpha", () => {
  it("is 0 on the ground, full at fullAlphaZ and above, rising in between", () => {
    expect(ribbonAlpha(0)).toBe(0);
    expect(ribbonAlpha(-3)).toBe(0);
    expect(ribbonAlpha(RIBBONS.fullAlphaZ)).toBe(1);
    expect(ribbonAlpha(26)).toBe(1);
    expect(ribbonAlpha(1)).toBeGreaterThan(0);
    expect(ribbonAlpha(1)).toBeLessThan(ribbonAlpha(3));
  });
});

describe("offsetPath", () => {
  it("offsets a straight eastbound path to the left (north) by the given metres, keeping z", () => {
    const out = offsetPath([p3(0, 0, 5), p3(100, 0, 6)], 4).map(toM);
    for (const [, y] of out) expect(y).toBeCloseTo(4, 3);
    expect(out[0]![2]).toBe(5);
    expect(out[1]![2]).toBe(6);
  });

  it("keeps a constant perpendicular distance around a right-angle corner (mitre)", () => {
    const out = offsetPath([p3(0, 0, 0), p3(50, 0, 0), p3(50, 50, 0)], -3).map(toM);
    // Right side of east-then-north: the corner offset sits at (53, -3).
    expect(out[1]![0]).toBeCloseTo(53, 2);
    expect(out[1]![1]).toBeCloseTo(-3, 2);
  });
});

describe("dropLines", () => {
  it("drops verticals to the ground every spacing metres where the deck is high enough", () => {
    const deck = [p3(0, 0, 10), p3(200, 0, 10)];
    const drops = dropLines(deck, 40, 4);
    expect(drops).toHaveLength(5); // at 20, 60, 100, 140, 180 m
    for (const d of drops) {
      expect(d.path[0][2]).toBe(10);
      expect(d.path[1][2]).toBe(0);
      expect(d.path[0].slice(0, 2)).toEqual(d.path[1].slice(0, 2));
    }
    expect(dropLines([p3(0, 0, 2), p3(200, 0, 2)], 40, 4)).toEqual([]);
  });
});

describe("toRoadGeometry", () => {
  it("builds a deck, two edges, two fascia lines and paired drop-lines per elevated run", () => {
    const g = toRoadGeometry([feature(way)]);
    expect(g.ribbons).toHaveLength(1);
    expect(g.ribbons[0]!.width).toBe(ROAD_WIDTH_M.motorway);
    expect(g.ribbons[0]!.alpha[0]).toBe(0); // ground foot fades out
    expect(g.ribbons[0]!.alpha[4]).toBe(1);
    expect(g.edges).toHaveLength(2);
    expect(g.fascia).toHaveLength(2);
    expect(g.fascia[0]!.path.every((p) => p[2] >= 0)).toBe(true);
    expect(g.drops.length).toBeGreaterThan(0);
    expect(g.drops.length % 2).toBe(0);
  });

  it("leaves out tunnel ways at or above ground and ground-only ways; no drop-lines on non-bridge ramps", () => {
    expect(toRoadGeometry([feature(way, { tunnel: true })]).ribbons).toHaveLength(0);
    expect(toRoadGeometry([feature([p3(0, 0, 0), p3(100, 0, 0)])]).ribbons).toHaveLength(0);
    expect(toRoadGeometry([feature(way, { bridge: false })]).drops).toHaveLength(0);
  });

  it("falls back to a default width for unknown highway classes", () => {
    expect(toRoadGeometry([feature(way, { highway: "busway" })]).ribbons[0]!.width).toBeGreaterThan(3);
  });

  it("makes deck, hidden edge, edge, fascia and drop-line layers", () => {
    expect(roadsLayers(toRoadGeometry([feature(way)])).map((l) => l.id)).toEqual([
      "roads-deck",
      "roads-edge-hidden",
      "roads-edge",
      "roads-fascia",
      "roads-drop",
    ]);
  });
});

describe("below ground (Stage B)", () => {
  const tunnelProps = { tunnel: true, bridge: false, layer: -1 } as const;

  it("draws the underground part of a tunnel way as an X-ray tunnel ribbon, not a normal one", () => {
    const g = toRoadGeometry([feature(tunnelWay, tunnelProps)]);
    expect(g.ribbons).toEqual([]);
    expect(g.edges).toEqual([]);
    expect(g.tunnel.ribbons).toHaveLength(1);
    expect(g.tunnel.edges).toHaveLength(2);
    const r = g.tunnel.ribbons[0]!;
    expect(r.width).toBe(ROAD_WIDTH_M.motorway);
    expect(r.alpha[0]).toBe(0); // the portal fades into the ground
    expect(Math.max(...r.alpha)).toBe(1);
    expect(Math.min(...r.path.map((p) => p[2]))).toBe(-12);
  });

  it("draws tunnels with dashed edges and no verticals along them (they must never read as bridge piers)", () => {
    const t = toRoadGeometry([feature(tunnelWay, tunnelProps)]).tunnel;
    expect(t.dashes.length).toBeGreaterThan(4);
    // Every dash lies within the tunnel edges' extent; no segment of any dash is vertical.
    for (const d of t.dashes) {
      expect(d.path.length).toBeGreaterThanOrEqual(2);
      for (let k = 1; k < d.path.length; k++) {
        const [a, b] = [toM(d.path[k - 1]!), toM(d.path[k]!)];
        expect(Math.hypot(b[0] - a[0], b[1] - a[1])).toBeGreaterThan(0);
      }
    }
    const dashed = t.dashes.reduce((s, d) => s + toM(d.path.at(-1)!)[0] - toM(d.path[0]!)[0], 0);
    const full = t.edges.reduce((s, e) => s + toM(e.path.at(-1)!)[0] - toM(e.path[0]!)[0], 0);
    expect(dashed / full).toBeCloseTo(TUNNELS.dashM.on / (TUNNELS.dashM.on + TUNNELS.dashM.off), 1);
  });

  it("hangs a curtain from the surface down to the tunnel, fading towards the surface", () => {
    const quads = curtainQuads([p3(0, 0, -0.2), p3(30, 0, -12), p3(60, 0, -12), p3(90, 0, 0)]);
    const n = TUNNELS.curtainBands.length;
    expect(quads).toHaveLength(3 * n);
    // First segment, bottom band: from the tunnel (z -0.2 and -12) up a third of the way.
    expect(quads[0]!.polygon.map((p) => p[2])).toEqual([-0.2, -12, expect.closeTo(-8, 9), expect.closeTo(-0.2 * (2 / 3), 9)]);
    // Top band ends at the surface.
    expect(quads[n - 1]!.polygon.slice(2).map((p) => p[2])).toEqual([0, 0].map(() => expect.closeTo(0, 9)));
    expect(quads.slice(0, n).map((q) => q.fade)).toEqual([...TUNNELS.curtainBands]);
    expect([...TUNNELS.curtainBands]).toEqual([...TUNNELS.curtainBands].sort((a, b) => b - a)); // fading upwards
    // Each quad is vertical: the same two lon/lat columns top and bottom.
    for (const q of quads) {
      expect(q.polygon[0]!.slice(0, 2)).toEqual(q.polygon[3]!.slice(0, 2));
      expect(q.polygon[1]!.slice(0, 2)).toEqual(q.polygon[2]!.slice(0, 2));
    }
    // A segment shallower than curtainMinDepthM at both ends gets none.
    expect(curtainQuads([p3(0, 0, -0.2), p3(30, 0, -0.3)])).toEqual([]);
  });

  it("marks the tunnel's line on the surface with a dotted trace at z = 0", () => {
    const { trace } = toRoadGeometry([feature(tunnelWay, tunnelProps)]).tunnel;
    expect(trace.length).toBeGreaterThan(4);
    expect(trace.every((d) => d.path.every((p) => p[2] === 0 && Math.abs(toM(p)[1]) < 1e-6))).toBe(true);
  });

  it("frames each portal where the tunnel meets street level, not where a way is clipped underground", () => {
    const { portals } = toRoadGeometry([feature(tunnelWay, tunnelProps)]).tunnel;
    expect(portals).toHaveLength(2);
    for (const p of portals) {
      expect(p.path.map((q) => q[2])).toEqual([0, TUNNELS.portalHeightM, TUNNELS.portalHeightM, 0]);
      // The posts stand at the two road edges (motorway width apart), across the road.
      const [l, r] = [toM(p.path[0]!), toM(p.path[3]!)];
      expect(Math.hypot(r[0] - l[0], r[1] - l[1])).toBeCloseTo(ROAD_WIDTH_M.motorway!, 3);
    }
    // Clipped deep at the extract edge (ends at -30 m): only the street-level end gets a portal.
    const clipped = [0, -4, -12, -30, -30].map((z, i) => p3(i * 30, 0, z));
    expect(toRoadGeometry([feature(clipped, tunnelProps)]).tunnel.portals).toHaveLength(1);
  });

  it("draws a below-ground way that is NOT a tunnel (an open cutting) as a normal solid ribbon with a rim at ground level", () => {
    const g = toRoadGeometry([feature(tunnelWay, { tunnel: false, bridge: false, layer: 0 })]);
    expect(g.tunnel.ribbons).toEqual([]);
    expect(g.ribbons).toHaveLength(1);
    expect(g.edges).toHaveLength(2);
    expect(g.ribbons[0]!.alpha[0]).toBe(0);
    expect(Math.max(...g.ribbons[0]!.alpha)).toBe(1);
    // The rim: along the edges, at ground level, faded out where the cutting is shallower than fasciaDepthM.
    expect(g.fascia).toHaveLength(2);
    expect(g.fascia.every((f) => f.path.every((p) => p[2] === 0))).toBe(true);
    expect(g.fascia[0]!.alpha[1]).toBe(0); // 1 m deep
    expect(Math.max(...g.fascia[0]!.alpha)).toBe(1);
    expect(g.drops).toEqual([]);
  });

  it("makes X-ray deck, portal and edge layers that ignore depth, write none, and follow the toggle", () => {
    const g = toRoadGeometry([feature(tunnelWay, tunnelProps)]).tunnel;
    const on = tunnelLayers(g, true);
    expect(on.map((l) => l.id)).toEqual(["tunnels-curtain", "tunnels-trace", "tunnels-deck", "tunnels-portal", "tunnels-edge", "tunnels-ceiling"]);
    for (const l of on) {
      expect(l.props.visible).toBe(true);
      expect(l.props.parameters).toMatchObject(XRAY_PARAMETERS);
    }
    expect(XRAY_PARAMETERS).toEqual({ depthWriteEnabled: false, depthCompare: "always" });
    expect(tunnelLayers(g, false).every((l) => l.props.visible === false)).toBe(true);
  });

  it("outlines the tube: a fainter copy of the dashed edges ceilingM above the floor, never above ground", () => {
    const g = toRoadGeometry([feature(tunnelWay, tunnelProps)]).tunnel;
    type PathProps = { getPath: (d: (typeof g.dashes)[0]) => Position3[]; getColor: (d: (typeof g.dashes)[0]) => number[][] };
    const layers = tunnelLayers(g, true);
    const get = (id: string) => layers.find((l) => l.id === id)!.props as unknown as PathProps;
    const [edge, ceiling] = [get("tunnels-edge"), get("tunnels-ceiling")];
    const { ceilingM, ceilingAlpha } = palette.roadTunnel;
    expect(g.dashes.length).toBeGreaterThan(0);
    for (const d of g.dashes) {
      const floor = edge.getPath(d);
      ceiling.getPath(d).forEach((p, i) => {
        expect(p.slice(0, 2)).toEqual(floor[i]!.slice(0, 2));
        expect(p[2]).toBe(Math.min(0, floor[i]![2] + ceilingM));
      });
      ceiling.getColor(d).forEach((c, i) => expect(c[3]).toBe(Math.round(palette.roadTunnel.edge[3] * d.alpha[i]! * ceilingAlpha)));
    }
  });

  it("uses dimmer, unsaturated colours for tunnels than for elevated ribbons", () => {
    const t = palette.roadTunnel;
    expect(t.edge[3]).toBeLessThan(palette.roadEdge[3]);
    expect(t.deck[3]).toBeLessThan(palette.roadDeck[3]);
    // Cooler than the elevated ribbons: bluer relative to red.
    expect(t.edge[2] - t.edge[0]).toBeGreaterThan(palette.roadEdge[2] - palette.roadEdge[0]);
    for (const c of [t.deck, t.edge, t.portal, t.curtain, t.trace]) {
      expect(chroma(c)).toBeLessThan(0.25);
      expect(c[3]).toBeGreaterThan(0);
    }
  });
});

describe("height source (?heights=source, dev only)", () => {
  it("counts only z_source measured as measured; estimated, ground and missing are estimated", () => {
    expect(heightConfidence("measured")).toBe("measured");
    expect(heightConfidence("estimated")).toBe("estimated");
    expect(heightConfidence("ground")).toBe("estimated");
    expect(heightConfidence(undefined)).toBe("estimated");
  });

  it("maps measured to the normal ghost colours and estimated to a dimmer, unsaturated, dashed variant", () => {
    const m = ribbonColors("measured");
    expect(m.deck).toEqual(palette.roadDeck);
    expect(m.edge).toEqual(palette.roadEdge);
    expect(m.edgeHidden).toEqual([...palette.roadEdge.slice(0, 3), palette.roadEdgeHiddenAlpha]);
    expect(m.fascia).toEqual(palette.roadFascia);
    expect(m.dashed).toBe(false);
    const e = ribbonColors("estimated");
    expect(e.dashed).toBe(true);
    const lum = (c: readonly number[]) => (c[0]! + c[1]! + c[2]!) / 3;
    for (const k of ["deck", "edge", "edgeHidden", "fascia"] as const) {
      expect(e[k][3]).toBeLessThan(m[k][3]); // dimmer
      expect(lum(e[k])).toBeLessThanOrEqual(lum(m[k]));
      expect(chroma(e[k])).toBeLessThan(0.25); // ghost palette, never saturated
      expect(e[k][3]).toBeGreaterThan(0); // still visible
    }
  });

  it("is on only for ?heights=source in dev builds", () => {
    expect(heightsViewFromQuery("?heights=source", true)).toBe("source");
    expect(heightsViewFromQuery("?other=1&heights=source", true)).toBe("source");
    expect(heightsViewFromQuery("?heights=source", false)).toBe("normal");
    expect(heightsViewFromQuery("?heights=other", true)).toBe("normal");
    expect(heightsViewFromQuery("", true)).toBe("normal");
  });

  it("carries the confidence onto ribbons, edges and fascia", () => {
    const measured = toRoadGeometry([feature(way, { z_source: "measured" })]);
    expect(measured.ribbons[0]!.confidence).toBe("measured");
    expect(measured.edges.every((e) => e.confidence === "measured")).toBe(true);
    expect(measured.fascia.every((e) => e.confidence === "measured")).toBe(true);
    expect(toRoadGeometry([feature(way)]).ribbons[0]!.confidence).toBe("estimated");
    expect(toRoadGeometry([feature(way, { z_source: "ground" })]).ribbons[0]!.confidence).toBe("estimated");
  });

  it("cuts a line into dashes, interpolating z and alpha at the cuts", () => {
    const line = { path: [p3(0, 0, 0), p3(20, 0, 20)], alpha: [0, 1], confidence: "estimated" as const };
    const dashes = dashLine(line, 6, 5);
    expect(dashes).toHaveLength(2); // 0–6 and 11–17; the next dash would start at 22, past the end
    expect(dashes[0]!.path.map((p) => toM(p)[0])).toEqual([0, expect.closeTo(6, 6)]);
    expect(dashes[1]!.path.map((p) => toM(p)[0])).toEqual([expect.closeTo(11, 6), expect.closeTo(17, 6)]);
    expect(dashes[1]!.path.map((p) => p[2])).toEqual([expect.closeTo(11, 6), expect.closeTo(17, 6)]);
    expect(dashes[1]!.alpha).toEqual([expect.closeTo(11 / 20, 6), expect.closeTo(17 / 20, 6)]);
    expect(dashes.every((d) => d.confidence === "estimated" && d.path.length === d.alpha.length)).toBe(true);
    // Dashes run across vertices.
    const bent = dashLine({ path: [p3(0, 0, 5), p3(4, 0, 5), p3(4, 4, 5)], alpha: [1, 1, 1], confidence: "estimated" }, 6, 5);
    expect(bent).toHaveLength(1);
    expect(bent[0]!.path).toHaveLength(3);
  });

  it("draws the same layers either way; the source view dashes only estimated lines", () => {
    const g = toRoadGeometry([feature(way), feature(way.map((p) => [p[0], p[1] + 0.001, p[2]] as Position3), { way_id: 2, z_source: "measured" })]);
    const normal = roadsLayers(g);
    const source = roadsLayers(g, "source");
    expect(source.map((l) => l.id)).toEqual(normal.map((l) => l.id));
    const edgeData = (ls: typeof normal) => ls.find((l) => l.id === "roads-edge")!.props.data as { confidence: string }[];
    expect(edgeData(normal)).toHaveLength(4);
    expect(edgeData(source).filter((e) => e.confidence === "measured")).toHaveLength(2);
    expect(edgeData(source).filter((e) => e.confidence === "estimated").length).toBeGreaterThan(2);
  });
});

const REAL_PATH = resolve(import.meta.dirname, "../../../data/processed/roads_3d.geojson");

describe.skipIf(!existsSync(REAL_PATH))("ribbons from the real roads_3d.geojson", () => {
  const data = JSON.parse(readFileSync(REAL_PATH, "utf8")) as RoadsCollection;
  const g = toRoadGeometry(data.features);

  it("draws the Kings Way / West Gate and Footscray Rd structures and no tunnels", () => {
    const names = new Set(g.ribbons.map((r) => r.name));
    for (const n of ["Kings Way", "West Gate Freeway", "West Gate Tunnel", "Footscray Road Offramp"]) expect(names).toContain(n);
    const tunnelIds = new Set(data.features.filter((f) => f.properties.tunnel).map((f) => f.properties.way_id));
    expect(g.ribbons.some((r) => tunnelIds.has(r.wayId))).toBe(false);
    // Data-shape check only (heights are recalibrated from time to time): decks are clearly off the ground, finite.
    const zs = g.ribbons.flatMap((r) => r.path.map((p) => p[2]));
    expect(zs.every(Number.isFinite)).toBe(true);
    expect(Math.max(...zs)).toBeGreaterThan(5);
  });

  it("draws the Burnley and Domain tunnels and the City Road underpass X-ray, the CityLink cutting solid", () => {
    const tunnelWays = new Set(g.tunnel.ribbons.map((r) => r.wayId));
    const solidWays = new Set(g.ribbons.map((r) => r.wayId));
    // Burnley (eastbound) and Domain (westbound) under the Yarra; City Road underpass.
    for (const w of [8066124, 8066126, 71189431, 71189432]) expect(tunnelWays).toContain(w);
    // The open-air CityLink cutting under Power St (tests/contracts CUTTING_WAYS): solid ribbons, never X-ray.
    for (const w of [5234624, 5234646, 5234953, 5234957]) {
      expect(solidWays).toContain(w);
      expect(tunnelWays).not.toContain(w);
    }
    // Every X-ray ribbon is a tunnel way; nothing above ground is X-ray.
    const tunnelIds = new Set(data.features.filter((f) => f.properties.tunnel).map((f) => f.properties.way_id));
    expect([...tunnelWays].every((w) => tunnelIds.has(w))).toBe(true);
    const tz = g.tunnel.ribbons.flatMap((r) => r.path.map((p) => p[2]));
    expect(Math.max(...tz)).toBeLessThanOrEqual(RIBBONS.minZ);
    expect(Math.min(...tz)).toBeLessThan(-25); // under the river bed
    // The Burnley tunnel reaches under the Yarra, east of the simulated road extract.
    const burnley = g.tunnel.ribbons.filter((r) => r.wayId === 8066124).flatMap((r) => r.path.map((p) => p[0]));
    expect(Math.max(...burnley)).toBeGreaterThan(144.98);
  });

  it("has both measured and estimated elevated ribbons (for ?heights=source)", () => {
    const kinds = new Set(g.ribbons.map((r) => r.confidence));
    expect(kinds).toEqual(new Set(["measured", "estimated"]));
  });

  it("stays a modest amount of geometry", () => {
    const vertices = g.ribbons.reduce((s, r) => s + r.path.length, 0);
    expect(vertices).toBeLessThan(20_000);
    expect(g.drops.length).toBeLessThan(10_000);
    expect(g.tunnel.ribbons.reduce((s, r) => s + r.path.length, 0)).toBeLessThan(5_000);
    expect(g.tunnel.dashes.length).toBeLessThan(5_000);
    expect(g.tunnel.portals.length).toBeLessThan(200);
    expect(g.tunnel.curtain.length).toBeLessThan(15_000);
  });
});
