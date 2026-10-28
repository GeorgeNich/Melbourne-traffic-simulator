// Glowing car playback: deck.gl layers over the prepared trips (prepare.ts).
//
// Per group (surface, then tunnel), three passes, all additive and all depth-TESTED but not depth-WRITING:
//   1. halo:  a wide, faint TripsLayer with a soft cross-width falloff (the glow);
//   2. core:  a thin, bright TripsLayer (the light trail; fades over the trail length);
//   3. heads: the cars themselves, a hot dot with a soft halo (CarHeadsLayer).
// Surface traffic uses depth test "less-equal": cars in front of buildings and on bridge decks show at full strength
// (decks don't write depth). The same three passes again with depth test "greater", dimmer (GLOW.throughBuildings),
// let cars behind a ghost building glow through it ("glow through"). Tunnel traffic is X-ray (palette XRAY): depth
// test "always" so it shows through the ground and the translucent city, wider, with hollow ring heads, a tether up
// to a ring on the surface above each car, and only while "Show tunnels" is on.
//
// Time buckets (prepare.ts): every group is split into ~60 s buckets, each with its own set of passes. Each frame only
// buckets whose time span overlaps [currentTime - trail, currentTime] are visible; the rest are skipped by deck.gl, so
// GPU work stays flat however long the run is.
//
// trafficRenderer() builds every data object, accessor and parameter object ONCE. The returned function is called
// each frame with the playback time; it creates new layer descriptors whose only changed prop is `currentTime` on the
// visible buckets. Hidden buckets keep a constant time and `visible: false`, so they don't change at all; a bucket's
// props change only when it enters or leaves the trail window (or the tunnel toggle flips). deck.gl just updates
// uniforms: no attribute is ever recomputed, and buffers are uploaded once at load.
import type { Layer } from "@deck.gl/core";
import { TripsLayer, type TripsLayerProps } from "@deck.gl/geo-layers";
import { GLOW, palette, TRAIL_SECONDS, XRAY } from "../theme/palette";
import { CarHeadsLayer, type CarHeadsData, CarTethersLayer } from "./CarHeadsLayer";
import { bucketActive, OPEN_CLIP, type PreparedTraffic, type TripBucket, type TripGroup } from "./prepare";

type RGBA = [number, number, number, number];

/**
 * A TripsLayer with two additions:
 * - a per-vertex clip window [clip0, clip1) (prepare.ts): fragments whose trail time is outside it are discarded, so
 *   pieces that meet at a bucket edge or a tunnel portal draw every fragment exactly once (no double-drawn caps);
 * - alpha falling off across the path width, (1 - |x|)^falloff, for a soft glow edge.
 */
class GlowTripsLayer<D = unknown> extends TripsLayer<D, { falloff?: number; getClipTimes?: unknown }> {
  static layerName = "GlowTripsLayer";
  // deck.gl merges defaultProps along the class chain; this only adds the clip accessor (binary data supplies it).
  static defaultProps = { getClipTimes: { type: "accessor", value: [-OPEN_CLIP, OPEN_CLIP] } } as unknown as typeof TripsLayer.defaultProps;

  initializeState() {
    super.initializeState();
    this.getAttributeManager()!.addInstanced({
      clipTimes: { size: 2, accessor: "getClipTimes", shaderAttributes: { instanceClipTimes: { vertexOffset: 0 } } },
    });
  }

  getShaders() {
    const shaders = super.getShaders();
    const inject = { ...(shaders.inject as Record<string, string>) };
    const add = (key: string, code: string, before = false) => {
      inject[key] = before ? `${code}\n${inject[key] ?? ""}` : `${inject[key] ?? ""}\n${code}`;
    };
    add("vs:#decl", "in vec2 instanceClipTimes;\nout vec2 vClip;");
    add("vs:#main-end", "vClip = instanceClipTimes;");
    add("fs:#decl", "in vec2 vClip;");
    add("fs:DECKGL_FILTER_COLOR", "if (vTime < vClip.x || vTime >= vClip.y) discard;", true);
    const k = Math.max(0, this.props.falloff ?? 0);
    if (k > 0) add("fs:DECKGL_FILTER_COLOR", `color.a *= pow(max(0.0, 1.0 - abs(geometry.uv.x)), ${k.toFixed(2)});`);
    shaders.inject = inject;
    return shaders;
  }
}

/** Additive blending: overlapping glows add up to light, like long-exposure traffic. */
const additive = {
  blend: true,
  blendColorOperation: "add",
  blendColorSrcFactor: "src-alpha",
  blendColorDstFactor: "one",
  blendAlphaOperation: "add",
  blendAlphaSrcFactor: "one",
  blendAlphaDstFactor: "one",
} as const;

export const SURFACE_PARAMETERS = { ...additive, depthWriteEnabled: false, depthCompare: "less-equal" } as const;
/** "Glow through": surface cars BEHIND a building (depth "greater"), drawn dimmer, glow through it. */
export const THROUGH_PARAMETERS = { ...additive, depthWriteEnabled: false, depthCompare: "greater" } as const;
export const TUNNEL_PARAMETERS = { ...additive, depthWriteEnabled: false, depthCompare: "always" } as const;

/** Soft glow falloff exponents across the trail width (halo) and the core. */
export const FALLOFF = { halo: 1.6, core: 0.35 } as const;

interface PathData {
  length: number;
  startIndices: Uint32Array;
  attributes: {
    getPath: { value: Float64Array; size: 3 };
    getTimestamps: { value: Float32Array; size: 1 };
    getClipTimes: { value: Float32Array; size: 2 };
  };
}

const pathData = (g: TripGroup): PathData => ({
  length: g.length,
  startIndices: g.startIndices,
  attributes: {
    getPath: { value: g.positions, size: 3 },
    getTimestamps: { value: g.timestamps, size: 1 },
    getClipTimes: { value: g.clip, size: 2 },
  },
});

const headsData = (g: TripGroup): CarHeadsData => ({
  length: g.heads.length,
  attributes: {
    getFrom: { value: g.heads.from, size: 3 },
    getTo: { value: g.heads.to, size: 3 },
    getTimes: { value: g.heads.times, size: 2 },
  },
});

/** The same heads with z = 0: a car's spot on the surface straight above it. */
function flatHeads(d: CarHeadsData): CarHeadsData {
  const flat = (v: Float64Array) => v.map((x, i) => (i % 3 === 2 ? 0 : x));
  return {
    length: d.length,
    attributes: {
      getFrom: { value: flat(d.attributes.getFrom.value), size: 3 },
      getTo: { value: flat(d.attributes.getTo.value), size: 3 },
      getTimes: d.attributes.getTimes,
    },
  };
}

const withAlpha = (rgb: readonly number[], a: number): RGBA => [rgb[0]!, rgb[1]!, rgb[2]!, Math.round(a)];

/** The head's hot centre: the traffic colour lifted toward white by `lift` (same hue, still reads as blue). */
export function headCoreColor(rgb: readonly number[], lift: number = palette.traffic.headCoreLift): RGBA {
  return [...rgb.slice(0, 3).map((c) => Math.round(c + (255 - c) * lift)), 255] as RGBA;
}

export type TrafficFrame = (currentTime: number, showTunnels: boolean) => Layer[];

/** Layer passes per bucket, in draw order. */
export const TRAFFIC_PASSES = ["halo", "core", "heads"] as const;
export type TrafficPass = (typeof TRAFFIC_PASSES)[number];
/** Extra passes: surface cars seen through buildings; tethers and surface rings for tunnel cars. */
export type ExtraPass = "thru-halo" | "thru-core" | "thru-heads" | "tethers" | "mark";

/** Id of one bucket's layer, e.g. "traffic-core-b3" or "traffic-tunnel-heads-b0". */
export function trafficLayerId(tunnel: boolean, pass: TrafficPass | ExtraPass, bucket: number): string {
  return `${tunnel ? "traffic-tunnel" : "traffic"}-${pass}-b${bucket}`;
}

/**
 * Build the per-frame layer function for prepared trips (look: palette GLOW, TRAIL_SECONDS, XRAY). Everything except
 * `currentTime` and the tunnel `visible` flag is fixed here, once.
 */
export function trafficRenderer(prepared: PreparedTraffic): TrafficFrame {
  const { core, halo, head, throughBuildings: k } = GLOW;
  const rgb = palette.traffic.flowing; // Phase 2: every car is "flowing" until speeds arrive (Phase 3).
  const trail = TRAIL_SECONDS;
  const groups = [
    { tunnel: false, buckets: prepared.surface, parameters: SURFACE_PARAMETERS },
    { tunnel: true, buckets: prepared.tunnel, parameters: TUNNEL_PARAMETERS },
  ];
  // X-ray tunnel cars are wider and glow more than surface cars, with hollow ring heads (palette XRAY).
  const surfaceScale = { coreWidth: 1, haloWidth: 1, headHaloScale: 1, headRadius: 1, headHaloAlpha: head.haloAlpha };
  const buckets = groups.flatMap(({ tunnel, buckets, parameters }) =>
    buckets.map((b: TripBucket) => {
      const trips: Omit<TripsLayerProps<unknown>, "id"> = {
        data: pathData(b) as unknown as TripsLayerProps<unknown>["data"],
        _pathType: "open",
        widthUnits: "meters",
        billboard: false,
        capRounded: true,
        jointRounded: true,
        fadeTrail: true,
        trailLength: trail,
        parameters,
      };
      const w = tunnel ? XRAY : surfaceScale;
      const haloProps = {
        ...trips,
        id: trafficLayerId(tunnel, "halo", b.index),
        getWidth: halo.widthM * w.haloWidth,
        widthMinPixels: halo.minPx * w.haloWidth,
        getColor: withAlpha(rgb, halo.alpha),
        falloff: FALLOFF.halo,
      };
      const coreProps = {
        ...trips,
        id: trafficLayerId(tunnel, "core", b.index),
        getWidth: core.widthM * w.coreWidth,
        widthMinPixels: core.minPx * w.coreWidth,
        getColor: withAlpha(rgb, core.alpha),
        falloff: FALLOFF.core,
      };
      const headsProps = {
        id: trafficLayerId(tunnel, "heads", b.index),
        data: headsData(b),
        radiusMeters: head.radiusM * w.headRadius,
        minPixels: head.minPx * w.headRadius,
        maxPixels: head.maxPx * w.headRadius,
        haloScale: head.haloScale * w.headHaloScale,
        haloAlpha: Math.max(head.haloAlpha, w.headHaloAlpha),
        ring: tunnel ? 1 : 0,
        color: withAlpha(rgb, 255),
        coreColor: headCoreColor(rgb),
        parameters,
      };
      if (!tunnel) {
        // "Glow through": the same three passes for cars hidden behind buildings, dimmer.
        const through = {
          halo: { ...haloProps, id: trafficLayerId(false, "thru-halo", b.index), getColor: withAlpha(rgb, halo.alpha * k), parameters: THROUGH_PARAMETERS },
          core: { ...coreProps, id: trafficLayerId(false, "thru-core", b.index), getColor: withAlpha(rgb, core.alpha * k), parameters: THROUGH_PARAMETERS },
          heads: { ...headsProps, id: trafficLayerId(false, "thru-heads", b.index), color: withAlpha(rgb, 255 * k), parameters: THROUGH_PARAMETERS },
        };
        return { bucket: b, tunnel, halo: haloProps, core: coreProps, heads: headsProps, through, tethers: null, mark: null };
      }
      const tethers = { id: trafficLayerId(true, "tethers", b.index), data: headsProps.data, widthPixels: 1.5, color: withAlpha(rgb, XRAY.tetherAlpha), parameters };
      // A small hollow ring on the surface straight above each tunnel car (the heads, flattened to z = 0).
      const s = XRAY.surfaceMarkScale;
      const mark = {
        ...headsProps,
        id: trafficLayerId(true, "mark", b.index),
        data: flatHeads(headsProps.data),
        radiusMeters: head.radiusM * s,
        minPixels: head.minPx * s,
        maxPixels: head.maxPx * s,
        haloScale: 1.4,
        haloAlpha: 0,
        ring: 1,
        color: withAlpha(rgb, 255 * XRAY.surfaceMark),
        coreColor: withAlpha(rgb, 255),
      };
      return { bucket: b, tunnel, halo: haloProps, core: coreProps, heads: headsProps, through: null, tethers, mark };
    }),
  );

  return (currentTime, showTunnels) =>
    buckets.flatMap((b) => {
      const visible = (!b.tunnel || showTunnels) && bucketActive(b.bucket, currentTime, trail);
      // Hidden buckets keep a constant time, so their props don't change from frame to frame.
      const t = visible ? currentTime : b.bucket.startS;
      const layers: Layer[] = [];
      if (b.through) {
        layers.push(
          new GlowTripsLayer({ ...b.through.halo, currentTime: t, visible }),
          new GlowTripsLayer({ ...b.through.core, currentTime: t, visible }),
          new CarHeadsLayer({ ...b.through.heads, currentTime: t, visible }),
        );
      }
      if (b.tethers) layers.push(new CarTethersLayer({ ...b.tethers, currentTime: t, visible }));
      if (b.mark) layers.push(new CarHeadsLayer({ ...b.mark, currentTime: t, visible }));
      layers.push(
        new GlowTripsLayer({ ...b.halo, currentTime: t, visible }),
        new GlowTripsLayer({ ...b.core, currentTime: t, visible }),
        new CarHeadsLayer({ ...b.heads, currentTime: t, visible }),
      );
      return layers;
    });
}
