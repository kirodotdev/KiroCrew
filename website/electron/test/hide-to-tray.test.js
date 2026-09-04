const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { hideToTray, cancelPendingTrayHide, DEFAULT_LEAVE_TIMEOUT_MS } = require("../hide-to-tray");

// Fake BrowserWindow/BaseWindow recording the calls hideToTray makes. Only the
// members the helper touches are implemented. `once` captures listeners so a
// test can fire macOS's asynchronous `leave-full-screen` by hand — the real
// Space animation takes ~0.5s, which is exactly why the helper cannot hide on
// the next line.
function makeWin({ fullScreen = false, destroyed = false, throwOnSetFullScreen = false } = {}) {
  const calls = [];
  const listeners = new Map();
  const win = {
    calls,
    listeners,
    isDestroyed: () => destroyed,
    isFullScreen: () => fullScreen,
    setFullScreen: (v) => {
      if (throwOnSetFullScreen) throw new Error("setFullScreen failed");
      calls.push(["setFullScreen", v]);
    },
    once: (event, fn) => {
      listeners.set(event, fn);
    },
    off: (event, fn) => {
      if (listeners.get(event) === fn) listeners.delete(event);
    },
    hide: () => calls.push(["hide"]),
    // Test-only: pretend macOS finished tearing the Space down.
    emitLeaveFullScreen() {
      const fn = listeners.get("leave-full-screen");
      assert.ok(fn, "expected a leave-full-screen listener to be armed");
      fn();
    },
    destroy() {
      destroyed = true;
    },
  };
  return win;
}

// Controllable timer pair so the backstop is asserted without real waiting.
function makeTimers() {
  const scheduled = [];
  return {
    scheduled,
    setTimeoutFn: (fn, ms) => {
      const handle = { fn, ms, cleared: false, unrefed: false, unref() { this.unrefed = true; return this; } };
      scheduled.push(handle);
      return handle;
    },
    clearTimeoutFn: (handle) => {
      if (handle) handle.cleared = true;
    },
    fire: (i = 0) => scheduled[i].fn(),
  };
}

// Every test pins `isMac` rather than inheriting the host platform: the
// fullscreen behaviour below is macOS-only, and CI runs this suite on Linux and
// Windows too, where an unpinned test would assert the wrong branch.
const mac = (extra = {}) => ({ isMac: true, ...extra });

describe("hideToTray", () => {
  // The common path: closing a windowed window must stay a plain, immediate
  // hide. A regression here would make the tray close feel laggy for everyone
  // on every platform, not just the fullscreen case being fixed.
  it("hides immediately when the window is not fullscreen", () => {
    const win = makeWin({ fullScreen: false });
    const result = hideToTray(win, mac());
    assert.deepEqual(win.calls, [["hide"]]);
    assert.deepEqual(result, { hidden: true, deferred: false, leftFullScreen: false });
  });

  // Regression guard for #1000. Hiding a window that owns a native macOS
  // fullscreen Space orphans the Space as a black surface and leaves the window
  // flagged fullscreen, so it later re-shows at a degenerate (tiny) frame. The
  // helper must leave fullscreen and NOT hide yet.
  it("leaves fullscreen first and does not hide until the Space is torn down", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    const result = hideToTray(win, mac(timers));

    assert.deepEqual(win.calls, [["setFullScreen", false]], "must not hide mid-transition");
    assert.deepEqual(result, { hidden: false, deferred: true, leftFullScreen: true });

    win.emitLeaveFullScreen();
    assert.deepEqual(win.calls, [["setFullScreen", false], ["hide"]]);
  });

  // setFullScreen(false) is async on macOS: hiding on the next line is the bug.
  // This pins the ORDER, which is the entire fix — an implementation that called
  // hide() before the event would pass the "eventually hides" assertion above.
  it("orders the fullscreen exit strictly before the hide", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac(timers));
    win.emitLeaveFullScreen();
    assert.deepEqual(win.calls.map(([name]) => name), ["setFullScreen", "hide"]);
  });

  // Scope guard. Windows/Linux fullscreen is a borderless maximized window with
  // no Space behind it, so the exit buys nothing there and costs something real:
  // the window would reopen WINDOWED, and the geometry listener (which persists
  // on leave-full-screen) would save it as windowed for the next launch too — a
  // visible regression on two platforms that never had this bug.
  it("does not touch fullscreen off macOS — a plain hide, as before", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    const result = hideToTray(win, { isMac: false, ...timers });

    assert.deepEqual(win.calls, [["hide"]], "no setFullScreen off darwin");
    assert.deepEqual(result, { hidden: true, deferred: false, leftFullScreen: false });
    assert.equal(win.listeners.size, 0, "no listener armed off darwin");
    assert.equal(timers.scheduled.length, 0, "no backstop timer armed off darwin");
  });

  // The backstop. If AppKit swallows the transition (an animation already in
  // flight) the event never lands, and without this the close button would
  // silently do nothing at all — worse than the orphaned Space.
  it("hides anyway if leave-full-screen never fires", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac(timers));

    assert.equal(timers.scheduled.length, 1);
    assert.equal(timers.scheduled[0].ms, DEFAULT_LEAVE_TIMEOUT_MS);
    assert.deepEqual(win.calls, [["setFullScreen", false]]);

    timers.fire();
    assert.deepEqual(win.calls, [["setFullScreen", false], ["hide"]]);
  });

  // `once` only self-removes when it FIRES, so the backstop path would otherwise
  // leave a listener armed for the life of the process, one per swallowed
  // transition, until MaxListenersExceededWarning.
  it("removes the leave-full-screen listener when the backstop wins", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac(timers));
    assert.equal(win.listeners.size, 1);

    timers.fire();
    assert.equal(win.listeners.size, 0, "the stale listener must not survive the backstop");
  });

  // Exactly-once: the event and the backstop race on every real fullscreen
  // close. A second hide() on an already-hidden window is not merely redundant
  // — on macOS it can re-order tray/Dock activation state.
  it("hides exactly once when both the event and the backstop fire", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac(timers));

    const armed = win.listeners.get("leave-full-screen");
    win.emitLeaveFullScreen();
    assert.equal(timers.scheduled[0].cleared, true, "the backstop must be cleared on success");
    timers.fire(); // fire it anyway — a real timer could already be in flight
    armed(); // and re-enter through the listener the same way a stray event would

    assert.deepEqual(win.calls, [["setFullScreen", false], ["hide"]]);
  });

  // A pending hide must never be the reason the process lingers on a real quit.
  it("unrefs the backstop timer", () => {
    const timers = makeTimers();
    hideToTray(makeWin({ fullScreen: true }), mac(timers));
    assert.equal(timers.scheduled[0].unrefed, true);
  });

  // The quit path destroys the window while the Space animation is still in
  // flight; hiding a destroyed window throws in Electron.
  it("does not hide a window destroyed during the fullscreen exit", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac(timers));
    win.destroy();
    win.emitLeaveFullScreen();
    assert.deepEqual(win.calls, [["setFullScreen", false]], "no hide() on a destroyed window");
  });

  // If the exit cannot even be started, nothing will ever fire the listener, so
  // falling through to the backstop would leave the window mapped for 2s.
  it("hides immediately when setFullScreen throws", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true, throwOnSetFullScreen: true });
    const result = hideToTray(win, mac(timers));
    assert.deepEqual(win.calls, [["hide"]]);
    assert.equal(result.hidden, true);
    assert.equal(result.leftFullScreen, false);
  });

  // Defensive: BaseWindow variants and test doubles may not expose the
  // fullscreen API at all. Falling back to a plain hide keeps the tray close
  // working rather than throwing out of a `close` handler.
  it("falls back to a plain hide when the fullscreen API is absent", () => {
    const calls = [];
    const result = hideToTray({ hide: () => calls.push(["hide"]) }, mac());
    assert.deepEqual(calls, [["hide"]]);
    assert.equal(result.hidden, true);
  });

  it("is a no-op for a destroyed or missing window", () => {
    const idle = { hidden: false, deferred: false, leftFullScreen: false };
    const win = makeWin({ fullScreen: true, destroyed: true });
    assert.deepEqual(hideToTray(win, mac()), idle);
    assert.deepEqual(win.calls, []);
    assert.deepEqual(hideToTray(null), idle);
    assert.deepEqual(hideToTray(undefined), idle);
  });

  it("never throws when hide() itself throws", () => {
    const win = {
      isDestroyed: () => false,
      isFullScreen: () => false,
      hide: () => {
        throw new Error("hide failed");
      },
    };
    assert.equal(hideToTray(win, mac()).hidden, false);
  });

  // The default must follow the real platform, since window-lifecycle.js passes
  // no options.
  it("defaults isMac to the host platform", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, timers);
    if (process.platform === "darwin") {
      assert.deepEqual(win.calls, [["setFullScreen", false]]);
    } else {
      assert.deepEqual(win.calls, [["hide"]]);
    }
  });
});

// A show request (Dock activate, tray "Show", the summon hotkey) landing while
// the hide is deferred to the fullscreen exit must win over the pending hide.
// Without a cancel, the show either gets skipped (the window is still visible,
// so `isVisible()` guards it away) or is silently undone moments later when
// `leave-full-screen` (or the backstop) fires — the user asks for the window
// back and watches it vanish.
describe("cancelPendingTrayHide", () => {
  it("disarms a deferred hide so the fullscreen exit no longer hides", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac(timers));

    assert.equal(cancelPendingTrayHide(win), true, "a pending hide must report disarmed");
    assert.equal(timers.scheduled[0].cleared, true, "the backstop must be cleared");
    assert.equal(win.listeners.size, 0, "the leave-full-screen listener must be removed");

    // The transition still completes (the exit itself is not reversed) and the
    // backstop may already be in flight — neither may hide now.
    timers.fire();
    assert.deepEqual(win.calls, [["setFullScreen", false]], "no hide after cancel");
  });

  it("returns false when nothing is pending", () => {
    assert.equal(cancelPendingTrayHide(makeWin()), false);
    assert.equal(cancelPendingTrayHide(null), false);
    assert.equal(cancelPendingTrayHide(undefined), false);
  });

  // An immediate (non-deferred) hide leaves nothing to cancel: the window is
  // already hidden, and the later show() re-shows it normally.
  it("has nothing to cancel after a non-fullscreen hide", () => {
    const win = makeWin({ fullScreen: false });
    hideToTray(win, mac());
    assert.equal(cancelPendingTrayHide(win), false);
  });

  // Exactly-once settlement is shared with the hide paths: once the event or
  // the backstop has hidden the window, there is no pending hide left, and a
  // late cancel must not report having disarmed anything.
  it("is a no-op after the deferred hide already landed", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac(timers));
    win.emitLeaveFullScreen();
    assert.deepEqual(win.calls, [["setFullScreen", false], ["hide"]]);
    assert.equal(cancelPendingTrayHide(win), false);
  });

  it("is idempotent — a second cancel reports nothing pending", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac(timers));
    assert.equal(cancelPendingTrayHide(win), true);
    assert.equal(cancelPendingTrayHide(win), false);
  });

  // The user closes again after summoning the window back: the next close must
  // defer-and-hide exactly as the first one did, unaffected by the cancel.
  it("does not break a subsequent hideToTray on the same window", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac(timers));
    cancelPendingTrayHide(win);

    hideToTray(win, mac(timers));
    assert.deepEqual(win.calls, [["setFullScreen", false], ["setFullScreen", false]]);
    win.emitLeaveFullScreen();
    assert.deepEqual(win.calls, [
      ["setFullScreen", false],
      ["setFullScreen", false],
      ["hide"],
    ]);
  });

  // The setFullScreen-throw path settles synchronously inside hideToTray, so it
  // must not leave a cancellable entry behind.
  it("has nothing to cancel when the exit could not be started", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true, throwOnSetFullScreen: true });
    hideToTray(win, mac(timers));
    assert.equal(cancelPendingTrayHide(win), false);
  });
});

// The helper above is only a fix if the owning window boundary routes close and
// show paths through it. A correct helper with the old `mainWindow.hide()` still
// at the call site passes every test above while the bug is fully intact. Main
// remains responsible for routing app-level user intent into that façade, so
// both composition and owner sources are pinned below.
describe("window lifecycle tray-close wiring", () => {
  const MAIN_JS = fs.readFileSync(path.join(__dirname, "..", "main.js"), "utf8");
  const WINDOW_LIFECYCLE_JS = fs.readFileSync(
    path.join(__dirname, "..", "window-lifecycle.js"),
    "utf8",
  );
  const IPC_REGISTRAR_JS = fs.readFileSync(
    path.join(__dirname, "..", "ipc-registrar.js"),
    "utf8",
  );

  it("the owning window boundary requires the helper and main composes that owner", () => {
    assert.match(WINDOW_LIFECYCLE_JS, /require\("\.\/hide-to-tray"\)/);
    assert.match(MAIN_JS, /const \{ createWindowLifecycle \} = require\("\.\/window-lifecycle"\)/);
    assert.match(MAIN_JS, /windows = createWindowLifecycle\(\{/);
  });

  it("routes the non-quit close through hideToTray, not a bare hide", () => {
    // The close handler's non-quit branch, up to its `return`.
    const branch = WINDOW_LIFECYCLE_JS.match(
      /mainWindow\.on\("close"[\s\S]*?if \(!isQuitting\(\)\) \{([\s\S]*?)return;/,
    );
    assert.ok(branch, "could not locate the close handler's non-quit branch");
    const body = branch[1];
    assert.match(body, /hideToTray\(mainWindow\)/);
    assert.doesNotMatch(
      body,
      /mainWindow\.hide\(\)/,
      "a direct hide() orphans the macOS fullscreen Space — go through hideToTray",
    );
  });

  // A correct cancel helper with the show paths still calling a bare show()
  // passes every unit test above while the bug is fully intact: an activate or
  // tray gesture during the deferred hide would still lose to it. Pin each
  // user-initiated show path to the cancel.
  it("cancels the pending hide before the activate show", () => {
    const composition = MAIN_JS.match(/app\.on\("activate", \(\) => \{([\s\S]*?)\}\);/);
    assert.ok(composition, "could not locate the activate handler");
    assert.match(
      composition[1],
      /windows\.activateMainWindow\(\)/,
      "main must preserve the activate user-intent route through the window owner",
    );

    const start = WINDOW_LIFECYCLE_JS.indexOf("function activateMainWindow()");
    const end = WINDOW_LIFECYCLE_JS.indexOf("function createTray()", start);
    assert.notEqual(start, -1, "could not locate activateMainWindow");
    assert.notEqual(end, -1, "could not bound activateMainWindow");
    const body = WINDOW_LIFECYCLE_JS.slice(start, end);
    assert.match(body, /cancelPendingTrayHide\(mainWindow\)/);
    assert.ok(
      body.indexOf("cancelPendingTrayHide") < body.indexOf(".show()"),
      "the cancel must run before the show",
    );
  });

  it("routes both tray show gestures through the cancelling helper", () => {
    // The menu item and the icon click are the same user intent; both must
    // disarm the pending hide, so both go through the one helper that does.
    const helper = WINDOW_LIFECYCLE_JS.match(
      /const showFromTray = \(\) => \{([\s\S]*?)\};/,
    );
    assert.ok(helper, "could not locate showFromTray");
    assert.match(helper[1], /cancelPendingTrayHide\(mainWindow\)/);
    assert.match(
      WINDOW_LIFECYCLE_JS,
      /\{ label: `Show \$\{app\.name\}`, click: showFromTray \}/,
    );
    assert.match(WINDOW_LIFECYCLE_JS, /tray\.on\("click", showFromTray\)/);
  });

  // The remaining user-intent shows of a possibly-deferred window: relaunching
  // the app (second-instance), the tray "New Connection Window…" item, opening
  // settings, and clicking the update notification. Each must disarm before it
  // shows, or the window it surfaces vanishes when the deferral settles.
  it("cancels the pending hide on every other user-intent show path", () => {
    const composedUserIntents = [
      [
        "second-instance",
        MAIN_JS,
        /app\.on\("second-instance"[\s\S]*?windows\?\.showMainWindow\(\{ focus: true \}\)/,
      ],
      [
        "update-notification click",
        IPC_REGISTRAR_JS,
        /notification\.on\("click"[\s\S]*?windows\.showMainWindow\(\{ focus: true \}\)/,
      ],
    ];
    for (const [name, source, route] of composedUserIntents) {
      assert.match(source, route, name + " must route through the cancelling window façade");
    }

    const ownerSites = [
      ["showMainWindow", "function showMainWindow", "function activateMainWindow"],
      [
        "openNewConnectionWindow",
        "async function openNewConnectionWindow",
        "function renameCurrentWindow",
      ],
      ["openSettingsPage", "function openSettingsPage", "function toggleAlwaysOnTop"],
    ];
    for (const [name, startMarker, endMarker] of ownerSites) {
      const start = WINDOW_LIFECYCLE_JS.indexOf(startMarker);
      const end = WINDOW_LIFECYCLE_JS.indexOf(endMarker, start);
      assert.notEqual(start, -1, "could not locate the " + name + " show path");
      assert.notEqual(end, -1, "could not bound the " + name + " show path");
      const body = WINDOW_LIFECYCLE_JS.slice(start, end);
      assert.match(body, /cancelPendingTrayHide\(/, name + " must disarm the pending hide");
      assert.ok(
        body.indexOf("cancelPendingTrayHide") < body.indexOf(".show()"),
        name + ": the cancel must run before the show",
      );
    }
  });
});
