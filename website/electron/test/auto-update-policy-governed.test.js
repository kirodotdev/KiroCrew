// Policy provider governs the install: the Electron feed lane stands down.
//
// When the gateway's security policy configures check_command / apply_command,
// that provider owns the check, the badge and the Update button. The Electron
// feed lane must not compete: it should emit not-available and return early
// rather than contacting the feed or firing a notification.
//
// The renderer notifies the main process via update:notify-policy-governed
// (ipc-registrar.js), which sets policyGoverned and calls updater.check() so
// the feed lane emits not-available immediately. These tests drive the
// feed-lane path directly through initAutoUpdate with isPolicyGoverned wired.
const { test } = require("node:test");
const assert = require("node:assert");

const { initAutoUpdate } = require("../auto-update");

function makeDeps({ isPolicyGoverned = () => false, appVersion = "1.0.0" } = {}) {
  const handlers = {};
  const states = [];
  let checkCalled = false;
  const autoUpdater = {
    setFeedURL: () => {},
    checkForUpdates: async () => { checkCalled = true; },
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
    isPolicyGoverned,
  };
  return { deps, states, handlers, isCheckCalled: () => checkCalled };
}

test("when policy governs, check() emits not-available without contacting the feed", async () => {
  let governed = true;
  const { deps, states, isCheckCalled } = makeDeps({ isPolicyGoverned: () => governed });

  const u = initAutoUpdate(deps);
  await u.check();

  // The feed must not be contacted: autoUpdater.checkForUpdates() must not run.
  assert.strictEqual(isCheckCalled(), false, "feed must not be checked when policy governs");
  // The state must clear any stale "found" entry: not-available must be emitted.
  const last = states[states.length - 1];
  assert.ok(last, "a state must have been pushed");
  assert.strictEqual(last.state, "not-available", "must emit not-available when policy governs");
});

test("when governance is cleared, the next check contacts the feed", async () => {
  let governed = true;
  const { deps, states, isCheckCalled } = makeDeps({ isPolicyGoverned: () => governed });

  const u = initAutoUpdate(deps);
  await u.check();
  assert.strictEqual(isCheckCalled(), false, "feed must not run while governed");

  governed = false;
  await u.check();
  // Governance is off; check() must now reach the feed (checkForUpdates called).
  assert.strictEqual(isCheckCalled(), true, "feed must run once governance is cleared");
});



test("when policy governs and update-downloaded fires, build is discarded and before-quit is not armed", async () => {
  let governed = false;
  const { deps, states, handlers } = makeDeps({ isPolicyGoverned: () => governed });
  const onceCalls = [];
  deps.app.once = (ev, fn) => onceCalls.push({ ev, fn });

  initAutoUpdate(deps);

  // Governance arrives before the download finishes.
  governed = true;

  // Simulate update-downloaded firing (as if a download started before governance arrived).
  assert.ok(typeof handlers["update-downloaded"] === "function",
    "feed-lane must register an update-downloaded handler");
  handlers["update-downloaded"]({ version: "2.0.0", notes: "" });

  // The build must be discarded: before-quit must NOT be armed.
  const beforeQuit = onceCalls.filter(c => c.ev === "before-quit");
  assert.strictEqual(beforeQuit.length, 0,
    "before-quit must not be registered when policy governs at download-complete time");

  // No state emitted from the update-downloaded path (it returned early).
  assert.ok(
    !states.some(s => s.state === "downloaded"),
    "downloaded state must not be emitted when policy governs"
  );
});
