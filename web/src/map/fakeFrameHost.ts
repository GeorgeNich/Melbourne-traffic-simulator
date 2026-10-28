// Test helper (never imported by the app, so never bundled): a hand-cranked FrameHost.
import type { FrameHost } from "./frameHost";

/** A hand-cranked host for unit tests: `frame(now)` runs every callback requested before it. */
export class FakeFrameHost implements FrameHost {
  hidden = false;
  private next = 1;
  private readonly pending = new Map<number, (now: number) => void>();
  private readonly visibility = new Set<() => void>();

  requestFrame(cb: (now: number) => void): number {
    const id = this.next++;
    this.pending.set(id, cb);
    return id;
  }

  cancelFrame(id: number): void {
    this.pending.delete(id);
  }

  isHidden(): boolean {
    return this.hidden;
  }

  onVisibilityChange(cb: () => void): () => void {
    this.visibility.add(cb);
    return () => this.visibility.delete(cb);
  }

  /** Callbacks currently waiting for a frame. */
  get scheduled(): number {
    return this.pending.size;
  }

  /** Run one browser frame at `now` (ms). A hidden page gets no frames, like a real background tab. */
  frame(now: number): void {
    if (this.hidden) return;
    const due = [...this.pending.values()];
    this.pending.clear();
    for (const cb of due) cb(now);
  }

  setHidden(hidden: boolean): void {
    this.hidden = hidden;
    for (const cb of this.visibility) cb();
  }
}
