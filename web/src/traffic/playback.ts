// Playback clock for the traffic replay: simulated seconds on the trips clock, looping over the trips' time range
// [start, duration] (a scenario's trips need not start at 0: am_peak replays 3600..5400).
//
// The clock runs outside React. While playing, every animation frame (at most MAX_UPDATES_PER_S a second) it advances
// and notifies frame listeners (the map overlay, which only swaps `currentTime` on the traffic layers). React (the
// controls) subscribes through a throttled snapshot, so the app never re-renders per frame. Paused or hidden, it
// requests no frames at all (redraw on demand: nothing is drawn unless something changed).

import { browserFrameHost, type FrameHost, FrameLimiter, MAX_UPDATES_PER_S } from "../map/frameHost";

/** Playback speeds offered in the controls (× real time). */
export const SPEEDS = [1, 4, 10, 30] as const;
export type Speed = (typeof SPEEDS)[number];

/** UI snapshots are refreshed at most this often while playing (ms); play/pause/seek/speed notify at once. */
export const UI_INTERVAL_MS = 100;
/** Playback updates at most MAX_UPDATES_PER_S per second (frameHost.ts), also on 120 Hz displays. */
export { MAX_UPDATES_PER_S };
/** A frame gap longer than this (tab hidden, debugger) advances by this much at most (s). */
export const MAX_FRAME_S = 0.1;

export interface PlaybackState {
  time: number;
  playing: boolean;
  speed: Speed;
  /** Start of the playback range (s on the trips clock; 0 for baseline). */
  start: number;
  /** End of the playback range (s on the trips clock; = meta.duration_s for baseline). */
  duration: number;
}

/** Advance by `dtS` real seconds at the current speed, looping at the end back to the start. Paused: unchanged. */
export function advance(s: PlaybackState, dtS: number): PlaybackState {
  if (!s.playing || s.duration <= s.start) return s;
  const dt = Math.min(Math.max(dtS, 0), MAX_FRAME_S) * s.speed;
  return { ...s, time: wrap(s.time + dt, s.duration, s.start) };
}

/** Time into [start, end). */
export function wrap(t: number, end: number, start = 0): number {
  const len = end - start;
  if (!(len > 0)) return start;
  const r = (t - start) % len;
  return start + (r < 0 ? r + len : r);
}

/** Clamp a scrub position into [start, end]. */
export function clampTime(t: number, end: number, start = 0): number {
  if (!Number.isFinite(t)) return start;
  return Math.min(Math.max(t, start), Math.max(end, start));
}

/** Width of a playback-range step (s): ranges start and end on whole minutes, like the 60 s edge-stats bins. */
export const RANGE_STEP_S = 60;

/**
 * The time range to replay: from the first trip's start to the last trip's end, rounded out to whole minutes and kept
 * within [0, durationS] (meta.duration_s). Baseline (0..899.5, duration 900) gives [0, 900]; am_peak (3600..5400) gives
 * [3600, 5400] whatever the run's duration. No trips: [0, durationS].
 */
export function playbackRange(trips: readonly { timestamps: readonly number[] }[], durationS: number): { start: number; end: number } {
  let lo = Infinity;
  let hi = -Infinity;
  for (const t of trips) {
    const a = t.timestamps[0];
    const b = t.timestamps[t.timestamps.length - 1];
    if (a !== undefined && a < lo) lo = a;
    if (b !== undefined && b > hi) hi = b;
  }
  const end = Math.max(durationS, 0);
  if (!(hi > lo)) return { start: 0, end };
  const start = Math.min(Math.max(Math.floor(lo / RANGE_STEP_S) * RANGE_STEP_S, 0), end);
  const last = Math.min(Math.ceil(hi / RANGE_STEP_S) * RANGE_STEP_S, end);
  return last > start ? { start, end: last } : { start: 0, end };
}

/** Sim clock text: mm:ss from scenario start (no wall-clock time; minutes keep counting past 59). */
export function formatClock(t: number): string {
  const s = Math.max(0, Math.floor(t));
  return `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
}

/** Wall-clock times are shown in Melbourne time (CLAUDE.md), whatever the viewer's time zone. */
export const LOCAL_TIME_ZONE = "Australia/Melbourne";

const localParts = new Intl.DateTimeFormat("en-AU", {
  timeZone: LOCAL_TIME_ZONE,
  weekday: "short",
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

/**
 * Real local time at sim time `t` (s), given meta.start_local (the wall-clock time of sim clock 0, ISO 8601 with an
 * offset): "Fri 23 Oct 2026 · 07:42" (seconds dropped). Null when start_local is absent or unreadable.
 */
export function formatLocalClock(t: number, startLocal: string | undefined): string | null {
  const ms = localMs(t, startLocal);
  if (ms === null) return null;
  const d = localDate(ms);
  return d && `${d.date} · ${d.time}`;
}

/** The local date of sim time `t`: "Fri 23 Oct 2026". Null without a readable start_local. */
export function formatLocalDate(t: number, startLocal: string | undefined): string | null {
  const ms = localMs(t, startLocal);
  return ms === null ? null : (localDate(ms)?.date ?? null);
}

function localMs(t: number, startLocal: string | undefined): number | null {
  if (!startLocal || !Number.isFinite(t)) return null;
  const t0 = Date.parse(startLocal);
  return Number.isFinite(t0) ? t0 + Math.floor(t) * 1000 : null;
}

function localDate(ms: number): { date: string; time: string } | null {
  const p: Record<string, string> = {};
  for (const { type, value } of localParts.formatToParts(ms)) p[type] = value;
  if (!p.weekday || !p.day || !p.month || !p.year || !p.hour || !p.minute) return null;
  return { date: `${p.weekday} ${p.day} ${p.month} ${p.year}`, time: `${p.hour}:${p.minute}` };
}

type Listener = () => void;

/**
 * The playback clock. Its animation-frame loop runs only while it has something to animate: started, playing, a
 * scenario loaded and the page visible. Paused or hidden, it asks the browser for no frames at all; play, or the page
 * shown again, restarts it, continuing from the same sim time (time spent paused or hidden is never played back).
 * Updates are capped at MAX_UPDATES_PER_S, so a 120 Hz display redraws the traffic at 60 per second, not 120.
 */
export class PlaybackClock {
  private state: PlaybackState;
  private snapshot: PlaybackState;
  private readonly frameListeners = new Set<(time: number) => void>();
  private readonly uiListeners = new Set<Listener>();
  private host: FrameHost | null = null;
  private stopVisibility: (() => void) | null = null;
  /** Identifies the current start() so only its own stop function detaches. */
  private session: object | null = null;
  private raf: number | null = null;
  private lastUpdate: number | null = null;
  private readonly limiter = new FrameLimiter();
  private lastUi = 0;

  constructor(duration = 0, { playing = true, speed = 1 as Speed, time = 0, start = 0 } = {}) {
    this.state = { time: clampTime(time, duration, start), playing, speed, start, duration };
    this.snapshot = this.state;
  }

  get time() {
    return this.state.time;
  }

  /** True while the clock has an animation frame requested (playing, visible, started). */
  get running(): boolean {
    return this.raf !== null;
  }

  /** The throttled snapshot React reads (stable object between notifications). */
  getSnapshot = (): PlaybackState => this.snapshot;

  subscribeUi = (l: Listener): (() => void) => {
    this.uiListeners.add(l);
    return () => this.uiListeners.delete(l);
  };

  /** Called on every playback update with the new time (and once immediately on seek / duration change). */
  onFrame(l: (time: number) => void): () => void {
    this.frameListeners.add(l);
    return () => this.frameListeners.delete(l);
  }

  /** Replay [0, duration], clamping the time into it. */
  setDuration(duration: number) {
    this.update({ start: 0, duration, time: clampTime(this.state.time, duration) }, true);
  }

  /** Replay [start, end] (s on the trips clock). A time outside the new range moves to its start (scenario switch). */
  setRange(start: number, end: number) {
    const t = this.state.time;
    this.update({ start, duration: end, time: t >= start && t <= end ? t : start }, true);
  }

  play() {
    this.update({ playing: true }, false);
  }

  pause() {
    this.update({ playing: false }, false);
  }

  toggle() {
    if (this.state.playing) this.pause();
    else this.play();
  }

  setSpeed(speed: Speed) {
    if (!SPEEDS.includes(speed)) throw new Error(`unsupported speed ${speed}`);
    this.update({ speed }, false);
  }

  seek(t: number) {
    this.update({ time: clampTime(t, this.state.duration, this.state.start) }, true);
  }

  /**
   * Attach the clock to the browser's animation frames and page visibility (a fake host in tests). Frames are requested
   * only while there is playback to animate. Returns a stop function.
   */
  start(host: FrameHost = browserFrameHost): () => void {
    this.detach();
    const session = {};
    this.session = session;
    this.host = host;
    this.stopVisibility = host.onVisibilityChange(() => this.sync());
    this.sync();
    return () => {
      if (this.session === session) this.detach(); // a stale stop (StrictMode re-start) leaves the newer start running
    };
  }

  /**
   * One frame at timestamp `now` (ms). Advances and notifies the frame listeners, unless paused or the frame comes
   * before its next 1/60 s slot (FrameLimiter: the 60 per second cap). Returns whether it updated. Public for tests.
   */
  tick(now: number): boolean {
    if (!this.state.playing) return false;
    if (!this.limiter.accept(now)) return false;
    const dtS = this.lastUpdate === null ? 0 : (now - this.lastUpdate) / 1000;
    this.lastUpdate = now;
    this.state = advance(this.state, dtS);
    for (const l of this.frameListeners) l(this.state.time);
    if (now - this.lastUi >= UI_INTERVAL_MS) {
      this.lastUi = now;
      this.publish();
    }
    return true;
  }

  private readonly loop = (now: number) => {
    this.raf = null;
    this.tick(now);
    // A listener may have restarted the loop from inside tick (e.g. pause then play): never book a second frame.
    if (this.wantsFrames() && this.raf === null) this.raf = this.host!.requestFrame(this.loop);
  };

  private wantsFrames(): boolean {
    return this.host !== null && this.state.playing && this.state.duration > this.state.start && !this.host.isHidden();
  }

  /** Start or stop the frame loop to match the state. A (re)start continues from the current sim time. */
  private sync() {
    const want = this.wantsFrames();
    if (want && this.raf === null) {
      this.lastUpdate = null; // the first frame after a pause or a hidden spell advances by 0
      this.limiter.reset();
      this.raf = this.host!.requestFrame(this.loop);
    } else if (!want && this.raf !== null) {
      this.host?.cancelFrame(this.raf);
      this.raf = null;
    }
  }

  private detach() {
    if (this.raf !== null) this.host?.cancelFrame(this.raf);
    this.raf = null;
    this.stopVisibility?.();
    this.stopVisibility = null;
    this.host = null;
    this.session = null;
  }

  private update(patch: Partial<PlaybackState>, frame: boolean) {
    this.state = { ...this.state, ...patch };
    if (frame) for (const l of this.frameListeners) l(this.state.time);
    this.publish();
    this.sync();
  }

  private publish() {
    this.snapshot = this.state;
    for (const l of this.uiListeners) l();
  }
}
