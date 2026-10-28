// Reading and runtime validation (dev) of sim/out/edge_stats_<scenario>.parquet against the contract in CLAUDE.md:
//   edge_id (string), t_bin (int32, 60 s bins, seconds on the trips clock), mean_speed_mps, speed_limit_mps,
//   speed_ratio (float64; ratio = mean / limit, may exceed 1 through driver speed factors), vehicle_count (int32 >= 1).
//   A missing (edge_id, t_bin) row means no vehicle, no data (drawn without colour, never as free flow); no NaN / null.
// Parquet is read in the browser with hyparquet (MIT licence, no dependencies, snappy built in). The rules follow
// qa-reviewer's tests/contracts/test_edge_stats_contract.py where the browser can check them without the network.

import { parquetMetadata, parquetRead } from "hyparquet";

/** Width of a t_bin (s): the contract's 60 s bins. */
export const EDGE_STATS_BIN_S = 60;

/** The six contract columns, in order, with their Parquet physical types. */
export const EDGE_STATS_COLUMNS = [
  { name: "edge_id", type: "BYTE_ARRAY" },
  { name: "t_bin", type: "INT32" },
  { name: "mean_speed_mps", type: "DOUBLE" },
  { name: "speed_limit_mps", type: "DOUBLE" },
  { name: "speed_ratio", type: "DOUBLE" },
  { name: "vehicle_count", type: "INT32" },
] as const;

/** The table as columns (one entry per row in each). Null or non-numeric cells are kept as NaN and reported. */
export interface EdgeStatsColumns {
  rows: number;
  edgeId: string[];
  tBin: Float64Array;
  meanSpeed: Float64Array;
  speedLimit: Float64Array;
  speedRatio: Float64Array;
  vehicleCount: Float64Array;
}

export interface EdgeStatsValidation {
  ok: boolean;
  errors: string[];
  warnings: string[];
  rows: number;
}

/** speed_ratio must equal mean / limit to the contract's 4 decimals (half a unit in the last place, plus float slack). */
export const RATIO_TOLERANCE = 0.5e-4 + 1e-9;
/** SUMO's default speedFactor distribution is capped at 2: ratios up to and including 2 pass (as the Python contract). */
export const MAX_SPEED_RATIO = 2;

const MAX_MESSAGES = 50;

interface SchemaLike {
  name: string;
  type?: string;
  converted_type?: string;
  logical_type?: { type: string };
  num_children?: number;
}

/** Schema errors: exactly the six columns in order, with the contract types (edge_id a UTF-8 string). */
export function validateEdgeStatsSchema(schema: readonly SchemaLike[]): string[] {
  const errors: string[] = [];
  const cols = schema.slice(1); // [0] is the root
  const names = cols.map((c) => c.name);
  const expected = EDGE_STATS_COLUMNS.map((c) => c.name);
  if (names.join() !== expected.join()) {
    errors.push(`columns are [${names.join(", ")}], expected exactly [${expected.join(", ")}] in that order`);
    return errors;
  }
  EDGE_STATS_COLUMNS.forEach((c, i) => {
    const s = cols[i]!;
    if (s.num_children) errors.push(`${c.name}: nested column`);
    if (s.type !== c.type) errors.push(`${c.name}: type ${s.type ?? "?"}, expected ${c.type}`);
  });
  const id = cols[0]!;
  if (id.type === "BYTE_ARRAY" && id.logical_type?.type !== "STRING" && id.converted_type !== "UTF8") {
    errors.push("edge_id: not a UTF-8 string column");
  }
  return errors;
}

/**
 * Read an edge_stats parquet file into columns. Throws if it isn't Parquet or its columns aren't the contract's (the
 * app reports that as a contract error); cell-level checks are validateEdgeStats.
 */
export async function readEdgeStats(file: ArrayBuffer): Promise<EdgeStatsColumns> {
  const metadata = parquetMetadata(file);
  const schemaErrors = validateEdgeStatsSchema(metadata.schema);
  if (schemaErrors.length) throw new EdgeStatsSchemaError(schemaErrors);
  const rows = Number(metadata.num_rows);
  const out: EdgeStatsColumns = {
    rows,
    edgeId: new Array<string>(rows).fill(""),
    tBin: new Float64Array(rows).fill(NaN),
    meanSpeed: new Float64Array(rows).fill(NaN),
    speedLimit: new Float64Array(rows).fill(NaN),
    speedRatio: new Float64Array(rows).fill(NaN),
    vehicleCount: new Float64Array(rows).fill(NaN),
  };
  const numeric: Record<string, Float64Array> = {
    t_bin: out.tBin,
    mean_speed_mps: out.meanSpeed,
    speed_limit_mps: out.speedLimit,
    speed_ratio: out.speedRatio,
    vehicle_count: out.vehicleCount,
  };
  await parquetRead({
    file,
    metadata,
    onChunk: ({ columnName, columnData, rowStart, rowEnd }) => {
      const n = Math.min(rowEnd, rows) - rowStart;
      if (columnName === "edge_id") {
        for (let i = 0; i < n; i++) {
          const v: unknown = columnData[i];
          out.edgeId[rowStart + i] = typeof v === "string" ? v : "";
        }
        return;
      }
      const dst = numeric[columnName];
      if (!dst) return;
      for (let i = 0; i < n; i++) {
        const v: unknown = columnData[i];
        dst[rowStart + i] = typeof v === "number" ? v : typeof v === "bigint" ? Number(v) : NaN;
      }
    },
  });
  return out;
}

export class EdgeStatsSchemaError extends Error {
  constructor(readonly errors: string[]) {
    super(`edge stats columns break the contract: ${errors.join("; ")}`);
  }
}

/**
 * Validate the cells of an edge_stats table. `durationS` (the scenario's meta) bounds t_bin; `edgeIds` (edge_geometry)
 * must hold every edge (the web needs a shape for every coloured edge).
 */
export function validateEdgeStats(
  t: EdgeStatsColumns,
  { durationS, edgeIds }: { durationS?: number; edgeIds?: ReadonlySet<string> } = {},
): EdgeStatsValidation {
  const errors: string[] = [];
  const warnings: string[] = [];
  const counts = new Map<string, { n: number; first: string }>();
  const fail = (kind: string, row: number, detail: string) => {
    const c = counts.get(kind);
    if (c) c.n++;
    else counts.set(kind, { n: 1, first: `row ${row} (${t.edgeId[row] || "?"} @ ${t.tBin[row]}): ${detail}` });
  };
  if (t.rows === 0) errors.push("no rows");
  const seen = new Set<string>();
  const unknown = new Set<string>();
  for (let i = 0; i < t.rows; i++) {
    const id = t.edgeId[i]!;
    const [bin, mean, limit, ratio, count] = [t.tBin[i]!, t.meanSpeed[i]!, t.speedLimit[i]!, t.speedRatio[i]!, t.vehicleCount[i]!];
    if (id === "") fail("edge_id null or empty", i, "no edge id");
    else if (id.startsWith(":")) fail("internal edge", i, "internal (junction) edge");
    else if (edgeIds && !edgeIds.has(id)) unknown.add(id);
    if (![bin, mean, limit, ratio, count].every(Number.isFinite)) {
      fail("null or non-finite cell", i, `[${bin}, ${mean}, ${limit}, ${ratio}, ${count}]`);
      continue;
    }
    if (!Number.isInteger(bin) || bin < 0 || bin % EDGE_STATS_BIN_S !== 0) fail("t_bin not a non-negative multiple of 60", i, `t_bin ${bin}`);
    else if (durationS !== undefined && bin >= durationS) fail("t_bin past the scenario", i, `t_bin ${bin} >= duration_s ${durationS}`);
    if (mean < 0) fail("negative mean speed", i, `mean_speed_mps ${mean}`);
    if (!(limit > 0)) fail("speed limit not positive", i, `speed_limit_mps ${limit}`);
    else {
      if (Math.abs(ratio - mean / limit) > RATIO_TOLERANCE) fail("speed_ratio != mean / limit", i, `${ratio} vs ${mean / limit}`);
      if (ratio > MAX_SPEED_RATIO) fail("speed_ratio implausibly high", i, `speed_ratio ${ratio}`);
    }
    if (!Number.isInteger(count) || count < 1) fail("vehicle_count not an integer >= 1", i, `vehicle_count ${count}`);
    const key = `${id}\u0000${bin}`;
    if (seen.has(key)) fail("duplicate (edge_id, t_bin)", i, "duplicate");
    else seen.add(key);
  }
  for (const [kind, { n, first }] of counts) errors.push(`${kind}: ${n} row${n === 1 ? "" : "s"}, first ${first}`);
  if (unknown.size) {
    errors.push(`${unknown.size} edge ids have no shape in edge_geometry.json, e.g. ${[...unknown].slice(0, 3).join(", ")}`);
  }
  return { ok: errors.length === 0, errors: errors.slice(0, MAX_MESSAGES), warnings, rows: t.rows };
}
