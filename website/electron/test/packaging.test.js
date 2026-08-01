const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "..");

describe("electron-builder files list", () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
  const bundledFiles = pkg.build.files;

  it("includes every local require() from main.js", () => {
    const main = fs.readFileSync(path.join(ROOT, "main.js"), "utf8");
    const localRequires = [...main.matchAll(/require\("\.\/([^"]+)"\)/g)].map(m => m[1] + ".js");

    const missing = localRequires.filter(f => !bundledFiles.includes(f));
    assert.deepStrictEqual(missing, [], `Missing from build.files: ${missing.join(", ")}`);
  });

  it("does not reference files that no longer exist", () => {
    const stale = bundledFiles.filter(f => !fs.existsSync(path.join(ROOT, f)));
    assert.deepStrictEqual(stale, [], `Stale entries in build.files: ${stale.join(", ")}`);
  });
});


describe("macOS bundle naming", () => {
  const pkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
  const extendInfo = pkg.build.mac.extendInfo || {};
  const buildScript = fs.readFileSync(
    path.resolve(ROOT, "..", "..", "packaging", "build-desktop.sh"),
    "utf8"
  );

  it("keeps CFBundleName aligned with productName for Electron helpers", () => {
    assert.equal(pkg.build.productName, "KiroCrew");
    assert.equal(
      Object.hasOwn(extendInfo, "CFBundleName"),
      false,
      "CFBundleName overrides break Electron helper-app discovery"
    );
  });

  it("uses CFBundleDisplayName for spaced stable and nightly names", () => {
    assert.equal(extendInfo.CFBundleDisplayName, "Kiro Crew");
    assert.match(
      buildScript,
      /-c\.mac\.extendInfo\.CFBundleDisplayName=Kiro Crew Nightly/
    );
    assert.doesNotMatch(buildScript, /-c\.mac\.extendInfo\.CFBundleName=/);
  });
});


describe("uninstall data preservation contract", () => {
  const electronPkg = JSON.parse(fs.readFileSync(path.join(ROOT, "package.json"), "utf8"));
  const websitePkg = JSON.parse(
    fs.readFileSync(path.resolve(ROOT, "..", "package.json"), "utf8")
  );
  const main = fs.readFileSync(path.join(ROOT, "main.js"), "utf8");

  it("defines no package-manager uninstall hooks", () => {
    for (const [name, scripts] of [
      ["electron", electronPkg.scripts || {}],
      ["website", websitePkg.scripts || {}],
    ]) {
      assert.equal(Object.hasOwn(scripts, "preuninstall"), false, `${name} preuninstall`);
      assert.equal(Object.hasOwn(scripts, "postuninstall"), false, `${name} postuninstall`);
    }
  });

  it("keeps the Squirrel uninstall handler shortcut-only", () => {
    const match = main.match(
      /else if \(cmd === "--squirrel-uninstall"\) \{([\s\S]*?)\n  \} else if/
    );
    assert.ok(match, "expected an explicit Squirrel uninstall branch");
    assert.equal(
      match[1].trim(),
      'run(["--removeShortcut=" + target]);',
      "Squirrel uninstall must remain shortcut-only and never touch the data home"
    );
    assert.notEqual(
      electronPkg.build.nsis?.deleteAppDataOnUninstall,
      true,
      "desktop uninstall must not opt into deleting app data"
    );
  });
});
