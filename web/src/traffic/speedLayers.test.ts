import { describe, expect, it } from "vitest";
import type { EdgeStatsColumns } from "../data/edgeStatsContract";
import { SPEED, speedEvidence } from "../theme/palette";
import { buildSpeedTable, type SpeedPiece } from "./speeds";
import { speedRenderer } from "./speedLayers";

const stats: EdgeStatsColumns = {
  rows: 2,
  edgeId: ["a", "b"],
  tBin: Float64Array.from([0, 60]),
  meanSpeed: Float64Array.from([9, 1]),
  speedLimit: Float64Array.from([10, 10]),
  speedRatio: Float64Array.from([0.9, 0.1]),
  vehicleCount: Float64Array.from([3, 3]),
};
const pieces: SpeedPiece[] = [
  { edge: 0, tunnel: false, path: [[144.96, -37.82, 0.25], [144.961, -37.82, 0.25]] },
  { edge: 1, tunnel: true, path: [[144.97, -37.82, -20], [144.971, -37.82, -20]] },
];

describe("speedRenderer", () => {
  const { table } = buildSpeedTable(["a", "b"], stats, 180);
  const render = speedRenderer(pieces, table);
  const ids = (ls: { id: string }[]) => ls.map((l) => l.id);

  it("draws glow-through, surface and X-ray tunnel passes, all with traffic ids", () => {
    expect(ids(render(10, true, true))).toEqual([
      "traffic-speed-thru-halo",
      "traffic-speed-thru-core",
      "traffic-speed-halo",
      "traffic-speed-core",
      "traffic-tunnel-speed-halo",
      "traffic-tunnel-speed-core",
    ]);
  });

  it("depth rules follow the cars: less-equal, greater (through buildings), always (X-ray); additive, no depth writes", () => {
    const ls = render(10, true, true);
    const depth = ls.map((l) => (l.props.parameters as { depthCompare: string }).depthCompare);
    expect(depth).toEqual(["greater", "greater", "less-equal", "less-equal", "always", "always"]);
    for (const l of ls) expect(l.props.parameters).toMatchObject({ blend: true, blendColorDstFactor: "one", depthWriteEnabled: false });
    expect(ls[3]!.props.opacity).toBeCloseTo(SPEED.core.alpha / 255);
    expect(ls[1]!.props.opacity).toBeCloseTo((SPEED.core.alpha / 255) * SPEED.throughBuildings);
  });

  it("Road speeds off hides everything; Show tunnels off hides only the tunnel passes", () => {
    expect(render(10, true, false).every((l) => !l.props.visible)).toBe(true);
    expect(render(10, false, true).map((l) => l.props.visible)).toEqual([true, true, true, true, false, false]);
  });

  it("props change only when the 60 s bin changes (redraw on demand: same bin = same colours trigger)", () => {
    const trig = (t: number) => (render(t, true, true)[3]!.props.updateTriggers as { getColor: number }).getColor;
    expect(trig(5)).toBe(trig(59.9));
    expect(trig(60)).not.toBe(trig(59.9));
  });

  it("colours come from the current bin (edge a blue at 0–60 s; edge b red at 60–120 s; nothing else)", () => {
    const color = (t: number, edge: number) => {
      const l = render(t, true, true)[3]!;
      return ((l.props as unknown as { getColor: (d: SpeedPiece) => number[] }).getColor)(pieces[edge]!);
    };
    expect(color(30, 0)).toEqual([56, 140, 255, Math.round(255 * speedEvidence(3) * SPEED.classStrength.flowing)]);
    expect(color(30, 1)[3]).toBe(0);
    expect(color(90, 0)[3]).toBe(0);
    expect(color(90, 1).slice(0, 3)).toEqual([235, 50, 50]);
  });
});
