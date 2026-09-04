// getInfo().lastState: the replay seed for a freshly mounted renderer.
//
// Update lifecycle state is PUSHED (onUpdateState), so it dies with the
// renderer. The one path that reloads the renderer without the user asking is
// install-failure recovery: the gateway is respawned and the window reconnects,
// which unmounts the failure card mid-error. getInfo() therefore carries the
// last emitted payload back out, so the fresh mount can restore what the user
// was looking at. These tests pin that contract from the main-process side; the
// renderer's seeding behaviour is pinned in
// src/test/AboutPanel.installFailureReplay.test.tsx.
const { test } = require("node:test");
const assert = require("node:assert");

const { initAutoUpdate } = require("../auto-update");

function makeDeps({ appVersion = "1.0.0" } = {}) {
  const handlers = {};
  const states = [];
  const autoUpdater = {
    setFeedURL: () => {},
    checkForUpdates: async () => {},
    downloadUpdate: async () => {},
    quitAndInstall: () => {},
    on: (ev, fn) => { handlers[ev] = fn; },
  };
  const deps = {
    app: {
      isPackaged: true,
      getVersion: () => appVersion,
      once: () => {},
      removeListener: () => {},
      exit: () => {},
    },
    autoUpdater,
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    Notification: function () { return { show: () => {} }; },
    getFlavor: () => "stable",
    stopGateway: async () => {},
    osPlatform: "darwin",
    feedBase: "https://cdn.example.dev/feed",
    onUpdateState: (s) => states.push(s),
    log: { info: () => {}, warn: () => {}, error: () => {} },
  };
  return { deps, states, emit: (ev, p) => handlers[ev] && handlers[ev](p) };
}

test("lastState is null before anything has been emitted", () => {
  const { deps } = makeDeps();
  const u = initAutoUpdate(deps);
  assert.strictEqual(u.getInfo().lastState, null);
});

test("lastState mirrors the most recent pushed payload", async () => {
  const { deps, states, emit } = makeDeps();
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  assert.ok(states.length >= 2, "check + found must both have been pushed");
  assert.deepStrictEqual(u.getInfo().lastState, states[states.length - 1]);
  assert.strictEqual(u.getInfo().lastState.state, "found");
  assert.strictEqual(u.getInfo().lastState.version, "1.1.0");
});

test("an install failure survives into lastState — the renderer-reload scenario", async () => {
  const { deps, emit } = makeDeps();
  const u = initAutoUpdate(deps);
  await u.check();
  emit("update-available", { version: "1.1.0" });
  await u.download();
  emit("update-downloaded", { version: "1.1.0" });
  await u.install(); // dispatch succeeds; the Squirrel handoff then fails:
  emit("error", new Error("ShipIt could not replace the application bundle"));
  const last = u.getInfo().lastState;
  assert.strictEqual(last.state, "error");
  assert.strictEqual(last.phase, "install");
  // The failure class travels too, so the renderer can pick localized copy.
  assert.strictEqual(typeof last.code, "string");
});

test("lastState is remembered even when the UI push throws", async () => {
  const { deps } = makeDeps();
  deps.onUpdateState = () => { throw new Error("renderer gone"); };
  const u = initAutoUpdate(deps);
  await u.check();
  // The push failed, but the state must still be replayable: a renderer that
  // missed the push is exactly the one the replay exists to catch up.
  assert.ok(u.getInfo().lastState, "state must be captured despite the throw");
  assert.strictEqual(typeof u.getInfo().lastState.state, "string");
});
