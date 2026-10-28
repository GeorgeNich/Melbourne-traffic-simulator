// Far plane below ground, so X-ray tunnels (down to -30 m under the Yarra) and tunnel cars are never clipped.
//
// MapLibre sizes its far clipping plane to the furthest visible point on the ground (z = 0), plus 1%. deck.gl draws
// in MapLibre's context with the same near/far planes (interleaved MapboxOverlay reads them from the custom-layer
// render parameters), so at pitch 0 and high zoom anything more than ~1% of the camera distance below ground (a few
// metres at zoom 17-18) falls behind the far plane and is clipped.
//
// MapLibre already handles ground below sea level: its near/far calculation (MercatorTransform
// _calculateNearFarZIfNeeded) takes min(elevation, minElevationForCurrentTile, ...) and, when that is negative, sizes
// the far plane for a lower plane at that depth (the "under the sea maps" case). Without terrain it resets
// minElevationForCurrentTile to 0 every frame, so we wrap that getter on the map's transform to never report more
// than -depthM. Every matrix MapLibre computes from then on (camera moves, flyTo, resize, the transform clones used
// by camera animations, which copy the value) uses a far plane deep enough for the tunnels, at any pitch. Nothing
// else reads the value. The cost is a slightly larger far/near ratio (depth precision), negligible at these depths.

import type { Map as MapLibreMap } from "maplibre-gl";
import { PLAUSIBLE_Z_M } from "../data/roadsContract";

/** How far below ground the far plane must reach (m): the deepest plausible road (-40 m) plus margin. */
export const FAR_PLANE_DEPTH_M = -PLAUSIBLE_Z_M.min + 5;

const KEY = "minElevationForCurrentTile";

function findGetter(obj: object, key: string): (() => number) | undefined {
  for (let o: object | null = Object.getPrototypeOf(obj) as object | null; o; o = Object.getPrototypeOf(o) as object | null) {
    const d = Object.getOwnPropertyDescriptor(o, key);
    if (d?.get) return d.get as () => number;
  }
  return undefined;
}

/**
 * Make `transform` report its minimum elevation as at most -depthM (an own getter shadowing the prototype's).
 * Returns false if the transform has no such getter (a MapLibre change): the caller logs it and the app runs as before.
 */
export function lowerMinElevation(transform: object, depthM: number = FAR_PLANE_DEPTH_M): boolean {
  const own = Object.getOwnPropertyDescriptor(transform, KEY);
  if (own?.get && (own.get as { twinFarPlane?: boolean }).twinFarPlane) return true; // already done
  const base = findGetter(transform, KEY);
  if (!base) return false;
  const get = function (this: object) {
    return Math.min(base.call(this), -depthM);
  };
  (get as { twinFarPlane?: boolean }).twinFarPlane = true;
  Object.defineProperty(transform, KEY, { configurable: true, get });
  return true;
}

/** Extend the map's far plane below ground (see the header comment) and recompute the matrices now. */
export function extendFarPlaneBelowGround(map: MapLibreMap, depthM: number = FAR_PLANE_DEPTH_M): void {
  const apply = () => {
    // `transform` is not in MapLibre's public typings; it is the live camera transform (deck.gl reads it too).
    const transform = (map as unknown as { transform: object }).transform;
    if (!lowerMinElevation(transform, depthM)) {
      console.warn("[farPlane] MapLibre transform has no minElevationForCurrentTile getter: tunnels may be clipped at pitch 0");
      return;
    }
    map.resize(); // recomputes the projection matrices with the new far plane
  };
  apply();
  // A projection change swaps in a new transform object; patch that one too.
  map.on("projectiontransition", apply);
}
