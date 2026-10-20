"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const {
  buildGatewayEnvironment,
  bundledKiroCliEnvironment,
  bundledKasEnvironment,
  staysWithinStagedTree,
  gatewayBytecodeEnvironment,
  GATEWAY_UTF8_ENV,
} = require("../gateway-env");

// The gateway supervisor's source: the runtime/gateway/ owners that define code
// these pins read, then the facade, as one text because the pins hold over all of
// it.
const SUPERVISOR_FILES = [
  "runtime/gateway/home-lock.js",
  "runtime/gateway/successor-handoff.js",
  "runtime/gateway/failure-dialog.js",
  "runtime/gateway/holder-decision.js",
  "runtime/gateway/shutdown.js",
  "runtime/gateway/reconnect.js",
  "gateway-supervisor.js",
];

for (const [platform, inheritedEncoding] of [
  ["win32", "cp1252"],
  ["darwin", "ascii"],
  ["linux", "latin-1"],
]) {
  test(`${platform} gateway launches override hostile Python encoding`, () => {
    const inherited = {
      PATH: platform === "win32" ? String.raw`C:\Windows\System32` : "/usr/bin",
      PYTHONUTF8: "0",
      PYTHONIOENCODING: inheritedEncoding,
    };

    const env = buildGatewayEnvironment(inherited);

    assert.deepStrictEqual(env, {
      PATH: inherited.PATH,
      PYTHONUTF8: "1",
      PYTHONIOENCODING: "utf-8:backslashreplace",
    });
    assert.equal(
      inherited.PYTHONUTF8,
      "0",
      "must not mutate Electron's environment",
    );
    assert.equal(inherited.PYTHONIOENCODING, inheritedEncoding);
  });
}

test("the gateway UTF-8 contract is explicit and stable", () => {
  assert.deepStrictEqual(GATEWAY_UTF8_ENV, {
    PYTHONUTF8: "1",
    PYTHONIOENCODING: "utf-8:backslashreplace",
  });
});

test("packaged bundles consume shipped bytecode; macOS also forbids writing it", () => {
  const cache = String.raw`C:\Users\test\.kiro\crew\cache\pycache`;
  const posixCache = "/Users/test/.kiro/crew/cache/pycache";

  // Windows: adjacent caches, writes allowed (Authenticode seals no resource
  // tree, and modules outside the traced closure benefit from caching).
  assert.deepStrictEqual(gatewayBytecodeEnvironment("win32", cache, true), {
    PYTHONPYCACHEPREFIX: "",
  });

  // macOS: adjacent caches AND no writes. codesign seals every file under
  // Contents/, so a single post-signing .pyc makes Gatekeeper call the app
  // "damaged". Redirecting instead would also prevent that, but a set prefix
  // makes CPython ignore the shipped closure and recompile it per version.
  assert.deepStrictEqual(gatewayBytecodeEnvironment("darwin", posixCache, true), {
    PYTHONPYCACHEPREFIX: "",
    PYTHONDONTWRITEBYTECODE: "1",
  });

  // Unpackaged: a dev tree ships no precompiled closure, so redirect.
  assert.deepStrictEqual(gatewayBytecodeEnvironment("win32", cache, false), {
    PYTHONPYCACHEPREFIX: cache,
  });
  assert.deepStrictEqual(gatewayBytecodeEnvironment("darwin", posixCache, false), {
    PYTHONPYCACHEPREFIX: posixCache,
  });

  // Linux: may be read-only, but has no signature to protect, so redirect.
  assert.deepStrictEqual(gatewayBytecodeEnvironment("linux", posixCache, true), {
    PYTHONPYCACHEPREFIX: posixCache,
  });
});

test("the macOS lock is a write ban, not a redirect", () => {
  // A redirect and a ban are not interchangeable: a non-empty prefix would send
  // bytecode outside the bundle but also make CPython ignore the shipped
  // checked-hash caches, so every version's first launch recompiles the tree. The packaged macOS answer must therefore be exactly
  // "adjacent caches, no writes".
  const env = gatewayBytecodeEnvironment("darwin", "/some/cache", true);
  assert.equal(env.PYTHONDONTWRITEBYTECODE, "1");
  assert.equal(
    env.PYTHONPYCACHEPREFIX,
    "",
    "a non-empty prefix on packaged macOS discards the shipped caches",
  );
});

test("a shipped bundled kiro-cli is exported as its directory with the update check off", () => {
  // The backend ranks this directory above every system install but below the
  // KIROCREW_KIRO_BIN operator override, so what the app was built against is
  // what runs. A DIRECTORY, not the binary: the entry name is the backend's
  // constant. KIRO_NO_AUTO_UPDATE is set here once so the whole gateway tree
  // inherits it, rather than at every spawn site.
  const seen = [];
  const fakeFs = {
    statSync(target) {
      seen.push(target);
      return { isDirectory: () => true };
    },
  };

  const env = bundledKiroCliEnvironment(fakeFs, path.posix, "/Applications/KiroCrew.app/Contents/Resources");

  assert.deepStrictEqual(env, {
    KIROCREW_BUNDLED_KIRO_DIR: "/Applications/KiroCrew.app/Contents/Resources/backend-dist/kiro-cli",
    KIRO_NO_AUTO_UPDATE: "1",
  });
  assert.deepStrictEqual(seen, ["/Applications/KiroCrew.app/Contents/Resources/backend-dist/kiro-cli"]);
});

test("a build without the payload exports nothing, so discovery falls through", () => {
  // BUNDLE_KIRO_CLI=0 ships no directory; the env var must be ABSENT rather
  // than empty, because the backend treats any set value as a directory
  // to rank first -- and the update switch must not leak onto a user's own CLI.
  const missing = {
    statSync() {
      const error = new Error("ENOENT");
      error.code = "ENOENT";
      throw error;
    },
  };
  assert.deepStrictEqual(bundledKiroCliEnvironment(missing, path.posix, "/res"), {});

  // A file at that path is not a layout the backend can use either.
  const file = { statSync: () => ({ isDirectory: () => false }) };
  assert.deepStrictEqual(bundledKiroCliEnvironment(file, path.posix, "/res"), {});
});

test("a source checkout has no resources path and is never probed", () => {
  const fakeFs = {
    statSync() {
      throw new Error("must not stat without a resources path");
    },
  };
  assert.deepStrictEqual(bundledKiroCliEnvironment(fakeFs, path.posix, undefined), {});
  assert.deepStrictEqual(bundledKiroCliEnvironment(fakeFs, path.posix, ""), {});
});

test("a bundled copy is exported only after it answers --version on this machine", () => {
  // The gateway resolver ranks the bundled directory first without probing it,
  // so a copy that cannot run here (glibc floor, quarantine, truncated payload)
  // would fail every session. The shell asks once, with the update check off,
  // and a green answer keeps the pre-probe contract byte for byte.
  const fakeFs = { statSync: () => ({ isDirectory: () => true }) };
  const calls = [];
  const spawnSync = (file, args, options) => {
    calls.push({ file, args, options });
    return { status: 0, signal: null };
  };
  const env = bundledKiroCliEnvironment(fakeFs, path.posix, "/res", {
    platform: "linux",
    spawnSync,
    env: { PATH: "/usr/bin", KIRO_NO_AUTO_UPDATE: undefined },
    log: () => assert.fail("a passing probe logs nothing"),
  });
  assert.deepStrictEqual(env, {
    KIROCREW_BUNDLED_KIRO_DIR: "/res/backend-dist/kiro-cli",
    KIRO_NO_AUTO_UPDATE: "1",
  });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].file, "/res/backend-dist/kiro-cli/kiro-cli-chat");
  assert.deepStrictEqual(calls[0].args, ["--version"]);
  assert.equal(calls[0].options.env.KIRO_NO_AUTO_UPDATE, "1");
  assert.equal(calls[0].options.env.PATH, "/usr/bin");
  assert.ok(calls[0].options.timeout > 0, "the probe is bounded");
});

test("the probe names kiro-cli.exe on Windows", () => {
  const fakeFs = { statSync: () => ({ isDirectory: () => true }) };
  const seen = [];
  bundledKiroCliEnvironment(fakeFs, path.win32, String.raw`C:\App\resources`, {
    platform: "win32",
    spawnSync: (file) => { seen.push(file); return { status: 0, signal: null }; },
  });
  assert.deepStrictEqual(seen, [String.raw`C:\App\resources\backend-dist\kiro-cli\kiro-cli.exe`]);
});

test("a bundled copy that does not run here is NOT exported, and the reason is logged", () => {
  // Fall-through, not failure: the env var must be ABSENT so discovery uses the
  // user's own kiro-cli, and KIRO_NO_AUTO_UPDATE must not leak onto it.
  const fakeFs = { statSync: () => ({ isDirectory: () => true }) };
  const logged = [];
  const nonzero = bundledKiroCliEnvironment(fakeFs, path.posix, "/res", {
    platform: "linux",
    spawnSync: () => ({ status: 1, signal: null }),
    log: (line) => logged.push(line),
  });
  assert.deepStrictEqual(nonzero, {});

  const enoent = new Error("spawnSync ENOENT");
  const failedSpawn = bundledKiroCliEnvironment(fakeFs, path.posix, "/res", {
    platform: "darwin",
    spawnSync: () => ({ error: enoent, status: null, signal: null }),
    log: (line) => logged.push(line),
  });
  assert.deepStrictEqual(failedSpawn, {});

  const timedOut = bundledKiroCliEnvironment(fakeFs, path.posix, "/res", {
    platform: "linux",
    spawnSync: () => ({ error: new Error("ETIMEDOUT"), status: null, signal: "SIGTERM" }),
    log: (line) => logged.push(line),
  });
  assert.deepStrictEqual(timedOut, {});

  assert.equal(logged.length, 3);
  for (const line of logged) {
    assert.match(line, /bundled kiro-cli at \/res\/backend-dist\/kiro-cli\/kiro-cli-chat does not run here/);
    assert.match(line, /falling through to the kiro-cli installed on this machine/);
  }
  assert.match(logged[0], /exit 1/);
  assert.match(logged[1], /ENOENT/);
});

// --- bundled KAS engine -------------------------------------------------------

/** A fake resources tree holding one staged KAS root; `files` maps path -> text. */
function kasResources(files) {
  const dirs = new Set();
  for (const file of Object.keys(files)) {
    let d = path.posix.dirname(file);
    while (d && d !== "/" && !dirs.has(d)) {
      dirs.add(d);
      d = path.posix.dirname(d);
    }
  }
  return {
    statSync(target) {
      if (dirs.has(target)) return { isDirectory: () => true };
      if (target in files) return { isDirectory: () => false };
      throw Object.assign(new Error(`ENOENT ${target}`), { code: "ENOENT" });
    },
    readFileSync(target) {
      if (target in files) return files[target];
      throw Object.assign(new Error(`ENOENT ${target}`), { code: "ENOENT" });
    },
  };
}

const KAS_RECORD = JSON.stringify({ node_bin: "node/node-v22.23.3-linux-x64/bin/node" });

test("a staged KAS engine is exported as its one root, read from BUNDLED-VERSION", () => {
  // The shell carries no pin: the version it probes is whatever the build staged.
  const fs = kasResources({
    "/res/backend-dist/kas/BUNDLED-VERSION": "0.66.33\n",
    "/res/backend-dist/kas/0.66.33/install.json": KAS_RECORD,
  });
  const env = bundledKasEnvironment(fs, path.posix, "/res", {});
  assert.deepStrictEqual(env, { KIROCREW_BUNDLED_KAS_DIR: "/res/backend-dist/kas" });
});

test("a universal build's one KAS root serves both architectures", () => {
  // The universal Node holds both slices, so the arm64 and the x64 shell export
  // the same root; arch-suffixed roots are not read.
  const fs = kasResources({
    "/res/backend-dist/kas/BUNDLED-VERSION": "0.66.33",
    "/res/backend-dist/kas/0.66.33/install.json": JSON.stringify({
      node_bin: "node/node-v22.23.3-darwin-arm64/bin/node",
      node_key: "darwin-universal",
    }),
  });
  assert.deepStrictEqual(bundledKasEnvironment(fs, path.posix, "/res"), {
    KIROCREW_BUNDLED_KAS_DIR: "/res/backend-dist/kas",
  });
  const suffixedOnly = kasResources({
    "/res/backend-dist/kas-arm64/BUNDLED-VERSION": "0.66.33",
    "/res/backend-dist/kas-arm64/0.66.33/install.json": KAS_RECORD,
  });
  assert.deepStrictEqual(bundledKasEnvironment(suffixedOnly, path.posix, "/res"), {});
});

test("no KAS payload, a source checkout, or a root without its record exports nothing", () => {
  assert.deepStrictEqual(bundledKasEnvironment(kasResources({}), path.posix, "/res", {}), {});
  assert.deepStrictEqual(bundledKasEnvironment(kasResources({}), path.posix, undefined, {}), {});
  const noRecord = kasResources({ "/res/backend-dist/kas/BUNDLED-VERSION": "0.66.33" });
  assert.deepStrictEqual(bundledKasEnvironment(noRecord, path.posix, "/res", {}), {});
});

test("the staged Node is never run at boot, even when a spawnSync is handed in", () => {
  // The route is off by default, so the shell spends nothing on it per launch: the
  // build probed the staged Node, and a copy that cannot run here fails at session
  // spawn for the users who turned it on.
  const fs = kasResources({
    "/res/backend-dist/kas/BUNDLED-VERSION": "0.66.33",
    "/res/backend-dist/kas/0.66.33/install.json": KAS_RECORD,
  });
  const env = bundledKasEnvironment(fs, path.posix, "/res", {
    spawnSync: () => assert.fail("bundledKasEnvironment executed the staged Node"),
  });
  assert.deepStrictEqual(env, { KIROCREW_BUNDLED_KAS_DIR: "/res/backend-dist/kas" });
});

test("a staged record whose node_bin or version could leave the staged tree is never executed", () => {
  // The backend seals the staged root read-only for sandboxed children; this is the
  // shell's own fence over the same two fields, so a rewritten record cannot make
  // the (unsandboxed) gateway run a file outside what the build staged.
  const probe = { log: () => {} };
  for (const nodeBin of ["../../../tmp/evil", "/tmp/evil", "C:\\evil\\node.exe", "node/./bin/node", "", "node//bin"]) {
    const fs = kasResources({
      "/res/backend-dist/kas/BUNDLED-VERSION": "0.66.33",
      "/res/backend-dist/kas/0.66.33/install.json": JSON.stringify({ node_bin: nodeBin }),
    });
    assert.deepStrictEqual(bundledKasEnvironment(fs, path.posix, "/res", probe), {}, nodeBin);
  }
  for (const version of ["../kas-x64/0.66.33", "0.66.33/..", "..", "a/b"]) {
    const fs = kasResources({
      "/res/backend-dist/kas/BUNDLED-VERSION": version,
      [`/res/backend-dist/kas/${version}/install.json`]: KAS_RECORD,
    });
    assert.deepStrictEqual(bundledKasEnvironment(fs, path.posix, "/res", probe), {}, version);
  }
  assert.strictEqual(staysWithinStagedTree("node/node-v22.23.3-linux-x64/bin/node"), true);
  assert.strictEqual(staysWithinStagedTree("node\\node-v22.23.3-win-x64\\node.exe"), true);
  assert.strictEqual(staysWithinStagedTree("0.66.33", { single: true }), true);
  assert.strictEqual(staysWithinStagedTree(42), false);
});

test("the owned gateway spawn exports the bundled kiro-cli directory after probing it", () => {
  const supervisor = SUPERVISOR_FILES
    .map((file) => fs.readFileSync(path.join(__dirname, "..", file), "utf8"))
    .join("\n");
  assert.match(
    supervisor,
    /env:\s*buildGatewayEnvironment\(\{[\s\S]*?bundledKiroCliEnvironment\(fs, path, processObj\.resourcesPath, \{[\s\S]*?spawnSync: defaultSpawnSync,[\s\S]*?log: glog,[\s\S]*?\}\)/,
  );
});

test("the one desktop gateway spawn uses the hardened environment builder", () => {
  const supervisor = SUPERVISOR_FILES
    .map((file) => fs.readFileSync(path.join(__dirname, "..", file), "utf8"))
    .join("\n");
  const gatewaySpawns = [...supervisor.matchAll(/spawn\(spawnBin, spawnArgs,/g)];

  assert.equal(gatewaySpawns.length, 1, "expected one owned gateway spawn boundary");
  assert.match(
    supervisor,
    /env:\s*buildGatewayEnvironment\(\{[\s\S]*?gatewayBytecodeEnvironment\([\s\S]*?\},\s*\{ bundled, platform: processObj\.platform \}\),/,
    "the owned gateway spawn must pass every initial launch and liveness respawn " +
      "through buildGatewayEnvironment",
  );
});

for (const platform of ["win32", "darwin", "linux"]) {
  test(`${platform} bundled gateway removes inherited Python import overrides`, () => {
    const inherited = {
      PATH: "system-bin",
      PYTHONPATH: "checkout",
      PYTHONHOME: "other-python",
      PYTHONNOUSERSITE: "",
      PythonPath: "mixed-checkout",
      pythonhome: "mixed-python",
      PythonNoUserSite: "",
    };
    const before = { ...inherited };
    const env = buildGatewayEnvironment(inherited, { bundled: true, platform });
    assert.equal(env.PYTHONPATH, undefined);
    assert.equal(env.PYTHONHOME, undefined);
    assert.equal(env.PYTHONNOUSERSITE, inherited.PYTHONNOUSERSITE);
    assert.equal(env.PythonNoUserSite, inherited.PythonNoUserSite);
    assert.equal(
      buildGatewayEnvironment({}, { bundled: true, platform }).PYTHONNOUSERSITE,
      undefined,
      "bundled isolation must not disable user-site imports in Python descendants",
    );
    for (const key of ["PythonPath", "pythonhome"]) {
      assert.equal(env[key], platform === "win32" ? undefined : inherited[key]);
    }
    assert.equal(env.PATH, inherited.PATH);
    assert.deepStrictEqual(inherited, before);
    const development = buildGatewayEnvironment(inherited, { bundled: false, platform });
    for (const key of Object.keys(inherited)) {
      assert.equal(development[key], inherited[key]);
    }
  });
}
