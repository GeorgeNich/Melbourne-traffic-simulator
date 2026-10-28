import { useEffect, useState } from "react";
import { TRAFFIC_MANIFEST_URL, type TrafficManifest } from "./useTraffic";

/** Written by scripts/sync-data.mjs beside traffic.json: every synced scenario (data-guard.mjs SCENARIOS_MANIFEST_FILENAME). */
export const SCENARIOS_MANIFEST_URL = `${import.meta.env.BASE_URL}data/scenarios.json`;

export interface ScenariosManifest {
  default?: string;
  scenarios: TrafficManifest[];
}

export type ScenariosState =
  | { status: "loading" }
  | { status: "error"; message: string }
  /** `initial` is traffic.json (the default scenario); `options` the scenario switch (initial included, in order). */
  | { status: "ready"; initial: TrafficManifest; options: TrafficManifest[] };

/**
 * The switch options: the scenarios.json list with traffic.json's entry for its own scenario (traffic.json is what
 * the app replays first). If the list does not include that scenario (or is missing), only traffic.json is offered.
 */
export function scenarioOptions(initial: TrafficManifest, list: ScenariosManifest | null): TrafficManifest[] {
  const listed = list?.scenarios ?? [];
  if (!listed.some((m) => m.scenario === initial.scenario)) return [initial];
  return listed.map((m) => (m.scenario === initial.scenario ? initial : m));
}

/** Load traffic.json (required) and scenarios.json (optional: without it there is no switch). */
export function useScenarios(): ScenariosState {
  const [state, setState] = useState<ScenariosState>({ status: "loading" });
  useEffect(() => {
    const ctrl = new AbortController();
    const get = async (url: string) => {
      const res = await fetch(url, { signal: ctrl.signal });
      if (!res.ok) throw new Error(`Could not load ${url}: HTTP ${res.status}`);
      return JSON.parse(await res.text()) as unknown;
    };
    (async () => {
      const [initial, list] = await Promise.all([
        get(TRAFFIC_MANIFEST_URL) as Promise<TrafficManifest>,
        (get(SCENARIOS_MANIFEST_URL) as Promise<ScenariosManifest>).catch(() => null),
      ]);
      setState({ status: "ready", initial, options: scenarioOptions(initial, list) });
    })().catch((err: unknown) => {
      if (ctrl.signal.aborted) return;
      setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
    });
    return () => ctrl.abort();
  }, []);
  return state;
}
