// Phase 2 gate material: the camera views and simulation times George reviewed at the Stage C look review, rendered
// in the picked (default) look. Shared by e2e/gate.spec.ts (stills) and e2e-perf/gate-video.spec.ts (the fly-through).
import { resolve } from "node:path";
import type { Page } from "@playwright/test";
import type { CameraView } from "../../src/camera/presets";

export const GATE_DIR = resolve(import.meta.dirname, "../../screenshots/phase-2-gate");

export interface GateView {
  id: string;
  label: string;
  view: CameraView;
  /** Simulation time (s from scenario start) to pause at. */
  time: number;
}

export const GATE_VIEWS: GateView[] = [
  // The CBD at a busy moment (about 120 cars in the Hoddle Grid): looking ENE along the grid, Southbank to the right.
  { id: "cbd-hero", label: "CBD, 08:00 (t = 480 s)", view: { longitude: 144.962, latitude: -37.8165, zoom: 15.8, pitch: 62, bearing: 70 }, time: 480 },
  // Burnley (eastbound, -30 m) and Domain (westbound) tunnels under the Yarra. At t = 525 s car rt734 is in the
  // Burnley tunnel right under the river and rt968 is in the Domain tunnel west of it.
  {
    id: "xray-river",
    label: "Burnley tunnel under the Yarra, from the south (t = 525 s)",
    view: { longitude: 144.9774, latitude: -37.825, zoom: 17.3, pitch: 76, bearing: 0 },
    time: 525,
  },
  {
    id: "xray-oblique",
    label: "Both tunnels, oblique from the south-west (t = 512 s)",
    view: { longitude: 144.9774, latitude: -37.8257, zoom: 17.0, pitch: 68, bearing: 40 },
    time: 512,
  },
  // Stacked buildings: 120 Collins (structure 814763, crown 260 m) and 101 Collins (818123, 235 m); Southbank.
  {
    id: "buildings-collins",
    label: "120 and 101 Collins St from the south-west",
    view: { longitude: 144.9701, latitude: -37.8158, zoom: 16.75, pitch: 55, bearing: 20 },
    time: 480,
  },
  {
    id: "buildings-southbank",
    label: "Southbank (Eureka, Australia 108) from across the Yarra",
    view: { longitude: 144.9643, latitude: -37.8225, zoom: 16.9, pitch: 60, bearing: 165 },
    time: 480,
  },
];

/** Hide the dev fps readout and the top-left control panel; the playback bar and the map attribution stay. */
export const HIDE_UI = '[data-testid="fps"], div:has(> [aria-label="Camera presets"]) { display: none !important; }';

/** Jump to a view, pause playback at `time`, and wait for the map to finish drawing (tiles loaded). */
export async function showAt(page: Page, v: GateView) {
  await page.evaluate(
    ({ view, time }) =>
      new Promise<void>((done) => {
        const pb = window.__twinPlayback;
        if (pb) {
          pb.pause();
          pb.seek(time);
        }
        const map = window.__twinMap!;
        map.jumpTo({ center: [view.longitude, view.latitude], zoom: view.zoom, pitch: view.pitch, bearing: view.bearing });
        map.once("idle", () => done());
        map.triggerRepaint();
      }),
    { view: v.view, time: v.time },
  );
  await page.waitForTimeout(500); // label fade-in
}
