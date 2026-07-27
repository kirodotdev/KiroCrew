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
  it("uses legacy config before migration and canonical config after it disappears", () => {
    withTempHome((home) => {
      const legacy = path.join(home, ".kirocrew");
      const canonical = path.join(home, ".kiro", "crew");
      fs.mkdirSync(legacy);
      fs.mkdirSync(canonical, { recursive: true });
      fs.writeFileSync(
        path.join(legacy, "config.json"),
        JSON.stringify({ dashboard: { url: "localhost:6777" } }),
      );
      fs.writeFileSync(
        path.join(canonical, "config.json"),
        JSON.stringify({ dashboard: { url: "http://localhost:6888" } }),
      );
      const candidates = [legacy, canonical];

      assert.equal(findConfiguredDashboardPort(fs, path, candidates), 6777);

      fs.rmSync(legacy, { recursive: true });
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
