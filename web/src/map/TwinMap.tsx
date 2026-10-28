import type { Layer } from "@deck.gl/core";
import { MapboxOverlay, type MapboxOverlayProps } from "@deck.gl/mapbox";
import type { StyleSpecification } from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { forwardRef, useEffect, useLayoutEffect, useRef, useState } from "react";
import { AttributionControl, Map, type MapRef, NavigationControl, useControl, useMap } from "react-map-gl/maplibre";
import type { CameraView } from "../camera/presets";
import { VIEW_LIMITS } from "../camera/presets";
import type { PlaybackClock } from "../traffic/playback";
import { COM_ATTRIBUTION_HTML } from "./basemap";
import { OnDemandFrames } from "./onDemandFrames";

/**
 * deck.gl drawn *inside* MapLibre's WebGL context (interleaved MapboxOverlay), so there is one canvas, one
 * camera (MapLibre's, with its native smooth flyTo / zoom / tilt / rotate) and a shared depth buffer.
 *
 * Redraw on demand: deck.gl's own frame loop runs only for a few frames after new layer props or a MapLibre render
 * (onDemandFrames.ts). MapLibre itself renders only when its camera, tiles or labels change, or when deck.gl asks for
 * a repaint because its layers changed. So a paused, still map draws nothing and asks the browser for no frames.
 */
function DeckOverlay({ layers, frame, ...props }: MapboxOverlayProps & { layers: Layer[]; frame?: FrameLayers }) {
  const [frames] = useState(() => new OnDemandFrames());
  const compose = (f: FrameLayers | undefined) => (f ? [...layers, ...f.layers(f.clock.time)] : layers);
  // `animationFrameProvider` is a luma.gl AnimationLoop prop that Deck forwards to its loop (not in deck.gl's typings),
  // so only that one prop is cast; everything else stays type-checked.
  const frameProvider = { animationFrameProvider: frames } as Partial<MapboxOverlayProps>;
  const deckProps: MapboxOverlayProps = { ...props, ...frameProvider };
  const overlay = useControl(() => new MapboxOverlay({ ...deckProps, layers: compose(frame) }));
  overlay.setProps({ ...deckProps, layers: compose(frame) });
  frames.wake();
  // Per-frame updates bypass React: on each clock frame, only the frame layers (traffic) get a new currentTime.
  const latest = useRef({ layers, frame });
  useLayoutEffect(() => {
    latest.current = { layers, frame };
  });
  const clock = frame?.clock;
  useEffect(
    () =>
      clock?.onFrame((t) => {
        const { layers: base, frame: f } = latest.current;
        if (f) {
          overlay.setProps({ layers: [...base, ...f.layers(t)] });
          frames.wake();
        }
      }),
    [clock, overlay, frames],
  );
  // Every MapLibre render (camera moves, tiles, label fades) also gives deck.gl's loop a few frames.
  const { current: mapRef } = useMap();
  useEffect(() => {
    const map = mapRef?.getMap();
    if (!map) return;
    const wake = () => frames.wake();
    map.on("render", wake);
    return () => {
      map.off("render", wake);
    };
  }, [mapRef, frames]);
  return null;
}

/**
 * React StrictMode (dev) adds, removes and re-adds the overlay in one tick, so two Decks race to attach to MapLibre's
 * WebGL context (luma.gl's attach is async) and the live one can lose with "WebGL context already attached to device".
 * Reusing the device already on the context avoids it; deck.gl sets this when it creates a device, not when it attaches.
 */
const DEVICE_PROPS = { _reuseDevices: true } as const;

/** Layers redrawn on every clock update (the traffic, while playing); everything else stays in `layers`. */
export interface FrameLayers {
  clock: Pick<PlaybackClock, "time" | "onFrame">;
  layers: (time: number) => Layer[];
}

interface Props {
  mapStyle: StyleSpecification;
  initialView: CameraView;
  layers: Layer[];
  /** Drawn after `layers`, rebuilt on every clock update (only their currentTime changes). */
  frame?: FrameLayers;
  onLoad?: () => void;
  onIdle?: () => void;
  onUserMove?: () => void;
  onZoom?: (zoom: number) => void;
}

export const TwinMap = forwardRef<MapRef, Props>(function TwinMap(
  { mapStyle, initialView, layers, frame, onLoad, onIdle, onUserMove, onZoom },
  ref,
) {
  return (
    <Map
      ref={ref}
      initialViewState={initialView}
      mapStyle={mapStyle}
      minZoom={VIEW_LIMITS.minZoom}
      maxZoom={VIEW_LIMITS.maxZoom}
      maxPitch={VIEW_LIMITS.maxPitch}
      attributionControl={false}
      onLoad={onLoad}
      onIdle={onIdle}
      // Camera events also fire during a programmatic flyTo; only a real user gesture has an originalEvent.
      onZoom={(e) => onZoom?.(e.viewState.zoom)}
      onMoveStart={(e) => {
        if (e.originalEvent) onUserMove?.();
      }}
      style={{ position: "absolute", inset: 0 }}
    >
      <DeckOverlay interleaved deviceProps={DEVICE_PROPS} layers={layers} frame={frame} />
      <NavigationControl position="bottom-left" visualizePitch />
      {/* Always expanded so the credits stay visible (compact mode hides them behind an "i" button). */}
      <AttributionControl position="bottom-right" compact={false} customAttribution={COM_ATTRIBUTION_HTML} />
    </Map>
  );
});
