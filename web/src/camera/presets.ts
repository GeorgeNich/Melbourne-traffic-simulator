// Camera presets. Bearings follow the Hoddle Grid / river so the skyline reads well; pitch 50–60° for depth.

export interface CameraView {
  longitude: number;
  latitude: number;
  zoom: number;
  pitch: number;
  bearing: number;
}

export type PresetId = "cbd" | "southbank" | "docklands" | "interchange" | "footscray" | "burnley" | "overview";

export interface CameraPreset {
  id: PresetId;
  label: string;
  view: CameraView;
}

export const CAMERA_PRESETS: readonly CameraPreset[] = [
  {
    id: "cbd",
    label: "CBD",
    // Hoddle Grid centre (around Collins / Elizabeth St); looking up the grid from the south-west.
    view: { longitude: 144.9628, latitude: -37.8145, zoom: 15.3, pitch: 55, bearing: -20 },
  },
  {
    id: "southbank",
    label: "Southbank",
    // Southbank towers south of the Yarra; looking north-east toward the river and CBD. Framed so the tops of
    // Australia 108 (317 m) and Eureka Tower (298 m) sit well inside the frame (presets.test.ts checks this).
    view: { longitude: 144.9645, latitude: -37.8219, zoom: 16, pitch: 55, bearing: 30 },
  },
  {
    id: "docklands",
    label: "Docklands",
    // Victoria Harbour / Docklands; looking east back toward the CBD.
    view: { longitude: 144.9445, latitude: -37.8175, zoom: 15.3, pitch: 52, bearing: 70 },
  },
  {
    id: "interchange",
    label: "Interchange",
    // Kings Way / West Gate Freeway interchange: low and steep, looking west along the West Gate decks where the
    // Kings Way bridge and its ramps cross them, so the stacked decks read against each other.
    view: { longitude: 144.9606, latitude: -37.8275, zoom: 17.1, pitch: 68, bearing: -100 },
  },
  {
    id: "footscray",
    label: "Footscray Rd",
    // West Gate Tunnel / CityLink viaducts over Footscray Road at the north-west corner (decks up to 26 m),
    // seen from the simulated area's edge looking west-south-west, low and steep.
    view: { longitude: 144.9352, latitude: -37.8062, zoom: 16.8, pitch: 68, bearing: -120 },
  },
  {
    id: "burnley",
    label: "Burnley tunnel",
    // X-ray close-up of the CityLink Burnley (eastbound, to -30 m) and Domain (westbound) tunnels where they pass
    // under the Yarra (~144.978-144.980), east of the simulated area but inside the building ring. From the north
    // bank looking south-south-east along the river, close and steep, so both tunnels cross under the water with their
    // depth curtains hanging from the dotted surface traces (30 m reads as a visible drop at this zoom).
    view: { longitude: 144.9786, latitude: -37.8252, zoom: 17.2, pitch: 65, bearing: 160 },
  },
  {
    id: "overview",
    label: "Overview",
    // The whole building ring (BUILDINGS_BBOX centre), tilted, looking up the Hoddle Grid.
    view: { longitude: 144.9575, latitude: -37.8175, zoom: 13.9, pitch: 45, bearing: -20 },
  },
];

export const DEFAULT_PRESET: PresetId = "southbank";

export function getPreset(id: PresetId): CameraPreset {
  const p = CAMERA_PRESETS.find((c) => c.id === id);
  if (!p) throw new Error(`Unknown camera preset: ${id}`);
  return p;
}

/** Fly-to duration (ms) between presets. */
export const FLY_DURATION_MS = 2400;

/** Zoom and pitch limits for free navigation. */
export const VIEW_LIMITS = { minZoom: 12, maxZoom: 19, maxPitch: 75 } as const;
