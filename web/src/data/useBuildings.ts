import { useEffect, useState } from "react";
import { toRooflines, toTowerEdges, type Roofline, type TowerEdge } from "../map/buildingsLayer";
import { type BuildingPolygon, type BuildingsCollection, toPolygons, validateBuildings, type ValidationResult } from "./buildingsContract";

/** Synced into the public folder by scripts/sync-data.mjs (real data; the fixture only with TWIN_USE_FIXTURE=1). */
export const BUILDINGS_URL = `${import.meta.env.BASE_URL}data/buildings.geojson`;

export type BuildingsState =
  | { status: "loading" }
  | { status: "error"; message: string; validation?: ValidationResult }
  | {
      status: "ready";
      polygons: BuildingPolygon[];
      rooflines: Roofline[];
      towerEdges: TowerEdge[];
      featureCount: number;
      isTestFixture: boolean;
      validation?: ValidationResult;
    };

/** Performance marks for load-time measurement (read by e2e-perf/orbit.spec.ts). */
const mark = (name: string) => performance.mark(`twin:${name}`);

export function useBuildings(): BuildingsState {
  const [state, setState] = useState<BuildingsState>({ status: "loading" });

  useEffect(() => {
    const ctrl = new AbortController();
    (async () => {
      mark("fetch-start");
      const res = await fetch(BUILDINGS_URL, { signal: ctrl.signal });
      if (!res.ok) throw new Error(`Could not load ${BUILDINGS_URL}: HTTP ${res.status}`);
      const text = await res.text();
      mark("fetched");
      const data = JSON.parse(text) as BuildingsCollection;
      mark("parsed");

      // Full contract validation in dev (and in tests); production builds trust the contract tests that gate
      // every data handoff (tests/contracts/, src/data/realBuildings.test.ts) and skip it.
      let validation: ValidationResult | undefined;
      if (import.meta.env.DEV) {
        validation = validateBuildings(data);
        mark("validated");
        if (validation.warnings.length) console.warn("[buildings] contract warnings", validation.warnings);
        if (!validation.ok) {
          console.error("[buildings] contract errors", validation.errors);
          setState({ status: "error", message: "buildings.geojson breaks the data contract", validation });
          return;
        }
      }
      // Belt and braces: label test data even when validation is off.
      const isTestFixture =
        data._test_fixture === true || data.features.some((f) => f.properties?.height_source === "test_fixture");
      const polygons = toPolygons(data);
      const rooflines = toRooflines(polygons);
      const towerEdges = toTowerEdges(polygons);
      mark("prepared");
      setState({ status: "ready", polygons, rooflines, towerEdges, featureCount: data.features.length, isTestFixture, validation });
    })().catch((err: unknown) => {
      if (ctrl.signal.aborted) return;
      setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
    });
    return () => ctrl.abort();
  }, []);

  return state;
}
