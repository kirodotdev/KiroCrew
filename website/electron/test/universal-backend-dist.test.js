"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const hook = require("../scripts/universal-backend-dist.js").default;
const pkg = require("../package.json");

const PRODUCT = "Kiro Crew";

function makeApp(dist, dirName, withBackend) {
  const resources = path.join(dist, dirName, `${PRODUCT}.app`, "Contents", "Resources");
  fs.mkdirSync(resources, { recursive: true });
  if (withBackend) {
    const tree = path.join(resources, "backend-dist", "kirocrew-backend-x64", "bin");
    fs.mkdirSync(tree, { recursive: true });
    fs.writeFileSync(path.join(tree, "kirocrew"), "x64 backend");
  }
  return path.join(dist, dirName);
}

function run(appOutDir, electronPlatformName = "darwin") {
  return hook({
    appOutDir,
    electronPlatformName,
    packager: { appInfo: { productFilename: PRODUCT } },
  });
}

function backendOf(appOutDir) {
  return path.join(appOutDir, `${PRODUCT}.app`, "Contents", "Resources", "backend-dist");
}

describe("universal-backend-dist afterPack hook", () => {
  it("is wired as build.afterPack", () => {
    assert.equal(pkg.build.afterPack, "scripts/universal-backend-dist.js");
  });

  it("takes backend-dist out of both temp apps and puts the x64 copy back in the merged app", async () => {
    const dist = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ubd-"));
    try {
      const x64 = makeApp(dist, "mac-universal-x64-temp", true);
      const arm64 = makeApp(dist, "mac-universal-arm64-temp", true);

      await run(x64);
      await run(arm64);
      assert.equal(fs.existsSync(backendOf(x64)), false);
      assert.equal(fs.existsSync(backendOf(arm64)), false);

      const merged = makeApp(dist, "mac-universal", false);
      await run(merged);
      assert.equal(
        fs.readFileSync(
          path.join(backendOf(merged), "kirocrew-backend-x64", "bin", "kirocrew"),
          "utf8",
        ),
        "x64 backend",
      );
      assert.equal(fs.existsSync(path.join(dist, ".backend-dist-x64")), false);
    } finally {
      fs.rmSync(dist, { recursive: true, force: true });
    }
  });

  it("fails the merged app when no backend was stashed, so a backend-less universal app cannot ship", async () => {
    const dist = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ubd-"));
    try {
      const merged = makeApp(dist, "mac-universal", false);
      await assert.rejects(run(merged), { code: "ENOENT" });
    } finally {
      fs.rmSync(dist, { recursive: true, force: true });
    }
  });

  it("leaves single-arch and non-macOS packs alone", async () => {
    const dist = fs.mkdtempSync(path.join(os.tmpdir(), "kc-ubd-"));
    try {
      const single = makeApp(dist, "mac-arm64", true);
      await run(single);
      assert.equal(fs.existsSync(backendOf(single)), true);

      const temp = makeApp(dist, "mac-universal-x64-temp", true);
      await run(temp, "linux");
      assert.equal(fs.existsSync(backendOf(temp)), true);
    } finally {
      fs.rmSync(dist, { recursive: true, force: true });
    }
  });
});
