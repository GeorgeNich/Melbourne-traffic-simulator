// qa-reviewer Playwright config for the web app (look, attribution, network and dev/prod review; Phase 1 + Phase 2).
//
// Run from the repo root (resolves @playwright/test from web/node_modules):
//   NODE_PATH=$PWD/web/node_modules PLAYWRIGHT_BROWSERS_PATH=0 \
//     pnpm -C web exec playwright test --config ../tests/e2e/playwright.review.config.ts
//
// Two dev servers on qa-reviewer's own ports, never reusing an existing server (so it can't silently test another
// worktree's server, and can't collide with web/playwright.config.ts on 5191/5192):
//   review-real    REVIEW_REAL_PORT (5174)    data/processed/buildings.geojson
//   review-fixture REVIEW_FIXTURE_PORT (5176) TWIN_USE_FIXTURE=1, synced into the gitignored web/public-fixture/
//   review-trips   REVIEW_TRIPS_PORT (5178)   TWIN_USE_FIXTURE=trips (labelled fixture cars over the REAL buildings),
//                                             synced into web/public-fixture/review-trips/ (gitignored with its parent)
// Only REVIEW_SERVERS (comma-separated project names, default all three) are started, so a run of one project
// doesn't start servers it never uses.
// Optional: PROD_URL=http://localhost:<port> runs the production checks against a `vite preview` of `pnpm build`.
import { defineConfig, devices } from "@playwright/test";

const realPort = Number(process.env.REVIEW_REAL_PORT ?? 5174);
const fixturePort = Number(process.env.REVIEW_FIXTURE_PORT ?? 5176);
const tripsPort = Number(process.env.REVIEW_TRIPS_PORT ?? 5178);
const wanted = (process.env.REVIEW_SERVERS ?? "review-real,review-fixture,review-trips").split(",");
const chrome = { ...devices["Desktop Chrome"], channel: "chromium", viewport: { width: 1440, height: 900 } };

export default defineConfig({
  testDir: ".",
  testMatch: /review_.*\.spec\.ts$/,
  timeout: 120_000,
  workers: 1,
  outputDir: "../../web/test-results/review",
  projects: [
    { name: "review-real", use: { ...chrome, baseURL: `http://localhost:${realPort}` } },
    { name: "review-fixture", use: { ...chrome, baseURL: `http://localhost:${fixturePort}` } },
    { name: "review-trips", use: { ...chrome, baseURL: `http://localhost:${tripsPort}` } },
  ].filter((p) => wanted.includes(p.name)),
  webServer: [
    {
      name: "review-real",
      command: `pnpm -C ../../web dev --port ${realPort} --strictPort`,
      url: `http://localhost:${realPort}`,
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      name: "review-fixture",
      command: `pnpm -C ../../web dev --port ${fixturePort} --strictPort`,
      url: `http://localhost:${fixturePort}`,
      env: { TWIN_USE_FIXTURE: "1", TWIN_PUBLIC_DIR: "public-fixture" },
      reuseExistingServer: false,
      timeout: 60_000,
    },
    {
      name: "review-trips",
      command: `pnpm -C ../../web dev --port ${tripsPort} --strictPort`,
      url: `http://localhost:${tripsPort}`,
      env: { TWIN_USE_FIXTURE: "trips", TWIN_PUBLIC_DIR: "public-fixture/review-trips" },
      reuseExistingServer: false,
      timeout: 60_000,
    },
  ]
    .filter((s) => wanted.includes(s.name))
    .map(({ name: _name, ...s }) => s),
});
