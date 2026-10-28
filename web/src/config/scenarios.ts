// Labels for the scenarios the app can replay (the scenario switch and the demand badge). The files themselves are
// listed by scripts/data-guard.mjs (SCENARIOS, scenarios.json); a scenario not named here shows its raw name.
import { isPlaceholderDemand, type TripsMeta } from "../data/tripsContract";
import { formatLocalDate } from "../traffic/playback";

export interface ScenarioInfo {
  /** Button text in the scenario switch. */
  label: string;
  /** The hours calibrated to counts, for the badge (PLAN.md 2026-10-27: 07–08 calibrated, 08–09 not yet). */
  calibrated?: string;
}

export const SCENARIO_INFO: Readonly<Record<string, ScenarioInfo>> = {
  am_peak: { label: "AM peak", calibrated: "7–8 am" },
  baseline: { label: "Baseline" },
};

export const scenarioLabel = (scenario: string): string => SCENARIO_INFO[scenario]?.label ?? scenario;

/**
 * Badge text for a replay: "Synthetic baseline" unless the demand is calibrated to counts, then
 * "Real counts · Fri 23 Oct 2026 · calibrated 7–8 am" (date from start_local, hours from SCENARIO_INFO; either is left
 * out when unknown).
 */
export function demandBadge(meta: Pick<TripsMeta, "scenario" | "demand" | "start_local">): string {
  if (isPlaceholderDemand(meta.demand)) return "Synthetic baseline";
  const hours = SCENARIO_INFO[meta.scenario]?.calibrated;
  return ["Real counts", formatLocalDate(0, meta.start_local), hours && `calibrated ${hours}`].filter(Boolean).join(" · ");
}
