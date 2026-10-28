#!/usr/bin/env node
// Writes the LABELLED TEST FIXTURE tests/fixtures/trips_fixture.json + trips_fixture.meta.json (ownership exception:
// frontend-engineer may write tests/fixtures/, CLAUDE.md). Hand-picked routes over real roads from
// data/processed/roads_3d.geojson (z included, e.g. the Kings Way flyover and the Bolte Bridge), timed at free-flow
// class speeds with a few signal stops. NOT simulated traffic: it only exercises the renderer before sim-engineer's
// sim/out/trips_baseline.json exists. Ids start with "fixture-", meta.demand is "test_fixture", meta has
// `_test_fixture: true`. Opt-in only (TWIN_USE_FIXTURE=1 or =trips in dev); a build refuses it.
//
// The two Domain Tunnel cars drive the CityLink Domain Tunnel westbound at the REAL roads_3d depth (Stage B: down to
// -30 m under the Yarra), ending at its western portal. Only the routes marked `tunnel` may go below ground, and only
// on tunnel ways: the other routes avoid the open CityLink cutting and the City Road underpass (main() checks this,
// as tests/contracts/test_trips_fixture_roads.py does).
//
// Run (from web/): node scripts/make-trips-fixture.mjs
import { readFileSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { buildGraph, direction, distanceM, mulberry32, nearestVertex, readOsmExtras, roundStrict, routeVia, timeRoute } from "./lib/road-graph.mjs";

const WEB_DIR = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const ROOT = resolve(WEB_DIR, "..");
const ROADS = resolve(ROOT, "data/processed/roads_3d.geojson");
const OSM = resolve(ROOT, "data/processed/osm_sim_area.osm.gz");
const OUT_TRIPS = resolve(ROOT, "tests/fixtures/trips_fixture.json");
const OUT_META = resolve(ROOT, "tests/fixtures/trips_fixture.meta.json");

export const SEED = 20261027;
export const DURATION_S = 300;
/** Below this z (m) a fixture car counts as underground (= web TUNNEL_Z_M and the qa fixture test). */
export const UNDERGROUND_Z_M = -0.5;
/** Chance of stopping at a traffic signal (non-motorway), and the stop length range (s). */
const SIGNAL_STOP = { chance: 0.35, minS: 6, maxS: 22 };

/**
 * Routes: each waypoint is { near: [lon, lat], name?, heading? } (the nearest vertex on a way with that name, else
 * on any non-service road; with `heading`, only where that way leaves the vertex heading that way, which picks the
 * right carriageway of a divided road) or { way, at } (a vertex of that OSM way: "start", "mid" or "end").
 * `starts` are departure times (s); one car per start.
 * @typedef {"north" | "south" | "east" | "west"} Heading
 * @typedef {{ near: [number, number], name?: string, heading?: Heading } | { way: number, at: "start" | "mid" | "end" }} Waypoint
 * @type {{ name: string, waypoints: Waypoint[], starts: number[], tunnel?: boolean }[]}
 */
const ROUTES = [
  // Kings Way flyover over the West Gate Freeway / Power St, both directions (measured deck heights ~10-11 m).
  {
    name: "kings-way-north",
    waypoints: [{ near: [144.9679, -37.8333], name: "Kings Way", heading: "north" }, { way: 11589928, at: "mid" }, { way: 23212198, at: "mid" }, { near: [144.9575, -37.8205], name: "Kings Way", heading: "north" }],
    starts: [0, 70, 150, 225],
  },
  {
    name: "kings-way-south",
    waypoints: [{ near: [144.9575, -37.8205], name: "Kings Way", heading: "south" }, { way: 11589924, at: "mid" }, { way: 23212191, at: "mid" }, { near: [144.9679, -37.8333], name: "Kings Way", heading: "south" }],
    starts: [20, 100, 185],
  },
  // West Gate Freeway through the Kings Way interchange, both directions. Eastbound ends on the freeway before the
  // CityLink cutting (below ground since Stage B, and not a tunnel).
  {
    name: "west-gate-east",
    waypoints: [{ way: 285898792, at: "start" }, { way: 5634370, at: "end" }, { way: 321793268, at: "end" }],
    starts: [10, 60, 120, 180],
  },
  {
    name: "west-gate-west",
    waypoints: [{ way: 50123947, at: "start" }, { way: 50123947, at: "end" }, { near: [144.9330, -37.8265], name: "West Gate Freeway", heading: "west" }],
    starts: [35, 95, 160, 215],
  },
  // CityLink over the Bolte Bridge (measured deck up to ~33 m), both directions.
  {
    name: "bolte-north",
    waypoints: [{ way: 34314083, at: "start" }, { way: 34314083, at: "end" }, { way: 1463060691, at: "end" }],
    starts: [5, 90, 200],
  },
  {
    name: "bolte-south",
    waypoints: [{ near: [144.9345, -37.8045], name: "CityLink", heading: "south" }, { way: 5607536, at: "start" }, { way: 5607536, at: "end" }],
    starts: [45, 140, 250],
  },
  // Domain Tunnel westbound (CityLink) at its real roads_3d depth (-30 m): the X-ray tunnel test. It starts deep
  // underground just inside SIM_EXTRACT_BBOX (the trips contract's extent; the tunnel itself now reaches further
  // east, under the Yarra, in TUNNEL_EXTRACT_BBOX) and ends at the western portal (way 26619050's end), where the
  // open cutting begins.
  {
    name: "domain-tunnel-west",
    waypoints: [{ near: [144.9775, -37.82575], name: "CityLink", heading: "west" }, { way: 8066126, at: "end" }, { way: 26619050, at: "end" }],
    starts: [15, 150],
    tunnel: true,
  },
  // Hoddle Grid (CBD preset).
  {
    name: "flinders-st-east",
    waypoints: [[144.9544, -37.8212], [144.96, -37.8195], [144.9654, -37.818], [144.9717, -37.8161]].map((p) => ({ near: p, name: "Flinders Street", heading: "east" })),
    starts: [0, 55, 130, 210],
  },
  {
    name: "william-st-north",
    waypoints: [[144.9592, -37.8177], [144.9572, -37.8133], [144.9548, -37.8081]].map((p) => ({ near: p, name: "William Street", heading: "north" })),
    starts: [25, 120, 205],
  },
  {
    name: "la-trobe-st-west",
    waypoints: [[144.9679, -37.8085], [144.9615, -37.8103], [144.9519, -37.8131]].map((p) => ({ near: p, name: "La Trobe Street", heading: "west" })),
    starts: [40, 160],
  },
  {
    name: "elizabeth-st-south",
    waypoints: [[144.9604, -37.8081], [144.9626, -37.8129], [144.9649, -37.8181]].map((p) => ({ near: p, name: "Elizabeth Street", heading: "south" })),
    starts: [30, 175],
  },
  {
    name: "spencer-st-south",
    waypoints: [[144.9501, -37.8105], [144.9523, -37.8153], [144.9549, -37.8209]].map((p) => ({ near: p, name: "Spencer Street", heading: "south" })),
    starts: [65, 190],
  },
  // Docklands and Southbank.
  {
    name: "wurundjeri-way-north",
    // Starts north of the short Wurundjeri Way tunnel under Flinders St (below ground since Stage B).
    waypoints: [[144.9499, -37.8185], [144.9496, -37.8169], [144.9452, -37.8122], [144.9379, -37.8066]].map((p) => ({ near: p, name: "Wurundjeri Way", heading: "north" })),
    starts: [10, 135],
  },
  {
    name: "city-rd-southbank",
    waypoints: [
      ...[[144.9553, -37.83], [144.961, -37.8251], [144.9648, -37.8224]].map((p) => ({ near: p, name: "City Road", heading: "east" })),
      { near: [144.9683, -37.8236], name: "Southbank Boulevard", heading: "east" },
    ],
    starts: [50, 170],
  },
];

/** Waypoints must snap within this distance (m) of where they are placed. */
const MAX_SNAP_M = 120;

/** Unit direction of each heading, [east, north]. */
const HEADINGS = { north: [0, 1], south: [0, -1], east: [1, 0], west: [-1, 0] };

function main() {
  const roads = JSON.parse(readFileSync(ROADS, "utf8"));
  const { oneway, signalKeys } = readOsmExtras(OSM);
  const g = buildGraph(roads.features, oneway, signalKeys);
  const rng = mulberry32(SEED);

  /** True if the edge from vertex `from` points within 60 degrees of the heading. */
  const heads = (/** @type {import("./lib/road-graph.mjs").GraphEdge} */ e, /** @type {number} */ from, /** @type {Heading} */ h) => {
    const [dx, dy] = direction(g.coords[from], g.coords[e.to]);
    const [hx, hy] = HEADINGS[h];
    return dx * hx + dy * hy >= 0.5;
  };
  /** @param {Waypoint} w */
  const waypoint = (w) => {
    if ("near" in w) {
      const v = nearestVertex(g, w.near, (e, from) => {
        if (w.name ? e.name !== w.name : ["service", "living_street"].includes(e.highway)) return false;
        return !w.heading || heads(e, from, w.heading);
      });
      const d = v < 0 ? Infinity : distanceM(g.coords[v], w.near);
      if (d > MAX_SNAP_M) throw new Error(`waypoint ${JSON.stringify(w)} snaps ${Math.round(d)} m away`);
      return v;
    }
    const ids = g.wayVertices.get(w.way);
    if (!ids) throw new Error(`way ${w.way} is not in roads_3d.geojson`);
    return w.at === "start" ? ids[0] : w.at === "end" ? ids[ids.length - 1] : ids[Math.floor(ids.length / 2)];
  };

  const trips = [];
  let tunnelVehicles = 0;
  for (const r of ROUTES) {
    let route;
    try {
      route = routeVia(g, r.waypoints.map(waypoint));
    } catch (err) {
      throw new Error(`route ${r.name}: ${err instanceof Error ? err.message : String(err)}`, { cause: err });
    }
    if (r.tunnel && !route.edges.some((e) => e.tunnel)) throw new Error(`route ${r.name} has no tunnel edges`);
    // Only tunnel routes go underground, and only on tunnel ways (a vertex of a tunnel edge).
    route.vertices.forEach((v, k) => {
      if (g.coords[v][2] >= UNDERGROUND_Z_M) return;
      const onTunnel = [route.edges[k - 1], route.edges[k]].some((e) => e?.tunnel);
      if (!r.tunnel || !onTunnel) {
        throw new Error(`route ${r.name} goes below ${UNDERGROUND_Z_M} m (z ${g.coords[v][2]}) ${r.tunnel ? "off a tunnel way" : "but is not a tunnel route"}`);
      }
    });
    r.starts.forEach((startS, n) => {
      const stops = new Map();
      route.vertices.forEach((v, k) => {
        const e = route.edges[k - 1];
        if (k > 0 && g.signals.has(v) && e && !e.highway.startsWith("motorway") && rng() < SIGNAL_STOP.chance) {
          stops.set(k, Math.round(SIGNAL_STOP.minS + rng() * (SIGNAL_STOP.maxS - SIGNAL_STOP.minS)));
        }
      });
      const speedFactor = 0.85 + rng() * 0.2;
      const timed = timeRoute(g, route, {
        startS,
        speedFactor,
        stops,
      });
      // Cut at the end of the scenario.
      let end = timed.timestamps.findIndex((t) => t > DURATION_S);
      if (end === -1) end = timed.timestamps.length;
      if (end < 2) return;
      trips.push({
        id: `fixture-${r.name}-${n + 1}`,
        type: "passenger",
        path: timed.path.slice(0, end).map(([lon, lat, z]) => [lon, lat, Math.round(z * 100) / 100]),
        timestamps: roundStrict(timed.timestamps.slice(0, end)),
      });
      if (r.tunnel) tunnelVehicles++;
    });
  }

  const meta = {
    scenario: "fixture",
    demand: "test_fixture",
    duration_s: DURATION_S,
    seed: SEED,
    sumo_version: "none: hand-made test fixture, not a SUMO run",
    vehicles: trips.length,
    teleports: 0,
    tunnel_vehicles: tunnelVehicles,
    generated_at: new Date().toISOString().replace(/\.\d+Z$/, "Z"),
    _test_fixture: true,
    _notes: [
      "TEST FIXTURE, not simulated traffic: hand-picked routes over real roads_3d.geojson ways, timed at free-flow class speeds with random signal stops (web/scripts/make-trips-fixture.mjs, seeded).",
      "Every z is the roads_3d.geojson vertex height (Stage B). The Domain Tunnel cars (fixture-domain-tunnel-west-*) drive the CityLink Domain Tunnel westbound at its real roads_3d depth (-30 m, estimated in roads_3d), from deep underground at the SIM_EXTRACT_BBOX east edge to the western portal, to exercise the X-ray tunnel styling; no other car goes below ground.",
      "Dev/test only (TWIN_USE_FIXTURE=1 or =trips). Never bundled into a build.",
    ],
  };

  writeFileSync(OUT_TRIPS, JSON.stringify(trips) + "\n");
  writeFileSync(OUT_META, JSON.stringify(meta, null, 2) + "\n");
  const points = trips.reduce((s, t) => s + t.path.length, 0);
  const lengthKm = trips.reduce((s, t) => s + t.path.slice(1).reduce((a, p, i) => a + distanceM(t.path[i], p), 0), 0) / 1000;
  const maxZ = Math.max(...trips.flatMap((t) => t.path.map((p) => p[2])));
  const minZ = Math.min(...trips.flatMap((t) => t.path.map((p) => p[2])));
  console.log(
    `[trips-fixture] ${trips.length} cars, ${points} points, ${lengthKm.toFixed(1)} km, z ${minZ}..${maxZ} m, ` +
      `${tunnelVehicles} tunnel cars -> ${OUT_TRIPS}`,
  );
}

main();
