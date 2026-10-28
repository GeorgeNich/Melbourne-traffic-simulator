// Look screenshots for review: real data, 1440x900, fps readout hidden, one per camera preset (button click, fly-to,
// then full tile load). Saved to web/screenshots/<LOOK_DIR>/<LOOK_PREFIX>-<preset>.png (gitignored).
// Default: screenshots/stage-a/after-<preset>.png.   Run: pnpm -C web e2e --project=real look
import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { test } from "@playwright/test";
import { CAMERA_PRESETS } from "../src/camera/presets";

const DIR = resolve(import.meta.dirname, "../screenshots", process.env.LOOK_DIR ?? "stage-a");
const PREFIX = process.env.LOOK_PREFIX ?? "ribbons";
const SUFFIX = process.env.LOOK_SUFFIX ?? "";
const QUERY = process.env.LOOK_QUERY ?? "";
const ONLY = process.env.LOOK_PRESETS?.split(",");

test("look screenshots per preset (real data, no fps readout)", async ({ page }, info) => {
  test.skip(info.project.name !== "real", "look screenshots are of the real data only");
  mkdirSync(DIR, { recursive: true });
  await page.goto(`/${QUERY}`);
  await page.waitForFunction(() => window.__twinReady === true, null, { timeout: 90_000 });
  await page.addStyleTag({ content: '[data-testid="fps"] { display: none !important; }' });
  for (const preset of CAMERA_PRESETS.filter((p) => !ONLY || ONLY.includes(p.id))) {
    await page.getByRole("button", { name: preset.label, exact: true }).click();
    await page.waitForFunction(() => !window.__twinMap!.isMoving(), null, { timeout: 30_000 });
    await page.evaluate(
      () =>
        new Promise<void>((done) => {
          const map = window.__twinMap!;
          if (map.loaded() && map.areTilesLoaded()) return done();
          map.once("idle", () => done());
        }),
    );
    await page.waitForTimeout(400); // label fade-in
    await page.screenshot({ path: resolve(DIR, `${PREFIX}-${preset.id}${SUFFIX}.png`) });
  }
});
