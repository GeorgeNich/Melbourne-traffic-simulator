import { useEffect, useState } from "react";
import { type RoadGeometry, toRoadGeometry } from "../map/roadsLayer";
import type { WayTunnel } from "../traffic/speeds";
import { type TunnelLookup, tunnelLookup } from "../traffic/tunnelWays";
import { type RoadsCollection, type RoadsValidation, validateRoads } from "./roadsContract";

/** Synced into the public folder by scripts/sync-data.mjs from data/processed/roads_3d.geojson (real data only). */
export const ROADS_URL = `${import.meta.env.BASE_URL}data/roads_3d.geojson`;

export type RoadsState =
  | { status: "loading" }
  | { status: "error"; message: string; validation?: RoadsValidation }
  /**
   * `tunnelAt`: the nearest below-ground way's tunnel flag, which decides which cars are drawn X-ray (useTraffic).
   * `wayTunnel`: the tunnel flag of an OSM way, which decides which road-speed edges are drawn X-ray (useSpeeds).
   */
  | { status: "ready"; geometry: RoadGeometry; tunnelAt: TunnelLookup; wayTunnel: WayTunnel; featureCount: number; elevatedWays: number };

const mark = (name: string) => performance.mark(`twin:roads-${name}`);

export function useRoads(): RoadsState {
  const [state, setState] = useState<RoadsState>({ status: "loading" });

  useEffect(() => {
    const ctrl = new AbortController();
    (async () => {
      mark("fetch-start");
      const res = await fetch(ROADS_URL, { signal: ctrl.signal });
      if (!res.ok) throw new Error(`Could not load ${ROADS_URL}: HTTP ${res.status}`);
      const data = JSON.parse(await res.text()) as RoadsCollection;
      mark("parsed");
      // Full contract validation in dev and tests; builds trust the contract tests that gate every handoff.
      if (import.meta.env.DEV) {
        const validation = validateRoads(data);
        if (validation.warnings.length) console.warn("[roads] contract warnings", validation.warnings);
        if (!validation.ok) {
          console.error("[roads] contract errors", validation.errors);
          setState({ status: "error", message: "roads_3d.geojson breaks the data contract", validation });
          return;
        }
      }
      const geometry = toRoadGeometry(data.features);
      const tunnelAt = tunnelLookup(data.features);
      const tunnelByWay = new Map(data.features.map((f) => [f.properties.way_id, f.properties.tunnel]));
      const wayTunnel: WayTunnel = (wayId) => tunnelByWay.get(wayId);
      mark("prepared");
      const elevatedWays = new Set(geometry.ribbons.map((r) => r.wayId)).size;
      setState({ status: "ready", geometry, tunnelAt, wayTunnel, featureCount: data.features.length, elevatedWays });
    })().catch((err: unknown) => {
      if (ctrl.signal.aborted) return;
      setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
    });
    return () => ctrl.abort();
  }, []);

  return state;
}
