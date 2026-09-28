"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const {
  buildGatewayEnvironment,
  bundledKiroCliEnvironment,
  gatewayBytecodeEnvironment,
  GATEWAY_UTF8_ENV,
} = require("../gateway-env");

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

test("the owned gateway spawn exports the bundled kiro-cli directory after probing it", () => {
  const supervisor = fs.readFileSync(path.join(__dirname, "..", "gateway-supervisor.js"), "utf8");
  assert.match(
    supervisor,
    /env:\s*buildGatewayEnvironment\(\{[\s\S]*?bundledKiroCliEnvironment\(fs, path, processObj\.resourcesPath, \{[\s\S]*?spawnSync: defaultSpawnSync,[\s\S]*?log: glog,[\s\S]*?\}\)/,
  );
});

test("the one desktop gateway spawn uses the hardened environment builder", () => {
  const supervisor = fs.readFileSync(
    path.join(__dirname, "..", "gateway-supervisor.js"),
    "utf8",
  );
  const gatewaySpawns = [...supervisor.matchAll(/spawn\(spawnBin, spawnArgs,/g)];

  assert.equal(gatewaySpawns.length, 1, "expected one owned gateway spawn boundary");
  assert.match(
    supervisor,
    /env:\s*buildGatewayEnvironment\(\{[\s\S]*?gatewayBytecodeEnvironment\([\s\S]*?\}\),/,
    "the owned gateway spawn must pass every initial launch and liveness respawn " +
      "through buildGatewayEnvironment",
  );
});
