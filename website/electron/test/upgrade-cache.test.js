"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");

const { clearCacheOnUpgrade } = require("../upgrade-cache");

function fakes(lastCacheVersion, { failClear = false } = {}) {
  const values = { lastCacheVersion };
  const calls = { clearCache: 0, clearStorageData: [] };
  const store = {
    get: (key, fallback) => (key in values ? values[key] : fallback),
    set: (key, value) => { values[key] = value; },
  };
  const session = {
    clearCache: async () => {
      calls.clearCache += 1;
      if (failClear) throw new Error("disk busy");
    },
    clearStorageData: async (options) => { calls.clearStorageData.push(options); },
  };
  return { store, session, values, calls };
}

const gateway = (version) => async () => ({ version });
const run = (deps, appVersion, probe) =>
  clearCacheOnUpgrade({ ...deps, appVersion, probe, log: () => {} });

test("a new app version clears the cache once and records the pair", async () => {
  const deps = fakes("0.9.0+0.9.0");
  assert.equal(await run(deps, "0.9.1", gateway("0.9.1")), true);
  assert.equal(deps.calls.clearCache, 1);
  assert.deepEqual(deps.calls.clearStorageData, [{ storages: ["serviceworkers", "cachestorage"] }]);
  assert.equal(deps.values.lastCacheVersion, "0.9.1+0.9.1");

  // The next launch on the same pair leaves the cache alone.
  assert.equal(await run(deps, "0.9.1", gateway("0.9.1")), false);
  assert.equal(deps.calls.clearCache, 1);
});

test("the same app and gateway versions do not clear the cache", async () => {
  const deps = fakes("0.9.1+0.9.1");
  assert.equal(await run(deps, "0.9.1", gateway("0.9.1")), false);
  assert.equal(deps.calls.clearCache, 0);
  assert.deepEqual(deps.calls.clearStorageData, []);
});

test("an older gateway kept after the upgrade does not use up the clear", async () => {
  const deps = fakes("0.9.0+0.9.0");
  await run(deps, "0.9.1", gateway("0.9.0"));
  // Restarted onto the current gateway: the same app version clears again.
  assert.equal(await run(deps, "0.9.1", gateway("0.9.1")), true);
  assert.equal(deps.calls.clearCache, 2);
});

test("a gateway that does not answer changes nothing", async () => {
  const deps = fakes("0.9.0+0.9.0");
  assert.equal(await run(deps, "0.9.1", async () => null), false);
  assert.equal(deps.calls.clearCache, 0);
  assert.equal(deps.values.lastCacheVersion, "0.9.0+0.9.0");
});

test("a failed clear leaves the pair unrecorded so the next launch retries", async () => {
  const deps = fakes("0.9.0+0.9.0", { failClear: true });
  assert.equal(await run(deps, "0.9.1", gateway("0.9.1")), false);
  assert.equal(deps.values.lastCacheVersion, "0.9.0+0.9.0");
});
