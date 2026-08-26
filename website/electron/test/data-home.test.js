const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { findConfiguredDashboardPort } = require("../data-home");

function withTempHome(run) {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "kirocrew-desktop-home-"));
  try {
    return run(home);
  } finally {
    fs.rmSync(home, { recursive: true, force: true });
  }
}

describe("findConfiguredDashboardPort", () => {
  it("uses the first readable candidate, falling through when it disappears", () => {
    withTempHome((home) => {
      const first = path.join(home, "first");
      const second = path.join(home, "second");
      fs.mkdirSync(first);
      fs.mkdirSync(second, { recursive: true });
      fs.writeFileSync(
        path.join(first, "config.json"),
        JSON.stringify({ dashboard: { url: "localhost:6777" } }),
      );
      fs.writeFileSync(
        path.join(second, "config.json"),
        JSON.stringify({ dashboard: { url: "http://localhost:6888" } }),
      );
      const candidates = [first, second];

      assert.equal(findConfiguredDashboardPort(fs, path, candidates), 6777);

      fs.rmSync(first, { recursive: true });
      assert.equal(findConfiguredDashboardPort(fs, path, candidates), 6888);
    });
  });

  it("skips malformed and out-of-range configured ports", () => {
    withTempHome((home) => {
      const first = path.join(home, "first");
      const second = path.join(home, "second");
      fs.mkdirSync(first);
      fs.mkdirSync(second);
      fs.writeFileSync(path.join(first, "config.json"), "{broken");
      fs.writeFileSync(
        path.join(second, "config.json"),
        JSON.stringify({ dashboard: { url: "http://localhost:70000" } }),
      );

      assert.equal(findConfiguredDashboardPort(fs, path, [first, second]), null);
    });
  });
});
