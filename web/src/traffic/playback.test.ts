import { describe, expect, it, vi } from "vitest";
import { FakeFrameHost } from "../map/fakeFrameHost";
import {
  advance,
  clampTime,
  formatClock,
  formatLocalClock,
  formatLocalDate,
  MAX_FRAME_S,
  MAX_UPDATES_PER_S,
  PlaybackClock,
  playbackRange,
  type PlaybackState,
  SPEEDS,
  UI_INTERVAL_MS,
  wrap,
} from "./playback";

const state = (over: Partial<PlaybackState> = {}): PlaybackState => ({ time: 0, playing: true, speed: 1, start: 0, duration: 300, ...over });
const trips = (...spans: [number, number][]) => spans.map(([a, b]) => ({ timestamps: [a, (a + b) / 2, b] }));

describe("playback range (trips that don't start at 0)", () => {
  it("baseline: 0..899.5 with duration 900 replays [0, 900]", () => {
    expect(playbackRange(trips([0, 120], [300, 899.5]), 900)).toEqual({ start: 0, end: 900 });
  });
  it("am_peak: 3600..5400 replays [3600, 5400], whatever the run's duration", () => {
    expect(playbackRange(trips([3600, 3700], [4000, 5400]), 5400)).toEqual({ start: 3600, end: 5400 });
    expect(playbackRange(trips([3600, 3700], [4000, 5400]), 10800)).toEqual({ start: 3600, end: 5400 });
  });
  it("rounds out to whole minutes and stays within [0, duration]", () => {
    expect(playbackRange(trips([3612.5, 3700], [4000, 5391]), 10800)).toEqual({ start: 3600, end: 5400 });
    expect(playbackRange(trips([20, 100]), 300)).toEqual({ start: 0, end: 120 });
    expect(playbackRange(trips([250, 299.9]), 280)).toEqual({ start: 240, end: 280 });
  });
  it("no trips: [0, duration]", () => {
    expect(playbackRange([], 900)).toEqual({ start: 0, end: 900 });
  });
  it("advances, loops and clamps within [start, end]", () => {
    const s = state({ start: 3600, duration: 5400, time: 5399.9, speed: 4 });
    expect(advance(s, 0.05).time).toBeCloseTo(3600.1, 6);
    expect(wrap(5400, 5400, 3600)).toBe(3600);
    expect(wrap(3500, 5400, 3600)).toBe(5300);
    expect(clampTime(100, 5400, 3600)).toBe(3600);
    expect(clampTime(9999, 5400, 3600)).toBe(5400);
    expect(clampTime(NaN, 5400, 3600)).toBe(3600);
  });
  it("setRange (scenario switch): a time outside the new range moves to its start; seeks clamp to it", () => {
    const clock = new PlaybackClock(900);
    clock.seek(480);
    clock.setRange(3600, 5400);
    expect(clock.time).toBe(3600);
    expect(clock.getSnapshot()).toMatchObject({ start: 3600, duration: 5400 });
    clock.seek(100);
    expect(clock.time).toBe(3600);
    clock.seek(4000);
    clock.setRange(3600, 5400);
    expect(clock.time).toBe(4000);
    clock.setRange(0, 900);
    expect(clock.time).toBe(0);
  });
});

describe("local clock (meta.start_local)", () => {
  const START = "2026-10-23T06:30:00+11:00";
  it("shows Melbourne time: sim 0 = 06:30, 3600 s = 07:30, 4920 s = 07:52", () => {
    expect(formatLocalClock(0, START)).toBe("Fri 23 Oct 2026 · 06:30");
    expect(formatLocalClock(3600, START)).toBe("Fri 23 Oct 2026 · 07:30");
    expect(formatLocalClock(4920, START)).toBe("Fri 23 Oct 2026 · 07:52");
    expect(formatLocalClock(4979.9, START)).toBe("Fri 23 Oct 2026 · 07:52"); // seconds dropped, never rounded up
    expect(formatLocalDate(5400, START)).toBe("Fri 23 Oct 2026");
  });
  it("is Melbourne time whatever offset start_local is written in", () => {
    expect(formatLocalClock(3600, "2026-10-22T19:30:00Z")).toBe("Fri 23 Oct 2026 · 07:30");
  });
  it("no or unreadable start_local: null (the mm:ss clock stays)", () => {
    expect(formatLocalClock(3600, undefined)).toBeNull();
    expect(formatLocalClock(3600, "not a date")).toBeNull();
    expect(formatLocalDate(0, undefined)).toBeNull();
  });
});

describe("playback state", () => {
  it("offers 1x / 4x / 10x / 30x", () => {
    expect([...SPEEDS]).toEqual([1, 4, 10, 30]);
  });

  it("advances by real time x speed, and not while paused", () => {
    expect(advance(state(), 0.05).time).toBeCloseTo(0.05);
    expect(advance(state({ speed: 30 }), 0.05).time).toBeCloseTo(1.5);
    expect(advance(state({ playing: false }), 0.05).time).toBe(0);
  });

  it("loops at the end of the scenario", () => {
    expect(advance(state({ time: 299.99, speed: 10 }), 0.05).time).toBeCloseTo(0.49);
    expect(wrap(301, 300)).toBeCloseTo(1);
    expect(wrap(-1, 300)).toBeCloseTo(299);
    expect(wrap(5, 0)).toBe(0);
  });

  it("caps a long frame gap (hidden tab) so playback never jumps", () => {
    expect(advance(state(), 5).time).toBeCloseTo(MAX_FRAME_S);
    expect(advance(state(), -1).time).toBe(0);
  });

  it("clamps scrub positions to the scenario", () => {
    expect(clampTime(-5, 300)).toBe(0);
    expect(clampTime(400, 300)).toBe(300);
    expect(clampTime(Number.NaN, 300)).toBe(0);
  });

  it("formats the sim clock as mm:ss from scenario start", () => {
    expect(formatClock(0)).toBe("00:00");
    expect(formatClock(59.9)).toBe("00:59");
    expect(formatClock(61)).toBe("01:01");
    expect(formatClock(1200)).toBe("20:00");
    expect(formatClock(3725)).toBe("62:05");
  });
});

describe("PlaybackClock", () => {
  it("notifies frame listeners every frame, the UI at most every UI_INTERVAL_MS", () => {
    const clock = new PlaybackClock(300);
    const frames = vi.fn();
    const ui = vi.fn();
    clock.onFrame(frames);
    clock.subscribeUi(ui);
    for (let i = 0; i <= 60; i++) clock.tick(1000 + (i * 1000) / 60); // one second at 60 fps
    expect(frames).toHaveBeenCalledTimes(61); // the first tick (dt 0) also draws
    expect(ui.mock.calls.length).toBeLessThanOrEqual(Math.ceil(1000 / UI_INTERVAL_MS) + 1);
    expect(clock.time).toBeCloseTo(1, 1);
    expect(clock.getSnapshot().time).toBeLessThanOrEqual(clock.time);
  });

  it("pause, play, speed and seek publish at once; seek also redraws", () => {
    const clock = new PlaybackClock(300);
    const frames = vi.fn();
    const ui = vi.fn();
    clock.onFrame(frames);
    clock.subscribeUi(ui);
    clock.pause();
    expect(clock.getSnapshot().playing).toBe(false);
    clock.tick(0);
    clock.tick(500);
    expect(clock.time).toBe(0); // paused
    clock.setSpeed(30);
    expect(clock.getSnapshot().speed).toBe(30);
    clock.seek(120);
    expect(frames).toHaveBeenLastCalledWith(120);
    clock.seek(1e6);
    expect(clock.time).toBe(300);
    clock.toggle();
    expect(clock.getSnapshot().playing).toBe(true);
    expect(ui).toHaveBeenCalledTimes(5);
    expect(() => clock.setSpeed(7 as never)).toThrow(/unsupported speed/);
  });

  it("keeps the snapshot object stable between notifications (useSyncExternalStore)", () => {
    const clock = new PlaybackClock(300);
    const a = clock.getSnapshot();
    expect(clock.getSnapshot()).toBe(a);
    clock.pause();
    expect(clock.getSnapshot()).not.toBe(a);
  });

  it("clamps the time when the duration changes", () => {
    const clock = new PlaybackClock(0);
    expect(clock.time).toBe(0);
    clock.setDuration(300);
    clock.seek(250);
    clock.setDuration(200);
    expect(clock.time).toBe(200);
  });
});

describe("PlaybackClock frame loop (redraw on demand)", () => {
  /** Run `seconds` of browser frames at `hz` from `t0` ms; returns the time after the last frame. */
  const run = (host: FakeFrameHost, hz: number, seconds: number, t0 = 1000, jitterMs = 0) => {
    const n = Math.round(hz * seconds);
    for (let i = 1; i <= n; i++) host.frame(t0 + (i * 1000) / hz + (i % 2 ? jitterMs : -jitterMs));
    return t0 + seconds * 1000;
  };

  it("paused: requests no frames at all, and a seek redraws once without starting the loop", () => {
    const host = new FakeFrameHost();
    const clock = new PlaybackClock(300, { playing: false });
    const frames = vi.fn();
    clock.onFrame(frames);
    clock.start(host);
    expect(host.scheduled).toBe(0);
    expect(clock.running).toBe(false);
    run(host, 60, 2);
    expect(frames).not.toHaveBeenCalled();
    clock.seek(42);
    expect(frames).toHaveBeenCalledTimes(1);
    expect(host.scheduled).toBe(0);
    clock.setSpeed(30);
    expect(host.scheduled).toBe(0);
  });

  it("play starts the loop and pause stops it (the pending frame is cancelled)", () => {
    const host = new FakeFrameHost();
    const clock = new PlaybackClock(300, { playing: false });
    clock.start(host);
    clock.play();
    expect(host.scheduled).toBe(1);
    run(host, 60, 0.5);
    expect(host.scheduled).toBe(1); // one frame at a time
    clock.pause();
    expect(host.scheduled).toBe(0);
    expect(clock.running).toBe(false);
  });

  it("no scenario loaded (duration 0): no frames even while 'playing'", () => {
    const host = new FakeFrameHost();
    const clock = new PlaybackClock(0);
    clock.start(host);
    expect(host.scheduled).toBe(0);
    clock.setDuration(300);
    expect(host.scheduled).toBe(1);
  });

  it(`playing on a 120 Hz display: at most ${MAX_UPDATES_PER_S} updates per second, at real-time speed`, () => {
    const host = new FakeFrameHost();
    const clock = new PlaybackClock(300);
    const frames = vi.fn();
    clock.onFrame(frames);
    clock.start(host);
    run(host, 120, 1);
    expect(frames.mock.calls.length).toBeLessThanOrEqual(MAX_UPDATES_PER_S + 1); // + the first frame (dt 0)
    expect(frames.mock.calls.length).toBeGreaterThanOrEqual(MAX_UPDATES_PER_S - 1);
    expect(clock.time).toBeCloseTo(1, 1);
    // A 144 Hz display: still never more than 60 per second.
    frames.mockClear();
    run(host, 144, 1, 3000);
    expect(frames.mock.calls.length).toBeLessThanOrEqual(MAX_UPDATES_PER_S);
  });

  it("playing on a 60 Hz display: every frame updates, even with timestamp jitter", () => {
    const host = new FakeFrameHost();
    const clock = new PlaybackClock(300);
    const frames = vi.fn();
    clock.onFrame(frames);
    clock.start(host);
    run(host, 60, 1, 1000, 0.4);
    expect(frames).toHaveBeenCalledTimes(60);
  });

  it("hidden: the loop stops and the sim time holds; shown again it continues from the same time", () => {
    const host = new FakeFrameHost();
    const clock = new PlaybackClock(300);
    clock.start(host);
    run(host, 60, 1);
    const t = clock.time;
    expect(t).toBeCloseTo(1, 1);
    host.setHidden(true);
    expect(host.scheduled).toBe(0);
    expect(clock.running).toBe(false);
    run(host, 60, 5, 2000); // a hidden page gets no frames anyway
    expect(clock.time).toBe(t);
    host.setHidden(false);
    expect(host.scheduled).toBe(1);
    host.frame(60_000); // first frame after a long hidden spell: dt 0, no jump
    expect(clock.time).toBe(t);
    run(host, 60, 1, 60_000);
    expect(clock.time).toBeCloseTo(t + 1, 1);
  });

  it("started while hidden: no frames until shown", () => {
    const host = new FakeFrameHost();
    host.hidden = true;
    const clock = new PlaybackClock(300);
    clock.start(host);
    expect(host.scheduled).toBe(0);
    host.setHidden(false);
    expect(host.scheduled).toBe(1);
  });

  it("stop() detaches: no frames, and visibility changes no longer restart it", () => {
    const host = new FakeFrameHost();
    const clock = new PlaybackClock(300);
    const stop = clock.start(host);
    expect(host.scheduled).toBe(1);
    stop();
    expect(host.scheduled).toBe(0);
    host.setHidden(true);
    host.setHidden(false);
    clock.play();
    expect(host.scheduled).toBe(0);
    // StrictMode start / stop / start: one loop only.
    const stop2 = clock.start(host);
    clock.start(host);
    expect(host.scheduled).toBe(1);
    stop2(); // a stale stop function doesn't stop the newer start
    expect(host.scheduled).toBe(1);
  });

  it("pause and play within a frame resume without a time jump", () => {
    const host = new FakeFrameHost();
    const clock = new PlaybackClock(300);
    clock.start(host);
    run(host, 60, 1);
    const t = clock.time;
    clock.pause();
    clock.play();
    host.frame(10_000);
    expect(clock.time).toBe(t);
  });

  it("a frame listener that pauses and plays inside a frame never books a second frame", () => {
    const host = new FakeFrameHost();
    const clock = new PlaybackClock(300);
    let bounced = false;
    clock.onFrame(() => {
      if (bounced) return;
      bounced = true;
      clock.pause();
      clock.play(); // restarts the loop from inside tick
    });
    clock.start(host);
    host.frame(1000);
    expect(host.scheduled).toBe(1);
    run(host, 60, 0.5, 1000);
    expect(host.scheduled).toBe(1);
  });
});
