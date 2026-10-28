import type { Map as MapLibreMap } from "maplibre-gl";
import { useEffect, useState } from "react";
import { palette } from "../theme/palette";

/** Readout refresh while the map is drawing (ms). */
const WINDOW_MS = 500;

/**
 * Dev-only map frame rate (rendered only when import.meta.env.DEV). Passive: it counts MapLibre renders (each also
 * draws the deck.gl layers) and never requests a frame itself, so it can't keep the map drawing. While renders arrive
 * it shows renders per second over WINDOW_MS; after a window with none it shows "0 fps idle" and stops its timer.
 */
export function FpsMeter({ map }: { map: MapLibreMap | null }) {
  const [fps, setFps] = useState<number | null>(null);

  useEffect(() => {
    if (!map) return;
    let frames = 0;
    let since = 0;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const report = () => {
      const now = performance.now();
      setFps(Math.round((frames * 1000) / (now - since)));
      if (frames === 0) {
        timer = null; // idle: the next render restarts the readout
        return;
      }
      frames = 0;
      since = now;
      timer = setTimeout(report, WINDOW_MS);
    };
    const onRender = () => {
      frames += 1;
      if (timer === null) {
        since = performance.now();
        timer = setTimeout(report, WINDOW_MS);
      }
    };
    map.on("render", onRender);
    timer = setTimeout(report, WINDOW_MS);
    since = performance.now();
    return () => {
      map.off("render", onRender);
      if (timer !== null) clearTimeout(timer);
    };
  }, [map]);

  return (
    <div
      data-testid="fps"
      title="MapLibre renders per second (dev only); 0 when nothing changes"
      style={{
        position: "absolute",
        right: 12,
        top: 12,
        padding: "4px 8px",
        font: "12px ui-monospace, SFMono-Regular, Menlo, monospace",
        color: palette.ui.textDim,
        background: palette.ui.panel,
        border: `1px solid ${palette.ui.border}`,
        borderRadius: 4,
        pointerEvents: "none",
      }}
    >
      {fps === null ? "-- fps" : `${fps} fps`} <span style={{ opacity: 0.6 }}>{fps === 0 ? "idle" : "dev"}</span>
    </div>
  );
}
