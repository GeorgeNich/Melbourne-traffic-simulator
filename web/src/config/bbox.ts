// Study-area bounding boxes, copied from pipeline/config.py (the single source of truth).
// src/config/bbox.test.ts parses pipeline/config.py and fails if these numbers drift from it.

export interface BBox {
  west: number;
  south: number;
  east: number;
  north: number;
}

/** CBD (Hoddle Grid) + Southbank + Docklands: the simulated road network. = pipeline/config.py SIM_BBOX */
export const SIM_BBOX: BBox = { west: 144.935, south: -37.833, east: 144.975, north: -37.805 };

/** Wider ring rendered as ghost buildings only. = pipeline/config.py BUILDINGS_BBOX */
export const BUILDINGS_BBOX: BBox = { west: 144.925, south: -37.84, east: 144.99, north: -37.795 };

/**
 * Envelope of the building area (the City of Melbourne boundary UNION BUILDINGS_BBOX, Phase 2 stacked tiers): every
 * building piece lies inside it. = pipeline/config.py BUILDINGS_AREA_BBOX
 */
export const BUILDINGS_AREA_BBOX: BBox = { west: 144.896, south: -37.851, east: 144.992, north: -37.775 };

/**
 * SIM_BBOX grown by 300 m on every side: the clip for the road extract (osm_sim_area.osm.gz, roads_3d.geojson).
 * = pipeline/config.py SIM_EXTRACT_BBOX = buffered(SIM_BBOX, SIM_BUFFER_M); bbox.test.ts recomputes it from there.
 */
export const SIM_EXTRACT_BBOX: BBox = {
  west: 144.9315884832548,
  south: -37.83569493352497,
  east: 144.97841151674518,
  north: -37.80230506647503,
};

/**
 * The clip for CityLink motorway TUNNEL ways only (roads_3d `tunnel: true`, `highway` motorway / motorway_link):
 * SIM_EXTRACT_BBOX with its east edge moved out to BUILDINGS_BBOX.east, so the Burnley and Domain tunnels include
 * their crossings under the Yarra. Every other way keeps SIM_EXTRACT_BBOX.
 * = pipeline/config.py TUNNEL_EXTRACT_BBOX; bbox.test.ts checks its definition there.
 */
export const TUNNEL_EXTRACT_BBOX: BBox = { ...SIM_EXTRACT_BBOX, east: BUILDINGS_BBOX.east };

/** Highway classes that get TUNNEL_EXTRACT_BBOX when tunnel=true (pipeline/config.py, Stage B). */
export const EXTENDED_CLIP_HIGHWAYS: readonly string[] = ["motorway", "motorway_link"];

export function bboxContainsPoint(b: BBox, lon: number, lat: number): boolean {
  return lon >= b.west && lon <= b.east && lat >= b.south && lat <= b.north;
}
