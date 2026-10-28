// MapLibre renders at most MAX_UPDATES_PER_S per second while the camera is still.
//
// Playing traffic asks for a repaint 60 times a second (playback.ts caps it), but on a 120 Hz display MapLibre still
// rendered 120 times a second: every render re-runs symbol placement, whose 300 ms label fade marks the map dirty
// again, so MapLibre schedules another render on the very next display frame, forever while anything keeps drawing.
// Those in-between renders draw the same traffic frame again. This wraps the map's render so that, while the camera
// is NOT moving, a render that comes before its 1/60 s slot (FrameLimiter) is pushed to the next display frame. Camera
// moves (gestures, flyTo presets, jumpTo) are never capped, and a paused, still map is untouched (it isn't rendering).
// `_render` is MapLibre's internal per-frame render (called from triggerRepaint's frame callback; map.redraw() calls
// it with timestamp 0, which is never deferred). If a MapLibre update removes it, this logs and does nothing.
// That reliance on MapLibre internals is why maplibre-gl is pinned exactly (web/package.json); qa-reviewer's
// tests/e2e/review_redraw.spec.ts guards the assumption, so run it before any MapLibre upgrade.

import type { Map as MapLibreMap } from "maplibre-gl";
import { FrameLimiter } from "./frameHost";

const MARK = "__twinRenderCap";

/** Cap the map's still-camera render rate (see the header comment). Idempotent. */
export function capStillRenderRate(map: MapLibreMap): boolean {
  const m = map as unknown as { _render?: (now: number) => unknown; [MARK]?: true };
  if (m[MARK]) return true;
  const render = m._render;
  if (typeof render !== "function") {
    console.warn("[renderCap] MapLibre map has no _render: still-camera renders are not capped");
    return false;
  }
  const limiter = new FrameLimiter();
  // Any camera change since the last render counts as moving, including a jumpTo (isMoving() is false for those).
  let moved = false;
  map.on("move", () => {
    moved = true;
  });
  m._render = (now: number) => {
    // map.redraw() renders synchronously with timestamp 0: never deferred.
    if (now > 0) {
      if (moved || map.isMoving()) limiter.note(now);
      else if (!limiter.accept(now)) {
        map.triggerRepaint(); // the frame request was cleared before this call, so this books the next frame
        return map;
      }
    }
    moved = false;
    return render.call(map, now);
  };
  m[MARK] = true;
  return true;
}
