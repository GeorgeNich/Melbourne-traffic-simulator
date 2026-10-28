import { defineConfig, devices } from "@playwright/test";

// Headed Chromium on the real GPU, for fps evidence. Headless (SwiftShader) numbers are not fps evidence.
// Run: pnpm -C web perf   (opens a browser window for ~20 s)
const PORT = Number(process.env.PERF_PORT ?? 5193);

export default defineConfig({
  testDir: "./e2e-perf",
  timeout: 120_000,
  workers: 1,
  use: {
    ...devices["Desktop Chrome"],
    baseURL: `http://localhost:${PORT}`,
    headless: false,
    viewport: { width: 1440, height: 900 },
    deviceScaleFactor: 2, // a retina laptop screen
  },
  webServer: {
    command: `pnpm dev --port ${PORT} --strictPort`,
    url: `http://localhost:${PORT}`,
    reuseExistingServer: false, // own port, never another worktree's server
    timeout: 60_000,
  },
});
