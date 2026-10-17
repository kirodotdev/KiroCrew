// Shared by auto-update.test.js and its auto-update.*.test.js parts. (Not a
// *.test.js file, so `node --test test/*.test.js` does not run it.)

// ---------------------------------------------------------------------------
// initAutoUpdate fixture: fake electron-updater AppUpdater (EventEmitter-like,
// recording setFeedURL / checkForUpdates / downloadUpdate / quitAndInstall)
// plus fake electron app/dialog/Notification. Platform comes in through the
// injected osPlatform dep -- no process.platform mutation needed.
// ---------------------------------------------------------------------------

function makeDeps(opts = {}) {
  const {
    appVersion = "1.0.0",
    osPlatform = "darwin",
    isPackaged = true,
    // Bundle location seams. Default to a normal /Applications install so every
    // pre-existing test keeps arming the updater; the bundle-location guard
    // tests drive these to the refused states.
    resourcesPath = "/Applications/Kiro Crew.app/Contents/Resources",
    bundleWritable = true,
    // Externally-managed verdict. null (the default) = not managed, decided
    // here so no test's outcome depends on the host filesystem.
    externallyManaged = null,
  } = opts;
  const calls = { setFeedURL: [], checkForUpdates: 0, downloadUpdate: 0, quitAndInstall: [] };
  const handlers = {};
  const states = [];
  const appOnce = [];
  const appRemoved = [];
  const autoUpdater = {
    setFeedURL: (o) => calls.setFeedURL.push(o),
    checkForUpdates: async () => { calls.checkForUpdates += 1; },
    downloadUpdate: async () => { calls.downloadUpdate += 1; },
    quitAndInstall: (...args) => calls.quitAndInstall.push(args),
    on: (ev, fn) => { handlers[ev] = fn; },
  };
  const deps = {
    app: {
      isPackaged,
      getVersion: () => appVersion,
      once: (ev, fn) => appOnce.push({ ev, fn }),
      removeListener: (ev, fn) => appRemoved.push({ ev, fn }),
      // Must exist: the force-exit failsafe timer (unref'd but still live)
      // calls app.exit(0) if the suite outlives it; without this stub it
      // would fall through to process.exit and kill the test runner.
      exit: () => {},
    },
    autoUpdater,
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    Notification: function () { return { show: () => {} }; },
    getFlavor: () => "stable",
    stopGateway: async () => {},
    osPlatform,
    resourcesPath,
    // Stubbed so the writable-vs-read-only axis is decided by the test, not by
    // whatever the host filesystem happens to allow.
    probeBundleWritable: () => bundleWritable,
    externallyManaged,
    feedBase: "https://cdn.example.dev/feed",
    onUpdateState: (s) => states.push(s),
    log: { info: () => {}, warn: () => {}, error: () => {} },
  };
  const emit = (ev, payload) => handlers[ev] && handlers[ev](payload);
  const stateNames = () => states.map((s) => s.state);
  return { deps, calls, handlers, emit, states, stateNames, appOnce, appRemoved };
}

module.exports = {
  makeDeps,
};
