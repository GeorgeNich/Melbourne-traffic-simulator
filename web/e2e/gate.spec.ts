// Phase 2 gate stills in the default look (palette.ts: trail 20 s, glow through, ground roads medium, X-ray depth,
// silhouette tower edges), with the REAL baseline traffic (sim/out/trips_baseline.json, placeholder demand), real
// buildings and roads, paused at fixed times: web/screenshots/phase-2-gate/<view>.png (views: e2e/support/gate.ts).
// Opt-in (about a minute): pnpm -C web gate
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "@playwright/test";
import { GATE_DIR, GATE_VIEWS, HIDE_UI, showAt } from "./support/gate";
import { waitForReady } from "./support/trips";

test("phase 2 gate stills (default look, real traffic)", async ({ page }, info) => {
  test.skip(info.project.name !== "real" || process.env.GATE !== "1", "opt-in: GATE=1, real project");
  test.setTimeout(300_000);
  mkdirSync(GATE_DIR, { recursive: true });
  await page.goto("/");
  await waitForReady(page);
  await page.addStyleTag({ content: HIDE_UI });
  for (const v of GATE_VIEWS) {
    await showAt(page, v);
    await page.screenshot({ path: resolve(GATE_DIR, `${v.id}.png`) });
  }
});
