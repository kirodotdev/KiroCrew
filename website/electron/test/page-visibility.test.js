const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(
  path.join(__dirname, "..", "window-lifecycle.js"),
  "utf8",
);

describe("dashboard page visibility", () => {
  it("keeps Chromium background throttling at its default", () => {
    assert.equal(/backgroundThrottling\s*:\s*false/.test(source), false);
  });
});
