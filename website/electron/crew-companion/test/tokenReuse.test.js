"use strict";

/**
 * The reconcile poll must REUSE its dashboard token across ticks.
 *
 * Minting one per tick is not a cosmetic waste: every mint is a full
 * link->session exchange on the gateway, which registers a nonce in a bounded
 * 50-slot ring. At a 5-second cadence that ring turned over every few minutes
 * and evicted OTHER pending one-time links — a phone-access QR among them,
 * before its own window had lapsed — while also issuing a fresh 30-day refresh
 * chain and appending to a persisted denylist each time. It additionally meant
 * dozens of live, full-privilege sign-in links existed at any moment purely as a
 * side effect of asking whether an app is enabled.
 *
 * These tests drive `reconcileOnce` directly (it is exported for exactly that)
 * rather than waiting on the 5-second interval.
 */

const assert = require("node:assert/strict");
const test = require("node:test");
const Module = require("node:module");
const path = require("node:path");

const INDEX = path.join(__dirname, "..", "index.js");

/**
 * Load a fresh copy of index.js with electron and the sibling window modules
 * stubbed, so the module under test runs without a real Electron main process.
 *
 * @param {{status: number[], mints: {n: number}}} wiring
 */
function loadCompanion(wiring) {
  const originalResolve = Module._resolveFilename;
  const originalLoad = Module._load;
  const stubs = {
    electron: {
      ipcMain: { on() {}, handle() {}, removeHandler() {} },
      BrowserWindow: { fromWebContents: () => null },
    },
  };
  const noopWindowModule = {
    setOverlayTarget() {},
    setPanelTarget() {},
    setGalleryTarget() {},
    setOverlayLogger() {},
    setPanelLogger() {},
    setGalleryLogger() {},
    registerPanelIpc() {},
    registerGalleryIpc() {},
    registerOverlayIpc() {},
    setAppearanceChangedHandler() {},
    setPanelClosedHandler() {},
    setGalleryOpenedHandler() {},
    setGalleryClosedHandler() {},
    broadcastToPets() {},
    openPetWindow() {},
    closePetWindow() {},
    closePanelWindow() {},
    closeGalleryWindow() {},
    petWindowCount: () => 0,
    startHitboxPoll() {},
    stopHitboxPoll() {},
  };

  Module._load = function (request, parent, isMain) {
    if (request === "electron") return stubs.electron;
    if (request === "node:http" || request === "http") {
      return {
        get(_url, _opts, cb) {
          const statusCode = wiring.status.shift();
          const res = {
            statusCode,
            on(evt, fn) {
              if (evt === "data") fn(JSON.stringify([{ name: "crew-companion", enabled: true }]));
              if (evt === "end") fn();
            },
          };
          setImmediate(() => cb(res));
          return { on() {}, destroy() {} };
        },
      };
    }
    if (
      request.startsWith("./pet") ||
      request.startsWith("./panel") ||
      request.startsWith("./gallery") ||
      request.startsWith("./page")
    ) {
      return noopWindowModule;
    }
    return originalLoad(request, parent, isMain);
  };

  try {
    delete require.cache[require.resolve(INDEX)];
    return require(INDEX);
  } finally {
    Module._load = originalLoad;
    Module._resolveFilename = originalResolve;
  }
}

/** Let the reconcile that `initCrewCompanion` fires itself settle. */
async function settle() {
  for (let i = 0; i < 10; i += 1) await new Promise((r) => setImmediate(r));
}

test("a later tick reuses the token instead of minting again", async () => {
  const mints = { n: 0 };
  // init fires one reconcile itself; the two hand-driven ticks follow.
  const mod = loadCompanion({ status: [200, 200, 200] });
  mod.initCrewCompanion({
    backendUrl: "http://127.0.0.1:5476",
    fetchLocalToken: async () => {
      mints.n += 1;
      return `tok-${mints.n}`;
    },
    glog: () => {},
  });
  await settle();
  assert.equal(mints.n, 1, "init's own reconcile mints exactly one token");

  await mod.reconcileOnce();
  await mod.reconcileOnce();

  assert.equal(mints.n, 1, "later ticks must not mint again");
  mod.shutdownCrewCompanion();
});

test("a refused token is re-minted exactly once", async () => {
  const mints = { n: 0 };
  // init's probe 401s (the cached token is refused); the single retry succeeds.
  const mod = loadCompanion({ status: [401, 200] });
  mod.initCrewCompanion({
    backendUrl: "http://127.0.0.1:5476",
    fetchLocalToken: async () => {
      mints.n += 1;
      return `tok-${mints.n}`;
    },
    glog: () => {},
  });
  await settle();

  assert.equal(mints.n, 2, "an auth refusal re-mints once, and only once");
  mod.shutdownCrewCompanion();
});
