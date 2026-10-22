"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const {
  CLOSE_ACTION_KEY,
  CLOSE_ACTION_BACKGROUND,
  CLOSE_ACTION_QUIT,
  getCloseAction,
  setCloseAction,
  decideClose,
  createCloseHandler,
} = require("../close-action");

function fakeStore(initial = {}) {
  const data = { ...initial };
  const writes = [];
  return {
    data,
    writes,
    get: (key) => data[key],
    set(key, value) {
      data[key] = value;
      writes.push([key, value]);
    },
  };
}

test("an untouched store keeps today's close-to-background behaviour", () => {
  assert.equal(getCloseAction(fakeStore()), CLOSE_ACTION_BACKGROUND);
});

test("only an exact \"quit\" makes the close button quit", () => {
  assert.equal(getCloseAction(fakeStore({ [CLOSE_ACTION_KEY]: "quit" })), CLOSE_ACTION_QUIT);
  // A hand-edited or foreign value must never turn closing a window into
  // ending the user's sessions.
  for (const value of ["QUIT", "Quit", " quit", "exit", true, 1, null, {}, "background"]) {
    assert.equal(
      getCloseAction(fakeStore({ [CLOSE_ACTION_KEY]: value })),
      CLOSE_ACTION_BACKGROUND,
      `${JSON.stringify(value)} must read as background`,
    );
  }
});

test("the setter stores one of the two canonical values and returns it", () => {
  const store = fakeStore();
  assert.equal(setCloseAction(store, "quit"), CLOSE_ACTION_QUIT);
  assert.equal(setCloseAction(store, "background"), CLOSE_ACTION_BACKGROUND);
  assert.equal(setCloseAction(store, "QUIT"), CLOSE_ACTION_BACKGROUND);
  assert.equal(setCloseAction(store, undefined), CLOSE_ACTION_BACKGROUND);
  assert.deepEqual(store.writes, [
    [CLOSE_ACTION_KEY, "quit"],
    [CLOSE_ACTION_KEY, "background"],
  ], "a write that changes nothing is skipped");
});

test("decideClose: a quit already under way always persists", () => {
  assert.equal(decideClose({ quitting: true, action: CLOSE_ACTION_QUIT }), "persist");
  assert.equal(decideClose({ quitting: true, action: CLOSE_ACTION_BACKGROUND }), "persist");
});

test("decideClose: otherwise the stored action picks quit or hide", () => {
  assert.equal(decideClose({ quitting: false, action: CLOSE_ACTION_QUIT }), "quit");
  assert.equal(decideClose({ quitting: false, action: CLOSE_ACTION_BACKGROUND }), "hide");
  assert.equal(decideClose({ quitting: false, action: "anything" }), "hide");
});

test("main.js ships the key with the background default", () => {
  const main = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  assert.match(main, new RegExp(`\\b${CLOSE_ACTION_KEY}: "${CLOSE_ACTION_BACKGROUND}"`));
});

// ── The close sequence, driven end to end ──────────────────────────────────
//
// A stand-in for Electron: `app.quit()` closes the main window again, which is
// what makes the quit-on-close branch re-enter the listener.

function closeRig({ action = "background", pendingHide = false } = {}) {
  const calls = [];
  let quitting = false;
  let pending = pendingHide;
  const store = fakeStore({ [CLOSE_ACTION_KEY]: action });
  let onClose = null;
  const fireClose = () => {
    let prevented = false;
    const decision = onClose({ preventDefault: () => { prevented = true; } });
    calls.push(`close:${decision}${prevented ? ":prevented" : ""}`);
    return { decision, prevented };
  };
  onClose = createCloseHandler({
    isQuitting: () => quitting,
    getAction: () => getCloseAction(store),
    cancelPendingHide: () => { calls.push("cancelPendingHide"); pending = false; },
    requestQuit: () => {
      calls.push("requestQuit");
      quitting = true; // main.js sets the flag before app.quit()
      calls.push(`pendingHideAtQuit:${pending}`);
      fireClose(); // app.quit() closes the window again
    },
    hide: () => calls.push("hide"),
    persist: () => calls.push("persist"),
  });
  return { calls, store, fireClose };
}

test("background close hides and keeps running, and never persists or quits", () => {
  const rig = closeRig();
  const { prevented } = rig.fireClose();
  assert.equal(prevented, true);
  assert.deepEqual(rig.calls, ["hide", "close:hide:prevented"]);
});

const nextTick = () => new Promise((resolve) => setImmediate(resolve));

test("quit close cancels a pending hide, then quits after the close returns", async () => {
  const rig = closeRig({ action: "quit", pendingHide: true });
  rig.fireClose();
  // The quit must not start inside the prevented close, or Electron cancels it.
  assert.deepEqual(rig.calls, ["cancelPendingHide", "close:quit:prevented"]);
  await nextTick();
  assert.deepEqual(rig.calls, [
    "cancelPendingHide",
    "close:quit:prevented",
    "requestQuit",
    "pendingHideAtQuit:false",
    // The close app.quit() issues is NOT prevented and saves geometry.
    "persist",
    "close:persist",
  ]);
  assert.equal(rig.calls.includes("hide"), false, "quit must never hide to the tray");
});

test("the action is read per close, so a change applies without a relaunch", async () => {
  const rig = closeRig();
  rig.fireClose();
  setCloseAction(rig.store, "quit");
  rig.fireClose();
  await nextTick();
  assert.equal(rig.calls[0], "hide");
  assert.ok(rig.calls.includes("requestQuit"));
});

test("a hand-edited value never makes the close button quit", () => {
  const rig = closeRig({ action: "QUIT" });
  rig.fireClose();
  assert.deepEqual(rig.calls, ["hide", "close:hide:prevented"]);
});
