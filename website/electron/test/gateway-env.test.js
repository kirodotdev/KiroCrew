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

test("Windows consumes packaged bytecode while POSIX redirects runtime caches", () => {
  const cache = String.raw`C:\Users\test\.kiro\crew\cache\pycache`;
  assert.deepStrictEqual(gatewayBytecodeEnvironment("win32", cache, true), {
    PYTHONPYCACHEPREFIX: "",
  });
  assert.deepStrictEqual(gatewayBytecodeEnvironment("win32", cache, false), {
    PYTHONPYCACHEPREFIX: cache,
  });
  assert.deepStrictEqual(gatewayBytecodeEnvironment("darwin", cache, true), {
    PYTHONPYCACHEPREFIX: cache,
  });
  assert.deepStrictEqual(gatewayBytecodeEnvironment("linux", cache, true), {
    PYTHONPYCACHEPREFIX: cache,
  });
});

test("the one desktop gateway spawn uses the hardened environment builder", () => {
  const main = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  const gatewaySpawns = [...main.matchAll(/spawn\(spawnBin, spawnArgs,/g)];

  assert.equal(gatewaySpawns.length, 1, "expected one owned gateway spawn boundary");
  assert.match(
    main,
    /env:\s*buildGatewayEnvironment\(\{[\s\S]*?gatewayBytecodeEnvironment\([\s\S]*?\}\),/,
    "the owned gateway spawn must pass every initial launch and liveness respawn " +
      "through buildGatewayEnvironment",
  );
});
