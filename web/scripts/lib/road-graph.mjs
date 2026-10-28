// A small routable graph over data/processed/roads_3d.geojson, for TEST DATA ONLY: the labelled trips fixture
// (scripts/make-trips-fixture.mjs) and the synthetic stress-test trips (scripts/make-stress-trips.mjs).
// Real traffic always comes from SUMO via sim/out/trips_<scenario>.json; nothing here is shipped or used by the app.
//
// Ways meet at shared OSM nodes, which have identical [lon, lat] in roads_3d (densified vertices are unique to their
// way), so vertices are joined by exact coordinate. One-way streets come from the OSM `oneway` tag in
// data/processed/osm_sim_area.osm.gz (motorways and roundabouts imply one-way, as in OSM), so test cars never drive
// against the traffic. Plain ESM JavaScript so Node runs it directly; types come from JSDoc.

import { readFileSync } from "node:fs";
import { gunzipSync } from "node:zlib";

/** @typedef {[number, number, number]} Position3 */
/** @typedef {{ to: number, lengthM: number, wayId: number, highway: string, name: string | null, tunnel: boolean }} GraphEdge */
/**
 * @typedef {object} RoadGraph
 * @property {Position3[]} coords    vertex positions [lon, lat, z_m]
 * @property {GraphEdge[][]} out     outgoing edges per vertex
 * @property {Set<number>} signals   vertices at OSM highway=traffic_signals nodes
 * @property {Map<number, number[]>} wayVertices  vertex indices of each way, in OSM order
 */

const M_PER_DEG_LAT = 110_574;
/** @param {number} lat */
const mPerDegLon = (lat) => 111_320 * Math.cos((lat * Math.PI) / 180);

/** Horizontal distance in metres between two [lon, lat, ...] positions. */
export function distanceM(/** @type {number[]} */ a, /** @type {number[]} */ b) {
  return Math.hypot((b[0] - a[0]) * mPerDegLon(a[1]), (b[1] - a[1]) * M_PER_DEG_LAT);
}

/** Unit horizontal direction (east, north) from a to b. */
export function direction(/** @type {number[]} */ a, /** @type {number[]} */ b) {
  const dx = (b[0] - a[0]) * mPerDegLon(a[1]);
  const dy = (b[1] - a[1]) * M_PER_DEG_LAT;
  const l = Math.hypot(dx, dy) || 1;
  return [dx / l, dy / l];
}

/**
 * Round timestamps to `step` seconds, keeping them strictly increasing (the trips contract): a rounded value that
 * would equal or precede the previous one is bumped to previous + step.
 * @param {number[]} ts
 * @param {number} [step]
 */
export function roundStrict(ts, step = 0.01) {
  /** @type {number[]} */
  const out = [];
  for (const t of ts) {
    let r = Math.round(t / step) * step;
    const prev = out.at(-1);
    if (prev !== undefined && r <= prev + step / 2) r = prev + step;
    out.push(Number(r.toFixed(6)));
  }
  return out;
}

/** Deterministic PRNG (mulberry32), so generated test data is reproducible from its seed. */
export function mulberry32(/** @type {number} */ seed) {
  let s = seed >>> 0;
  return () => {
    s = (s + 0x6d2b79f5) >>> 0;
    let t = s;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

/** Typical free-flow speeds (m/s) by OSM highway class, for test data timing only. */
export const CLASS_SPEED_MPS = {
  motorway: 22,
  motorway_link: 15,
  trunk: 16,
  trunk_link: 12,
  primary: 13,
  primary_link: 10,
  secondary: 11,
  secondary_link: 9,
  tertiary: 10,
  tertiary_link: 8,
  unclassified: 8,
  residential: 8,
  living_street: 4,
  service: 5,
};
export const DEFAULT_SPEED_MPS = 8;
/** @param {string} highway */
export const classSpeed = (highway) => /** @type {Record<string, number>} */ (CLASS_SPEED_MPS)[highway] ?? DEFAULT_SPEED_MPS;

const coordKey = (/** @type {number[]} */ p) => `${p[0]},${p[1]}`;

/**
 * One-way ways and signal positions from the OSM extract.
 * @param {string} osmGzPath
 * @returns {{ oneway: Map<number, 1 | -1>, signalKeys: Set<string> }}
 */
export function readOsmExtras(osmGzPath) {
  const xml = gunzipSync(readFileSync(osmGzPath)).toString("utf8");
  /** @type {Map<number, 1 | -1>} */
  const oneway = new Map();
  for (const m of xml.matchAll(/<way id="(\d+)"[^>]*>([\s\S]*?)<\/way>/g)) {
    const body = m[2];
    const tag = (/** @type {string} */ k) => new RegExp(`<tag k="${k}" v="([^"]*)"`).exec(body)?.[1];
    const ow = tag("oneway");
    const hw = tag("highway") ?? "";
    const implied = hw === "motorway" || hw === "motorway_link" || tag("junction") === "roundabout";
    if (ow === "-1") oneway.set(Number(m[1]), -1);
    else if (ow === "yes" || ow === "1" || ow === "true" || (implied && ow !== "no")) oneway.set(Number(m[1]), 1);
  }
  // roads_3d stores lon/lat rounded to 6 decimals; OSM nodes have 7.
  const signalKeys = new Set();
  for (const m of xml.matchAll(/<node id="\d+"[^>]*lat="([-\d.]+)" lon="([-\d.]+)">\s*((?:<tag[^>]*>\s*)+)<\/node>/g)) {
    if (m[3].includes('k="highway" v="traffic_signals"')) {
      signalKeys.add(coordKey([Number(Number(m[2]).toFixed(6)), Number(Number(m[1]).toFixed(6))]));
    }
  }
  return { oneway, signalKeys };
}

/**
 * Build the graph from roads_3d features.
 * @param {{ properties: { way_id: number, highway: string, name: string | null, tunnel: boolean }, geometry: { coordinates: Position3[] } }[]} features
 * @param {Map<number, 1 | -1>} oneway
 * @param {Set<string>} [signalKeys]
 * @returns {RoadGraph}
 */
export function buildGraph(features, oneway, signalKeys = new Set()) {
  /** @type {Map<string, number>} */
  const index = new Map();
  /** @type {Position3[]} */
  const coords = [];
  /** @type {GraphEdge[][]} */
  const out = [];
  /** @type {Map<number, number[]>} */
  const wayVertices = new Map();
  const signals = new Set();
  const vertex = (/** @type {Position3} */ p) => {
    const key = coordKey(p);
    let i = index.get(key);
    if (i === undefined) {
      i = coords.length;
      index.set(key, i);
      coords.push(p);
      out.push([]);
      if (signalKeys.has(key)) signals.add(i);
    }
    return i;
  };
  for (const f of features) {
    const { way_id: wayId, highway, name, tunnel } = f.properties;
    const ids = f.geometry.coordinates.map(vertex);
    wayVertices.set(wayId, ids);
    const dir = oneway.get(wayId) ?? 0;
    for (let k = 1; k < ids.length; k++) {
      const a = ids[k - 1];
      const b = ids[k];
      if (a === b) continue;
      const lengthM = distanceM(coords[a], coords[b]);
      if (dir >= 0) out[a].push({ to: b, lengthM, wayId, highway, name, tunnel });
      if (dir <= 0) out[b].push({ to: a, lengthM, wayId, highway, name, tunnel });
    }
  }
  return { coords, out, signals, wayVertices };
}

/** Routing penalty by class: through traffic avoids service roads and laneways. */
export const ROUTE_PENALTY = { service: 5, living_street: 5, unclassified: 2 };

/** Routing cost of an edge: free-flow travel time (s), penalised on minor classes, so routes keep to main roads. */
export const edgeCost = (/** @type {GraphEdge} */ e) =>
  (e.lengthM / classSpeed(e.highway)) * (/** @type {Record<string, number>} */ (ROUTE_PENALTY)[e.highway] ?? 1);

/**
 * Fastest path from `from` to `to` (Dijkstra, binary heap). Returns vertex indices and the edges taken, or null.
 * @param {RoadGraph} g
 * @param {number} from
 * @param {number} to
 * @returns {{ vertices: number[], edges: GraphEdge[] } | null}
 */
export function shortestPath(g, from, to) {
  const n = g.coords.length;
  const dist = new Float64Array(n).fill(Infinity);
  /** @type {(GraphEdge | null)[]} */
  const via = new Array(n).fill(null);
  const prev = new Int32Array(n).fill(-1);
  /** @type {[number, number][]} */
  const heap = [[0, from]];
  dist[from] = 0;
  const push = (/** @type {[number, number]} */ item) => {
    heap.push(item);
    let i = heap.length - 1;
    while (i > 0) {
      const p = (i - 1) >> 1;
      if (heap[p][0] <= heap[i][0]) break;
      [heap[p], heap[i]] = [heap[i], heap[p]];
      i = p;
    }
  };
  const pop = () => {
    const top = heap[0];
    const last = /** @type {[number, number]} */ (heap.pop());
    if (heap.length) {
      heap[0] = last;
      let i = 0;
      for (;;) {
        const l = 2 * i + 1;
        const r = l + 1;
        let m = i;
        if (l < heap.length && heap[l][0] < heap[m][0]) m = l;
        if (r < heap.length && heap[r][0] < heap[m][0]) m = r;
        if (m === i) break;
        [heap[m], heap[i]] = [heap[i], heap[m]];
        i = m;
      }
    }
    return top;
  };
  while (heap.length) {
    const [d, u] = pop();
    if (d > dist[u]) continue;
    if (u === to) break;
    for (const e of g.out[u]) {
      const nd = d + edgeCost(e);
      if (nd < dist[e.to]) {
        dist[e.to] = nd;
        prev[e.to] = u;
        via[e.to] = e;
        push([nd, e.to]);
      }
    }
  }
  if (!Number.isFinite(dist[to])) return null;
  const vertices = [to];
  /** @type {GraphEdge[]} */
  const edges = [];
  for (let v = to; v !== from; v = prev[v]) {
    edges.push(/** @type {GraphEdge} */ (via[v]));
    vertices.push(prev[v]);
  }
  return { vertices: vertices.reverse(), edges: edges.reverse() };
}

/**
 * Fastest path through a list of waypoint vertices, in order.
 * @param {RoadGraph} g
 * @param {number[]} waypoints
 */
export function routeVia(g, waypoints) {
  /** @type {number[]} */
  const vertices = [waypoints[0]];
  /** @type {GraphEdge[]} */
  const edges = [];
  for (let i = 1; i < waypoints.length; i++) {
    const leg = shortestPath(g, waypoints[i - 1], waypoints[i]);
    if (!leg) throw new Error(`no route from vertex ${waypoints[i - 1]} to ${waypoints[i]}`);
    vertices.push(...leg.vertices.slice(1));
    edges.push(...leg.edges);
  }
  return { vertices, edges };
}

/**
 * Nearest graph vertex to [lon, lat] with an outgoing edge that passes `accept` (default: any edge), so a route can
 * start or pass there. Returns -1 if none.
 * @param {RoadGraph} g
 * @param {number[]} p
 * @param {(e: GraphEdge, from: number) => boolean} [accept]
 */
export function nearestVertex(g, p, accept = () => true) {
  let best = -1;
  let bestD = Infinity;
  for (let i = 0; i < g.coords.length; i++) {
    if (!g.out[i].some((e) => accept(e, i))) continue;
    const d = distanceM(g.coords[i], p);
    if (d < bestD) {
      bestD = d;
      best = i;
    }
  }
  return best;
}

/**
 * Random walk forward from `start` for at least `lengthM` metres (stress-test data only): never turns straight back,
 * prefers continuing on the same way. Stops early at a dead end.
 * @param {RoadGraph} g
 * @param {number} start
 * @param {number} lengthM
 * @param {() => number} rng
 */
export function randomWalk(g, start, lengthM, rng) {
  const vertices = [start];
  /** @type {GraphEdge[]} */
  const edges = [];
  let along = 0;
  let prev = -1;
  let wayId = -1;
  let u = start;
  while (along < lengthM) {
    const options = g.out[u].filter((e) => e.to !== prev);
    if (!options.length) break;
    const same = options.filter((e) => e.wayId === wayId);
    const e = same.length && rng() < 0.75 ? same[0] : options[Math.floor(rng() * options.length)];
    edges.push(e);
    vertices.push(e.to);
    along += e.lengthM;
    prev = u;
    wayId = e.wayId;
    u = e.to;
  }
  return { vertices, edges };
}

/**
 * Time a route: free-flow class speeds × `speedFactor`, plus optional stops (seconds) at given vertex positions in
 * the route, drawn as a repeated position with a later timestamp. Returns the contract's {path, timestamps}.
 * @param {RoadGraph} g
 * @param {{ vertices: number[], edges: GraphEdge[] }} route
 * @param {{ startS: number, speedFactor?: number, stops?: Map<number, number>, z?: (vertexPos: number, p: Position3) => number }} opts
 *   stops: route position (index into route.vertices) -> stop duration (s); z: optional z override per point
 */
export function timeRoute(g, route, { startS, speedFactor = 1, stops = new Map(), z }) {
  /** @type {Position3[]} */
  const path = [];
  /** @type {number[]} */
  const timestamps = [];
  let t = startS;
  const point = (/** @type {number} */ k) => {
    const p = g.coords[route.vertices[k]];
    return /** @type {Position3} */ ([p[0], p[1], z ? z(k, p) : p[2]]);
  };
  path.push(point(0));
  timestamps.push(t);
  for (let k = 1; k < route.vertices.length; k++) {
    const e = route.edges[k - 1];
    t += e.lengthM / (classSpeed(e.highway) * speedFactor);
    path.push(point(k));
    timestamps.push(t);
    const stop = stops.get(k);
    if (stop && k < route.vertices.length - 1) {
      t += stop;
      path.push(point(k));
      timestamps.push(t);
    }
  }
  return { path, timestamps };
}
