// The feed lane's phase flags, mapped to the enums that will replace them.
//
// Each row drives the real lane to a moment it reaches today and reads its flags
// there. `flagsOf` is the one place the mapping is written: a row passes only when
// the live flags are exactly what its enum values stand for, so every reachable
// combination in the table has a value that holds it, and the renderer states that
// led there are pinned beside it. When the flags become enums, the rows stay and
// compare the enum values directly.
//
//   check      idle | running | abandoned-by-download | abandoned-by-quit
//              (checking, checkInFlight, abandonedCheck, and verifyQuiet, which
//              marks the check the quit's verify joined until it settles)
//   gateOwner  none | download | install    (preparingDownload, verifyingStage)
//   download   idle | running               (downloading)
//   quitClaim  none | claimed               (quitHandled)
//   quitPath   idle | holding | installing | staging | staging-quit-held | quit-replayed
//              (quitPathVerifying, installing, deferredInstalling, stagingHold,
//              stagingQuitHeld, quitDeferredForInstall)
//
// The download and the quit claim are enums of their own because two rows need
// them apart: a versionless stage installs while its download still runs, and a
// retraction releases the quit claim while the quit path still holds the quit.
// `stagedArmed` and `relaunchAfterInstall` record what the user chose (Install,
// Restart now), not a phase, so they stay plain fields; the rows pin them too.

const { test } = require("node:test");
const assert = require("node:assert");

const { initAutoUpdate } = require("../auto-update");
const { FRESHNESS_CHECK_TIMEOUT_MS } = require("../runtime/update/freshness-gate");
const { resetForTests: resetRelaunchIntent } = require("../relaunch-intent");

const PHASES = {
  check: ["idle", "running", "abandoned-by-download", "abandoned-by-quit"],
  gateOwner: ["none", "download", "install"],
  download: ["idle", "running"],
  quitClaim: ["none", "claimed"],
  quitPath: ["idle", "holding", "installing", "staging", "staging-quit-held", "quit-replayed"],
};
const IDLE = { check: "idle", gateOwner: "none", download: "idle", quitClaim: "none", quitPath: "idle" };

/** The flags a phase tuple stands for, plus the two user choices. */
function flagsOf({ check, gateOwner, download, quitClaim, quitPath }, { armed = false, restart = false } = {}) {
  const installing = ["installing", "staging", "staging-quit-held"].includes(quitPath);
  return {
    checking: check !== "idle",
    checkInFlight: check !== "idle",
    abandonedCheck: check.startsWith("abandoned"),
    verifyQuiet: check === "abandoned-by-quit" || (check === "running" && gateOwner === "install"),
    preparingDownload: gateOwner === "download",
    verifyingStage: gateOwner === "install",
    downloading: download === "running",
    quitHandled: quitClaim === "claimed",
    quitPathVerifying: quitPath === "holding",
    installing,
    deferredInstalling: installing,
    stagingHold: quitPath === "staging" || quitPath === "staging-quit-held",
    stagingQuitHeld: quitPath === "staging-quit-held",
    quitDeferredForInstall: quitPath === "quit-replayed",
    stagedArmed: armed,
    relaunchAfterInstall: restart,
  };
}

/** Let the lane's async bodies run up to their next parked await. */
async function flush(times = 8) {
  for (let i = 0; i < times; i += 1) await new Promise((resolve) => setImmediate(resolve));
}

/**
 * A lane whose every await is parked until the row lets it go: the check, the
 * quit-time read of the feed, the download and the gateway stop.
 */
function lane({ platform = "darwin", autoDownload = true, channel = "" } = {}) {
  const states = [];
  const handlers = {};
  const quitListeners = [];
  const native = {};
  const parked = {};
  const park = (name) => new Promise((resolve, reject) => { parked[name] = { resolve, reject }; });
  const choice = { channel };
  const app = {
    isPackaged: true,
    getVersion: () => "1.0.0",
    once: (ev, fn) => { if (ev === "before-quit") quitListeners.push(fn); },
    removeListener: (ev, fn) => {
      const at = quitListeners.indexOf(fn);
      if (at !== -1) quitListeners.splice(at, 1);
    },
    // Electron emits `before-quit` from app.quit() as from Cmd+Q, and it spends
    // every once-listener.
    quit: () => {
      for (const fn of quitListeners.splice(0)) fn({ preventDefault: () => {} });
    },
    relaunch: () => {},
    exit: () => {},
  };
  const u = initAutoUpdate({
    app,
    autoUpdater: {
      setFeedURL: () => {},
      checkForUpdates: () => park("check"),
      isUpdaterActive: () => true,
      getUpdateInfoAndProvider: () => park("read"),
      isUpdateAvailable: async (info) => info.version !== "1.0.0",
      downloadUpdate: () => park("download"),
      quitAndInstall: () => {},
      on: (ev, fn) => { handlers[ev] = fn; },
    },
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    Notification: function () { return { show: () => {} }; },
    getFlavor: () => "stable",
    getChannelPreference: () => choice.channel,
    getAutoDownloadPreference: () => autoDownload,
    stopGateway: () => park("stop"),
    osPlatform: platform,
    osArch: "arm64",
    feedBase: "https://cdn.example.dev/feed",
    onUpdateState: (s) => states.push(s.state),
    nativeAutoUpdater: { once: (ev, fn) => { native[ev] = fn; } },
    log: { info: () => {}, warn: () => {}, error: () => {} },
  });
  const emit = (ev, payload) => handlers[ev](payload);
  const release = (name, value) => parked[name].resolve(value);
  return {
    u,
    states,
    choice,
    emit,
    quit: () => app.quit(),
    release,
    fail: (name, err) => parked[name].reject(err),
    /** The parked check reports `ev`, then settles, as electron-updater does. */
    answerCheck: (ev, payload = {}) => { emit(ev, payload); release("check"); },
    installerTakesOver: () => native["before-quit-for-update"](),
  };
}

const stage = (h, version = "1.1.0") => h.emit("update-downloaded", version ? { version } : {});
const bound = (t) => t.mock.timers.tick(FRESHNESS_CHECK_TIMEOUT_MS);

/** Quit with a versioned stage, and let the read answer that it is still the newest. */
async function toGatewayStop(h) {
  stage(h);
  h.quit();
  await flush();
  h.release("read", { info: { version: "1.1.0" } });
  await flush();
}

/** A poll in flight when the user quits with a versioned stage: the verify joins it. */
async function joinedAtQuit(h) {
  stage(h);
  void h.u.check();
  await flush();
  h.quit();
  await flush();
}

const ROWS = [
  {
    name: "nothing in flight",
    phase: {},
    drive: async () => {},
    states: [],
  },
  {
    name: "a check in flight",
    phase: { check: "running" },
    drive: async (h) => { void h.u.check(); },
    states: ["checking"],
  },
  {
    name: "a Download click waiting on its own re-check",
    autoDownload: false,
    phase: { check: "running", gateOwner: "download" },
    drive: async (h) => {
      h.emit("update-available", { version: "1.1.0" });
      void h.u.download();
    },
    states: ["found", "downloading"],
  },
  {
    name: "a download running",
    autoDownload: false,
    phase: { download: "running" },
    drive: async (h) => {
      h.emit("update-available", { version: "1.1.0" });
      void h.u.download();
      await flush();
      h.answerCheck("update-available", { version: "1.1.0" });
    },
    states: ["found", "downloading", "downloading"],
  },
  {
    name: "a Download click that gave up on a poll at the bound downloads the offered build",
    autoDownload: false,
    phase: { check: "abandoned-by-download", download: "running" },
    drive: async (h, t) => {
      h.emit("update-available", { version: "1.1.0" });
      void h.u.check();
      await flush();
      void h.u.download();
      await flush();
      bound(t);
    },
    states: ["found", "checking", "downloading", "downloading"],
  },
  {
    name: "a Download click on an offer from the channel the user left asks the new lane",
    autoDownload: false,
    phase: { check: "abandoned-by-download" },
    drive: async (h, t) => {
      h.emit("update-available", { version: "1.1.0" });
      void h.u.download();
      await flush();
      h.choice.channel = "insider";
      bound(t);
    },
    states: ["found", "downloading", "checking"],
  },
  {
    name: "a stage the Install click armed for the quit",
    choices: { armed: true },
    phase: {},
    drive: async (h) => {
      stage(h);
      await h.u.install();
    },
    states: ["downloaded", "downloaded"],
  },
  {
    name: "the quit path reading the feed for its verdict",
    phase: { gateOwner: "install", quitClaim: "claimed", quitPath: "holding" },
    drive: async (h) => {
      stage(h);
      h.quit();
    },
    states: ["downloaded", "installing"],
  },
  {
    name: "the quit path waiting on a check already in flight",
    phase: { check: "running", gateOwner: "install", quitClaim: "claimed", quitPath: "holding" },
    drive: joinedAtQuit,
    states: ["downloaded", "checking", "installing"],
  },
  {
    name: "a retraction releases the quit claim while the quit path's verify still runs",
    phase: { check: "running", gateOwner: "install", quitPath: "holding" },
    drive: async (h) => {
      await joinedAtQuit(h);
      h.emit("update-not-available");
    },
    states: ["downloaded", "checking", "installing", "not-available"],
  },
  {
    name: "Restart now with an armed stage: the quit path reads the feed",
    choices: { armed: true, restart: true },
    phase: { gateOwner: "install", quitClaim: "claimed", quitPath: "holding" },
    drive: async (h) => {
      stage(h);
      await h.u.install();
      h.u.relaunch();
    },
    states: ["downloaded", "downloaded", "installing"],
  },
  {
    name: "the quit path stopping the gateway",
    phase: { quitClaim: "claimed", quitPath: "holding" },
    drive: toGatewayStop,
    states: ["downloaded", "installing"],
  },
  {
    name: "a check the quit's verify gave up on at the bound, during the gateway stop",
    phase: { check: "abandoned-by-quit", quitClaim: "claimed", quitPath: "holding" },
    drive: async (h, t) => {
      await joinedAtQuit(h);
      bound(t);
    },
    states: ["downloaded", "checking", "installing"],
  },
  {
    name: "a check straddling the quit of a versionless stage, during the gateway stop",
    phase: { check: "running", quitClaim: "claimed", quitPath: "holding" },
    drive: async (h) => {
      stage(h, null);
      void h.u.check();
      await flush();
      h.quit();
    },
    states: ["downloaded", "checking", "installing"],
  },
  {
    name: "that straddling check's retraction releases the quit claim during the gateway stop",
    phase: { check: "running", quitPath: "holding" },
    drive: async (h) => {
      stage(h, null);
      void h.u.check();
      await flush();
      h.quit();
      await flush();
      h.emit("update-not-available");
    },
    states: ["downloaded", "checking", "installing", "not-available"],
  },
  {
    name: "the quit path ended without installing a superseded stage",
    phase: { quitClaim: "claimed" },
    drive: async (h) => {
      stage(h);
      h.quit();
      await flush();
      h.release("read", { info: { version: "1.2.0" } });
    },
    states: ["downloaded", "installing"],
  },
  {
    name: "the quit path ended without installing a stage from the channel the user left",
    channel: "insider",
    phase: { quitClaim: "claimed" },
    drive: async (h) => {
      stage(h);
      h.choice.channel = "";
      h.quit();
      await flush();
      h.fail("read", Object.assign(new Error("offline"), { code: "ENOTFOUND" }));
    },
    states: ["downloaded", "installing"],
  },
  {
    name: "macOS: Squirrel stages the install and quits are held",
    phase: { quitClaim: "claimed", quitPath: "staging" },
    drive: async (h) => {
      await toGatewayStop(h);
      h.release("stop");
    },
    states: ["downloaded", "installing"],
  },
  {
    name: "macOS: a quit during Squirrel's staging is held",
    phase: { quitClaim: "claimed", quitPath: "staging-quit-held" },
    drive: async (h) => {
      await toGatewayStop(h);
      h.release("stop");
      await flush();
      h.quit();
    },
    states: ["downloaded", "installing"],
  },
  {
    name: "macOS: the installer took over and the app waits for Squirrel's handoff quit",
    phase: { quitClaim: "claimed", quitPath: "installing" },
    drive: async (h) => {
      await toGatewayStop(h);
      h.release("stop");
      await flush();
      h.installerTakesOver();
    },
    states: ["downloaded", "installing"],
  },
  {
    name: "macOS: an installer failure after a held staging quit replays that quit",
    phase: { quitClaim: "claimed", quitPath: "quit-replayed" },
    drive: async (h) => {
      await toGatewayStop(h);
      h.release("stop");
      await flush();
      h.quit();
      h.emit("error", new Error("code signature invalid"));
    },
    states: ["downloaded", "installing", "error"],
  },
  {
    name: "the install dispatched with the quit's abandoned check still in flight",
    phase: { check: "abandoned-by-quit", quitClaim: "claimed", quitPath: "staging" },
    drive: async (h, t) => {
      await joinedAtQuit(h);
      bound(t);
      await flush();
      h.release("stop");
    },
    states: ["downloaded", "checking", "installing"],
  },
  {
    name: "Windows: a versionless stage installs while its download still runs",
    platform: "win32",
    phase: { download: "running", quitClaim: "claimed", quitPath: "installing" },
    drive: async (h) => {
      stage(h, null);
      void h.u.check();
      await flush();
      h.answerCheck("update-available", { version: "1.2.0" });
      await flush();
      h.quit();
      await flush();
      h.release("stop");
    },
    states: ["downloaded", "checking", "found", "downloading", "installing"],
  },
];

for (const row of ROWS) {
  test(`lane phase: ${row.name}`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
    t.after(resetRelaunchIntent);
    const h = lane({ platform: row.platform, autoDownload: row.autoDownload, channel: row.channel });
    await row.drive(h, t);
    await flush();
    assert.deepStrictEqual(h.u.flags(), flagsOf({ ...IDLE, ...row.phase }, row.choices));
    assert.deepStrictEqual(h.states, row.states);
  });
}

test("lane phase: each flag combination has exactly one tuple, and every value is reached by a row", () => {
  const tuples = Object.entries(PHASES).reduce(
    (acc, [dim, values]) => acc.flatMap((p) => values.map((v) => ({ ...p, [dim]: v }))),
    [{}],
  );
  const combos = new Set(tuples.map((p) => JSON.stringify(flagsOf(p))));
  assert.strictEqual(combos.size, tuples.length, "two tuples stand for the same flags");

  for (const [dim, values] of Object.entries(PHASES)) {
    const reached = new Set(ROWS.map((row) => ({ ...IDLE, ...row.phase })[dim]));
    assert.deepStrictEqual([...reached].sort(), [...values].sort(), `${dim}: a value no row reaches, or one not declared`);
  }
});
