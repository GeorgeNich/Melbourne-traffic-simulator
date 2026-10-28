// Which below-ground road a car is on: a tunnel (drawn X-ray, behind "Show tunnels") or an open cutting / underpass
// (drawn solid, like any surface car). The trips contract (CLAUDE.md): a car segment is "in a tunnel" by the `tunnel`
// flag of the NEAREST BELOW-GROUND roads_3d way, not by z alone, so cars in the open CityLink cutting stay solid.
//
// Only below-ground segments of roads_3d are indexed (segment mid-height below 0). `tunnel=building_passage` ways are
// never lowered (roads_3d keeps them at street level), so they never enter the index, as the meta's tunnel_vehicles
// leaves them out too. A uniform lon/lat grid holds each segment in every cell its bounding box touches; a lookup scans
// the 3x3 cells around the point (at least CELL_DEG, ~44 m east-west, in every direction).
import type { RoadFeature } from "../data/roadsContract";

/**
 * Tunnel flag of the nearest below-ground road at [lon, lat]; null when no below-ground road is within reach (then the
 * caller falls back to depth alone: below ground with no road data near, e.g. synthetic test cars).
 */
export type TunnelLookup = (lon: number, lat: number) => boolean | null;

/** Grid cell size, degrees. */
export const CELL_DEG = 0.0005;

const M_PER_DEG_LAT = 110_574;
const mPerDegLon = (lat: number) => 111_320 * Math.cos((lat * Math.PI) / 180);

export function tunnelLookup(features: readonly RoadFeature[]): TunnelLookup {
  // Segments as flat [lon0, lat0, lon1, lat1] plus the way's tunnel flag.
  const ends: number[] = [];
  const tunnel: boolean[] = [];
  const grid = new Map<number, number[]>();
  // Cell key: both indices fit easily (lon / lat cells around Melbourne are ~±400k).
  const key = (x: number, y: number) => x * 1_000_003 + y;
  for (const f of features) {
    const c = f.geometry.coordinates;
    for (let i = 1; i < c.length; i++) {
      const a = c[i - 1]!;
      const b = c[i]!;
      if ((a[2] + b[2]) / 2 >= 0) continue;
      const s = tunnel.length;
      ends.push(a[0], a[1], b[0], b[1]);
      tunnel.push(f.properties.tunnel);
      for (let x = Math.floor(Math.min(a[0], b[0]) / CELL_DEG); x <= Math.floor(Math.max(a[0], b[0]) / CELL_DEG); x++) {
        for (let y = Math.floor(Math.min(a[1], b[1]) / CELL_DEG); y <= Math.floor(Math.max(a[1], b[1]) / CELL_DEG); y++) {
          const k = key(x, y);
          const cell = grid.get(k);
          if (cell) cell.push(s);
          else grid.set(k, [s]);
        }
      }
    }
  }
  return (lon, lat) => {
    const kx = mPerDegLon(lat);
    const gx = Math.floor(lon / CELL_DEG);
    const gy = Math.floor(lat / CELL_DEG);
    let best = Infinity;
    let flag: boolean | null = null;
    for (let x = gx - 1; x <= gx + 1; x++) {
      for (let y = gy - 1; y <= gy + 1; y++) {
        for (const s of grid.get(key(x, y)) ?? []) {
          const ax = (ends[s * 4]! - lon) * kx;
          const ay = (ends[s * 4 + 1]! - lat) * M_PER_DEG_LAT;
          const dx = (ends[s * 4 + 2]! - lon) * kx - ax;
          const dy = (ends[s * 4 + 3]! - lat) * M_PER_DEG_LAT - ay;
          const l2 = dx * dx + dy * dy;
          const t = l2 > 0 ? Math.min(1, Math.max(0, -(ax * dx + ay * dy) / l2)) : 0;
          const d = Math.hypot(ax + t * dx, ay + t * dy);
          if (d < best) {
            best = d;
            flag = tunnel[s]!;
          }
        }
      }
    }
    return flag;
  };
}
