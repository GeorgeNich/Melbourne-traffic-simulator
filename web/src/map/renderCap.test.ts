import { describe, expect, it, vi } from "vitest";
import type { Map as MapLibreMap } from "maplibre-gl";
import { MAX_UPDATES_PER_S } from "./frameHost";
import { capStillRenderRate } from "./renderCap";

/** A stand-in for MapLibre's map: triggerRepaint books one frame; frame(now) runs it through _render. */
function fakeMap() {
  let booked = false;
  const onMove: (() => void)[] = [];
  const map = {
    moving: false,
    on(type: string, cb: () => void) {
      if (type === "move") onMove.push(cb);
    },
    jumpTo() {
      for (const cb of onMove) cb();
      booked = true;
    },
    rendered: [] as number[],
    isMoving() {
      return this.moving;
    },
    triggerRepaint() {
      booked = true;
    },
    _render(now: number) {
      this.rendered.push(now);
      booked = true; // MapLibre keeps asking while labels fade / traffic plays
      return this;
    },
    frame(now: number) {
      if (!booked) return;
      booked = false;
      (this as unknown as { _render: (n: number) => unknown })._render(now);
    },
  };
  return map;
}

describe("still-camera render cap", () => {
  it(`a 120 Hz display renders at most ${MAX_UPDATES_PER_S} times a second while still, every frame while moving`, () => {
    const map = fakeMap();
    expect(capStillRenderRate(map as unknown as MapLibreMap)).toBe(true);
    map.triggerRepaint();
    for (let i = 1; i <= 120; i++) map.frame((i * 1000) / 120);
    expect(map.rendered.length).toBeLessThanOrEqual(MAX_UPDATES_PER_S);
    expect(map.rendered.length).toBeGreaterThanOrEqual(MAX_UPDATES_PER_S - 1);
    map.rendered.length = 0;
    map.moving = true;
    for (let i = 121; i <= 240; i++) map.frame((i * 1000) / 120);
    expect(map.rendered.length).toBe(120);
    // A jumpTo every frame (isMoving() stays false) is a moving camera too.
    map.rendered.length = 0;
    map.moving = false;
    for (let i = 241; i <= 360; i++) {
      map.jumpTo();
      map.frame((i * 1000) / 120);
    }
    expect(map.rendered.length).toBe(120);
  });

  it("a 60 Hz display keeps every frame", () => {
    const map = fakeMap();
    capStillRenderRate(map as unknown as MapLibreMap);
    map.triggerRepaint();
    for (let i = 1; i <= 60; i++) map.frame((i * 1000) / 60 + (i % 2 ? 1.5 : -1.5));
    expect(map.rendered.length).toBe(60);
  });

  it("is idempotent, and does nothing (with a warning) without MapLibre's _render", () => {
    const map = fakeMap();
    capStillRenderRate(map as unknown as MapLibreMap);
    const wrapped = (map as unknown as { _render: unknown })._render;
    capStillRenderRate(map as unknown as MapLibreMap);
    expect((map as unknown as { _render: unknown })._render).toBe(wrapped);
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(capStillRenderRate({ isMoving: () => false, on: () => {} } as unknown as MapLibreMap)).toBe(false);
    expect(warn).toHaveBeenCalledOnce();
    warn.mockRestore();
  });
});
