// The ghost city's own dark base style: a pared-down, committed derivative of OpenFreeMap's "dark" style
// (https://tiles.openfreemap.org/styles/dark, fetched 2026-10-26), using OpenFreeMap's vector tiles and fonts only.
// No other tile host, no key, no sprite (no icons are drawn).
//
// Why committed code rather than patching the fetched style at runtime:
// - the look is pinned: an upstream edit to OpenFreeMap's style can't silently bring back POIs, casings or grey;
// - every colour comes from theme/palette.ts, so the base map is tuned in the same one place as everything else;
// - it is type-checked against MapLibre's StyleSpecification and validated by the style-spec in unit tests;
// - one network round trip fewer before the map can start (MapLibre gets the style object directly).
// The cost is that we don't pick up upstream fixes automatically; the layer set is small enough to review by hand.
//
// Layer order (bottom to top): ground, water, parks, piers, roads, rail, labels. deck.gl's interleaved overlay is
// appended after the last style layer, so every label is drawn UNDER the ghost buildings (and later the traffic).

import type { ExpressionSpecification, LayerSpecification, StyleSpecification } from "maplibre-gl";
import { cssRgba, GROUND_ROADS, palette } from "../../theme/palette";

/** OpenFreeMap endpoints: the only hosts this style talks to. */
export const OPENFREEMAP = {
  tilejson: "https://tiles.openfreemap.org/planet",
  glyphs: "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf",
} as const;

/** The vector source id (same as OpenFreeMap's styles). */
export const TILE_SOURCE_ID = "openmaptiles";

const FONT = ["Noto Sans Regular"];
const bm = palette.basemap;

const isLine: ExpressionSpecification = ["match", ["geometry-type"], ["LineString", "MultiLineString"], true, false];
const isArea: ExpressionSpecification = ["match", ["geometry-type"], ["Polygon", "MultiPolygon"], true, false];
/** Tunnels are left out of the base map: Stage B draws them X-ray style, behind a toggle. */
const notTunnel: ExpressionSpecification = ["!=", ["get", "brunnel"], "tunnel"];
const name: ExpressionSpecification = ["coalesce", ["get", "name_en"], ["get", "name"]];

/** Line width (px) by zoom, exponential so roads stay hairlines zoomed out and thicken gently close up. */
const width = (z12: number, z16: number, z19: number): ExpressionSpecification => [
  "interpolate", ["exponential", 1.6], ["zoom"], 12, z12, 16, z16, 19, z19,
];

const roadClasses = {
  minor: ["minor", "service"],
  tertiary: ["tertiary"],
  major: ["primary", "secondary", "trunk"],
  motorway: ["motorway"],
} as const;

function roadLayer(id: keyof typeof roadClasses, lineWidth: ExpressionSpecification, opacity: number): LayerSpecification {
  return {
    id: `road_${id}`,
    type: "line",
    source: TILE_SOURCE_ID,
    "source-layer": "transportation",
    filter: ["all", isLine, notTunnel, ["match", ["get", "class"], [...roadClasses[id]], true, false]],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": cssRgba(bm.line, opacity), "line-width": lineWidth },
  };
}

const labelPaint = (opacity: number) => ({
  "text-color": cssRgba(bm.label, opacity),
  "text-halo-color": cssRgba(bm.labelHalo, 0.8),
  "text-halo-width": 1,
  "text-halo-blur": 1,
});

export const LABEL_LAYER_IDS = ["label_road_major", "label_suburb"] as const;

export function twinDarkStyle(attributionHtml: string): StyleSpecification {
  const { opacity: roads, majorWidthScale: k } = GROUND_ROADS;
  return {
    version: 8,
    name: "Melbourne Traffic Simulator dark (from OpenFreeMap dark)",
    sources: {
      [TILE_SOURCE_ID]: { type: "vector", url: OPENFREEMAP.tilejson, attribution: attributionHtml },
    },
    glyphs: OPENFREEMAP.glyphs,
    layers: [
      { id: "background", type: "background", paint: { "background-color": cssRgba(bm.ground) } },
      {
        id: "water",
        type: "fill",
        source: TILE_SOURCE_ID,
        "source-layer": "water",
        filter: ["all", isArea, notTunnel],
        paint: { "fill-color": cssRgba(bm.water), "fill-antialias": true },
      },
      {
        id: "waterway",
        type: "line",
        source: TILE_SOURCE_ID,
        "source-layer": "waterway",
        filter: isLine,
        paint: { "line-color": cssRgba(bm.water), "line-width": width(0.5, 1.5, 4) },
      },
      {
        id: "park",
        type: "fill",
        source: TILE_SOURCE_ID,
        "source-layer": "landuse",
        filter: ["all", isArea, ["==", ["get", "class"], "park"]],
        paint: { "fill-color": cssRgba(bm.park) },
      },
      // Piers (Docklands, Station Pier) sit over water; draw them in the ground colour so they read as land.
      {
        id: "pier_area",
        type: "fill",
        source: TILE_SOURCE_ID,
        "source-layer": "transportation",
        filter: ["all", isArea, ["==", ["get", "class"], "pier"]],
        paint: { "fill-color": cssRgba(bm.ground) },
      },
      roadLayer("minor", width(0.25, 0.7, 2.5), roads.minor),
      roadLayer("tertiary", width(0.5, 1.3, 4.5), roads.tertiary),
      roadLayer("major", width(0.5 * k, 1.3 * k, 4.5 * k), roads.major),
      roadLayer("motorway", width(0.7 * k, 1.8 * k, 6 * k), roads.motorway),
      {
        id: "rail",
        type: "line",
        source: TILE_SOURCE_ID,
        "source-layer": "transportation",
        minzoom: 13,
        filter: ["all", isLine, notTunnel, ["==", ["get", "class"], "rail"], ["!", ["has", "service"]]],
        layout: { "line-join": "round" },
        paint: { "line-color": cssRgba(bm.line, bm.rail), "line-width": width(0.4, 0.8, 2) },
      },
      // A few major road names (freeways, trunk and primary roads, no ramps), mid zoom only. Small, dim, wide-spaced.
      {
        id: "label_road_major",
        type: "symbol",
        source: TILE_SOURCE_ID,
        "source-layer": "transportation_name",
        minzoom: 14,
        maxzoom: 16.5,
        filter: [
          "all",
          isLine,
          notTunnel,
          ["match", ["get", "class"], ["motorway", "trunk", "primary"], true, false],
          // Freeway ramps carry long names ("West Gate In - Kings Ramp Off"); leave them out.
          ["!", ["in", "Ramp", ["coalesce", ["get", "name"], ""]]],
        ],
        layout: {
          "symbol-placement": "line",
          "symbol-spacing": 600,
          "text-field": name,
          "text-font": FONT,
          "text-size": 9,
          "text-letter-spacing": 0.1,
          "text-transform": "uppercase",
          "text-max-angle": 25,
          "text-rotation-alignment": "map",
          "text-pitch-alignment": "viewport",
        },
        paint: labelPaint(bm.labelOpacity.road),
      },
      // Suburb names (Melbourne, Southbank, Docklands, …), zoomed out to mid zoom only.
      {
        id: "label_suburb",
        type: "symbol",
        source: TILE_SOURCE_ID,
        "source-layer": "place",
        minzoom: 12,
        maxzoom: 15.5,
        filter: ["all", ["match", ["geometry-type"], ["Point", "MultiPoint"], true, false], ["==", ["get", "class"], "suburb"]],
        layout: {
          "text-field": name,
          "text-font": FONT,
          "text-size": 10,
          "text-letter-spacing": 0.25,
          "text-transform": "uppercase",
        },
        paint: labelPaint(bm.labelOpacity.suburb),
      },
    ],
  };
}
