// qa-reviewer: look, attribution, network and dev/prod checks for the ghost-city web app.
// Phase 1 checks, extended for Phase 2 Stage A (custom dark base style, tower edges, elevated-road ribbons,
// ground-road setting, Interchange / Footscray Rd presets) and Stage C (glowing cars: the saturation check now allows
// saturated traffic blue only, by hue; the attribution must stay uncovered with the playback panel present).
// Stage C look review (cfb379f): George's picks are the only look: ground roads "medium" by default, silhouette tower
// edges (one line per outer corner through stacked tiers), and the dev look URL params are gone.
// Stage C traffic checks live in review_traffic.spec.ts.
// See playwright.review.config.ts for how to run it. Projects:
//   review-real    -> real buildings + roads + the real baseline traffic; re-shoots the Stage A views to
//                     web/test-results/review/stage-a-rerun/ (gitignored). The Stage A record screenshots in
//                     docs/screenshots/phase-2/stage-a/ are only rewritten with REVIEW_STAGE_A_SHOTS=1.
//   review-fixture -> the labelled buildings + trips fixtures (TWIN_USE_FIXTURE=1) beside the real roads; no screenshots
// The Phase 1 gate screenshots in docs/screenshots/phase-1/ are a record and are no longer overwritten; the CBD one is
// read back as the wash-out baseline.
import { mkdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { type BrowserContext, expect, type Page, test } from "@playwright/test";
import { expectAttributionUnobstructed, saturationBreakdown } from "./support/pixels";

const SHOTS = process.env.REVIEW_STAGE_A_SHOTS
  ? resolve(__dirname, "../../docs/screenshots/phase-2/stage-a")
  : resolve(__dirname, "../../web/test-results/review/stage-a-rerun");
const PHASE1_CBD = resolve(__dirname, "../../docs/screenshots/phase-1/cbd.png");

// Kept in sync with web/src/camera/presets.ts by the button-list check below.
const PRESETS = [
  { id: "cbd", label: "CBD" },
  { id: "southbank", label: "Southbank" },
  { id: "docklands", label: "Docklands" },
  { id: "interchange", label: "Interchange" },
  { id: "footscray", label: "Footscray Rd" },
  { id: "burnley", label: "Burnley tunnel" },
  { id: "overview", label: "Overview" },
] as const;
/**
 * The look URL params the Stage C look review used (all removed in cfb379f): none may change anything any more, in dev
 * or in a build.
 */
const OLD_LOOK_QUERY = "?look=b&trail=long&glow=soft&roads=faint&xray=X1&edges=all";
/**
 * Ground roads: George's "medium" pick (Stage C look review), from the brief and the Stage A "medium" level: minor
 * streets stay at the faint 0.08, major roads 0.42, motorways 0.5; major roads and motorways 1.8x wider.
 */
const GROUND_ROAD_OPACITY = { road_minor: 0.08, road_tertiary: 0.18, road_major: 0.42, road_motorway: 0.5 } as const;
const MAJOR_WIDTH_SCALE = 1.8;

const STATUS_REAL = /^\d{1,3}(,\d{3})+ buildings · \d{1,3}(,\d{3})* elevated roads$/;
const STATUS_FIXTURE = /^9 buildings · \d{1,3}(,\d{3})* elevated roads$/;

/** The only host allowed off localhost (OpenFreeMap tiles, TileJSON and fonts). */
const ALLOWED_HOSTS = ["tiles.openfreemap.org"];

/**
 * Brief: cars and congested roads are the only saturated things on screen. Max share of strongly saturated pixels
 * that are NOT traffic blue (Phase 2: every car is "flowing" blue; amber / red arrive with Phase 3 speeds).
 */
const MAX_SATURATED_FRACTION = 0.002;
/** Phase 1 wash-out limit for the central band. */
const MAX_BRIGHT_FRACTION = 0.15;

type Box = { x: number; y: number; width: number; height: number };

const onlyIn = (name: string) =>
  test.beforeEach(({}, info) => test.skip(info.project.name !== name, `runs in ${name} only`));

async function ready(page: Page, query = "") {
  await page.goto(`/${query}`);
  await page.waitForFunction(() => window.__twinReady === true, null, { timeout: 90_000 });
}

async function settled(page: Page) {
  await page.waitForFunction(() => window.__twinMap !== undefined && !window.__twinMap.isMoving(), null, { timeout: 30_000 });
  await page.evaluate(
    () =>
      new Promise<void>((done) => {
        const map = window.__twinMap!;
        if (map.loaded() && map.areTilesLoaded()) return done();
        map.once("idle", () => done());
      }),
  );
  await page.waitForTimeout(400); // let the deck.gl overlay draw the final frame and labels fade in
}

/** Every request this context makes (page, workers, fetches), as URLs. Register before the first goto. */
function recordRequests(context: BrowserContext): string[] {
  const urls: string[] = [];
  context.on("request", (r) => urls.push(r.url()));
  return urls;
}

function offSiteHosts(urls: string[]): string[] {
  const hosts = new Set<string>();
  for (const u of urls) {
    const url = new URL(u);
    if (url.protocol === "data:" || url.protocol === "blob:") continue;
    if (url.hostname === "localhost" || url.hostname === "127.0.0.1") continue;
    hosts.add(url.hostname);
  }
  return [...hosts].sort();
}

/**
 * Share of saturated pixels outside the traffic palette (see support/pixels.ts), ignoring the given rectangles. Since
 * Phase 3B (road speeds under the cars) the additive pile-up of flowing blue, cyan (hue 170-199) on its way to white,
 * counts as traffic light too, as in review_traffic.spec.ts's final-look test.
 */
async function saturatedFraction(page: Page, png: Buffer, ignore: Box[]) {
  const s = await saturationBreakdown(page, png, ignore);
  const cyan = Object.entries(s.otherHues).filter(([h]) => Number(h) >= 170 && Number(h) < 200).reduce((n, [, c]) => n + c, 0) / s.counted;
  return s.other - cyan;
}

/**
 * Wash-out metric for the building area: share of pixels in the central band that are bright (luma > 150).
 * The Phase 1 pre-fix CBD with depth writes off stacked faces into a pale haze; this should stay small.
 */
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
    for (let i = 0; i < data.length; i += 4) {
      if (0.2126 * data[i]! + 0.7152 * data[i + 1]! + 0.0722 * data[i + 2]! > 150) bright++;
    }
    return bright / (data.length / 4);
  }, png.toString("base64"));
}

/** Share of pixels that differ by more than 24 (any channel) between two same-size screenshots. */
async function diffFraction(page: Page, a: Buffer, b: Buffer) {
  return page.evaluate(
    async ({ a64, b64 }) => {
      const load = async (s: string) => {
        const img = new Image();
        img.src = `data:image/png;base64,${s}`;
        await img.decode();
        const c = document.createElement("canvas");
        c.width = img.width;
        c.height = img.height;
        const ctx = c.getContext("2d")!;
        ctx.drawImage(img, 0, 0);
        return ctx.getImageData(0, 0, c.width, c.height).data;
      };
      const [da, db] = [await load(a64), await load(b64)];
      let diff = 0;
      for (let i = 0; i < da.length; i += 4) {
        if (Math.max(Math.abs(da[i]! - db[i]!), Math.abs(da[i + 1]! - db[i + 1]!), Math.abs(da[i + 2]! - db[i + 2]!)) > 24) diff++;
      }
      return diff / (da.length / 4);
    },
    { a64: a.toString("base64"), b64: b.toString("base64") },
  );
}

async function goPreset(page: Page, label: string, settle: (p: Page) => Promise<void> = settled) {
  const btn = page.getByRole("button", { name: label, exact: true });
  await btn.click();
  await expect(btn).toHaveAttribute("aria-pressed", "true");
  await settle(page);
}

test.describe("dev server, fixture data", () => {
  onlyIn("review-fixture");
  test.beforeEach(async ({ page }) => ready(page));

  test("fixture banner, fps readout, attribution, muted look at every preset", async ({ page }) => {
    const banner = page.getByTestId("fixture-banner");
    await expect(banner).toBeVisible();
    await expect(page.getByTestId("status")).toHaveText(STATUS_FIXTURE);
    await expect(page.getByTestId("fps")).toHaveText(/\d+ fps/, { timeout: 5_000 });
    for (const p of PRESETS) {
      await goPreset(page, p.label);
      await expectAttributionUnobstructed(page);
      const png = await page.screenshot();
      const frac = await saturatedFraction(page, png, [(await banner.boundingBox())!]);
      expect(frac, `${p.id}: saturated pixel fraction outside the banner`).toBeLessThan(MAX_SATURATED_FRACTION);
    }
  });
});

test.describe("dev server, real data", () => {
  onlyIn("review-real");

  test("no fixture banner; real building and road counts; contract-valid; preset buttons", async ({ page }) => {
    const warnings: string[] = [];
    page.on("console", (m) => {
      if (m.type() === "warning" || m.type() === "error") warnings.push(m.text());
    });
    await ready(page);
    await expect(page.getByTestId("fixture-banner")).toHaveCount(0);
    await expect(page.getByTestId("status")).toHaveText(STATUS_REAL);
    await expect(page.locator("pre[role=alert]")).toHaveCount(0); // dev contract validation passed
    // Fixed in a908a3b: pieces in the City of Melbourne beyond BUILDINGS_BBOX (allowed by the stacked-tier contract) no
    // longer warn; any [roads] / [buildings] warning is a finding again.
    expect(warnings.filter((w) => w.includes("[roads]") || w.includes("[buildings]"))).toEqual([]);
    const labels = await page.getByRole("group", { name: "Camera presets" }).getByRole("button").allInnerTexts();
    expect(labels).toEqual(PRESETS.map((p) => p.label));
  });

  test("building opacity slider is limited to 15-30% and defaults to 25%", async ({ page }) => {
    await ready(page);
    const slider = page.getByRole("slider", { name: "Building opacity" });
    await expect(slider).toHaveAttribute("min", "0.15");
    await expect(slider).toHaveAttribute("max", "0.3");
    await expect(slider).toHaveValue("0.25");
  });

  test("base style: OpenFreeMap only, no sprite, no buildings / raster, labels under the deck group", async ({ page }) => {
    await ready(page);
    const style = await page.evaluate(() => {
      const m = window.__twinMap!;
      const s = m.getStyle();
      const ids = m.getLayersOrder();
      return {
        sources: s.sources,
        glyphs: s.glyphs,
        sprite: s.sprite ?? null,
        layers: s.layers.map((l) => ({
          id: l.id,
          type: l.type,
          sourceLayer: (l as { "source-layer"?: string })["source-layer"],
          visibility: (l.layout as { visibility?: string } | undefined)?.visibility,
        })),
        order: ids.map((id) => ({ id, type: m.getLayer(id)?.type })),
      };
    });
    expect(Object.keys(style.sources)).toEqual(["openmaptiles"]);
    expect((style.sources.openmaptiles as { url: string }).url).toBe("https://tiles.openfreemap.org/planet");
    expect(style.glyphs).toBe("https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf");
    expect(style.sprite).toBeNull();
    expect(style.layers).toHaveLength(12);
    const visible = style.layers.filter((l) => l.visibility !== "none");
    expect(visible.filter((l) => l.type === "raster" || l.type === "hillshade")).toEqual([]);
    expect(visible.filter((l) => l.sourceLayer === "building" || l.type === "fill-extrusion")).toEqual([]);
    expect(JSON.stringify(style).toLowerCase()).not.toMatch(/carto|api[_-]?key|access[_-]?token|\bkey=/);
    const deckIdx = style.order.findIndex((l) => l.id.startsWith("deck-layer-group"));
    const symbolIdx = style.order.flatMap((l, i) => (l.type === "symbol" ? [i] : []));
    expect(deckIdx).toBeGreaterThan(0);
    expect(symbolIdx.length).toBeGreaterThan(0);
    expect(Math.max(...symbolIdx)).toBeLessThan(deckIdx);
  });

  test("every request off localhost goes to tiles.openfreemap.org (all presets)", async ({ page, context }) => {
    const urls = recordRequests(context);
    await ready(page);
    for (const p of PRESETS) await goPreset(page, p.label);
    const hosts = offSiteHosts(urls);
    console.log(`requests: ${urls.length}, off-site hosts: ${JSON.stringify(hosts)}`);
    expect(urls.some((u) => u.startsWith("https://tiles.openfreemap.org/"))).toBe(true);
    expect(hosts).toEqual(ALLOWED_HOSTS);
    expect(urls.filter((u) => /[?&](key|token|access_token)=/i.test(u))).toEqual([]);
  });

  test("ground roads default to George's medium pick; the old look params change nothing in dev", async ({ page }) => {
    const roadPaint = () =>
      page.evaluate(() => {
        const m = window.__twinMap!;
        const ids = ["road_minor", "road_tertiary", "road_major", "road_motorway"];
        return Object.fromEntries(ids.map((id) => [id, { color: String(m.getPaintProperty(id, "line-color")), width: m.getPaintProperty(id, "line-width") }]));
      });
    type Paint = Record<string, { color: string; width: unknown }>;
    await ready(page);
    const plain = (await roadPaint()) as Paint;
    for (const [id, a] of Object.entries(GROUND_ROAD_OPACITY)) {
      expect(plain[id]!.color, `${id} opacity`).toMatch(new RegExp(`^rgba\\(\\d+, \\d+, \\d+, ${a}\\)$`));
    }
    // Widths: ["interpolate", ["exponential", 1.6], ["zoom"], 12, w12, 16, w16, 19, w19]; major = tertiary x 1.8.
    const stops = (id: string) => (plain[id]!.width as number[]).filter((_, i) => i >= 4 && i % 2 === 0) as number[];
    stops("road_major").forEach((w, i) => expect(w).toBeCloseTo(stops("road_tertiary")[i]! * MAJOR_WIDTH_SCALE, 6));
    // The dev-only look params are gone: identical style with every old param set.
    await ready(page, OLD_LOOK_QUERY);
    expect(await roadPaint()).toEqual(plain);
  });

  test("ribbon layers: elevated non-tunnel ways only, metres, depth-tested not depth-written, ghost colours", async ({ page }) => {
    await ready(page);
    const r = await page.evaluate(async () => {
      type L = { id: string; props: Record<string, unknown> };
      const deck = (window.__twinMap as unknown as { __deck: { props: { layers: L[] } } }).__deck;
      const layers = deck.props.layers;
      const byId = Object.fromEntries(layers.map((l) => [l.id, l]));
      const fc = (await (await fetch("/data/roads_3d.geojson")).json()) as {
        features: { properties: { way_id: number; tunnel: boolean }; geometry: { coordinates: number[][] } }[];
      };
      const tunnelIds = new Set(fc.features.filter((f) => f.properties.tunnel).map((f) => f.properties.way_id));
      // Independent: non-tunnel ways that leave the ground (> 0.3 m up: bridges, ramps; or < -0.3 m: the open cutting
      // and underpass approaches, solid since Stage B) are exactly the ways that get a ribbon.
      const offGround = new Set(
        fc.features
          .filter((f) => !f.properties.tunnel && f.geometry.coordinates.some((c) => Math.abs(c[2]!) > 0.3))
          .map((f) => f.properties.way_id),
      );
      type Ribbon = { wayId: number; path: [number, number, number][]; alpha: number[]; width: number };
      const ribbons = byId["roads-deck"]!.props.data as Ribbon[];
      const getColor = byId["roads-deck"]!.props.getColor as (d: Ribbon) => number[][];
      const colours = ribbons.flatMap((d) => getColor(d));
      const chroma = (c: number[]) => (Math.max(c[0]!, c[1]!, c[2]!) - Math.min(c[0]!, c[1]!, c[2]!)) / 255;
      const lineColours = ["roads-edge", "roads-edge-hidden", "roads-fascia"].flatMap((id) => {
        const l = byId[id]!;
        const g = l.props.getColor as (d: unknown) => number[][];
        return (l.props.data as unknown[]).slice(0, 200).flatMap((d) => g(d));
      });
      const drop = byId["roads-drop"]!;
      const param = (id: string) => byId[id]!.props.parameters as Record<string, unknown>;
      return {
        order: layers.map((l) => l.id),
        runs: ribbons.length,
        ways: new Set(ribbons.map((d) => d.wayId)).size,
        tunnelRibbons: ribbons.filter((d) => tunnelIds.has(d.wayId)).length,
        tunnelWaysInFile: tunnelIds.size,
        everyRunOffGround: ribbons.every((d) => d.path.some((p) => Math.abs(p[2]) > 0.3)),
        sameWays: [...offGround].sort().join() === [...new Set(ribbons.map((d) => d.wayId))].sort().join(),
        offGroundWays: offGround.size,
        maxZ: Math.max(...ribbons.flatMap((d) => d.path.map((p) => p[2]))),
        minWidth: Math.min(...ribbons.map((d) => d.width)),
        maxWidth: Math.max(...ribbons.map((d) => d.width)),
        drops: (drop.props.data as unknown[]).length,
        deck: {
          widthUnits: byId["roads-deck"]!.props.widthUnits,
          billboard: byId["roads-deck"]!.props.billboard,
          params: param("roads-deck"),
        },
        lineParams: ["roads-edge", "roads-edge-hidden", "roads-fascia", "roads-drop"].map((id) => param(id)),
        maxDeckChroma: Math.max(...colours.map(chroma)),
        maxDeckAlpha: Math.max(...colours.map((c) => c[3]!)),
        maxLineChroma: Math.max(...lineColours.map(chroma), chroma(drop.props.getColor as number[])),
      };
    });
    console.log("ribbons:", JSON.stringify({ runs: r.runs, ways: r.ways, offGroundWays: r.offGroundWays, drops: r.drops, maxZ: r.maxZ, tunnelWaysInFile: r.tunnelWaysInFile }));
    // Roads are drawn after every building layer.
    const lastBuilding = Math.max(...r.order.flatMap((id, i) => (id.startsWith("buildings-") ? [i] : [])));
    const firstRoad = r.order.findIndex((id) => id.startsWith("roads-"));
    expect(firstRoad).toBeGreaterThan(lastBuilding);
    expect(r.tunnelWaysInFile).toBeGreaterThan(0);
    expect(r.tunnelRibbons).toBe(0);
    expect(r.everyRunOffGround).toBe(true);
    expect(r.sameWays, "ribbon ways = non-tunnel ways off the ground in roads_3d").toBe(true);
    // Stage A pinned 381 runs over 378 ways (roads_3d sha256 8890fb66...); Stage B added the solid below-ground runs, so
    // the set check above replaces the counts. Max: the Bolte Bridge, measured in the point cloud.
    expect(r.drops).toBeGreaterThan(100);
    expect(r.maxZ).toBe(32.93);
    expect(r.deck.widthUnits).toBe("meters");
    expect(r.deck.billboard).toBe(false);
    expect(r.minWidth).toBeGreaterThanOrEqual(4);
    expect(r.maxWidth).toBeLessThanOrEqual(12);
    expect(r.deck.params).toMatchObject({ depthWriteEnabled: false, depthCompare: "less-equal" });
    for (const p of r.lineParams) expect(p.depthWriteEnabled).toBe(false);
    expect(r.maxDeckChroma).toBeLessThan(0.2); // the building tone, not a traffic colour
    expect(r.maxDeckAlpha).toBeLessThanOrEqual(77); // <= 30% like the buildings
    expect(r.maxLineChroma).toBeLessThan(0.2);
  });

  test("tower edges and ribbons really draw pixels; silhouette tower edges on >= 60 m towers, every outer tier corner covered", async ({ page }) => {
    const errors: string[] = [];
    page.on("console", (m) => {
      if (m.type() === "error") errors.push(m.text());
    });
    await ready(page);
    await page.addStyleTag({ content: '[data-testid="fps"] { display: none !important; }' });
    type L = { id: string; props: Record<string, unknown>; clone: (p: Record<string, unknown>) => L };
    type Deck = { props: { layers: L[] }; setProps: (p: { layers: L[] }) => void };
    // Silhouette tower edges (George's pick, cfb379f) on the real buildings, checked independently of the app's
    // algorithm: every line is vertical and runs from some tier's base to some tier's top (>= 60 m); and for every tier
    // that tops out at >= 60 m and is >= 15 m tall, each corner on its footprint's convex hull (turn >= 35 deg, >= 4 m
    // apart, <= 12, as TOWER_EDGES) has a line within 3 m spanning the whole tier, so no set-back tier loses a corner.
    // Recomputed here, not taken from toTowerEdges. Known issues bounded below: crowns < 15 m tall start no lines of
    // their own (by design), and a few side-by-side pieces sharing a corner draw it twice.
    const edges = await page.evaluate(async () => {
      const deck = (window.__twinMap as unknown as { __deck: Deck }).__deck;
      const l = deck.props.layers.find((x) => x.id === "buildings-tower-edges")!;
      const data = l.props.data as { path: [number, number, number][] }[];
      type Piece = { polygon: number[][][]; height: number; base: number };
      const { toPolygons } = (await import("/src/data/buildingsContract.ts" as string)) as { toPolygons: (fc: unknown) => Piece[] };
      const pieces = toPolygons(await (await fetch("/data/buildings.geojson")).json());
      const KY = 110_574;
      const kx = (lat: number) => 111_320 * Math.cos((lat * Math.PI) / 180);
      /** Hull corners of a ring (lon/lat), sharpest first, as TOWER_EDGES: turn >= 35 deg, >= 4 m apart, <= 12. */
      const hullCorners = (ring: number[][]) => {
        const closed = ring.length > 1 && ring[0]![0] === ring.at(-1)![0] && ring[0]![1] === ring.at(-1)![1];
        const src = closed ? ring.slice(0, -1) : ring;
        if (ring.length < 4 || src.length < 3) return [];
        const k = kx(src[0]![1]!);
        const pts = src.map((p) => [(p[0]! - src[0]![0]!) * k, (p[1]! - src[0]![1]!) * KY] as [number, number]);
        const n = pts.length;
        const order = [...pts.keys()].sort((a, b) => pts[a]![0] - pts[b]![0] || pts[a]![1] - pts[b]![1]);
        const cross = (o: number, a: number, b: number) =>
          (pts[a]![0] - pts[o]![0]) * (pts[b]![1] - pts[o]![1]) - (pts[a]![1] - pts[o]![1]) * (pts[b]![0] - pts[o]![0]);
        const half = (idx: number[]) => {
          const h: number[] = [];
          for (const i of idx) {
            while (h.length >= 2 && cross(h[h.length - 2]!, h[h.length - 1]!, i) <= 0) h.pop();
            h.push(i);
          }
          return h;
        };
        const hull = new Set([...half(order), ...half([...order].reverse())]);
        const cs: { i: number; turn: number }[] = [];
        for (let i = 0; i < n; i++) {
          if (!hull.has(i)) continue;
          const a = pts[(i - 1 + n) % n]!, b = pts[i]!, c = pts[(i + 1) % n]!;
          const ux = b[0] - a[0], uy = b[1] - a[1], vx = c[0] - b[0], vy = c[1] - b[1];
          const lu = Math.hypot(ux, uy), lv = Math.hypot(vx, vy);
          if (lu < 1e-6 || lv < 1e-6) continue;
          const turn = Math.acos(Math.min(1, Math.max(-1, (ux * vx + uy * vy) / (lu * lv))));
          if (turn >= (35 * Math.PI) / 180) cs.push({ i, turn });
        }
        cs.sort((p, q) => q.turn - p.turn);
        const kept: number[] = [];
        for (const { i } of cs) {
          if (kept.length >= 12) break;
          if (kept.every((j) => Math.hypot(pts[j]![0] - pts[i]![0], pts[j]![1] - pts[i]![1]) >= 4)) kept.push(i);
        }
        return kept.map((i) => src[i]!);
      };
      // Grid index of the lines (~45 m cells).
      const G = 0.0005;
      const grid = new Map<string, number[]>();
      data.forEach((e, i) => {
        const key = `${Math.floor(e.path[0]![0] / G)},${Math.floor(e.path[0]![1] / G)}`;
        (grid.get(key) ?? grid.set(key, []).get(key)!).push(i);
      });
      const near = (lon: number, lat: number, r: number) => {
        const out: number[] = [];
        const gx = Math.floor(lon / G), gy = Math.floor(lat / G);
        for (let x = gx - 1; x <= gx + 1; x++)
          for (let y = gy - 1; y <= gy + 1; y++)
            for (const i of grid.get(`${x},${y}`) ?? []) {
              const p = data[i]!.path[0]!;
              if (Math.hypot((p[0] - lon) * kx(lat), (p[1] - lat) * KY) <= r) out.push(i);
            }
        return out;
      };
      let tiers = 0, corners = 0, perTierCorners = 0;
      const missed: string[] = [];
      for (const p of pieces) {
        if (p.height < 60) continue;
        // Without merging through tiers: one line per outer (hull) corner of every >= 60 m piece.
        perTierCorners += hullCorners(p.polygon[0]!).length;
        if (p.height - p.base < 15) continue;
        tiers++;
        for (const c of hullCorners(p.polygon[0]!)) {
          corners++;
          const ok = near(c[0]!, c[1]!, 3).some((i) => data[i]!.path[0]![2] <= p.base + 1 && data[i]!.path[1]![2] >= p.height - 1);
          if (!ok && missed.length < 20) missed.push(`${c[0]},${c[1]} tier ${p.base}-${p.height}`);
        }
      }
      let duplicates = 0;
      for (let i = 0; i < data.length; i++) {
        for (const j of near(data[i]!.path[0]![0], data[i]!.path[0]![1], 1)) {
          if (j <= i) continue;
          const [a, b] = [data[i]!.path, data[j]!.path];
          if (Math.min(a[1]![2], b[1]![2]) - Math.max(a[0]![2], b[0]![2]) > 1) duplicates++;
        }
      }
      const bases = new Set(pieces.map((p) => p.base));
      const tops = new Set(pieces.map((p) => p.height));
      return {
        count: data.length,
        perTierCorners,
        tiers,
        corners,
        missed,
        duplicates,
        billboard: l.props.billboard,
        minTop: Math.min(...data.map((e) => e.path[1]![2])),
        endsAreTierEnds: data.every((e) => bases.has(e.path[0]![2]) && tops.has(e.path[1]![2])),
        upward: data.every((e) => e.path[0]![2] >= 0 && e.path[1]![2] > e.path[0]![2]),
        vertical: data.every((e) => e.path[0]![0] === e.path[1]![0] && e.path[0]![1] === e.path[1]![1]),
      };
    });
    console.log("tower edges:", JSON.stringify(edges));
    expect(edges.count).toBeGreaterThan(0);
    // Silhouette: far fewer lines than one per tier corner (2,236 vs ~5,300 hull corners per tier, 2026-10 data).
    expect(edges.count).toBeLessThan(edges.perTierCorners * 0.6);
    expect(edges.tiers).toBeGreaterThan(400); // 434 tiers >= 15 m tall topping out at >= 60 m (2026-10 data)
    expect(edges.missed, `${edges.missed.length} outer tier corners without a line`).toEqual([]);
    // Known issue (qa, cfb379f): 30 lines drawn twice where side-by-side pieces share a corner.
    expect(edges.duplicates).toBeLessThanOrEqual(30);
    expect(edges.billboard).toBe(true);
    expect(edges.minTop).toBeGreaterThanOrEqual(60);
    expect(edges.endsAreTierEnds).toBe(true);
    expect(edges.upward).toBe(true);
    expect(edges.vertical).toBe(true);

    /**
     * Screenshot with the deck layers whose id starts with any of `prefixes` hidden, then show them again. Uses
     * clone({visible}) so deck.gl matches the layers by id and keeps their state (re-adding finalized layer
     * instances trips a deck.gl assertion).
     */
    const shotWithout = async (prefixes: string[]) => {
      const setVisible = (visible: boolean) =>
        page.evaluate(
          ({ pre, visible }) => {
            const deck = (window.__twinMap as unknown as { __deck: Deck }).__deck;
            deck.setProps({
              layers: deck.props.layers.map((l) => (pre.some((p) => l.id.startsWith(p)) ? l.clone({ visible }) : l)),
            });
            window.__twinMap!.triggerRepaint();
          },
          { pre: prefixes, visible },
        );
      await setVisible(false);
      await page.waitForTimeout(500);
      const png = await page.screenshot();
      await setVisible(true);
      await page.waitForTimeout(500);
      return png;
    };

    // Paused (real traffic is playing): only the hidden layers may change the picture.
    await page.evaluate(() => window.__twinPlayback?.pause());
    await goPreset(page, "CBD");
    const cbd = await page.screenshot();
    const cbdNoEdges = await shotWithout(["buildings-tower-edges"]);
    const cbdRestored = await page.screenshot();
    const edgeDiff = await diffFraction(page, cbd, cbdNoEdges);
    const brightWith = await brightFraction(page, cbd);
    const brightWithout = await brightFraction(page, cbdNoEdges);
    await goPreset(page, "Interchange");
    const inter = await page.screenshot();
    const interNoRoads = await shotWithout(["roads-"]);
    const roadDiff = await diffFraction(page, inter, interNoRoads);
    console.log(
      `tower-edge pixel share at CBD ${edgeDiff.toFixed(5)} (bright band ${brightWithout.toFixed(4)} -> ${brightWith.toFixed(4)}); ` +
        `ribbon pixel share at Interchange ${roadDiff.toFixed(5)}`,
    );
    expect(edgeDiff, "tower edges change the CBD picture").toBeGreaterThan(0.001);
    expect(roadDiff, "ribbons change the Interchange picture").toBeGreaterThan(0.005);
    expect(await diffFraction(page, cbd, cbdRestored), "CBD back to normal after showing the edges again").toBeLessThan(0.001);
    expect(errors).toEqual([]);
  });

  // qa (cfb379f): toTowerEdges follows a corner up/down through stackLinks with no visited set. Two overlapping tiers
  // at the same level that are at most STACK_TOLERANCE_M (1 m) thick each sit "on" the other, so the walk loops
  // forever and the tab runs out of memory at load. Contract-valid input (min_height_m < height_m); the real data has
  // 8 stacked pieces under 1 m thick today. Run in a module Worker so a hang can be cut off.
  test("silhouette tower edges terminate on contract-valid thin tiers at the same level", async ({ page }) => {
    await ready(page);
    const r = await page.evaluate(async () => {
      const src = `
        import { toTowerEdges } from "${location.origin}/src/map/buildingsLayer.ts";
        const kx = 111320 * Math.cos((-37.815 * Math.PI) / 180), ky = 110574;
        const sq = (s) => [[[0, 0], [s, 0], [s, s], [0, s], [0, 0]].map(([x, y]) => [144.96 + x / kx, -37.815 + y / ky])];
        const pieces = [
          { id: "qa_tower", polygon: sq(30), height: 100, base: 0 },
          { id: "qa_thin_1", polygon: sq(30), height: 100.5, base: 100 },
          { id: "qa_thin_2", polygon: sq(30), height: 100.5, base: 100 },
        ];
        // Control: the tower alone (proves the module loads in the Worker), then with the two thin tiers.
        self.postMessage({ control: toTowerEdges(pieces.slice(0, 1)).length });
        self.postMessage({ edges: toTowerEdges(pieces).length });
      `;
      const w = new Worker(URL.createObjectURL(new Blob([src], { type: "text/javascript" })), { type: "module" });
      return new Promise<{ control?: number; edges?: number; error?: string; timedOut?: boolean }>((done) => {
        let control: number | undefined;
        const timer = setTimeout(() => {
          w.terminate();
          done({ control, timedOut: true });
        }, 10_000);
        w.onmessage = (e) => {
          if ("control" in e.data) {
            control = e.data.control;
            return;
          }
          clearTimeout(timer);
          w.terminate();
          done({ control, ...e.data });
        };
        w.onerror = (e) => {
          clearTimeout(timer);
          w.terminate();
          done({ error: String(e.message) });
        };
      });
    });
    console.log("thin same-level tiers:", JSON.stringify(r));
    expect(r.error).toBeUndefined();
    expect(r.control, "control: the tower alone gets its 4 corner lines").toBe(4);
    expect(r.timedOut, "toTowerEdges never returns (infinite loop through two thin tiers)").toBeUndefined();
    expect(r.edges).toBe(4);
  });

  test("web roads validator vs the Python contract (informational)", async ({ page }) => {
    await ready(page);
    const r = await page.evaluate(async () => {
      const { validateRoads, ROAD_PROPERTIES } = (await import("/src/data/roadsContract.ts" as string)) as {
        validateRoads: (d: unknown) => { ok: boolean; errors: string[] };
        ROAD_PROPERTIES: readonly string[];
      };
      const road = (props: Record<string, unknown>) => ({
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            properties: { way_id: 1, highway: "primary", layer: 0, bridge: false, tunnel: false, name: "X", ...props },
            geometry: { type: "LineString", coordinates: [[144.96, -37.82, 0], [144.961, -37.82, 0]] },
          },
        ],
      });
      const real = await (await fetch("/data/roads_3d.geojson")).json();
      return {
        keys: [...ROAD_PROPERTIES].sort(),
        real: validateRoads(real).ok,
        emptyName: validateRoads(road({ name: "" })).ok,
        zeroWayId: validateRoads(road({ way_id: 0 })).ok,
        boolLayer: validateRoads(road({ layer: true })).ok,
      };
    });
    console.log("web validator:", JSON.stringify(r));
    expect(r.keys).toEqual(["bridge", "highway", "layer", "name", "tunnel", "way_id", "z_source"]);
    expect(r.real).toBe(true);
    expect(r.boolLayer).toBe(false);
    // The Python contract also rejects these two; the web accepts them (reported as a minor finding, not gated).
    test.info().annotations.push({ type: "web-validator", description: `emptyName ok=${r.emptyName}, way_id 0 ok=${r.zeroWayId}` });
  });

  test("final screenshots: every preset (default look); attribution, saturation and wash-out", async ({ page }) => {
    mkdirSync(SHOTS, { recursive: true });
    const metrics: Record<string, { saturated: number; bright: number }> = {};
    const brightWithTraffic: Record<string, number> = {};
    const hideFps = () => page.addStyleTag({ content: '[data-testid="fps"] { display: none !important; }' });
    /** Paused, traffic layers hidden (clone): the Phase 1 haze metric is about the buildings, not the car glow. */
    const withoutTraffic = async () => {
      const set = (visible: boolean) =>
        page.evaluate((visible) => {
          type L = { id: string; clone: (p: Record<string, unknown>) => L };
          const deck = (window.__twinMap as unknown as { __deck: { props: { layers: L[] }; setProps: (p: { layers: L[] }) => void } }).__deck;
          deck.setProps({ layers: deck.props.layers.map((l) => (l.id.startsWith("traffic") ? l.clone({ visible }) : l)) });
          window.__twinMap!.triggerRepaint();
        }, visible);
      await set(false);
      await page.waitForTimeout(500);
      const png = await page.screenshot();
      await set(true);
      await page.waitForTimeout(300);
      return png;
    };
    const shoot = async (name: string) => {
      await expectAttributionUnobstructed(page);
      await page.evaluate(() => {
        window.__twinPlayback?.pause();
        window.__twinPlayback?.seek(480);
      });
      await page.waitForTimeout(400);
      const png = await page.screenshot({ path: resolve(SHOTS, `${name}.png`) });
      const bare = await withoutTraffic();
      metrics[name] = { saturated: await saturatedFraction(page, bare, []), bright: await brightFraction(page, bare) };
      brightWithTraffic[name] = await brightFraction(page, png);
      expect(metrics[name].saturated, `${name}: saturated pixel fraction`).toBeLessThan(MAX_SATURATED_FRACTION);
      expect(metrics[name].bright, `${name}: bright (washed-out) share of the central band`).toBeLessThan(MAX_BRIGHT_FRACTION);
    };

    await ready(page);
    await hideFps();
    for (const p of PRESETS) {
      await goPreset(page, p.label);
      await shoot(p.id);
    }
    const phase1Cbd = await brightFraction(page, readFileSync(PHASE1_CBD));
    console.log("review metrics (traffic hidden):", JSON.stringify({ ...metrics, phase1_cbd_bright: phase1Cbd }), "bright with traffic:", JSON.stringify(brightWithTraffic));
    // Haze: Phase 1 gate 0.081, Stage A record 0.099; cfb379f (traffic hidden) 0.149, of which the tower edges are 0.022
    // and the faint -> medium ground roads 0.0004 (qa attribution run); the rest is the stacked-tier buildings (merged
    // before cfb379f). Known issue: the CBD central band sits just under the 0.15 wash-out limit above. Guard against
    // any further rise.
    expect(phase1Cbd).toBeLessThan(0.1);
    expect(metrics.cbd!.bright).toBeLessThan(0.155);
  });
});

// Production checks: run only when PROD_URL points at a `vite preview` of `pnpm build` (real data).
test.describe("production build", () => {
  onlyIn("review-real");
  test.skip(!process.env.PROD_URL, "PROD_URL not set");

  const prodSettled = async (page: Page) => {
    // No dev hook in a build: wait for the fly-to (FLY_DURATION_MS) and tiles to finish.
    await page.waitForTimeout(3500);
    await page.waitForLoadState("networkidle");
    await page.waitForTimeout(500);
  };

  test("no fixture banner, no fps readout, no dev hooks; roads shipped; attribution and look at every preset", async ({ page, context }) => {
    const urls = recordRequests(context);
    await page.goto(process.env.PROD_URL!);
    await expect(page.getByTestId("status")).toHaveText(STATUS_REAL, { timeout: 90_000 });
    await page.waitForTimeout(2000);
    await expect(page.getByTestId("fixture-banner")).toHaveCount(0);
    await expect(page.getByTestId("fps")).toHaveCount(0);
    expect(await page.evaluate(() => window.__twinMap === undefined)).toBe(true);
    for (const f of ["data/buildings.geojson", "data/roads_3d.geojson"]) {
      const res = await page.evaluate(async (p) => {
        const r = await fetch(p);
        return { ok: r.ok, text: await r.text() };
      }, f);
      expect(res.ok, f).toBe(true);
      expect(res.text).not.toContain('"test_fixture"');
      expect(res.text).not.toContain('"_test_fixture"');
    }
    for (const p of PRESETS) {
      await goPreset(page, p.label, prodSettled);
      await expectAttributionUnobstructed(page);
      const frac = await saturatedFraction(page, await page.screenshot(), []);
      expect(frac, `prod ${p.id}: saturated pixel fraction`).toBeLessThan(MAX_SATURATED_FRACTION);
    }
    expect(offSiteHosts(urls)).toEqual(ALLOWED_HOSTS);
  });

  test("the old look params are ignored in production", async ({ page }) => {
    const shot = async (query: string) => {
      await page.goto(`${process.env.PROD_URL!}/${query}`);
      await expect(page.getByTestId("status")).toHaveText(STATUS_REAL, { timeout: 90_000 });
      await goPreset(page, "Overview", prodSettled);
      return page.screenshot();
    };
    const plain = await shot("");
    const look = await shot(OLD_LOOK_QUERY);
    const again = await shot("");
    const noise = await diffFraction(page, plain, again);
    const d = await diffFraction(page, plain, look);
    console.log(`prod ${OLD_LOOK_QUERY} diff ${d.toFixed(5)} (run-to-run noise ${noise.toFixed(5)})`);
    expect(d).toBeLessThan(Math.max(0.002, noise * 3));
  });
});
