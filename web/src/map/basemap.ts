// Base map: OpenFreeMap (https://openfreemap.org) tiles and fonts. No API key, no registration, no usage limits,
// commercial use allowed. The style itself is our own pared-down derivative of OpenFreeMap's dark style
// (./style/twinDarkStyle.ts). Attribution is required and is shown on the map by MapLibre's attribution control.
// Never CARTO or any style that needs a key (CLAUDE.md, PLAN.md Decisions log).

import type { StyleSpecification } from "maplibre-gl";
import { twinDarkStyle } from "./style/twinDarkStyle";

const link = (href: string, text: string) => `<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`;

/**
 * Attribution shown on the map. OpenFreeMap's TileJSON says "OpenFreeMap © OpenMapTiles Data from OpenStreetMap";
 * we keep all three credits and use the OSM wording asked for in the brief: "© OpenStreetMap contributors".
 */
export const ATTRIBUTION_HTML = [
  link("https://openfreemap.org", "OpenFreeMap"),
  link("https://www.openmaptiles.org/", "© OpenMapTiles"),
  link("https://www.openstreetmap.org/copyright", "© OpenStreetMap contributors"),
].join(" ");

export const ATTRIBUTION_TEXT = "OpenFreeMap © OpenMapTiles © OpenStreetMap contributors";

/**
 * Credit for City of Melbourne Open Data (CC BY 4.0): building heights (2023 Building Footprints) and measured road
 * deck heights (3D Point Cloud 2018). One line covers both. Shown by the attribution control next to the base-map credits.
 */
export const COM_ATTRIBUTION_TEXT = "Building and road heights: Contains City of Melbourne Open Data (CC BY 4.0), modified";
export const CC_BY_4_URL = "https://creativecommons.org/licenses/by/4.0/";
export const COM_ATTRIBUTION_HTML = link(CC_BY_4_URL, COM_ATTRIBUTION_TEXT);

/** The base map style: our committed dark style on OpenFreeMap's tiles, with the credits above on its source. */
export function basemapStyle(): StyleSpecification {
  return twinDarkStyle(ATTRIBUTION_HTML);
}
