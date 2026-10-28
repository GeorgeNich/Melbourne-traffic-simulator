// Road speed layers: every edge a thin glowing line in the traffic palette (speeds.ts), drawn just under the cars.
//
// The cars' language, quieter: two additive passes per group, a soft halo (falloff across its width) and a thin core
// (palette SPEED). Surface edges: depth "less-equal" (in front of buildings and on decks) plus a dimmer "greater"
// pass, so a street behind a ghost tower glows through it like the cars do. Tunnel edges: X-ray (depth "always") and
// only while "Show tunnels" is on. Additive light on the near-black base adds no haze where there is no data (alpha 0).
//
// Redraw on demand: the renderer builds data, accessors and parameters once. Per frame it returns new descriptors whose
// props are identical unless the 60 s bin changed (then only `updateTriggers.getColor` changes, and deck.gl refills
// just the colour attribute) or a toggle flipped (`visible`). Layer ids start with "traffic" (they are traffic, and
// review tooling that hides the traffic to measure the buildings' haze hides these too).
import type { Layer } from "@deck.gl/core";
import { PathLayer } from "@deck.gl/layers";
import { SPEED } from "../theme/palette";
import { binAt, binColors, type SpeedPiece, type SpeedTable } from "./speeds";
import { SURFACE_PARAMETERS, THROUGH_PARAMETERS, TUNNEL_PARAMETERS } from "./trafficLayers";

/** A PathLayer whose alpha falls off across the width, (1 - |x|)^falloff: the soft glow edge (as GlowTripsLayer). */
class GlowPathLayer<D = unknown> extends PathLayer<D, { falloff?: number }> {
  static layerName = "GlowPathLayer";

  getShaders() {
    const shaders = super.getShaders();
    const k = Math.max(0, this.props.falloff ?? 0);
    if (k > 0) {
      const inject = { ...(shaders.inject as Record<string, string>) };
      inject["fs:DECKGL_FILTER_COLOR"] = `${inject["fs:DECKGL_FILTER_COLOR"] ?? ""}\ncolor.a *= pow(max(0.0, 1.0 - abs(geometry.uv.x)), ${k.toFixed(2)});`;
      shaders.inject = inject;
    }
    return shaders;
  }
}

/** Halo falloff across the width (the car halo uses 1.6). */
export const SPEED_FALLOFF = 1.6;

export type SpeedPass = "halo" | "core" | "thru-halo" | "thru-core";

/** Layer id of a pass, e.g. "traffic-speed-core" or "traffic-tunnel-speed-halo". */
export function speedLayerId(tunnel: boolean, pass: SpeedPass): string {
  return `${tunnel ? "traffic-tunnel-speed" : "traffic-speed"}-${pass}`;
}

export type SpeedFrame = (currentTime: number, showTunnels: boolean, showSpeeds: boolean) => Layer[];

export function speedRenderer(pieces: readonly SpeedPiece[], table: SpeedTable): SpeedFrame {
  const surface = pieces.filter((p) => !p.tunnel);
  const tunnel = pieces.filter((p) => p.tunnel);
  // The current bin's colours (one RGBA per edge), recomputed only when the bin changes.
  let bin = -1;
  let colors: Uint8Array = new Uint8Array(0);
  const getColor = (d: SpeedPiece): [number, number, number, number] => {
    const i = d.edge * 4;
    return [colors[i]!, colors[i + 1]!, colors[i + 2]!, colors[i + 3]!];
  };
  const base = {
    getPath: (d: SpeedPiece) => d.path,
    getColor,
    widthUnits: "meters" as const,
    billboard: false,
    capRounded: true,
    jointRounded: true,
  };
  const halo = { getWidth: SPEED.halo.widthM, widthMinPixels: SPEED.halo.minPx, falloff: SPEED_FALLOFF, opacity: SPEED.halo.alpha / 255 };
  const core = { getWidth: SPEED.core.widthM, widthMinPixels: SPEED.core.minPx, falloff: 0, opacity: SPEED.core.alpha / 255 };
  const k = SPEED.throughBuildings;
  const passes = [
    { id: speedLayerId(false, "thru-halo"), data: surface, ...halo, opacity: halo.opacity * k, parameters: THROUGH_PARAMETERS, tunnel: false },
    { id: speedLayerId(false, "thru-core"), data: surface, ...core, opacity: core.opacity * k, parameters: THROUGH_PARAMETERS, tunnel: false },
    { id: speedLayerId(false, "halo"), data: surface, ...halo, parameters: SURFACE_PARAMETERS, tunnel: false },
    { id: speedLayerId(false, "core"), data: surface, ...core, parameters: SURFACE_PARAMETERS, tunnel: false },
    { id: speedLayerId(true, "halo"), data: tunnel, ...halo, parameters: TUNNEL_PARAMETERS, tunnel: true },
    { id: speedLayerId(true, "core"), data: tunnel, ...core, parameters: TUNNEL_PARAMETERS, tunnel: true },
  ];
  return (currentTime, showTunnels, showSpeeds) => {
    const b = binAt(currentTime, table.nBins, table.binS);
    if (b !== bin) {
      bin = b;
      colors = binColors(table, b);
    }
    return passes.map(
      ({ tunnel: isTunnel, ...p }) =>
        new GlowPathLayer<SpeedPiece>({
          ...base,
          ...p,
          visible: showSpeeds && (!isTunnel || showTunnels),
          updateTriggers: { getColor: bin },
        }),
    );
  };
}
