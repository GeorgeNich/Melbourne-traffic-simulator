import { useEffect, useMemo, useState } from "react";
import { playbackRange } from "../traffic/playback";
import { prepareTrips, type PreparedTraffic } from "../traffic/prepare";
import type { TunnelLookup } from "../traffic/tunnelWays";
import { crossCheckTrips, type Trip, type TripsMeta, type TripsValidation, validateTrips, validateTripsMeta } from "./tripsContract";

/** Written by scripts/sync-data.mjs: which traffic files were synced (data-guard.mjs TrafficManifest). */
export const TRAFFIC_MANIFEST_URL = `${import.meta.env.BASE_URL}data/traffic.json`;
const dataUrl = (name: string) => `${import.meta.env.BASE_URL}data/${name}`;

export interface TrafficManifest {
  scenario: string;
  source: "real" | "fixture" | "none";
  trips: string | null;
  meta: string | null;
  /** Road speeds for this scenario (data-guard.mjs resolveSpeeds); absent when there are none (or fixture trips). */
  speeds?: { edgeGeometry: string; edgeStats: string };
}

/** Fetched and validated, not yet prepared (preparing waits for the roads: tunnel classification needs them). */
type Loaded =
  | { status: "loading" }
  | { status: "none"; scenario: string }
  | { status: "error"; message: string; errors?: string[] }
  | { status: "loaded"; manifest: TrafficManifest; meta: TripsMeta; trips: Trip[]; isTestFixture: boolean };

export type TrafficState =
  | { status: "loading" }
  /** No trips exported yet: the app runs without cars and says so. */
  | { status: "none"; scenario: string }
  | { status: "error"; message: string; errors?: string[] }
  /** `range`: the trips' time range to replay (playbackRange; [0, duration_s] for baseline, [3600, 5400] for am_peak). */
  | { status: "ready"; meta: TripsMeta; prepared: PreparedTraffic; isTestFixture: boolean; range: { start: number; end: number } };

const mark = (name: string) => performance.mark(`twin:traffic-${name}`);

async function getJson(url: string, signal: AbortSignal): Promise<unknown> {
  const res = await fetch(url, { signal });
  if (!res.ok) throw new Error(`Could not load ${url}: HTTP ${res.status}`);
  return JSON.parse(await res.text()) as unknown;
}

const failed = (what: string, v: TripsValidation) => v.errors.map((e) => `${what}: ${e}`);

/**
 * Load, validate and prepare the traffic replay listed in `manifest` (useScenarios; undefined while it loads, and a new
 * manifest reloads: the scenario switch). `tunnelAt` (useRoads) decides which below-ground cars are in a tunnel
 * (X-ray) rather than in an open cutting: undefined while the roads load (preparing waits for them), null if the roads
 * failed (depth alone decides; the roads failure is reported on its own).
 */
export function useTraffic(manifest: TrafficManifest | undefined, tunnelAt: TunnelLookup | null | undefined): TrafficState {
  const [loaded, setState] = useState<Loaded>({ status: "loading" });

  useEffect(() => {
    if (!manifest) return;
    const ctrl = new AbortController();
    (async () => {
      mark("fetch-start");
      if (!manifest.trips || !manifest.meta) {
        setState({ status: "none", scenario: manifest.scenario });
        return;
      }
      const [meta, trips] = await Promise.all([getJson(dataUrl(manifest.meta), ctrl.signal), getJson(dataUrl(manifest.trips), ctrl.signal)]);
      mark("parsed");
      const isTestFixture = manifest.source === "fixture" || (meta as Partial<TripsMeta>)._test_fixture === true;
      // Full contract validation in dev and tests; builds trust the contract tests that gate every handoff.
      if (import.meta.env.DEV) {
        const vMeta = validateTripsMeta(meta, { isFixture: isTestFixture });
        const durationS = vMeta.ok ? (meta as TripsMeta).duration_s : undefined;
        const vTrips = validateTrips(trips, { isFixture: isTestFixture, durationS });
        const cross = vMeta.ok && vTrips.ok ? crossCheckTrips(trips as Trip[], meta as TripsMeta) : { errors: [], info: [] };
        const warnings = [...vMeta.warnings, ...vTrips.warnings];
        if (warnings.length) console.warn("[traffic] contract warnings", warnings);
        if (cross.info.length) console.info("[traffic]", cross.info.join("; "));
        if (!vMeta.ok || !vTrips.ok || cross.errors.length) {
          const errors = [...failed(manifest.meta, vMeta), ...failed(manifest.trips, vTrips), ...cross.errors];
          console.error("[traffic] contract errors", errors);
          setState({ status: "error", message: "the traffic replay breaks the data contract", errors });
          return;
        }
      }
      setState({ status: "loaded", manifest, meta: meta as TripsMeta, trips: trips as Trip[], isTestFixture });
    })().catch((err: unknown) => {
      if (ctrl.signal.aborted) return;
      setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
    });
    return () => ctrl.abort();
  }, [manifest]);

  return useMemo<TrafficState>(() => {
    if (loaded.status !== "loaded") return loaded;
    if (loaded.manifest !== manifest) return { status: "loading" }; // switched: the old scenario is never shown
    if (tunnelAt === undefined) return { status: "loading" };
    const prepared = prepareTrips(loaded.trips, { tunnelAt });
    mark("prepared");
    if (import.meta.env.DEV && prepared.tunnelVehicles !== loaded.meta.tunnel_vehicles) {
      console.info(
        `[traffic] ${prepared.tunnelVehicles} cars drawn X-ray (below ground on roads_3d tunnel ways); meta.tunnel_vehicles ` +
          `${loaded.meta.tunnel_vehicles} (on OSM tunnel ways)`,
      );
    }
    const range = playbackRange(loaded.trips, loaded.meta.duration_s);
    return { status: "ready", meta: loaded.meta, prepared, isTestFixture: loaded.isTestFixture, range };
  }, [loaded, manifest, tunnelAt]);
}
