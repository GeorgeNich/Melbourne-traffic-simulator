import type { Map as MaplibreMap } from "maplibre-gl";
import type { PlaybackClock } from "./traffic/playback";
import type { EdgeSpeed, SpeedTable } from "./traffic/speeds";

declare global {
  interface Window {
    /** Dev only: the MapLibre map, for Playwright camera control and measurement. */
    __twinMap?: MaplibreMap;
    /** Set (with performance mark "twin:ready") once buildings and roads are loaded (and, in dev, pass their contracts) and the map first goes idle. */
    __twinReady?: boolean;
    /** Dev only: the traffic playback clock (pause, seek, speed), for Playwright. */
    __twinPlayback?: PlaybackClock;
    /** Dev only: the road-speed table and the smoothed speed of an edge at a playback time, for Playwright. */
    __twinSpeeds?: { table: SpeedTable; speedAt: (edgeId: string, t: number) => EdgeSpeed | null };
  }
}
