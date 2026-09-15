"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const {
  buildGatewayEnvironment,
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
