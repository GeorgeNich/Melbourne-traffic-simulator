import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { validateBuildings } from "./buildingsContract";

// The committed contract output from data-engineer. Skipped only if the file is absent (e.g. a fresh checkout
// before Phase 1 task A); the app itself refuses to run without it.
const REAL_PATH = resolve(import.meta.dirname, "../../../data/processed/buildings.geojson");

describe.skipIf(!existsSync(REAL_PATH))("data/processed/buildings.geojson", () => {
  it("passes the web contract validation and is not test data", () => {
    const r = validateBuildings(JSON.parse(readFileSync(REAL_PATH, "utf8")));
    expect(r.errors).toEqual([]);
    expect(r.isTestFixture).toBe(false);
    expect(r.featureCount).toBeGreaterThan(1000);
  });
});
