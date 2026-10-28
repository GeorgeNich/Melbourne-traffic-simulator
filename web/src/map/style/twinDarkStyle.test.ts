import { validateStyleMin } from "@maplibre/maplibre-gl-style-spec";
import type { LayerSpecification, StyleSpecification } from "maplibre-gl";
import { describe, expect, it } from "vitest";
import { GROUND_ROADS, palette } from "../../theme/palette";
import { LABEL_LAYER_IDS, OPENFREEMAP, TILE_SOURCE_ID, twinDarkStyle } from "./twinDarkStyle";

const style = twinDarkStyle("credits");
const layer = (id: string) => style.layers.find((l) => l.id === id) as LayerSpecification & Record<string, unknown>;
const lum = ([r, g, b]: readonly number[]) => (r! + g! + b!) / 3;

describe("twin dark base style", () => {
  it("is a valid MapLibre style", () => {
    expect(validateStyleMin(style as Parameters<typeof validateStyleMin>[0])).toEqual([]);
  });

  it("uses OpenFreeMap tiles and fonts only: no other host, no key, no CARTO", () => {
    expect(Object.keys(style.sources)).toEqual([TILE_SOURCE_ID]);
    expect(style.sources[TILE_SOURCE_ID]).toMatchObject({ type: "vector", url: "https://tiles.openfreemap.org/planet" });
    expect(style.glyphs).toBe(OPENFREEMAP.glyphs);
    const urls = JSON.stringify(style).match(/https?:\/\/[^"\s]+/g) ?? [];
    expect(urls.length).toBeGreaterThan(0);
    for (const url of urls) expect(new URL(url.replace(/[{}]/g, "")).host).toBe("tiles.openfreemap.org");
    expect(JSON.stringify(style)).not.toMatch(/carto|key=|token=|access_token/i);
  });

  it("draws no icons: no sprite, no POIs, no transit icons, no house numbers", () => {
    expect(style.sprite).toBeUndefined();
    for (const l of style.layers) {
      expect(JSON.stringify(l)).not.toMatch(/icon-image|fill-pattern|line-pattern/);
      if ("source-layer" in l) expect(["poi", "housenumber", "aerodrome_label", "mountain_peak"]).not.toContain(l["source-layer"]);
    }
  });

  it("labels only suburbs and a few major road names, at mid zoom, small and dim", () => {
    const symbols = style.layers.filter((l) => l.type === "symbol");
    expect(symbols.map((l) => l.id)).toEqual([...LABEL_LAYER_IDS]);
    expect(layer("label_suburb")["source-layer"]).toBe("place");
    expect(JSON.stringify(layer("label_suburb").filter)).toContain('"suburb"');
    expect(JSON.stringify(layer("label_road_major").filter)).toContain('["motorway","trunk","primary"]');
    expect(JSON.stringify(layer("label_road_major").filter)).toContain('"Ramp"');
    for (const l of symbols) {
      const s = l as LayerSpecification & { minzoom: number; maxzoom: number; layout: { "text-size": number } };
      expect(s.minzoom).toBeGreaterThanOrEqual(12);
      expect(s.maxzoom).toBeLessThanOrEqual(16.5);
      expect(s.layout["text-size"]).toBeLessThanOrEqual(10);
    }
    for (const o of Object.values(palette.basemap.labelOpacity)) expect(o).toBeLessThanOrEqual(0.5);
  });

  it("draws labels last, so deck.gl's interleaved overlay (appended on top) draws buildings over them", () => {
    const ids = style.layers.map((l) => l.id);
    expect(ids.slice(-LABEL_LAYER_IDS.length)).toEqual([...LABEL_LAYER_IDS]);
  });

  it("near-black ground, water a touch lifted and blue-black, parks barely there", () => {
    const { ground, water, park } = palette.basemap;
    expect(Math.max(...ground)).toBeLessThanOrEqual(12);
    expect(lum(water)).toBeGreaterThan(lum(ground));
    expect(water[2]).toBe(Math.max(...water)); // blue dominates
    expect(Math.max(...water)).toBeLessThanOrEqual(40);
    expect(lum(park) - lum(ground)).toBeGreaterThan(0);
    expect(lum(park) - lum(ground)).toBeLessThan(6);
    expect(layer("background").paint).toEqual({ "background-color": "rgba(5, 7, 10, 1)" });
  });

  it("ground roads at the picked \"medium\" level: motorways > major > tertiary > minor, still a base map; no tunnels", () => {
    const { minor, tertiary, major, motorway } = GROUND_ROADS.opacity;
    expect(minor).toBeLessThan(tertiary);
    expect(tertiary).toBeLessThan(major);
    expect(major).toBeLessThan(motorway);
    expect(motorway).toBeLessThanOrEqual(0.5); // still a base map, not traffic
    expect(GROUND_ROADS.majorWidthScale).toBeGreaterThan(1); // major roads a little wider
    const paint = (id: string) => (layer(id) as { paint: Record<string, unknown> }).paint;
    expect(JSON.stringify(paint("road_major")["line-color"])).toContain(`${major})`);
    expect(JSON.stringify(paint("road_minor")["line-color"])).toContain(`${minor})`);
    for (const id of ["road_minor", "road_tertiary", "road_major", "road_motorway", "rail"]) {
      expect(JSON.stringify(layer(id).filter)).toContain('["!=",["get","brunnel"],"tunnel"]');
    }
  });

  it("keeps the base map unsaturated (traffic owns saturation)", () => {
    const { ground, water, park, line, label } = palette.basemap;
    for (const c of [ground, water, park, line, label]) {
      expect((Math.max(...c) - Math.min(...c)) / 255).toBeLessThan(0.2);
    }
  });

  it("returns a fresh object each call", () => {
    const a: StyleSpecification = twinDarkStyle("x");
    a.layers.pop();
    expect(twinDarkStyle("x").layers.length).toBe(style.layers.length);
  });
});
