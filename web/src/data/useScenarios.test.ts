import { describe, expect, it } from "vitest";
import { scenarioOptions } from "./useScenarios";
import type { TrafficManifest } from "./useTraffic";

const m = (scenario: string, trips = `trips_${scenario}.json`): TrafficManifest => ({ scenario, source: "real", trips, meta: `${scenario}.meta.json` });

describe("scenarioOptions", () => {
  it("lists every synced scenario, with traffic.json's entry for the default", () => {
    const initial = m("am_peak", "routed.json");
    expect(scenarioOptions(initial, { default: "am_peak", scenarios: [m("am_peak"), m("baseline")] })).toEqual([initial, m("baseline")]);
  });
  it("only traffic.json when scenarios.json is missing or doesn't list it (no switch)", () => {
    expect(scenarioOptions(m("baseline"), null)).toEqual([m("baseline")]);
    expect(scenarioOptions(m("routed"), { scenarios: [m("am_peak"), m("baseline")] })).toEqual([m("routed")]);
  });
});
