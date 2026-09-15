"use strict";

/**
 * An update install stops the gateway and quits the app. suspendCrewCompanion()
 * must close the overlays AND latch the reconcile loop, because the loop treats a
 * gateway it cannot reach as "unknown" and leaves every window as-is — so without
 * the latch the ghost floats orphaned over the vanished dashboard during the quit
 * handoff, and a tick firing while the gateway is briefly still up (dispatch runs
 * before stopGateway is awaited) could reopen a just-closed overlay.
 *
 * A failed install does NOT quit, so resumeCrewCompanion() must let the loop
 * reopen the overlay once the restored gateway answers again — WITHOUT re-running
 * initCrewCompanion (which would stack a second IPC listener).
 *
 * These drive the exported reconcile/suspend/resume directly rather than waiting
 * on the 5-second interval.
 */

const assert = require("node:assert/strict");
const test = require("node:test");
const Module = require("node:module");
const path = require("node:path");

const INDEX = path.join(__dirname, "..", "index.js");

function loadCompanion(wiring) {
  const originalLoad = Module._load;
  const calls = { open: 0, closePet: 0, closePanel: 0, closeGallery: 0 };
  let windowOpen = false;
  const stubs = {
    electron: {
      ipcMain: { on() {}, handle() {}, removeHandler() {} },
      BrowserWindow: { fromWebContents: () => null },
    },
  };
  const noopWindowModule = {
    setOverlayTarget() {}, setPanelTarget() {}, setGalleryTarget() {},
    setOverlayLogger() {}, setPanelLogger() {}, setGalleryLogger() {},
    registerPanelIpc() {}, registerGalleryIpc() {}, registerOverlayIpc() {},
    setAppearanceChangedHandler() {}, setPanelClosedHandler() {},
    setGalleryOpenedHandler() {}, setGalleryClosedHandler() {},
    broadcastToPets() {},
    openPetWindow() { windowOpen = true; calls.open += 1; },
    closePetWindow() { windowOpen = false; calls.closePet += 1; },
    closePanelWindow() { calls.closePanel += 1; },
    closeGalleryWindow() { calls.closeGallery += 1; },
    petWindowCount: () => (windowOpen ? 1 : 0),
    startHitboxPoll() {}, stopHitboxPoll() {},
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
      request.startsWith("./pet") || request.startsWith("./panel") ||
      request.startsWith("./gallery") || request.startsWith("./page")
    ) {
      return noopWindowModule;
    }
    return originalLoad(request, parent, isMain);
  };

  try {
    delete require.cache[require.resolve(INDEX)];
    const mod = require(INDEX);
    mod.__calls = calls;
    return mod;
  } finally {
    Module._load = originalLoad;
  }
}

async function settle() {
  for (let i = 0; i < 10; i += 1) await new Promise((r) => setImmediate(r));
}

test("suspend closes the overlays and latches the reconcile loop", async () => {
  // init's own reconcile opens the overlay; the latched tick must NOT reopen it.
  const mod = loadCompanion({ status: [200] });
  mod.initCrewCompanion({
    backendUrl: "http://127.0.0.1:5476",
    fetchLocalToken: async () => "tok",
    glog: () => {},
  });
  await settle();
  assert.equal(mod.__calls.open, 1, "init reconcile opens the overlay while enabled");

  mod.suspendCrewCompanion();
  assert.equal(mod.__calls.closePet, 1, "suspend closes the pet overlay");
  assert.equal(mod.__calls.closePanel, 1, "suspend closes the panel");
  assert.equal(mod.__calls.closeGallery, 1, "suspend closes the gallery");

  // A tick during the quit handoff must be a no-op: it must not even probe (so
  // no status is consumed) and must not reopen the overlay it just closed.
  await mod.reconcileOnce();
  assert.equal(mod.__calls.open, 1, "a suspended tick must not reopen the overlay");

  mod.shutdownCrewCompanion();
});

test("resume lets the loop reopen the overlay after a failed install", async () => {
  // status: [init probe, resume's own reconcile]. The suspended tick consumes none.
  const mod = loadCompanion({ status: [200, 200] });
  mod.initCrewCompanion({
    backendUrl: "http://127.0.0.1:5476",
    fetchLocalToken: async () => "tok",
    glog: () => {},
  });
  await settle();

  mod.suspendCrewCompanion();
  await mod.reconcileOnce();
  assert.equal(mod.__calls.open, 1, "still latched — no reopen while suspended");

  mod.resumeCrewCompanion();
  await settle();
  assert.equal(mod.__calls.open, 2, "resume reopens the overlay once the gateway answers");

  mod.shutdownCrewCompanion();
});

test("a reconcile already in flight when suspend fires does not reopen the overlay", async () => {
  // The dangerous race: a tick passed the top-of-function latch and is awaiting
  // the gateway probe when the update dispatch calls suspend. status: [init, the
  // in-flight tick]. Both probe "enabled".
  const mod = loadCompanion({ status: [200, 200] });
  mod.initCrewCompanion({
    backendUrl: "http://127.0.0.1:5476",
    fetchLocalToken: async () => "tok",
    glog: () => {},
  });
  await settle();
  assert.equal(mod.__calls.open, 1, "init opened the overlay");

  // Start a tick (it clears the top latch — not yet suspended — and awaits the
  // probe), THEN suspend synchronously before the probe resolves.
  const inFlight = mod.reconcileOnce();
  mod.suspendCrewCompanion();
  await inFlight;
  await settle();

  assert.equal(mod.__calls.closePet, 1, "suspend closed the overlay");
  assert.equal(mod.__calls.open, 1, "the in-flight tick must re-check suspended and NOT reopen");

  mod.shutdownCrewCompanion();
});
