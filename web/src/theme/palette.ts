// The one place every on-screen colour is defined. Tune the look here.
//
// The brief: a dark, minimal scene where cars and congested roads are the ONLY saturated things on screen.
// Buildings are a cool, pale, translucent "ghost" tone with thin bright outlines; nothing else is saturated.
// Colours are deck.gl RGBA arrays (0–255).

export type RGB = readonly [number, number, number];
export type RGBA = readonly [number, number, number, number];

/**
 * Ground roads in the base map (George's pick at the Stage C look review: "medium"). Minor streets stay faint; major
 * ground roads (motorway, trunk, primary, secondary) are brighter and a little wider, so the grid reads under the cars.
 */
export const GROUND_ROADS = {
  /** Line opacity per road class. */
  opacity: { minor: 0.08, tertiary: 0.18, major: 0.42, motorway: 0.5 },
  /** Width multiplier for major roads and motorways. */
  majorWidthScale: 1.8,
} as const;

/** Car trail length: simulated seconds of fading tail behind each car (George's pick: 20 s). */
export const TRAIL_SECONDS = 20;

/**
 * Car glow (George's pick: "glow through"). A bright core inside a wide additive halo, a hot head dot, and cars
 * behind a ghost building still glow through it, dimmer, instead of vanishing (an extra depth-"greater" pass).
 */
export const GLOW = {
  /** Bright core of the trail: width in metres (never thinner than minPx), alpha 0–255 at the car. */
  core: { widthM: 2.4, minPx: 2, alpha: 255 },
  /** Wide faint halo around the trail (the glow), drawn additively under the core. */
  halo: { widthM: 16, minPx: 8, alpha: 75 },
  /** The car itself: a bright dot (radius in metres, clamped in pixels) with a soft halo `haloScale` times wider. */
  head: { radiusM: 2.8, minPx: 2.8, maxPx: 10, haloScale: 4.5, haloAlpha: 0.65 },
  /** Surface cars behind a building glow through it at this fraction of their brightness (0–1). */
  throughBuildings: 0.4,
} as const;

/**
 * X-ray tunnel cars (George's pick, "depth"): full brightness (the X-ray depth test already sets them apart), wider
 * trails and a bigger head glow than surface cars, and a hollow ring head (an X-ray mark, never a solid surface dot).
 * Each tunnel car hangs from a tether up to a small hollow ring on the surface straight above it. The tunnel ribbons
 * themselves are palette.roadTunnel.
 */
export const XRAY = {
  /** Width multipliers for tunnel trails (core and halo) and the head halo, on top of GLOW. */
  coreWidth: 1.6,
  haloWidth: 1.6,
  headHaloScale: 1.5,
  /** Head halo alpha (0–1; at least GLOW's) and head radius multiplier. */
  headHaloAlpha: 0.75,
  headRadius: 1.6,
  /** Alpha (0–255) of the vertical tether from each tunnel car up to the surface. */
  tetherAlpha: 130,
  /** Alpha (0–1) of the hollow ring on the surface above each tunnel car, and its size relative to the head. */
  surfaceMark: 0.55,
  surfaceMarkScale: 1.6,
} as const;

/**
 * Road speeds (Phase 3): every SUMO edge drawn as a thin glowing line in the traffic palette by its speed_ratio for the
 * current 60 s bin (edge_stats). Blue >= flowing, amber >= slowing, red below; an edge with no row for the bin (no
 * vehicle, no data) gets no colour and keeps the base map's neutral road. Same "glow through" and X-ray rules as the
 * cars, quieter than them so the moving car glows stay on top (speedLayers.ts).
 */
export const SPEED = {
  /** speed_ratio thresholds (mean speed / speed limit): >= flowing is blue, >= slowing is amber, below is red. */
  thresholds: { flowing: 0.7, slowing: 0.4 },
  /**
   * Trailing smoothing over the 60 s bins, weighted by vehicle_count: the current minute at weight 1 plus the previous
   * minute at `previousWeight`. Colour only where the CURRENT bin has data (no row = no data, never filled in).
   */
  smoothing: { previousWeight: 0.5 },
  /**
   * Evidence: the colour's strength rises with the vehicles behind it (current bin + weighted previous bin), from
   * minAlpha for one car to full at fullAt cars, so one car's stopped tail can't shout as loud as a real queue.
   */
  evidence: { minAlpha: 0.45, fullAt: 4 },
  /**
   * Strength per class (0–1): flowing roads are the quiet backdrop (the cars carry the blue light), congestion pops.
   */
  classStrength: { flowing: 0.6, slowing: 1, jammed: 1 },
  /** The bright line along the edge: width in metres (never thinner than minPx), alpha 0–255 at full evidence. */
  core: { widthM: 1.6, minPx: 1.2, alpha: 120 },
  /** The soft glow around it, additive, with a falloff across its width (as the car halo). */
  halo: { widthM: 7, minPx: 4, alpha: 34 },
  /** Edges behind a building glow through it at this fraction (the cars use GLOW.throughBuildings). */
  throughBuildings: 0.3,
  /** Lift above the road (m), under the cars' CAR_LIFT_M so the cars draw over their road. */
  liftM: 0.25,
} as const;

export type SpeedClass = "flowing" | "slowing" | "jammed";

/** The traffic class of a speed_ratio (SPEED.thresholds); null for no data (NaN / not finite). */
export function speedClass(ratio: number): SpeedClass | null {
  if (!Number.isFinite(ratio)) return null;
  if (ratio >= SPEED.thresholds.flowing) return "flowing";
  if (ratio >= SPEED.thresholds.slowing) return "slowing";
  return "jammed";
}

/** Strength (0–1) of a speed colour backed by `vehicles` vehicles (SPEED.evidence); 0 with no vehicles. */
export function speedEvidence(vehicles: number): number {
  if (!(vehicles > 0)) return 0;
  const { minAlpha, fullAt } = SPEED.evidence;
  const t = Math.min(1, Math.max(0, (vehicles - 1) / (fullAt - 1)));
  return minAlpha + (1 - minAlpha) * t;
}

/** Building fill opacity range from the brief (15–30%), and the default. */
export const BUILDING_OPACITY = { min: 0.15, max: 0.3, default: 0.25 } as const;

export const palette = {
  /** Page background behind the map; the same near-black as the base map's ground (basemap.ground). */
  background: "#05070a",

  /**
   * Base map (src/map/style/twinDarkStyle.ts). Everything here must recede so the buildings and, later, the
   * traffic carry the light: near-black ground, a slightly lifted blue-black for water, roads and labels as faint
   * cool greys whose visibility comes from opacity, not brightness.
   */
  basemap: {
    ground: [5, 7, 10] as RGB,
    water: [9, 19, 32] as RGB,
    park: [9, 12, 12] as RGB,
    /** Roads, rail and labels share one cool grey; each class sets its own opacity below. */
    line: [150, 170, 195] as RGB,
    rail: 0.07,
    label: [160, 175, 195] as RGB,
    labelOpacity: { suburb: 0.5, road: 0.38 },
    labelHalo: [0, 0, 0] as RGB,
  },

  /** Building faces: cool pale blue-grey; alpha applied via buildingFill(). */
  buildingFill: [200, 220, 240] as RGB,
  /** Building rooflines in view: thin, bright, cool white. */
  buildingOutline: [215, 235, 255, 210] as RGBA,
  /** Alpha of rooflines hidden behind nearer buildings (the x-ray hint), 0–255. */
  hiddenRooflineAlpha: 60,
  /**
   * Alpha of the vertical corner edges on tall towers, 0–255: fainter than rooflines, so towers read as forms. One
   * silhouette line per outer corner through stacked tiers (George's pick; buildingsLayer.toTowerEdges).
   */
  towerEdgeAlpha: 120,

  /**
   * Elevated-road ribbons (bridges, viaducts, ramps): the buildings' ghost language. A faint translucent deck in the
   * building tone, bright thin edge lines, fainter edges where hidden behind buildings, faint drop-lines to ground.
   */
  roadDeck: [200, 220, 240, 60] as RGBA,
  roadEdge: [215, 235, 255, 210] as RGBA,
  roadEdgeHiddenAlpha: 45,
  roadFascia: [215, 235, 255, 90] as RGBA,
  roadDropLine: [215, 235, 255, 80] as RGBA,

  /**
   * X-ray tunnels (behind "Show tunnels"), George's "depth" pick at the Stage C look review: the elevated ribbons'
   * language, dimmer and cooler, seen through the ground, the water and the translucent city, with dashed edges. A dark
   * floor, a dashed ceiling `ceilingM` above it (so the ribbon reads as a tube in the ground, not a road on it), and a
   * curtain from the surface down to the tunnel to show its depth. The portal frame marks where a tunnel meets the
   * street. Low chroma (under the 0.25 ghost limit): tunnels are structure, not traffic.
   */
  roadTunnel: {
    deck: [150, 178, 212, 26] as RGBA,
    edge: [170, 198, 230, 190] as RGBA,
    portal: [185, 210, 240, 170] as RGBA,
    /** The depth curtain from the surface down to the tunnel: alpha at its bottom band (fades towards the surface). */
    curtain: [150, 178, 212, 80] as RGBA,
    /** Dotted trace of the tunnel on the surface above it (the top of the curtain). */
    trace: [175, 200, 232, 170] as RGBA,
    /** Height (m) of the dashed ceiling edges above the tunnel floor (clamped to the ground), drawn at ceilingAlpha. */
    ceilingM: 6,
    ceilingAlpha: 0.6,
  },

  /**
   * Dev-only `?heights=source` view (off by default): elevated roads by where their heights come from (z_source).
   * Measured heights keep the normal ribbon colours above. Estimated heights (and `ground`, and files without z_source)
   * use this dimmer, greyer variant with dashed edge and fascia lines. Still the ghost palette: low chroma, never saturated.
   */
  roadEstimated: {
    deck: [170, 185, 200, 30] as RGBA,
    edge: [170, 185, 200, 140] as RGBA,
    edgeHiddenAlpha: 30,
    fascia: [170, 185, 200, 60] as RGBA,
    /** Dash pattern along estimated edges, metres. */
    dashM: { on: 6, off: 5 },
  },

  /**
   * Traffic colours: cars (Phase 2, all "flowing" until speeds arrive) and road speed_ratio (Phase 3).
   * Blue = flowing, amber = slowing, red = jammed (George's call, PLAN.md Decisions log).
   * These are the ONLY saturated colours on screen.
   */
  traffic: {
    flowing: [56, 140, 255] as RGB,
    slowing: [255, 176, 32] as RGB,
    jammed: [235, 50, 50] as RGB,
    /** Hot centre of a car's head dot: the traffic colour lifted toward white (still the same hue). */
    headCoreLift: 0.55,
  },

  /** UI chrome (overlays, buttons): muted, unsaturated. */
  ui: {
    text: "#cfd8e3",
    textDim: "#7d8794",
    panel: "rgba(16, 20, 26, 0.78)",
    border: "rgba(200, 220, 240, 0.18)",
    accent: "#d7ebff",
    /** The test-fixture warning banner is deliberately loud: it must never be mistaken for real data. */
    warningBg: "#5c3d00",
    warningText: "#ffd27a",
  },
} as const;

/** Roofline colour for edges hidden behind other buildings: the outline colour, faint. */
export function hiddenRoofline(): RGBA {
  const [r, g, b] = palette.buildingOutline;
  return [r, g, b, palette.hiddenRooflineAlpha];
}

/**
 * Roofline opacity by zoom: zoomed out over thousands of small low-rise roofs, full-strength outlines turn into
 * bright speckle, so they fade from 100% at zoom 15.5 down to 30% at zoom 13.5 and below.
 */
export const ROOFLINE_FADE = { fullZoom: 15.5, fadedZoom: 13.5, minOpacity: 0.3 } as const;

/**
 * Outer-ring fade: rooflines of buildings outside the simulated area (SIM_BBOX) fade with distance from it, down to
 * minFactor at distanceM and beyond. The context ring stays legible but stops competing with the study area.
 */
export const OUTER_RING_FADE = { distanceM: 700, minFactor: 0.35 } as const;

/** Multiplier (minFactor–1) for a building `distanceM` metres outside SIM_BBOX (0 = inside). Smoothstep. */
export function outerRingFade(distanceM: number): number {
  const { distanceM: d0, minFactor } = OUTER_RING_FADE;
  const t = Math.min(1, Math.max(0, distanceM / d0));
  const s = t * t * (3 - 2 * t);
  return 1 - (1 - minFactor) * s;
}

/** Tower edges keep at least this opacity zoomed out, so the skyline stays solid at Overview. */
export const TOWER_EDGE_MIN_OPACITY = 0.6;

/** 0 at ROOFLINE_FADE.fadedZoom and below, 1 at fullZoom and above. */
export function rooflineZoomT(zoom: number): number {
  const { fullZoom, fadedZoom } = ROOFLINE_FADE;
  return Math.min(1, Math.max(0, (zoom - fadedZoom) / (fullZoom - fadedZoom)));
}

export function rooflineOpacity(zoom: number): number {
  const { minOpacity } = ROOFLINE_FADE;
  return minOpacity + (1 - minOpacity) * rooflineZoomT(zoom);
}

/**
 * Low-rise fade: zoomed out, thousands of small roofs are the speckle, so rooflines of buildings under highM fade
 * (down to minFactor at lowM and below). It disappears as you zoom in (fully gone at ROOFLINE_FADE.fullZoom).
 */
export const LOW_RISE_FADE = { lowM: 10, highM: 40, minFactor: 0.4 } as const;

export function lowRiseFade(heightM: number, zoom: number): number {
  const { lowM, highM, minFactor } = LOW_RISE_FADE;
  const t = Math.min(1, Math.max(0, (heightM - lowM) / (highM - lowM)));
  const byHeight = minFactor + (1 - minFactor) * t * t * (3 - 2 * t);
  return 1 - (1 - byHeight) * (1 - rooflineZoomT(zoom));
}

/** Clamp an opacity to the brief's 15–30% range. */
export function clampBuildingOpacity(opacity: number): number {
  if (!Number.isFinite(opacity)) return BUILDING_OPACITY.default;
  return Math.min(BUILDING_OPACITY.max, Math.max(BUILDING_OPACITY.min, opacity));
}

/** Building fill colour as RGBA at the given opacity (clamped to 15–30%). */
export function buildingFill(opacity: number = BUILDING_OPACITY.default): RGBA {
  const [r, g, b] = palette.buildingFill;
  return [r, g, b, Math.round(clampBuildingOpacity(opacity) * 255)];
}

/**
 * Chroma (0–1): how far a colour is from grey (max − min channel). A better "is it saturated?" measure than HSL
 * saturation, which calls pale tints saturated. Tests use it to keep the ghost city muted.
 */
export function chroma([r, g, b]: RGB | RGBA): number {
  return (Math.max(r, g, b) - Math.min(r, g, b)) / 255;
}

/** A palette RGB plus an opacity (0–1) as a CSS colour string, for the MapLibre style. */
export function cssRgba([r, g, b]: RGB, alpha = 1): string {
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
