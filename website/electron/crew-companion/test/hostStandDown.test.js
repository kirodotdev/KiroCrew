"use strict";

/**
 * The built-in companion stands down for a record the desktop window host serves.
 *
 * Once the companion's record declares a manifest `desktop` block, the window host
 * draws it; these main-process windows reading that record as enabled would draw a
 * second one. The record, not the name, decides: the built-in record (no block)
 * keeps today's three states exactly.
 */

const assert = require("node:assert/strict");
const test = require("node:test");
const Module = require("node:module");
const path = require("node:path");

const INDEX = path.join(__dirname, "..", "index.js");

/** The companion module, whose `/api/apps` probe answers `wiring.body` (JSON text). */
function loadCompanion(wiring) {
  const originalLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === "electron") return { ipcMain: { on() {}, handle() {}, removeHandler() {} }, BrowserWindow: {} };
    if (request === "http" || request === "node:http") {
      return {
        get(_url, _opts, cb) {
          const res = {
            statusCode: 200,
            on(evt, fn) {
              if (evt === "data") fn(wiring.body);
              if (evt === "end") fn();
            },
          };
          setImmediate(() => cb(res));
          return { on() {}, destroy() {} };
        },
      };
    }
    if (/^\.\/(pet|panel|gallery|page)/.test(request)) return {};
    return originalLoad(request, parent, isMain);
  };
  try {
    delete require.cache[require.resolve(INDEX)];
    return require(INDEX);
  } finally {
    Module._load = originalLoad;
  }
}

const DESKTOP = { windows: [{ id: "pet", entry: "desktop/pet.mjs", kind: "overlay", displays: "all" }] };
const AUTH = { value: "tok", viaCookie: false };

async function stateFor(body) {
  const wiring = { body: JSON.stringify(body) };
  return loadCompanion(wiring).probeEnabled(AUTH);
}

test("the built-in record keeps its three states", async () => {
  const row = (enabled) => ({ name: "crew-companion", enabled, origin: "builtin", manifest: { name: "crew-companion" } });
  assert.equal(await stateFor([row(true)]), "enabled");
  assert.equal(await stateFor({ apps: [row(false)] }), "disabled");
  assert.equal(await stateFor([]), "unknown", "absent is not disabled");
  // Another app's block says nothing about the companion's.
  assert.equal(await stateFor([{ name: "other", enabled: true, manifest: { desktop: DESKTOP } }, row(true)]), "enabled");
});

test("a record the window host serves reads as released, on or off", async () => {
  // The block decides even on a row that still says it is the built-in's, or
  // predates `origin`: the host draws it, so these windows must not.
  for (const enabled of [true, false]) {
    for (const origin of ["registry", "builtin", undefined]) {
      const row = { name: "crew-companion", enabled, origin, manifest: { name: "crew-companion", desktop: DESKTOP } };
      assert.equal(await stateFor([row]), "released", `origin=${origin} enabled=${enabled}`);
      assert.equal(await stateFor({ apps: [row] }), "released", `origin=${origin} enabled=${enabled}`);
    }
  }
});
