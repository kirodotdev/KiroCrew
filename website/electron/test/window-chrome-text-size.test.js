const { test } = require("node:test");
const assert = require("node:assert/strict");
const { createWindowChrome } = require("../runtime/window/chrome");
const { DEFAULT_TEXT_SIZE_PX } = require("../text-size");

// Let every queued microtask and immediate drain. The apply is async and its
// failure path is a catch, so there is nothing to await from the outside; a
// bounded pump is how the other Electron tests settle one (the `outcome`
// helper in http-probe-cutoff.test.js). Never a timer: a promise-sleep in this
// tree is what the J1 flake detector exists to reject.
async function settle() {
  for (let round = 0; round < 20; round += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

// The repository has no shared fake for `webContents.debugger`, so this is it.
// `attached` models Chromium's single debugger slot per webContents, which is
// the resource developer tools competes for.
//
// Every failure mode is separately selectable because they are genuinely
// different paths through the apply: `failAttach` is the slot already being
// held (the scenario AC9 describes), `failSend` is the command being refused
// after a successful attach, and `destroyThrows` is the guard itself throwing.
function fakeDashboardWindow({
  attached = false,
  failAttach = false,
  failSend = false,
  destroyed: destroyedAtStart = false,
  destroyThrowsAfter = null,
  hasView = true,
} = {}) {
  let destroyed = destroyedAtStart;
  const calls = [];
  const focusListeners = [];
  const loadListeners = [];
  let isAttached = attached;
  // `destroyThrowsAfter` counts calls, because the enumeration asks first and
  // the apply's own guard asks second. Throwing from the first call is consumed
  // by the enumeration and never reaches the guard, so a test that wants the
  // guard's path has to let the first call through.
  let destroyedChecks = 0;
  const assertLive = () => {
    if (destroyed) throw new Error("Object has been destroyed");
  };
  const webContents = {
    isDestroyed: () => {
      destroyedChecks += 1;
      if (destroyThrowsAfter !== null && destroyedChecks > destroyThrowsAfter) {
        throw new Error("Object has been destroyed");
      }
      return destroyed;
    },
    on(event, listener) {
      if (event === "focus") focusListeners.push(listener);
      if (event === "did-finish-load") loadListeners.push(listener);
    },
    debugger: {
      // Electron throws on any debugger access once the view is gone. Modelling
      // that is what lets a test tell the apply's own destroyed guard apart
      // from the enumeration filter that usually runs in front of it.
      isAttached: () => {
        assertLive();
        return isAttached;
      },
      attach(version) {
        assertLive();
        if (failAttach) throw new Error("Another debugger is already attached");
        if (isAttached) throw new Error("already attached");
        isAttached = true;
        calls.push(["attach", version]);
      },
      // Developer tools closing: the slot frees, so a later attach succeeds.
      releaseSlot() { failAttach = false; },
      detach() {
        isAttached = false;
        calls.push(["detach"]);
      },
      async sendCommand(method, params) {
        calls.push(["sendCommand", method, params]);
        if (failSend) throw new Error("debugger is held elsewhere");
      },
    },
  };
  const win = {
    isDestroyed: () => destroyed,
    // A window with no `_mcView` is not a dashboard: a prompt window, the
    // updater splash, a companion surface. The embedded browser panel is a
    // child view and never one either.
    ...(hasView ? { _mcView: { webContents } } : {}),
  };
  return {
    win,
    webContents,
    calls,
    kinds: () => calls.map((call) => call[0]),
    sent: () => calls.filter((call) => call[0] === "sendCommand"),
    focusListeners,
    loadListeners,
    emitFocus: () => focusListeners.forEach((listener) => listener()),
    emitLoad: () => loadListeners.forEach((listener) => listener()),
    isAttached: () => isAttached,
    retireTo: (fn) => { webContents.debugger.sendCommand = fn; },
    // The window closes between the deferral and the retry.
    destroyNow: () => { destroyed = true; },
  };
}

function fakeStore(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    values,
    get: (key) => values.get(key),
    set: (key, value) => values.set(key, value),
  };
}

function makeChrome({ store = fakeStore(), log = () => {}, windows = [] } = {}) {
  const chrome = createWindowChrome({
    BaseWindow: { getAllWindows: () => windows.map((entry) => entry.win) },
    nativeTheme: {},
    screen: {},
    store,
    log,
    isMac: false,
    isWindows: false,
    windowForWebContents: () => null,
  });
  return { chrome, store };
}

// ── reading the stored size (R6) ──

test("an absent stored size reads as the default", () => {
  assert.equal(makeChrome().chrome.getFontSize(), DEFAULT_TEXT_SIZE_PX);
});

test("a stored tier reads back", () => {
  const { chrome } = makeChrome({ store: fakeStore({ "displayPreferences.fontSize": 20 }) });
  assert.equal(chrome.getFontSize(), 20);
});

test("a corrupt stored size reads as the default, warns, and is NOT rewritten", () => {
  for (const corrupt of ["20", 13, {}, true, NaN]) {
    const warnings = [];
    const store = fakeStore({ "displayPreferences.fontSize": corrupt });
    const { chrome } = makeChrome({ store, log: (line) => warnings.push(line) });

    assert.equal(chrome.getFontSize(), DEFAULT_TEXT_SIZE_PX, `${String(corrupt)} falls back`);
    assert.equal(warnings.length, 1, `${String(corrupt)} warns once`);
    // The operator's config is their document: a value somebody is debugging
    // must still be there after we decline it.
    assert.equal(
      store.values.get("displayPreferences.fontSize"),
      corrupt,
      `${String(corrupt)} is left in the store`,
    );
  }
});

// Absent is not corrupt, in either spelling. electron-store answers undefined
// for a missing key; seven fakes in this directory answer null. Warning on null
// would put a spurious line in every window-lifecycle harness test.
test("an unset key does not warn, whether it reads undefined or null", () => {
  for (const absent of [undefined, null]) {
    const warnings = [];
    const store = { get: () => absent, set: () => {} };
    const { chrome } = makeChrome({ store, log: (line) => warnings.push(line) });
    assert.equal(chrome.getFontSize(), DEFAULT_TEXT_SIZE_PX);
    assert.deepEqual(warnings, [], `${String(absent)} is silent`);
  }
});

// ── every dashboard window, not one ──

// setupWindowContents runs for the main window AND each connection window, so
// an apply that reached only one would reach whichever was built last. It also
// has to reach all of them to agree with the construction seed, which writes
// the stored size into every dashboard view.
test("the apply reaches every dashboard window", async () => {
  const main = fakeDashboardWindow();
  const connection = fakeDashboardWindow();
  const { chrome } = makeChrome({ windows: [main, connection] });

  chrome.setFontSize(20);
  await settle();

  for (const [name, window] of [["main", main], ["connection", connection]]) {
    assert.deepEqual(
      window.sent(),
      [["sendCommand", "Page.setFontSizes", { fontSizes: { standard: 20, fixed: 20 } }]],
      `${name} window received the command once`,
    );
  }
});

test("a window that is not a dashboard is never touched", async () => {
  const dashboard = fakeDashboardWindow();
  const promptWindow = fakeDashboardWindow({ hasView: false });
  const { chrome } = makeChrome({ windows: [dashboard, promptWindow] });

  chrome.setFontSize(12);
  await settle();

  assert.equal(dashboard.sent().length, 1);
  assert.deepEqual(promptWindow.calls, [], "no debugger traffic to a non-dashboard window");
});

// The branch that made the single-target version drop an apply in full.
test("a destroyed window is skipped and the live ones still get the command", async () => {
  const dead = fakeDashboardWindow({ destroyed: true });
  const live = fakeDashboardWindow();
  const { chrome } = makeChrome({ windows: [dead, live] });

  chrome.setFontSize(20);
  await settle();

  assert.deepEqual(dead.calls, [], "nothing sent to the dead window");
  assert.equal(live.sent().length, 1, "the live window still received it");
});

test("every dashboard window being dead is not an error", async () => {
  const dead = fakeDashboardWindow({ destroyed: true });
  const { chrome, store } = makeChrome({ windows: [dead] });

  assert.equal(chrome.setFontSize(20), 20);
  await settle();
  assert.equal(store.values.get("displayPreferences.fontSize"), 20, "the choice still persists");
});

test("no window open at all is not an error", async () => {
  const { chrome, store } = makeChrome({ windows: [] });
  assert.equal(chrome.setFontSize(20), 20);
  await settle();
  assert.equal(store.values.get("displayPreferences.fontSize"), 20);
});

// ── persisting ──

test("setting a tier persists it, caches it, and sends the protocol command", async () => {
  const dashboard = fakeDashboardWindow();
  const { chrome, store } = makeChrome({ windows: [dashboard] });

  assert.equal(chrome.setFontSize(20), 20);
  await settle();

  assert.equal(store.values.get("displayPreferences.fontSize"), 20);
  assert.equal(chrome.getFontSize(), 20);
});

// R5's other half: the command carries `fixed` as well as `standard`, so code
// blocks move with the prose around them instead of staying at Chromium's 13px.
test("the command sets monospace as well as standard", async () => {
  const dashboard = fakeDashboardWindow();
  const { chrome } = makeChrome({ windows: [dashboard] });

  chrome.setFontSize(12);
  await settle();

  const [, , params] = dashboard.sent()[0];
  assert.equal(params.fontSizes.standard, 12);
  assert.equal(params.fontSizes.fixed, 12);
});

test("an unsupported size is refused and changes nothing", async () => {
  const dashboard = fakeDashboardWindow();
  const { chrome, store } = makeChrome({ windows: [dashboard] });

  assert.equal(chrome.setFontSize(13), DEFAULT_TEXT_SIZE_PX);
  await settle();

  assert.equal(store.values.get("displayPreferences.fontSize"), undefined);
  assert.deepEqual(dashboard.sent(), []);
});

// ── R1: the debugger slot is shared, so attach and detach are asymmetric ──

test("attaches at protocol 1.3 and detaches when it attached", async () => {
  const dashboard = fakeDashboardWindow({ attached: false });
  const { chrome } = makeChrome({ windows: [dashboard] });

  chrome.setFontSize(20);
  await settle();

  assert.deepEqual(dashboard.kinds(), ["attach", "sendCommand", "detach"]);
  assert.equal(dashboard.calls[0][1], "1.3");
  assert.equal(dashboard.isAttached(), false);
});

// Detaching a session somebody else owns is what broke Toggle Developer Tools.
test("a session owned by somebody else is neither attached nor detached", async () => {
  const dashboard = fakeDashboardWindow({ attached: true });
  const { chrome } = makeChrome({ windows: [dashboard] });

  chrome.setFontSize(20);
  await settle();

  assert.deepEqual(dashboard.kinds(), ["sendCommand"], "no attach, and no detach");
  assert.equal(dashboard.isAttached(), true, "their session is still open");
});

// ── R2 and R3: a blocked apply repairs itself, and never rejects ──

// The scenario AC9 actually names: developer tools hold the slot, so the attach
// itself fails. The command is never sent, so nothing must be detached either.
test("an attach refused by another debugger defers without detaching", async () => {
  const warnings = [];
  const blocked = fakeDashboardWindow({ failAttach: true });
  const { chrome } = makeChrome({ windows: [blocked], log: (line) => warnings.push(line) });

  chrome.setFontSize(20);
  await settle();

  assert.deepEqual(blocked.kinds(), [], "no attach recorded, no command, no detach");
  assert.equal(warnings.length, 1, "warned once");
  assert.equal(blocked.focusListeners.length, 1, "one retry listener");

  // While the slot is still held, a focus changes nothing: the attach fails
  // again and the pick stays pending rather than being lost.
  blocked.emitFocus();
  await settle();
  assert.deepEqual(blocked.sent(), [], "still blocked, still nothing sent");

  // Developer tools close, the window regains focus, and the pick lands.
  blocked.webContents.debugger.releaseSlot();
  blocked.emitFocus();
  await settle();

  assert.deepEqual(blocked.kinds(), ["attach", "sendCommand", "detach"]);
  assert.deepEqual(blocked.sent(), [
    ["sendCommand", "Page.setFontSizes", { fontSizes: { standard: 20, fixed: 20 } }],
  ]);
});

test("a command refused after a successful attach still detaches our session", async () => {
  const blocked = fakeDashboardWindow({ failSend: true });
  const { chrome } = makeChrome({ windows: [blocked] });

  chrome.setFontSize(20);
  await settle();

  assert.deepEqual(blocked.kinds(), ["attach", "sendCommand", "detach"]);
  assert.equal(blocked.isAttached(), false, "we did not leave our own session open");
});

test("a failing apply resolves rather than rejecting", async () => {
  const blocked = fakeDashboardWindow({ failSend: true });
  const { chrome } = makeChrome({ windows: [blocked] });

  assert.equal(chrome.setFontSize(20), 20);
  await settle();
  assert.equal(chrome.getFontSize(), 20, "the choice still stands");
});

// R3 held everywhere except one guard that sat outside the try, where a throw
// from isDestroyed left a rejected promise both `void` callers ignored.
test("a throw from isDestroyed inside the apply is caught rather than rejecting", async () => {
  const rejections = [];
  const onRejection = (err) => rejections.push(err);
  process.on("unhandledRejection", onRejection);
  try {
    // One check for the enumeration, then the apply's own guard throws.
    const hostile = fakeDashboardWindow({ destroyThrowsAfter: 1 });
    const { chrome } = makeChrome({ windows: [hostile] });
    assert.equal(chrome.setFontSize(20), 20);
    await settle();
    // Give Node the extra turn it needs to report an unhandled rejection.
    await settle();
  } finally {
    process.off("unhandledRejection", onRejection);
  }
  assert.deepEqual(
    rejections.map((err) => (err && err.message) || String(err)),
    [],
    "no unhandled rejection escaped the apply",
  );
});

test("a blocked apply re-applies on the next focus", async () => {
  const blocked = fakeDashboardWindow({ failSend: true });
  const { chrome } = makeChrome({ windows: [blocked] });

  chrome.setFontSize(20);
  await settle();
  assert.equal(blocked.focusListeners.length, 1, "one focus listener registered");

  blocked.retireTo(async (method, params) => { blocked.calls.push(["retry", method, params]); });
  blocked.emitFocus();
  await settle();

  assert.deepEqual(blocked.calls.find((call) => call[0] === "retry"), [
    "retry",
    "Page.setFontSizes",
    { fontSizes: { standard: 20, fixed: 20 } },
  ]);
});

// The symptom this guards: a second blocked pick stacking a second listener,
// so one later focus fires the apply twice.
test("repeated failures register exactly one focus listener per window", async () => {
  const blocked = fakeDashboardWindow({ failSend: true });
  const { chrome } = makeChrome({ windows: [blocked] });

  for (const px of [12, 16, 20]) {
    chrome.setFontSize(px);
    await settle();
  }

  assert.equal(blocked.focusListeners.length, 1);
});

test("the re-apply uses the latest pick, not the one that first failed", async () => {
  const blocked = fakeDashboardWindow({ failSend: true });
  const { chrome } = makeChrome({ windows: [blocked] });

  chrome.setFontSize(12);
  await settle();
  chrome.setFontSize(20);
  await settle();

  const sizes = [];
  blocked.retireTo(async (_method, params) => { sizes.push(params.fontSizes.standard); });
  blocked.emitFocus();
  await settle();

  assert.deepEqual(sizes, [20]);
});

test("a successful focus re-apply does not run again on the next focus", async () => {
  const blocked = fakeDashboardWindow({ failSend: true });
  const { chrome } = makeChrome({ windows: [blocked] });

  chrome.setFontSize(20);
  await settle();

  const sizes = [];
  blocked.retireTo(async (_method, params) => { sizes.push(params.fontSizes.standard); });
  blocked.emitFocus();
  await settle();
  blocked.emitFocus();
  await settle();

  assert.deepEqual(sizes, [20], "the pending size cleared once it landed");
});

// Pending sizes are keyed per webContents, so a window holding its debugger
// slot cannot make the others wait for a focus event they do not need.
test("one blocked window does not defer the windows that succeeded", async () => {
  const blocked = fakeDashboardWindow({ failSend: true });
  const healthy = fakeDashboardWindow();
  const { chrome } = makeChrome({ windows: [blocked, healthy] });

  chrome.setFontSize(20);
  await settle();

  assert.equal(healthy.sent().length, 1, "the healthy window applied first time");
  assert.equal(healthy.focusListeners.length, 0, "and registered no retry");
  assert.equal(blocked.focusListeners.length, 1, "only the blocked window retries");
});

// ── the three defensive guards, each driven on its own path ──
//
// Each of these exists because the guard it covers could be deleted with the
// rest of the suite green. The enumeration drops a destroyed window before the
// apply sees it, so the obvious test proves the filter and not the guard; and
// the two wrapping `try` blocks only matter when something inside them throws.

// The one path with no enumeration in front of it: a focus retry on a window
// closed since it deferred. Without the apply's own destroyed guard, touching
// the debugger throws and the window logs a second warning and re-defers,
// instead of skipping in silence.
test("a retry against a window closed since it deferred is skipped silently", async () => {
  const warnings = [];
  const blocked = fakeDashboardWindow({ failSend: true });
  const { chrome } = makeChrome({ windows: [blocked], log: (line) => warnings.push(line) });

  chrome.setFontSize(20);
  await settle();
  assert.equal(warnings.length, 1, "the blocked apply warned once");
  assert.equal(blocked.focusListeners.length, 1);
  // The first apply attached and its command was refused, so compare the retry
  // against this rather than against an empty list.
  const callsBeforeRetry = blocked.calls.length;

  blocked.destroyNow();
  blocked.emitFocus();
  await settle();

  assert.equal(warnings.length, 1, "the retry on a dead window added no warning");
  assert.equal(blocked.calls.length, callsBeforeRetry, "and touched the debugger not at all");
});

// The wrap around the catch body. Without it a throw from the log escapes the
// catch and both `void` callers get a rejected promise.
test("a throw from the log inside the failure path does not reject", async () => {
  const rejections = [];
  const onRejection = (err) => rejections.push(err);
  process.on("unhandledRejection", onRejection);
  try {
    const blocked = fakeDashboardWindow({ failSend: true });
    const { chrome } = makeChrome({
      windows: [blocked],
      log: () => { throw new Error("the log itself failed"); },
    });
    assert.equal(chrome.setFontSize(20), 20);
    await settle();
    await settle();
  } finally {
    process.off("unhandledRejection", onRejection);
  }
  assert.deepEqual(
    rejections.map((err) => (err && err.message) || String(err)),
    [],
    "no unhandled rejection escaped the failure path",
  );
});

// The wrap around the fan-out. Enumerating the windows reaches Electron, and
// both callers reach the fan-out through `void`.
test("a throw while enumerating the windows is logged rather than rejecting", async () => {
  const rejections = [];
  const onRejection = (err) => rejections.push(err);
  const warnings = [];
  process.on("unhandledRejection", onRejection);
  let chrome;
  try {
    const hostile = createWindowChrome({
      BaseWindow: { getAllWindows: () => { throw new Error("BaseWindow is gone"); } },
      nativeTheme: {},
      screen: {},
      store: fakeStore(),
      log: (line) => warnings.push(line),
      isMac: false,
      isWindows: false,
      windowForWebContents: () => null,
    });
    chrome = hostile;
    assert.equal(chrome.setFontSize(20), 20);
    await settle();
    await settle();
  } finally {
    process.off("unhandledRejection", onRejection);
  }
  assert.deepEqual(
    rejections.map((err) => (err && err.message) || String(err)),
    [],
    "no unhandled rejection escaped the fan-out",
  );
  assert.equal(warnings.length, 1, "the failure was logged once");
  assert.equal(chrome.getFontSize(), 20, "and the choice still stands");
});

// ── the per-load re-apply ──
//
// A replaced renderer reads `defaultFontSize` out of the webPreferences the
// window was built with, so a pick made after that build would be lost. The
// Windows caption reserve is re-pushed from the same event for the same reason.

test("a load re-applies the stored size to that view", async () => {
  const dashboard = fakeDashboardWindow();
  const { chrome } = makeChrome({
    store: fakeStore({ "displayPreferences.fontSize": 20 }),
    windows: [dashboard],
  });

  chrome.trackFontSizeChrome({ webContents: dashboard.webContents });
  assert.equal(dashboard.loadListeners.length, 1, "one did-finish-load listener");

  dashboard.emitLoad();
  await settle();

  assert.deepEqual(dashboard.sent(), [
    ["sendCommand", "Page.setFontSizes", { fontSizes: { standard: 20, fixed: 20 } }],
  ]);
});

test("the re-apply uses the size picked since the window was built", async () => {
  const dashboard = fakeDashboardWindow();
  const { chrome } = makeChrome({ windows: [dashboard] });
  chrome.trackFontSizeChrome({ webContents: dashboard.webContents });

  // Built at the 16px default, then the user picks 12.
  chrome.setFontSize(12);
  await settle();
  const afterPick = dashboard.sent().length;

  dashboard.emitLoad();
  await settle();

  const reapplied = dashboard.sent().slice(afterPick);
  assert.deepEqual(reapplied, [
    ["sendCommand", "Page.setFontSizes", { fontSizes: { standard: 12, fixed: 12 } }],
  ], "the newer pick, not the build-time default");
});

// The re-apply runs through the same apply, so it inherits R3: a view that died
// between the load and the handler must not reject.
test("a re-apply against a view destroyed since the load does not reject", async () => {
  const rejections = [];
  const onRejection = (err) => rejections.push(err);
  process.on("unhandledRejection", onRejection);
  try {
    const dashboard = fakeDashboardWindow();
    const { chrome } = makeChrome({ windows: [dashboard] });
    chrome.trackFontSizeChrome({ webContents: dashboard.webContents });
    dashboard.destroyNow();
    dashboard.emitLoad();
    await settle();
    await settle();
  } finally {
    process.off("unhandledRejection", onRejection);
  }
  assert.deepEqual(rejections.map((err) => (err && err.message) || String(err)), []);
});

// A slow failure races a newer pick. Without the staleness guard in
// deferFontSize, the slow apply's catch queues ITS size after the newer one has
// already landed, and the next focus re-applies the older size over it, leaving
// the window disagreeing with both the menu and the store.
test("a slow failure does not queue its stale size over a newer pick", async () => {
  const dashboard = fakeDashboardWindow();
  const { chrome } = makeChrome({ windows: [dashboard] });

  // The first apply hangs until released, then fails.
  let failFirst;
  const firstInFlight = new Promise((_resolve, reject) => { failFirst = reject; });
  dashboard.retireTo(async () => { await firstInFlight; });

  chrome.setFontSize(12);
  await settle();

  // A newer pick lands while the first is still in flight.
  dashboard.retireTo(async () => {});
  chrome.setFontSize(20);
  await settle();
  assert.equal(chrome.getFontSize(), 20);

  // Only now does the first apply fail.
  failFirst(new Error("the slot was held all along"));
  await settle();

  const reapplied = [];
  dashboard.retireTo(async (_method, params) => { reapplied.push(params.fontSizes.standard); });
  dashboard.emitFocus();
  await settle();

  assert.deepEqual(reapplied, [], "the stale 12px was never queued for a retry");
});
