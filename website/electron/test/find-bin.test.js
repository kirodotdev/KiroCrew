const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const path = require("path");
const fs = require("fs");
const { findKirocrewBin, findSshBin } = require("../find-bin");

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
  it("returns the bundled backend launcher (backend-dist/.../bin/kirocrew) when it exists", () => {
    const venvLayout = path.join(RESOURCES, "backend-dist", "kirocrew-backend", "bin", "kirocrew");
    const fakeFs = only(venvLayout);
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME);
    assert.equal(result, venvLayout);
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

  it("falls back to bare 'kirocrew' when no candidates are executable (POSIX)", () => {
    const result = findKirocrewBin(none, fakeOs, path, RESOURCES, DIRNAME, "x64", false);
    assert.equal(result, "kirocrew");
  });

  it("falls back to bare 'kirocrew.exe' when no candidates are executable (Windows)", () => {
    const result = findKirocrewBin(none, fakeOs, path, RESOURCES, DIRNAME, "x64", true);
    assert.equal(result, "kirocrew.exe");
  });

  it("finds the venv Scripts\\kirocrew.exe two levels up from electron/ on Windows", () => {
    const venvExe = path.resolve(DIRNAME, "..", "..", ".venv", "Scripts", "kirocrew.exe");
    const fakeFs = only(venvExe);
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME, "x64", true);
    assert.equal(result, venvExe);
  });

  it("finds the bundled Windows backend Scripts\\kirocrew.exe on Windows", () => {
    const bundledExe = path.join(
      RESOURCES, "backend-dist", "kirocrew-backend", "Scripts", "kirocrew.exe"
    );
    const fakeFs = only(bundledExe);
    const result = findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME, "x64", true);
    assert.equal(result, bundledExe);
  });

  // A real Windows bundle contains BOTH launchers: build-desktop.sh writes
  // bin\kirocrew.cmd, and the `pip install` that populates the tree also drops
  // a console-script Scripts\kirocrew.exe. Every Windows case above uses
  // only() -- a one-candidate world -- so none of them could express which of
  // the two wins. That gap let the two swap places and reach a nightly, where
  // the build-time resolver gate failed with "the builder output layout and
  // find-bin.js candidate list have drifted apart".
  const present = (...targets) => {
    const set = new Set(targets.map((t) => path.resolve(t)));
    return {
      accessSync: (p) => {
        if (set.has(path.resolve(p))) return;
        const e = new Error("ENOENT");
        e.code = "ENOENT";
        throw e;
      },
      constants: { X_OK: fs.constants.X_OK },
    };
  };

  it("prefers the relocatable bin\\kirocrew.cmd over the bundle's Scripts\\kirocrew.exe", () => {
    // THE REGRESSION. pip's console-script .exe embeds the ABSOLUTE interpreter
    // path of the machine that built it (distlib), so inside a shipped bundle it
    // points at a build-agent path like D:\a\KiroCrew\... that does not exist on
    // the user's machine. The .cmd shim resolves the interpreter through %~dp0
    // and is the only relocatable launcher of the two -- so it must win whenever
    // both are present, which in a real bundle is always.
    const cmd = path.join(RESOURCES, "backend-dist", "kirocrew-backend", "bin", "kirocrew.cmd");
    const exe = path.join(RESOURCES, "backend-dist", "kirocrew-backend", "Scripts", "kirocrew.exe");
    const result = findKirocrewBin(present(cmd, exe), fakeOs, path, RESOURCES, DIRNAME, "x64", true);
    assert.equal(result, cmd);
  });

  it("prefers the bundle's Scripts\\kirocrew.exe over a user-level install", () => {
    // A packaged app must run the backend it shipped with, never whatever
    // happens to be installed on the machine.
    const exe = path.join(RESOURCES, "backend-dist", "kirocrew-backend", "Scripts", "kirocrew.exe");
    const userLocal = path.join(HOME, ".local", "bin", "kirocrew.exe");
    const result = findKirocrewBin(
      present(exe, userLocal), fakeOs, path, RESOURCES, DIRNAME, "x64", true
    );
    assert.equal(result, exe);
  });

  it("still finds a user-level kirocrew.exe when nothing is bundled", () => {
    const userLocal = path.join(HOME, ".local", "bin", "kirocrew.exe");
    const result = findKirocrewBin(
      present(userLocal), fakeOs, path, RESOURCES, DIRNAME, "x64", true
    );
    assert.equal(result, userLocal);
  });

  it("does not probe Windows .exe candidates on POSIX", () => {
    const probed = [];
    const fakeFs = {
      accessSync: (p) => { probed.push(p); throw new Error("ENOENT"); },
      constants: { X_OK: fs.constants.X_OK },
    };
    findKirocrewBin(fakeFs, fakeOs, path, RESOURCES, DIRNAME, "x64", false);
    assert.deepStrictEqual(probed.filter((p) => p.endsWith(".exe")), []);
  });

  it("returns first match when multiple candidates exist", () => {
    const bundled = path.join(RESOURCES, "backend-dist", "kirocrew-backend", "bin", "kirocrew");
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
    const devBin = path.resolve(DIRNAME, "backend-dist", "kirocrew-backend", "bin", "kirocrew");
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

describe("findSshBin", () => {
  const executable = (...present) => ({
    accessSync: (p) => { if (!present.includes(p)) throw new Error("ENOENT"); },
    constants: { X_OK: fs.constants.X_OK },
  });

  it("takes /usr/bin/ssh and never consults PATH on POSIX", () => {
    const planted = executable("/home/u/.local/bin/ssh", "/usr/bin/ssh");
    assert.equal(findSshBin(planted, path.posix, false), "/usr/bin/ssh");
  });

  it("finds a NixOS system ssh in the trusted system profile", () => {
    const nix = "/run/current-system/sw/bin/ssh";
    assert.equal(findSshBin(executable(nix), path.posix, false), nix);
  });

  it("ignores an ssh that exists only in a PATH-only directory", () => {
    const result = findSshBin(executable("/opt/homebrew/bin/ssh"), path.posix, false);
    assert.equal(result, "/usr/bin/ssh");
  });

  // `realpathSync.native` stands in for the kernel's answer about
  // `\\?\GLOBALROOT\SystemRoot`; `resolved` is what it returns, or an Error it throws.
  const kernelRoot = (resolved) => {
    const probed = [];
    return {
      probed,
      realpathSync: {
        native: (p) => {
          probed.push(p);
          if (resolved instanceof Error) throw resolved;
          return resolved;
        },
      },
    };
  };

  it("takes the in-box client under the Windows directory the kernel names", () => {
    const fsMod = kernelRoot("D:\\Windows");
    assert.equal(findSshBin(fsMod, path, true), "D:\\Windows\\System32\\OpenSSH\\ssh.exe");
    assert.deepStrictEqual(fsMod.probed, ["\\\\?\\GLOBALROOT\\SystemRoot"]);
  });

  it("strips a namespaced prefix from the kernel's answer", () => {
    assert.equal(
      findSshBin(kernelRoot("\\\\?\\C:\\WINDOWS"), path, true),
      "C:\\WINDOWS\\System32\\OpenSSH\\ssh.exe",
    );
  });

  it("ignores SystemRoot, which HKCU\\Environment can rewrite", () => {
    const saved = process.env.SystemRoot;
    process.env.SystemRoot = "C:\\Planted";
    try {
      assert.equal(
        findSshBin(kernelRoot("D:\\Windows"), path, true),
        "D:\\Windows\\System32\\OpenSSH\\ssh.exe",
      );
    } finally {
      if (saved === undefined) delete process.env.SystemRoot;
      else process.env.SystemRoot = saved;
    }
  });

  it("refuses on Windows when the kernel's answer is not a plain X:\\Windows", () => {
    for (const resolved of [
      new Error("EINVAL"),
      "\\\\?\\Volume{0b1e5a8c-0000-0000-0000-100000000000}\\Windows",
      "C:\\Users\\u\\Windows",
      "C:\\WINNT",
      "",
    ]) {
      assert.equal(findSshBin(kernelRoot(resolved), path, true), null, String(resolved));
    }
  });
});
