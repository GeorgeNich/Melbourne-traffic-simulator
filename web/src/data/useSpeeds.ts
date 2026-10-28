import { useEffect, useMemo, useState } from "react";
import { buildSpeedTable, type SpeedPiece, speedPieces, type SpeedTable, type WayTunnel } from "../traffic/speeds";
import type { TunnelLookup } from "../traffic/tunnelWays";
import { type EdgeGeometry, validateEdgeGeometry } from "./edgeGeometryContract";
import { EdgeStatsSchemaError, readEdgeStats, type EdgeStatsColumns, validateEdgeStats } from "./edgeStatsContract";
import type { TrafficManifest } from "./useTraffic";

const dataUrl = (name: string) => `${import.meta.env.BASE_URL}data/${name}`;

/** Edge stats file name for a scenario (CLAUDE.md contract): the scenario switch only changes the name. */
export const edgeStatsFile = (scenario: string) => `edge_stats_${scenario}.parquet`;

type Loaded =
  | { status: "loading" }
  | { status: "none"; scenario: string }
  | { status: "error"; message: string; errors?: string[] }
  | { status: "loaded"; manifest: TrafficManifest; scenario: string; edges: EdgeGeometry[]; stats: EdgeStatsColumns };

export type SpeedsState =
  | { status: "loading" }
  /** No edge stats for this scenario (or fixture / no traffic): no road speeds, and the toggle says so. */
  | { status: "none"; scenario: string }
  | { status: "error"; message: string; errors?: string[] }
  | { status: "ready"; scenario: string; table: SpeedTable; pieces: SpeedPiece[]; rows: number; edges: number };

/** What preparing needs from the roads (useRoads): tunnel flags per OSM way and the nearest-below-ground lookup. */
export interface RoadsForSpeeds {
  wayTunnel: WayTunnel;
  tunnelAt: TunnelLookup;
}

const mark = (name: string) => performance.mark(`twin:speeds-${name}`);

/**
 * Load, validate and prepare the road speeds for the replayed scenario: edge_geometry.json + edge_stats_<scenario>
 * .parquet as listed in its manifest (useScenarios; undefined while it loads, a new one reloads). `roads` undefined = still loading (preparing waits: tunnel edges follow
 * the roads_3d tunnel flag of their way), null = roads failed (depth alone decides). `durationS` (the trips meta)
 * bounds t_bin and sets the number of bins.
 */
export function useSpeeds(manifest: TrafficManifest | undefined, roads: RoadsForSpeeds | null | undefined, durationS: number | undefined): SpeedsState {
  const [loaded, setState] = useState<Loaded>({ status: "loading" });

  useEffect(() => {
    if (!manifest) return;
    const ctrl = new AbortController();
    (async () => {
      const speeds = manifest.speeds;
      if (!speeds) {
        setState({ status: "none", scenario: manifest.scenario });
        return;
      }
      mark("fetch-start");
      const [geomRes, statsRes] = await Promise.all([
        fetch(dataUrl(speeds.edgeGeometry), { signal: ctrl.signal }),
        fetch(dataUrl(speeds.edgeStats), { signal: ctrl.signal }),
      ]);
      for (const [r, name] of [[geomRes, speeds.edgeGeometry], [statsRes, speeds.edgeStats]] as const) {
        if (!r.ok) throw new Error(`Could not load ${name}: HTTP ${r.status}`);
      }
      const edges = JSON.parse(await geomRes.text()) as unknown;
      let stats: EdgeStatsColumns;
      try {
        stats = await readEdgeStats(await statsRes.arrayBuffer());
      } catch (err) {
        if (err instanceof EdgeStatsSchemaError) {
          setState({ status: "error", message: `${speeds.edgeStats} breaks the data contract`, errors: err.errors.map((e) => `${speeds.edgeStats}: ${e}`) });
          return;
        }
        throw err;
      }
      mark("parsed");
      setState({ status: "loaded", manifest, scenario: manifest.scenario, edges: edges as EdgeGeometry[], stats });
    })().catch((err: unknown) => {
      if (ctrl.signal.aborted) return;
      setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
    });
    return () => ctrl.abort();
  }, [manifest]);

  // Full contract validation in dev and tests; builds trust the contract tests that gate every handoff.
  const validated = useMemo<Loaded>(() => {
    if (loaded.status === "loaded" && loaded.manifest !== manifest) return { status: "loading" }; // switched
    if (loaded.status !== "loaded" || !import.meta.env.DEV) return loaded;
    if (durationS === undefined) return { status: "loading" };
    const vGeom = validateEdgeGeometry(loaded.edges);
    const ids = vGeom.ok ? new Set(loaded.edges.map((e) => e.edge_id)) : undefined;
    const vStats = validateEdgeStats(loaded.stats, { durationS, edgeIds: ids });
    const warnings = [...vGeom.warnings, ...vStats.warnings];
    if (warnings.length) console.warn("[speeds] contract warnings", warnings);
    if (!vGeom.ok || !vStats.ok) {
      const errors = [...vGeom.errors.map((e) => `edge_geometry.json: ${e}`), ...vStats.errors.map((e) => `${edgeStatsFile(loaded.scenario)}: ${e}`)];
      console.error("[speeds] contract errors", errors);
      return { status: "error", message: "the road speeds break the data contract", errors };
    }
    return loaded;
  }, [loaded, manifest, durationS]);

  return useMemo<SpeedsState>(() => {
    if (validated.status !== "loaded") return validated;
    if (roads === undefined || durationS === undefined) return { status: "loading" };
    const { table, skippedRows } = buildSpeedTable(
      validated.edges.map((e) => e.edge_id),
      validated.stats,
      durationS,
    );
    if (skippedRows) console.warn(`[speeds] ${skippedRows} edge_stats rows skipped (unknown edge or bad cell)`);
    const pieces = speedPieces(validated.edges, roads?.wayTunnel ?? (() => undefined), roads?.tunnelAt ?? null);
    mark("prepared");
    return { status: "ready", scenario: validated.scenario, table, pieces, rows: validated.stats.rows, edges: validated.edges.length };
  }, [validated, roads, durationS]);
}
