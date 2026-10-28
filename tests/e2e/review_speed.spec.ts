// qa-reviewer: Phase 3B road speeds (frontend-engineer's speed colouring), checked independently. Project review-real
// (dev server, real data: sim/out/edge_geometry.json + edge_stats_baseline.parquet). See playwright.review.config.ts.
//
// - Served files: the manifest lists the speeds, and the served edge_geometry / edge_stats are the committed sim/out files.
// - Bin lookup and smoothing (the app's window.__twinSpeeds.speedAt) against our own reading of the parquet: the bin is
//   floor(t / 60) on the trips clock; colour only where the CURRENT bin has a row; vehicle-weighted mean of the current
//   bin (weight 1) and the previous bin (weight 0.5); never a later bin (no look-ahead).
// - Palette: with Road speeds on, amber / red appear; with it off they vanish (cars are all "flowing" blue in the
//   placeholder baseline), so amber and red come only from the speed layers; nothing outside the traffic palette is
//   saturated either way.
// - Haze: the building wash-out gate stays the traffic-hidden metric (as Phase 2); the speeds-on numbers are measured
//   and guarded against a further rise, not used to loosen the gate.
// - Tunnel classification: a speed segment is X-ray exactly where a car there would be (the cars' rule: the tunnel flag
//   of the nearest below-ground roads_3d way).
// Stills go to docs/screenshots/phase-3/3b-speed-review/ (qa review record of this change).
import { mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { expect, type Page, test } from "@playwright/test";
import { type Box, expectAttributionUnobstructed, saturationBreakdown } from "./support/pixels";

const ROOT = resolve(__dirname, "../..");
const SHOTS = resolve(ROOT, "docs/screenshots/phase-3/3b-speed-review");
const STATS = resolve(ROOT, "sim/out/edge_stats_baseline.parquet");
const GEOMETRY = resolve(ROOT, "sim/out/edge_geometry.json");
const BIN_S = 60;
const PREVIOUS_WEIGHT = 0.5; // the documented smoothing (palette SPEED.smoothing), stated here, not read from the app
const TUNNEL_Z_M = -0.5;
const LIFT_M = 0.25; // speed lines are lifted this much above the road (palette SPEED.liftM)
/** Phase 1 wash-out limit for the central band, and the Phase 2 CBD record (traffic hidden). */
const MAX_BRIGHT_TRAFFIC_HIDDEN = 0.155;
/** Guards on the speeds-on look (recorded 2026-10-27 at 0.1553 / 0.1676 by the frontend): no further rise. */
const MAX_BRIGHT_SPEEDS_ONLY = 0.16;
const MAX_BRIGHT_ALL = 0.175;
const MAX_OTHER_SATURATED = 0.002;

type Row = { ratio: number; count: number };

async function readStats(): Promise<Map<string, Row>> {
  // hyparquet (web's own dependency, MIT) is ESM-only and lives in web/node_modules: import it by file URL.
  const entry = pathToFileURL(resolve(ROOT, "web/node_modules/hyparquet/src/index.js")).href;
  const { parquetRead } = (await import(entry)) as typeof import("hyparquet");
  const buf = readFileSync(STATS);
  const file = buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.byteLength);
  const rows: unknown[][] = [];
  await parquetRead({ file, onComplete: (data: unknown[][]) => rows.push(...data) });
  const out = new Map<string, Row>();
  for (const [edge, tBin, , , ratio, count] of rows) {
    out.set(`${edge as string}|${Number(tBin) / BIN_S}`, { ratio: Number(ratio), count: Number(count) });
  }
  return out;
}

function expected(stats: Map<string, Row>, edge: string, t: number): { ratio: number; vehicles: number } | null {
  const b = Math.max(0, Math.min(14, Math.floor(t / BIN_S)));
  const cur = stats.get(`${edge}|${b}`);
  if (!cur) return null;
  const prev = b > 0 ? stats.get(`${edge}|${b - 1}`) : undefined;
  const wp = prev ? prev.count * PREVIOUS_WEIGHT : 0;
  return { ratio: (cur.count * cur.ratio + wp * (prev?.ratio ?? 0)) / (cur.count + wp), vehicles: cur.count + wp };
}

async function ready(page: Page) {
  await page.goto("/");
  await page.waitForFunction(() => window.__twinReady === true && window.__twinSpeeds !== undefined, null, { timeout: 120_000, polling: 200 });
  await page.addStyleTag({ content: '[data-testid="fps"] { display: none !important; }' });
}

/** Pause at `time`, jump the camera, wait for idle and the label fade. */
async function frameAt(page: Page, view: { center: [number, number]; zoom: number; pitch: number; bearing: number } | null, time: number) {
  await page.evaluate(
    ({ view, time }) =>
      new Promise<void>((done) => {
        const pb = window.__twinPlayback!;
        pb.pause();
        pb.seek(time);
        const map = window.__twinMap!;
        if (view) map.jumpTo(view);
        map.once("idle", () => done());
        map.triggerRepaint();
      }),
    { view, time },
  );
  await page.waitForTimeout(600);
}

async function goPreset(page: Page, label: string) {
  const btn = page.getByRole("button", { name: label, exact: true });
  await btn.click();
  await expect(btn).toHaveAttribute("aria-pressed", "true");
  await page.waitForFunction(() => !window.__twinMap!.isMoving(), null, { timeout: 30_000, polling: 100 });
}

/** Show only the deck layers `keep` accepts (by id); the rest of the traffic hidden. Restored by passing null. */
async function onlyLayers(page: Page, keep: "none" | "speeds" | "all") {
  await page.evaluate((keep) => {
    type L = { id: string; props: { visible?: boolean }; clone: (p: Record<string, unknown>) => L };
    const deck = (window.__twinMap as unknown as { __deck: { props: { layers: L[] }; setProps: (p: { layers: L[] }) => void } }).__deck;
    const w = window as unknown as { __qaVisible?: Record<string, boolean> };
    w.__qaVisible ??= Object.fromEntries(deck.props.layers.map((l) => [l.id, l.props.visible !== false]));
    const isSpeed = (id: string) => /^traffic(-tunnel)?-speed-/.test(id);
    deck.setProps({
      layers: deck.props.layers.map((l) => {
        if (!l.id.startsWith("traffic")) return l;
        const show = keep === "all" || (keep === "speeds" && isSpeed(l.id));
        return l.clone({ visible: show && w.__qaVisible![l.id]! });
      }),
    });
    window.__twinMap!.triggerRepaint();
  }, keep);
  await page.waitForTimeout(600);
}

async function brightFraction(page: Page, png: Buffer) {
  return page.evaluate(async (b64) => {
    const img = new Image();
    img.src = `data:image/png;base64,${b64}`;
    await img.decode();
    const c = document.createElement("canvas");
    c.width = img.width;
    c.height = img.height;
    const ctx = c.getContext("2d")!;
    ctx.drawImage(img, 0, 0);
    const x0 = Math.round(c.width * 0.25), y0 = Math.round(c.height * 0.2);
    const { data } = ctx.getImageData(x0, y0, Math.round(c.width * 0.5), Math.round(c.height * 0.6));
    let bright = 0;
    for (let i = 0; i < data.length; i += 4) if (0.2126 * data[i]! + 0.7152 * data[i + 1]! + 0.0722 * data[i + 2]! > 150) bright++;
    return bright / (data.length / 4);
  }, png.toString("base64"));
}

test.describe("road speeds (dev, real data)", () => {
  test.beforeEach(({}, info) => test.skip(info.project.name !== "review-real", "runs in review-real only"));

  test("the served speed files are the committed sim/out files", async ({ page }) => {
    await ready(page);
    const served = await page.evaluate(async () => {
      const m = (await (await fetch("/data/traffic.json")).json()) as { speeds?: { edgeGeometry: string; edgeStats: string } };
      const bytes = async (n: string) => Array.from(new Uint8Array(await (await fetch(`/data/${n}`)).arrayBuffer()));
      return { m, geom: await (await fetch(`/data/${m.speeds!.edgeGeometry}`)).text(), stats: await bytes(m.speeds!.edgeStats) };
    });
    expect(served.m.speeds).toEqual({ edgeGeometry: "edge_geometry.json", edgeStats: "edge_stats_baseline.parquet" });
    expect(served.geom).toBe(readFileSync(GEOMETRY, "utf8"));
    expect(Buffer.from(served.stats).equals(readFileSync(STATS))).toBe(true);
    await expect(page.locator("pre[role=alert]")).toHaveCount(0);
  });

  test("bin lookup on the trips clock, missing rows uncoloured, trailing smoothing that never looks ahead", async ({ page }) => {
    test.setTimeout(180_000);
    const stats = await readStats();
    await ready(page);
    const geometry = JSON.parse(readFileSync(GEOMETRY, "utf8")) as { edge_id: string }[];
    const edges = geometry.map((e) => e.edge_id).filter((_, i) => i % 5 === 0);
    // Bin edges, both sides of every boundary, mid-bins and the end of the scenario (900 = duration: last bin).
    const times = [0, 0.5, 59.9, 60, 60.1, 119.99, 120, 300, 479.5, 480, 600.25, 839.9, 840, 899.5, 900];
    const got = await page.evaluate(
      ({ edges, times }) => edges.map((id) => times.map((t) => window.__twinSpeeds!.speedAt(id, t))),
      { edges, times },
    );
    const bad: string[] = [];
    let colored = 0;
    let uncolored = 0;
    let lookahead = 0;
    edges.forEach((id, i) =>
      times.forEach((t, j) => {
        const want = expected(stats, id, t);
        const have = got[i]![j];
        if (want === null) {
          uncolored++;
          if (stats.has(`${id}|${Math.min(14, Math.floor(t / BIN_S)) + 1}`)) lookahead++;
          if (have !== null) bad.push(`${id} @ ${t}: no row in this bin but coloured ${JSON.stringify(have)}`);
        } else {
          colored++;
          if (have === null || Math.abs(have.ratio - want.ratio) > 1e-9 || Math.abs(have.vehicles - want.vehicles) > 1e-9) {
            bad.push(`${id} @ ${t}: app ${JSON.stringify(have)} vs expected ${JSON.stringify(want)}`);
          }
        }
      }),
    );
    console.log(`speedAt checked: ${colored} coloured, ${uncolored} uncoloured (${lookahead} with data in the next bin)`);
    expect(colored, "some edges coloured").toBeGreaterThan(500);
    expect(lookahead, "cases where a look-ahead would show (next bin has data)").toBeGreaterThan(50);
    expect(bad, bad.slice(0, 10).join("\n")).toEqual([]);
  });

  test("amber / red come only from the road speeds; nothing outside the traffic palette; haze gate and guards", async ({ page }) => {
    test.setTimeout(240_000);
    mkdirSync(SHOTS, { recursive: true });
    await ready(page);
    const legend = (await page.getByTestId("speed-legend").boundingBox()) as Box;
    const metrics: Record<string, Record<string, number>> = {};
    const hues: Record<string, Record<string, number>> = {};
    const shoot = async (name: string) => {
      await expectAttributionUnobstructed(page);
      const png = await page.screenshot({ path: resolve(SHOTS, `${name}.png`) });
      const s = await saturationBreakdown(page, png, [legend]);
      // Additive pile-up of flowing blue turns cyan (hue 170-199) on its way to white: traffic light, counted apart (as
      // the final-look test in review_traffic.spec.ts).
      const cyan = Object.entries(s.otherHues).filter(([h]) => Number(h) >= 170 && Number(h) < 200).reduce((n, [, c]) => n + c, 0) / s.counted;
      metrics[name] = { blue: s.trafficBlue, amber: s.trafficAmber, red: s.trafficRed, cyanPileUp: cyan, other: s.other - cyan, bright: await brightFraction(page, png) };
      hues[name] = s.otherHues;
      return png;
    };
    const toggle = page.getByRole("checkbox", { name: "Road speeds" });
    for (const [id, label] of [["cbd", "CBD"], ["interchange", "Interchange"], ["southbank", "Southbank"], ["burnley", "Burnley tunnel"]] as const) {
      await goPreset(page, label);
      await frameAt(page, null, 480);
      await shoot(`${id}-speeds-on`);
      await toggle.uncheck();
      await frameAt(page, null, 480);
      await shoot(`${id}-speeds-off`);
      await toggle.check();
    }
    // Haze at the CBD: traffic hidden (the building gate), speeds only, everything.
    await goPreset(page, "CBD");
    await frameAt(page, null, 480);
    await onlyLayers(page, "none");
    const hidden = await brightFraction(page, await page.screenshot());
    await onlyLayers(page, "speeds");
    const speedsOnly = await brightFraction(page, await page.screenshot({ path: resolve(SHOTS, "cbd-speeds-only.png") }));
    await onlyLayers(page, "all");
    const all = await brightFraction(page, await page.screenshot());
    console.log("speed look metrics:", JSON.stringify(metrics));
    console.log("other saturated hues (10 deg bins, pixel counts):", JSON.stringify(hues));
    console.log(`CBD central-band bright share: traffic hidden ${hidden.toFixed(4)}, speeds only ${speedsOnly.toFixed(4)}, cars + speeds ${all.toFixed(4)}`);

    const sum = (k: string, on: boolean) => Object.entries(metrics).filter(([n]) => n.endsWith(on ? "-on" : "-off")).reduce((s, [, m]) => s + m[k]!, 0);
    expect(sum("amber", true) + sum("red", true), "road speeds draw amber / red somewhere").toBeGreaterThan(0.0002);
    expect(sum("amber", false) + sum("red", false), "with Road speeds off, no amber / red at all").toBeLessThan(0.00005);
    for (const [n, m] of Object.entries(metrics)) expect(m.other, `${n}: saturated pixels outside the traffic palette`).toBeLessThan(MAX_OTHER_SATURATED);
    expect(hidden, "building wash-out gate (traffic hidden)").toBeLessThan(MAX_BRIGHT_TRAFFIC_HIDDEN);
    expect(speedsOnly, "speeds-on haze: no further rise").toBeLessThan(MAX_BRIGHT_SPEEDS_ONLY);
    expect(all, "cars + speeds haze: no further rise").toBeLessThan(MAX_BRIGHT_ALL);
  });

  // qa 3B round 1: this failed on e21b018 (speed lines used the edge's single way_id flag; 5 multi-way edges at portals
  // and underpasses, 35 segments, ~418 m, disagreed with the cars); fixed in c98c370 (tunnelAt per segment midpoint).
  test("tunnel X-ray of the speed lines agrees with the cars' rule on every below-ground segment", async ({ page }) => {
    await ready(page);
    const result = await page.evaluate(
      async ({ tunnelZ, lift }) => {
        type F = { properties: { tunnel: boolean }; geometry: { coordinates: [number, number, number][] } };
        const roads = (await (await fetch("/data/roads_3d.geojson")).json()) as { features: F[] };
        const lat0 = -37.82;
        const mx = 111_320 * Math.cos((lat0 * Math.PI) / 180);
        const xy = (lon: number, lat: number) => [(lon - 144.95) * mx, (lat - lat0) * 111_320] as const;
        const segs: { a: readonly [number, number]; b: readonly [number, number]; tunnel: boolean }[] = [];
        for (const f of roads.features) {
          const c = f.geometry.coordinates;
          for (let i = 1; i < c.length; i++) {
            if ((c[i - 1]![2] + c[i]![2]) / 2 < 0) segs.push({ a: xy(c[i - 1]![0], c[i - 1]![1]), b: xy(c[i]![0], c[i]![1]), tunnel: f.properties.tunnel });
          }
        }
        const dist = (p: readonly [number, number], s: (typeof segs)[number]) => {
          const [dx, dy] = [s.b[0] - s.a[0], s.b[1] - s.a[1]];
          const L = dx * dx + dy * dy;
          const t = L ? Math.max(0, Math.min(1, ((p[0] - s.a[0]) * dx + (p[1] - s.a[1]) * dy) / L)) : 0;
          return Math.hypot(p[0] - s.a[0] - t * dx, p[1] - s.a[1] - t * dy);
        };
        type P = { edge: number; tunnel: boolean; path: [number, number, number][] };
        type L = { id: string; props: { data: P[] } };
        const deck = (window.__twinMap as unknown as { __deck: { props: { layers: L[] } } }).__deck;
        const pieces = [
          ...(deck.props.layers.find((l) => l.id === "traffic-speed-core")?.props.data ?? []),
          ...(deck.props.layers.find((l) => l.id === "traffic-tunnel-speed-core")?.props.data ?? []),
        ];
        const ids = window.__twinSpeeds!.table.edgeIds;
        const off: string[] = [];
        let checked = 0;
        for (const p of pieces) {
          for (let k = 1; k < p.path.length; k++) {
            const [a, b] = [p.path[k - 1]!, p.path[k]!];
            if ((a[2] + b[2]) / 2 - lift >= tunnelZ) continue;
            checked++;
            const mid = xy((a[0] + b[0]) / 2, (a[1] + b[1]) / 2);
            let best = segs[0]!;
            let bd = Infinity;
            for (const s of segs) {
              const d = dist(mid, s);
              if (d < bd) [bd, best] = [d, s];
            }
            if (best.tunnel !== p.tunnel) off.push(`${ids[p.edge]} (speed ${p.tunnel ? "X-ray" : "solid"}, car rule ${best.tunnel ? "X-ray" : "solid"})`);
          }
        }
        return { checked, off: [...new Set(off)], nOff: off.length };
      },
      { tunnelZ: TUNNEL_Z_M, lift: LIFT_M },
    );
    console.log(`tunnel rule: ${result.checked} below-ground speed segments, ${result.nOff} disagree: ${result.off.join("; ")}`);
    expect(result.checked).toBeGreaterThan(100);
    expect(result.off).toEqual([]);
  });
});
