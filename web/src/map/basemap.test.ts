import { describe, expect, it } from "vitest";
import { ATTRIBUTION_HTML, basemapStyle, COM_ATTRIBUTION_HTML, COM_ATTRIBUTION_TEXT } from "./basemap";
import { TILE_SOURCE_ID } from "./style/twinDarkStyle";

describe("basemap", () => {
  it("credits OpenFreeMap, OpenMapTiles and OpenStreetMap contributors", () => {
    expect(ATTRIBUTION_HTML).toContain("OpenFreeMap");
    expect(ATTRIBUTION_HTML).toContain("© OpenMapTiles");
    expect(ATTRIBUTION_HTML).toContain("© OpenStreetMap contributors");
    expect(ATTRIBUTION_HTML).toContain("https://www.openstreetmap.org/copyright");
  });

  it("credits City of Melbourne building and road heights with the exact text and the CC BY 4.0 link", () => {
    expect(COM_ATTRIBUTION_TEXT).toBe("Building and road heights: Contains City of Melbourne Open Data (CC BY 4.0), modified");
    expect(COM_ATTRIBUTION_HTML).toContain('href="https://creativecommons.org/licenses/by/4.0/"');
    expect(COM_ATTRIBUTION_HTML).toContain(`>${COM_ATTRIBUTION_TEXT}</a>`);
  });

  it("puts the credits on the OpenFreeMap tile source, which MapLibre's attribution control shows", () => {
    const style = basemapStyle();
    expect(style.sources[TILE_SOURCE_ID]).toMatchObject({ type: "vector", attribution: ATTRIBUTION_HTML });
  });

  it("draws no base-map buildings: the extruded ghost buildings are the only buildings", () => {
    const layers = basemapStyle().layers;
    expect(layers.some((l) => "source-layer" in l && l["source-layer"] === "building")).toBe(false);
  });
});
