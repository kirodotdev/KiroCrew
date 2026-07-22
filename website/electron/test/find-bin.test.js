const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const { findKirocrewBin } = require("../find-bin");

const HOME = "/mock/home";
const RESOURCES = "/mock/resources";
const DIRNAME = "/mock/electron";

const fakeOs = { homedir: () => HOME };

const only = (target) => ({
  accessSync: (p) => { if (p !== target) throw new Error("ENOENT"); },
  constants: { X_OK: fs.constants.X_OK },
});

const none = {
  accessSync: () => { throw new Error("ENOENT"); },
  constants: { X_OK: fs.constants.X_OK },
};

describe("findKirocrewBin", () => {
  it("returns bundled path when it exists", () => {
    const bundled = path.join(RESOURCES, "backend-dist", "kirocrew-backend", "kirocrew-backend");
    const fakeFs = only(bundled);
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, bundled);
  });

  it("returns bundled venv layout (backend-dist/.../bin/kirocrew) when the flat PyInstaller exe is absent", () => {
    const venvLayout = path.join(RESOURCES, "backend-dist", "kirocrew-backend", "bin", "kirocrew");
    const fakeFs = only(venvLayout);
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, venvLayout);
  });

  it("prefers the flat PyInstaller exe over the venv-layout bin/kirocrew", () => {
    const bundled = path.join(RESOURCES, "backend-dist", "kirocrew-backend", "kirocrew-backend");
    const venvLayout = path.join(RESOURCES, "backend-dist", "kirocrew-backend", "bin", "kirocrew");
    const fakeFs = {
      accessSync: (p) => { if (p !== bundled && p !== venvLayout) throw new Error("ENOENT"); },
      constants: { X_OK: fs.constants.X_OK },
    };
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, bundled);
  });

  it("returns ~/.toolbox/bin/kirocrew when bundled paths don't exist", () => {
    const toolboxBin = path.join(HOME, ".toolbox", "bin", "kirocrew");
    const fakeFs = only(toolboxBin);
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, toolboxBin);
  });

  it("returns ~/.local/bin/kirocrew when bundled and toolbox paths don't exist", () => {
    const localBin = path.join(HOME, ".local", "bin", "kirocrew");
    const fakeFs = only(localBin);
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, localBin);
  });

  it("returns ~/.kirocrew-app/.venv/bin/kirocrew when only venv binary exists", () => {
    const venvBin = path.join(HOME, ".kirocrew-app", ".venv", "bin", "kirocrew");
    const fakeFs = only(venvBin);
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, venvBin);
  });

  it("returns ../bin/kirocrew relative to dirname when only that path exists", () => {
    const binPath = path.resolve(DIRNAME, "..", "bin", "kirocrew");
    const fakeFs = only(binPath);
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, binPath);
  });

  it("falls back to bare 'kirocrew' when no candidates are executable", () => {
    const result = findKirocrewBin(none, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, "kirocrew");
  });

  it("returns first match when multiple candidates exist", () => {
    const bundled = path.join(RESOURCES, "backend-dist", "kirocrew-backend", "kirocrew-backend");
    const localBin = path.join(HOME, ".local", "bin", "kirocrew");
    const fakeFs = {
      accessSync: (p) => { if (p !== bundled && p !== localBin) throw new Error("ENOENT"); },
      constants: { X_OK: fs.constants.X_OK },
    };
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, bundled);
  });

  it("handles resourcesPath being undefined", () => {
    const localBin = path.join(HOME, ".local", "bin", "kirocrew");
    const fakeFs = only(localBin);
    const result = findKirocrewBin(fakeFs, fakeOs, path, undefined, DIRNAME);
    assert.equal(result, localBin);
  });

  it("resolves dirname-relative dev path correctly", () => {
    const devBin = path.resolve(DIRNAME, "backend-dist", "kirocrew-backend", "kirocrew-backend");
    const fakeFs = only(devBin);
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, devBin);
  });

  it("skips candidates that throw non-ENOENT errors (e.g. EACCES)", () => {
    const venvBin = path.join(HOME, ".kirocrew-app", ".venv", "bin", "kirocrew");
    const fakeFs = {
      accessSync: (p) => { if (p !== venvBin) throw new Error("EACCES"); },
      constants: { X_OK: fs.constants.X_OK },
    };
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, venvBin);
  });

  // Universal-bundle layout: arch-suffixed backend trees under backend-dist/.
  const archBin = (arch) =>
    path.join(RESOURCES, "backend-dist", `kirocrew-backend-${arch}`, "bin", "kirocrew");
  const armBackend = archBin("arm64");
  const x64Backend = archBin("x64");

  const both = (targets) => ({
    accessSync: (p) => { if (!targets.includes(p)) throw new Error("ENOENT"); },
    constants: { X_OK: fs.constants.X_OK },
  });

  it("picks the arm64 backend tree for arch 'arm64' when both suffixed dirs exist", () => {
    const fakeFs = both([armBackend, x64Backend]);
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME, "arm64");
    assert.equal(result, armBackend);
  });

  it("picks the x64 backend tree for arch 'x64' when both suffixed dirs exist", () => {
    const fakeFs = both([armBackend, x64Backend]);
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME, "x64");
    assert.equal(result, x64Backend);
  });

  it("prefers the arch-suffixed tree over the unsuffixed layout when both exist", () => {
    const unsuffixed = path.join(RESOURCES, "backend-dist", "kirocrew-backend", "bin", "kirocrew");
    const fakeFs = both([armBackend, unsuffixed]);
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME, "arm64");
    assert.equal(result, armBackend);
  });

  it("falls back to the unsuffixed layout when arch-suffixed dirs are absent", () => {
    const unsuffixed = path.join(RESOURCES, "backend-dist", "kirocrew-backend", "bin", "kirocrew");
    const fakeFs = only(unsuffixed);
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME, "arm64");
    assert.equal(result, unsuffixed);
  });

  it("skips arch-suffixed candidates cleanly for an unmapped arch (e.g. 'ia32')", () => {
    const probed = [];
    const unsuffixed = path.join(RESOURCES, "backend-dist", "kirocrew-backend", "bin", "kirocrew");
    const fakeFs = {
      accessSync: (p) => { probed.push(p); if (p !== unsuffixed) throw new Error("ENOENT"); },
      constants: { X_OK: fs.constants.X_OK },
    };
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME, "ia32");
    assert.equal(result, unsuffixed);
    assert.deepStrictEqual(probed.filter((p) => p.includes("kirocrew-backend-")), []);
  });
});
