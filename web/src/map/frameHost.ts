// The browser's animation-frame and page-visibility APIs behind a small interface, so the redraw-on-demand loops
// (playback.ts, onDemandFrames.ts) can be unit-tested with a fake host (fakeFrameHost.ts) and never touch `window` in tests.

/** Redraws per second at most while the camera is still (traffic playback, label fades), also on 120 Hz displays. */
export const MAX_UPDATES_PER_S = 60;
const INTERVAL_MS = 1000 / MAX_UPDATES_PER_S;
/**
 * How early a frame may come and still count for its 1/60 s slot (ms). Frame timestamps on a 60 Hz display jitter by
 * a millisecond or two around 16.7 ms, and none of those frames may be dropped; half a 120 Hz frame is enough slack.
 */
export const FRAME_SLACK_MS = 4;

/**
 * At most MAX_UPDATES_PER_S updates a second, on a fixed 1/60 s grid: a frame is accepted once it reaches its slot
 * (less FRAME_SLACK_MS), and the next slot is one interval later. A 60 Hz display updates every frame even with jittery
 * timestamps; 120 Hz updates every other frame; 144 Hz updates on 60 of its frames. After a stall the grid restarts.
 */
export class FrameLimiter {
  private due: number | null = null;

  /** Should the frame at `now` (ms) update? Accepting books the next slot. */
  accept(now: number): boolean {
    if (this.due !== null && now < this.due - FRAME_SLACK_MS) return false;
    this.note(now);
    return true;
  }

  /** An update happened at `now` regardless (e.g. the camera moved): the next slot is one interval later. */
  note(now: number): void {
    this.due = this.due === null || now - this.due >= INTERVAL_MS ? now + INTERVAL_MS : this.due + INTERVAL_MS;
  }

  /** Forget the grid (the loop stopped); the next frame is accepted. */
  reset(): void {
    this.due = null;
  }
}

export interface FrameHost {
  requestFrame(cb: (now: number) => void): number;
  cancelFrame(id: number): void;
  /** True while the page is hidden (background tab, minimised window). */
  isHidden(): boolean;
  /** Called whenever the page is hidden or shown. Returns an unsubscribe function. */
  onVisibilityChange(cb: () => void): () => void;
}

/** The real browser host. */
export const browserFrameHost: FrameHost = {
  requestFrame: (cb) => window.requestAnimationFrame(cb),
  cancelFrame: (id) => window.cancelAnimationFrame(id),
  isHidden: () => document.visibilityState === "hidden",
  onVisibilityChange: (cb) => {
    document.addEventListener("visibilitychange", cb);
    return () => document.removeEventListener("visibilitychange", cb);
  },
};
