import { describe, expect, it, vi } from "vitest";
import { FakeFrameHost } from "./fakeFrameHost";
import { OnDemandFrames, WAKE_FRAMES } from "./onDemandFrames";

/** Mimic luma.gl's AnimationLoop: it requests its next frame from inside every frame. */
function fakeLoop(frames: OnDemandFrames) {
  const onFrame = vi.fn();
  let id: number | null = null;
  const loop = (t: number) => {
    onFrame(t);
    id = frames.requestAnimationFrame(loop);
  };
  id = frames.requestAnimationFrame(loop);
  return { onFrame, stop: () => id !== null && frames.cancelAnimationFrame(id) };
}

describe("OnDemandFrames (deck.gl's frame loop, on demand)", () => {
  it("runs a few frames after start, then sleeps: no frames requested from the browser", () => {
    const host = new FakeFrameHost();
    const frames = new OnDemandFrames(host);
    const { onFrame } = fakeLoop(frames);
    for (let i = 0; i < 60; i++) host.frame(i * 16.7);
    expect(onFrame).toHaveBeenCalledTimes(WAKE_FRAMES);
    expect(host.scheduled).toBe(0);
    expect(frames.awake).toBe(false);
  });

  it("wakes for WAKE_FRAMES frames on demand, and the loop's next request is kept for the next wake", () => {
    const host = new FakeFrameHost();
    const frames = new OnDemandFrames(host);
    const { onFrame } = fakeLoop(frames);
    for (let i = 0; i < 10; i++) host.frame(i);
    onFrame.mockClear();
    frames.wake();
    expect(host.scheduled).toBe(1);
    for (let i = 0; i < 10; i++) host.frame(100 + i);
    expect(onFrame).toHaveBeenCalledTimes(WAKE_FRAMES);
    // Waking every frame (playing, or the camera moving) keeps it running every frame.
    onFrame.mockClear();
    for (let i = 0; i < 30; i++) {
      frames.wake();
      host.frame(200 + i);
    }
    expect(onFrame).toHaveBeenCalledTimes(30);
  });

  it("never schedules more than one browser frame at a time", () => {
    const host = new FakeFrameHost();
    const frames = new OnDemandFrames(host);
    fakeLoop(frames);
    frames.wake();
    frames.wake(5);
    frames.wake();
    expect(host.scheduled).toBe(1);
  });

  it("cancel drops the deferred frame (the loop stopped)", () => {
    const host = new FakeFrameHost();
    const frames = new OnDemandFrames(host);
    const { onFrame, stop } = fakeLoop(frames);
    stop();
    expect(host.scheduled).toBe(0);
    frames.wake();
    host.frame(0);
    expect(onFrame).not.toHaveBeenCalled();
  });
});
