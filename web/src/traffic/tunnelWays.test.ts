import { describe, expect, it } from "vitest";
import type { Position3, RoadFeature } from "../data/roadsContract";
import { tunnelLookup } from "./tunnelWays";

// Hand-written test roads in local metres around the CBD.
const LAT0 = -37.825;
const LON0 = 144.97;
const kx = 111_320 * Math.cos((LAT0 * Math.PI) / 180);
const p = (x: number, y: number, z: number): Position3 => [LON0 + x / kx, LAT0 + y / 110_574, z];
const way = (id: number, tunnel: boolean, coords: Position3[], extra: Partial<RoadFeature["properties"]> = {}): RoadFeature => ({
  type: "Feature",
  properties: { way_id: id, highway: "motorway", layer: tunnel ? -1 : 0, bridge: false, tunnel, name: null, z_source: "estimated", ...extra },
  geometry: { type: "LineString", coordinates: coords },
});

describe("tunnelLookup (nearest below-ground roads_3d way)", () => {
  // An open cutting running east at -6 m, then a tunnel at -20 m carrying on east; a parallel surface road 15 m north;
  // a building passage (tunnel-tagged, at street level) 30 m south.
  const cutting = way(1, false, [p(0, 0, 0), p(50, 0, -6), p(200, 0, -6)]);
  const tunnel = way(2, true, [p(200, 0, -6), p(260, 0, -20), p(500, 0, -20)]);
  const surface = way(3, false, [p(0, 15, 0), p(500, 15, 0)]);
  const passage = way(4, true, [p(0, -30, 0), p(500, -30, 0)], { highway: "service", layer: 0 });
  const at = tunnelLookup([cutting, tunnel, surface, passage]);
  const q = (x: number, y: number) => at(...(p(x, y, 0).slice(0, 2) as [number, number]));

  it("gives the open cutting's flag in the cutting and the tunnel's flag in the tunnel", () => {
    expect(q(120, 0)).toBe(false);
    expect(q(120, 2)).toBe(false); // a lane a couple of metres off the centreline
    expect(q(350, -2)).toBe(true);
    // Either side of the portal, the nearer way wins.
    expect(q(190, 0)).toBe(false);
    expect(q(215, 0)).toBe(true);
  });

  it("ignores roads at or above ground (a surface road over a car, a building passage) and says null with none near", () => {
    expect(q(120, 12)).toBe(false); // nearest BELOW-GROUND way is the cutting, not the surface road 3 m away
    expect(q(350, -25)).toBe(true); // the tunnel, not the street-level building passage 5 m away
    expect(q(2000, 0)).toBeNull();
    expect(tunnelLookup([surface, passage])(LON0, LAT0)).toBeNull();
  });
});
