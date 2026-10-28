import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";
import {
  assertNoFixtureIn,
  assertRealDataPresent,
  DEFAULT_PATHS,
  fixtureRequested,
  publicDir,
} from "./scripts/data-guard.mjs";

/**
 * Build-time guard: the test fixture never ships (CLAUDE.md, PLAN.md Decisions log).
 * `pnpm build` already runs sync-data in build mode; this also covers a bare `vite build` after `pnpm dev`
 * left the fixture in public/data/, and any source import of the fixture file.
 */
function noTestFixtureInBuild(): Plugin {
  return {
    name: "twin:no-test-fixture-in-build",
    apply: "build",
    buildStart() {
      if (fixtureRequested()) this.error("Build refused: TWIN_USE_FIXTURE is set. Fixtures never ship.");
      assertRealDataPresent(DEFAULT_PATHS.outDir);
      assertNoFixtureIn(publicDir());
    },
    resolveId(source) {
      if (source.includes("buildings_fixture") || source.includes("trips_fixture") || source.includes("tests/fixtures")) {
        this.error(`Build refused: source imports the test fixture (${source}). Fixtures never ship.`);
      }
      return null;
    },
  };
}

export default defineConfig({
  plugins: [react(), noTestFixtureInBuild()],
  // TWIN_PUBLIC_DIR: the fixture e2e server uses its own folder (see scripts/data-guard.mjs).
  publicDir: publicDir(),
  // A second dev server (fixture e2e) gets its own dep-optimiser cache so the two never race.
  cacheDir: process.env.TWIN_PUBLIC_DIR ? "node_modules/.vite-fixture" : "node_modules/.vite",
  build: {
    outDir: "dist",
    // deck.gl + MapLibre are large by nature; split them out so app code changes don't bust their cache.
    chunkSizeWarningLimit: 1500,
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            { name: "maplibre", test: /node_modules[\\/]maplibre-gl/ },
            { name: "deckgl", test: /node_modules[\\/](@deck\.gl|@luma\.gl|@math\.gl|@loaders\.gl|@probe\.gl)/ },
          ],
        },
      },
    },
  },
  test: {
    include: ["src/**/*.test.{ts,tsx}", "scripts/**/*.test.ts"],
  },
});
