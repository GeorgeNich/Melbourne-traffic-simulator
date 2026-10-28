// deck.gl's own frame loop, run only on demand.
//
// deck.gl runs a luma.gl AnimationLoop that asks for a browser animation frame after every frame, forever, even when
// nothing has changed (it then only checks whether a redraw is needed). In our interleaved setup MapLibre does all the
// drawing (deck.gl's layers are drawn inside MapLibre's render), so deck.gl's loop only has to notice new props and ask
// MapLibre for a repaint. This provider (passed to Deck as `animationFrameProvider`, which deck.gl forwards to its
// AnimationLoop) holds the loop's next frame request and lets it reach the browser only for a few frames after a wake
// (new layer props, a MapLibre render). With nothing changing, the loop sleeps and the page asks for no frames at all.
//
// Rule for callers: after any deck prop change, call wake(); add a pointermove wake when picking is added.

import { browserFrameHost, type FrameHost } from "./frameHost";

/** Frames deck.gl's loop keeps running after a wake: enough for props -> redraw check -> repaint request to settle. */
export const WAKE_FRAMES = 2;

/** The animation-frame provider interface luma.gl's AnimationLoop calls (requestAnimationFrame / cancelAnimationFrame). */
export interface AnimationFrameProvider {
  requestAnimationFrame(cb: (time: number) => void): number;
  cancelAnimationFrame(id: number): void;
}

export class OnDemandFrames implements AnimationFrameProvider {
  /** Frames left before the loop sleeps. */
  private budget = WAKE_FRAMES; // awake at start: deck.gl initialises on its first frames
  /** Frame requests waiting for the browser (normally one: deck.gl's loop; StrictMode can briefly have two Decks). */
  private readonly pending = new Map<number, (time: number) => void>();
  private hostId: number | null = null;
  private nextId = 1;

  constructor(private readonly host: FrameHost = browserFrameHost) {}

  requestAnimationFrame = (cb: (time: number) => void): number => {
    // A non-null id even when the frame is deferred: the loop treats a null id as "nothing scheduled".
    const id = this.nextId++;
    this.pending.set(id, cb);
    this.schedule();
    return id;
  };

  cancelAnimationFrame = (id: number): void => {
    this.pending.delete(id);
    if (this.pending.size === 0 && this.hostId !== null) {
      this.host.cancelFrame(this.hostId);
      this.hostId = null;
    }
  };

  /** Something may need deck.gl's attention (new props, a map render): let its loop run for `frames` more frames. */
  wake(frames: number = WAKE_FRAMES): void {
    this.budget = Math.max(this.budget, frames);
    this.schedule();
  }

  /** True while a frame for deck.gl's loop is requested from the browser. */
  get awake(): boolean {
    return this.hostId !== null;
  }

  private schedule() {
    if (this.pending.size === 0 || this.hostId !== null || this.budget <= 0) return;
    this.hostId = this.host.requestFrame(this.run);
  }

  private readonly run = (now: number) => {
    this.hostId = null;
    const due = [...this.pending.values()];
    this.pending.clear();
    this.budget -= 1;
    // Each loop asks for its next frame from inside; that is scheduled only while budget remains.
    for (const cb of due) cb(now);
  };
}
