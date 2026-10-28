import { describe, expect, it } from "vitest";
import { APP_TITLE } from "./app";

describe("App", () => {
  it("has the project title", () => {
    expect(APP_TITLE).toBe("Melbourne Traffic Twin");
  });
});
