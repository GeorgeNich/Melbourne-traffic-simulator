import { defineConfig, devices } from "@playwright/test";

// Two dev servers on this config's own ports, never reusing an existing server, so parallel worktrees (and
// qa-reviewer's review config on 5174) can't cross-talk:
//   real    -> data/processed/buildings.geojson (the default app)
//   fixture -> TWIN_USE_FIXTURE=1, synced into its own gitignored public-fixture/ folder
// channel "chromium" = full Chromium in new headless mode (chrome-headless-shell hangs on this WebGL page).
// Headless numbers are never fps evidence; see playwright.perf.config.ts.
const REAL_PORT = Number(process.env.E2E_REAL_PORT ?? 5191);
const FIXTURE_PORT = Number(process.env.E2E_FIXTURE_PORT ?? 5192);
const chrome = { ...devices["Desktop Chrome"], channel: "chromium", viewport: { width: 1440, height: 900 } };

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  workers: 1,
  projects: [
    { name: "real", use: { ...chrome, baseURL: `http://localhost:${REAL_PORT}` } },
    { name: "fixture", use: { ...chrome, baseURL: `http://localhost:${FIXTURE_PORT}` } },
  ],
  webServer: [
    {
      command: `pnpm dev --port ${REAL_PORT} --strictPort`,
      url: `http://localhost:${REAL_PORT}`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      command: `pnpm dev --port ${FIXTURE_PORT} --strictPort`,
      url: `http://localhost:${FIXTURE_PORT}`,
      env: { TWIN_USE_FIXTURE: "1", TWIN_PUBLIC_DIR: "public-fixture" },
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ],
});
