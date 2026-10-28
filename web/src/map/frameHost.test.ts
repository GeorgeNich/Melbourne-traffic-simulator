import { describe, expect, it } from "vitest";
import { FRAME_SLACK_MS, FrameLimiter, MAX_UPDATES_PER_S } from "./frameHost";

/** Updates accepted over `seconds` of frames at `hz`, with timestamps jittered by ±`jitter` ms (alternating). */
function accepted(hz: number, seconds = 1, jitter = 0, limiter = new FrameLimiter(), t0 = 1000) {
  let n = 0;
  for (let i = 0; i < Math.round(hz * seconds); i++) if (limiter.accept(t0 + (i * 1000) / hz + (i % 2 ? jitter : -jitter))) n++;
  return n;
}

describe("FrameLimiter (60 updates a second at most)", () => {
  it("60 Hz: every frame, even with timestamp jitter", () => {
    expect(accepted(60)).toBe(60);
    expect(accepted(60, 10, 2)).toBe(600);
    expect(FRAME_SLACK_MS).toBeGreaterThan(2);
  });

  it("120 Hz: every other frame; 144 Hz and 240 Hz: never more than 60 a second", () => {
    expect(accepted(120)).toBe(MAX_UPDATES_PER_S);
    expect(accepted(120, 10)).toBe(10 * MAX_UPDATES_PER_S);
    expect(accepted(144, 10)).toBeLessThanOrEqual(10 * MAX_UPDATES_PER_S + 1);
    expect(accepted(144, 10)).toBeGreaterThanOrEqual(10 * MAX_UPDATES_PER_S - 10);
    expect(accepted(240, 10)).toBeLessThanOrEqual(10 * MAX_UPDATES_PER_S + 1);
  });

  it("slower displays (30 Hz, 50 Hz) update every frame", () => {
    expect(accepted(30)).toBe(30);
    expect(accepted(50)).toBe(50);
  });

  it("after a stall the grid restarts instead of bursting to catch up", () => {
    const l = new FrameLimiter();
    expect(l.accept(0)).toBe(true);
    expect(l.accept(1000)).toBe(true); // 1 s stall
    expect(l.accept(1008)).toBe(false);
    expect(l.accept(1016.7)).toBe(true);
  });

  it("reset: the next frame is accepted at once", () => {
    const l = new FrameLimiter();
    l.accept(0);
    expect(l.accept(5)).toBe(false);
    l.reset();
    expect(l.accept(5)).toBe(true);
  });
});
