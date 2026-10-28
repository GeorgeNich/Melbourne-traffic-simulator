import { describe, expect, it } from "vitest";
import { chroma, GLOW, palette, TRAIL_SECONDS, XRAY } from "../theme/palette";
import { prepareTrips } from "./prepare";
import {
  type ExtraPass,
  headCoreColor,
  SURFACE_PARAMETERS,
  THROUGH_PARAMETERS,
  trafficLayerId,
  type TrafficPass,
  trafficRenderer,
  TUNNEL_PARAMETERS,
} from "./trafficLayers";

const passes = (tunnel: boolean, list: readonly (TrafficPass | ExtraPass)[], b = 0) => list.map((p) => trafficLayerId(tunnel, p, b));
/** Draw order per bucket: surface = see-through passes, then the normal three; tunnel = tethers, surface rings, then three. */
const SURFACE = ["thru-halo", "thru-core", "thru-heads", "halo", "core", "heads"] as const;
const TUNNEL = ["tethers", "mark", "halo", "core", "heads"] as const;

const prepared = prepareTrips([
  { id: "a", type: "passenger", path: [[144.96, -37.818, 0], [144.961, -37.818, 0]], timestamps: [0, 10] },
  { id: "b", type: "passenger", path: [[144.96, -37.82, 0], [144.961, -37.82, -8], [144.962, -37.82, 0]], timestamps: [0, 5, 10] },
]);

type Props = { getColor?: number[]; color?: number[]; parameters?: unknown; ring?: number; getWidth?: number; trailLength?: number; haloAlpha?: number };

describe("trafficRenderer", () => {
  const render = trafficRenderer(prepared);
  const byId = (layers = render(1, true)) => Object.fromEntries(layers.map((l) => [l.id, l.props as unknown as Props]));

  it("draws see-through, halo, core and heads for surface traffic; tethers, surface rings and the three for tunnels", () => {
    expect(render(1, true).map((l) => l.id)).toEqual([...passes(false, SURFACE), ...passes(true, TUNNEL)]);
  });

  it("changes only currentTime between frames: same data, accessors and parameters (no attribute rebuilds)", () => {
    const a = render(1, true);
    const b = render(2, true);
    a.forEach((la, i) => {
      const lb = b[i]!;
      const pa = la.props as unknown as Record<string, unknown>;
      const pb = lb.props as unknown as Record<string, unknown>;
      expect(pa.currentTime).toBe(1);
      expect(pb.currentTime).toBe(2);
      const changed = Object.keys(pa).filter((k) => pa[k] !== pb[k]);
      expect(changed, la.id).toEqual(["currentTime"]);
    });
  });

  it("hides tunnel traffic (only), with its tethers and surface rings, with Show tunnels off", () => {
    const vis = Object.fromEntries(render(1, false).map((l) => [l.id, l.props.visible]));
    for (const id of passes(false, SURFACE)) expect(vis[id], id).toBe(true);
    for (const id of passes(true, TUNNEL)) expect(vis[id], id).toBe(false);
  });

  it("uses the palette trail length (20 s)", () => {
    expect(TRAIL_SECONDS).toBe(20);
    expect(byId()["traffic-core-b0"]!.trailLength).toBe(20);
    expect(byId()["traffic-tunnel-core-b0"]!.trailLength).toBe(20);
  });

  it("draws surface cars depth-tested, again dimmer behind buildings (glow through), and tunnel cars X-ray; all additive", () => {
    const l = byId();
    expect(l["traffic-core-b0"]!.parameters).toBe(SURFACE_PARAMETERS);
    expect(l["traffic-thru-core-b0"]!.parameters).toBe(THROUGH_PARAMETERS);
    expect(l["traffic-tunnel-core-b0"]!.parameters).toBe(TUNNEL_PARAMETERS);
    expect(l["traffic-tunnel-tethers-b0"]!.parameters).toBe(TUNNEL_PARAMETERS);
    expect(SURFACE_PARAMETERS).toMatchObject({ depthCompare: "less-equal", depthWriteEnabled: false, blendColorDstFactor: "one" });
    expect(THROUGH_PARAMETERS).toMatchObject({ depthCompare: "greater", depthWriteEnabled: false, blendColorDstFactor: "one" });
    expect(TUNNEL_PARAMETERS).toMatchObject({ depthCompare: "always", depthWriteEnabled: false, blendColorDstFactor: "one" });
    // The see-through passes are the normal ones at GLOW.throughBuildings of their brightness.
    for (const p of ["halo", "core"]) expect(l[`traffic-thru-${p}-b0`]!.getColor![3]).toBe(Math.round(l[`traffic-${p}-b0`]!.getColor![3]! * GLOW.throughBuildings));
    expect(l["traffic-thru-heads-b0"]!.color![3]).toBe(Math.round(255 * GLOW.throughBuildings));
  });

  it("colours every car with the palette's flowing blue; tunnel cars full strength, wider, with ring heads", () => {
    const l = byId();
    for (const id of ["traffic-core-b0", "traffic-tunnel-core-b0", "traffic-tunnel-tethers-b0"]) {
      const c = l[id]!.getColor ?? l[id]!.color!;
      expect(c.slice(0, 3), id).toEqual([...palette.traffic.flowing]);
    }
    expect(l["traffic-heads-b0"]!.color!.slice(0, 3)).toEqual([...palette.traffic.flowing]);
    expect(l["traffic-tunnel-core-b0"]!.getColor![3]).toBe(l["traffic-core-b0"]!.getColor![3]);
    expect(l["traffic-tunnel-core-b0"]!.getWidth).toBeCloseTo(l["traffic-core-b0"]!.getWidth! * XRAY.coreWidth);
    expect(l["traffic-tunnel-heads-b0"]!.ring).toBe(1);
    expect(l["traffic-tunnel-heads-b0"]!.color![3]).toBe(255);
    expect(l["traffic-heads-b0"]!.ring).toBe(0);
    expect(l["traffic-thru-heads-b0"]!.ring).toBe(0);
    expect(l["traffic-tunnel-tethers-b0"]!.color![3]).toBe(XRAY.tetherAlpha);
  });

  it("puts each tunnel car's surface ring at z = 0, straight above the car", () => {
    const mark = render(1, true).find((l) => l.id === "traffic-tunnel-mark-b0")!.props as unknown as {
      ring: number;
      haloAlpha: number;
      data: { attributes: { getFrom: { value: Float64Array }; getTo: { value: Float64Array } } };
    };
    const heads = render(1, true).find((l) => l.id === "traffic-tunnel-heads-b0")!.props as unknown as typeof mark;
    expect(mark.ring).toBe(1);
    expect(mark.haloAlpha).toBe(0);
    const from = mark.data.attributes.getFrom.value;
    const src = heads.data.attributes.getFrom.value;
    expect(from.length).toBe(src.length);
    for (let i = 0; i < from.length; i++) expect(from[i]).toBe(i % 3 === 2 ? 0 : src[i]);
    expect(src.some((z, i) => i % 3 === 2 && z < 0)).toBe(true);
  });

  it("skips an empty group", () => {
    const surfaceOnly = prepareTrips([{ id: "a", type: "passenger", path: [[144.96, -37.818, 0], [144.961, -37.818, 0]], timestamps: [0, 10] }]);
    expect(trafficRenderer(surfaceOnly)(1, true).map((l) => l.id)).toEqual(passes(false, SURFACE));
  });
});

describe("headCoreColor", () => {
  it("lifts the traffic colour toward white but keeps its hue (blue still dominates)", () => {
    const c = headCoreColor(palette.traffic.flowing);
    expect(c[2]).toBe(255);
    expect(c[2]).toBeGreaterThan(c[0]);
    expect(chroma(c)).toBeLessThan(chroma(palette.traffic.flowing));
    expect(chroma(c)).toBeGreaterThan(0.2);
  });
});
