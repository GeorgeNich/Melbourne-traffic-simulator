import { describe, expect, it } from "vitest";
import { demandBadge, scenarioLabel } from "./scenarios";

describe("scenario labels and the demand badge", () => {
  it("am_peak (calibrated, start_local): real counts, the date and the calibrated hour", () => {
    expect(demandBadge({ scenario: "am_peak", demand: "calibrated_counts", start_local: "2026-10-23T06:30:00+11:00" })).toBe(
      "Real counts · Fri 23 Oct 2026 · calibrated 7–8 am",
    );
  });
  it("baseline (placeholder, no start_local) and fixtures keep 'Placeholder traffic'", () => {
    expect(demandBadge({ scenario: "baseline", demand: "placeholder_random" })).toBe("Placeholder traffic");
    expect(demandBadge({ scenario: "fixture", demand: "test_fixture", start_local: "2026-10-23T06:30:00+11:00" })).toBe("Placeholder traffic");
  });
  it("leaves out what it doesn't know", () => {
    expect(demandBadge({ scenario: "other", demand: "calibrated_counts" })).toBe("Real counts");
  });
  it("labels the switch buttons", () => {
    expect(scenarioLabel("am_peak")).toBe("AM peak");
    expect(scenarioLabel("baseline")).toBe("Baseline");
    expect(scenarioLabel("fixture")).toBe("fixture");
  });
});
