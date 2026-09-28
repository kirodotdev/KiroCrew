const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  hideToTray,
  cancelPendingTrayHide,
  shouldKeepAppHidden,
  DEFAULT_LEAVE_TIMEOUT_MS,
  POST_LEAVE_SETTLE_MS,
  HIDE_REASSERT_MS,
  TRANSITION_QUIET_MS,
} = require("../hide-to-tray");

// Fake BrowserWindow/BaseWindow recording the calls hideToTray makes. Only the
// members the helper touches are implemented. `once` captures listeners so a
// test can fire macOS's asynchronous `leave-full-screen` by hand — the real
// Space animation takes ~0.5s, which is exactly why the helper cannot hide on
// the next line.
function makeWin({
  fullScreen = false,
  destroyed = false,
  throwOnSetFullScreen = false,
  throwOnOnce = false,
} = {}) {
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
      // The style mask flips at the START of the transition, long before the
      // terminal event; a backstop that reads isFullScreen() sees this.
      fullScreen = v;
    },
    once: (event, fn) => {
      if (throwOnOnce) throw new Error("once failed");
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
    // Test-only: the exit toggle was swallowed — the flag never flipped.
    stayFullScreen() {
      fullScreen = true;
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
    fire: (i = 0) => {
      scheduled[i].cleared = true;
      scheduled[i].fn();
    },
    // The most recently scheduled timer — after leave-full-screen that is the
    // settle delay, not the backstop. Explicit fires may target a cleared timer
    // to model a callback that was already queued when clearTimeout ran.
    fireLast: () => {
      const handle = scheduled[scheduled.length - 1];
      handle.cleared = true;
      handle.fn();
    },
    live: () => scheduled.filter((h) => !h.cleared),
  };
}

// Records the app-level hide the backstop falls back to, so a test can tell it
// apart from the window-level hide() it must NOT use there.
function makeAppHide(win) {
  return { hideAppFn: () => win.calls.push(["app.hide"]) };
}

// Every test pins `isMac` rather than inheriting the host platform: the
// fullscreen behaviour below is macOS-only, and CI runs this suite on Linux and
// Windows too, where an unpinned test would assert the wrong branch.
const mac = (extra = {}) => ({ isMac: true, ...extra });
const idleResult = {
  hidden: false,
  deferred: false,
  leftFullScreen: false,
  stalled: false,
  hidApp: false,
  waitedForQuiet: false,
};

describe("hideToTray", () => {
  // The common path: closing a windowed window must stay a plain, immediate
  // hide of that WINDOW. A regression here would make the tray close feel laggy
  // for everyone on every platform, and hiding the whole app for an ordinary
  // close would take unrelated windows down with it.
  it("hides immediately when the window is not fullscreen", () => {
    const win = makeWin({ fullScreen: false });
    const result = hideToTray(win, mac(makeAppHide(win)));
    assert.deepEqual(win.calls, [["hide"]], "the window, not the app");
    assert.deepEqual(result, { ...idleResult, hidden: true });
  });

  // Electron flips isFullScreen() to the target state at transition start. A
  // user-initiated EXIT therefore looks windowed even though AppKit still owns
  // the fullscreen snapshot. The watch's pending target keeps that interval on
  // the app-hide path, without interrupting the exit with a duplicate toggle.
  it("attaches to an in-flight user exit without requesting another exit", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: false });
    const result = hideToTray(
      win,
      mac({ ...timers, ...makeAppHide(win), transitionTarget: false }),
    );

    assert.deepEqual(win.calls, [], "must not hide or toggle during the active exit");
    assert.equal(win.listeners.has("leave-full-screen"), true);
    assert.deepEqual(result, { ...idleResult, deferred: true });

    win.emitLeaveFullScreen();
    assert.deepEqual(win.calls, [], "the terminal event still has an AppKit ordering tail");
    timers.live().find((handle) => handle.ms === POST_LEAVE_SETTLE_MS).fn();
    assert.deepEqual(win.calls, [["app.hide"]]);
  });

  // The watch disarms its pending target inside leave-full-screen, while AppKit
  // can still order the real window in afterward. A close in that known tail
  // must not fall back to win.hide() merely because the terminal event fired.
  it("protects a close during the post-exit settle tail", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: false });
    let sinceExit = 0;
    const result = hideToTray(
      win,
      mac({ ...timers, ...makeAppHide(win), exitSettlingFor: () => sinceExit }),
    );

    assert.deepEqual(win.calls, [], "must not hide the window inside AppKit's exit tail");
    assert.equal(result.deferred, true);

    sinceExit = POST_LEAVE_SETTLE_MS;
    timers.live().find((handle) => handle.ms === POST_LEAVE_SETTLE_MS).fn();
    assert.deepEqual(win.calls, [["app.hide"]]);
  });

  // Regression guard for #1000. Hiding a window that owns a native macOS
  // fullscreen Space orphans the Space as a black surface and leaves the window
  // flagged fullscreen, so it later re-shows at a degenerate (tiny) frame. The
  // helper must leave fullscreen and NOT hide yet.
  it("leaves fullscreen first and does not hide until the Space is torn down", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    const result = hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));

    assert.deepEqual(win.calls, [["setFullScreen", false]], "must not hide mid-transition");
    assert.deepEqual(result, { ...idleResult, deferred: true, leftFullScreen: true });

    win.emitLeaveFullScreen();
    timers.fireLast();
    assert.deepEqual(win.calls, [["setFullScreen", false], ["app.hide"]]);
    assert.equal(result.hidApp, true);
  });

  // ROOT CAUSE, and why the fullscreen path hides the APP. AppKit does not queue
  // a fullscreen toggle issued while one of its own transitions is animating: it
  // abandons the running transition and leaves that transition's full-display
  // snapshot overlay on screen owned by nothing. Hiding only the WINDOW then
  // leaves the user with the overlay as the only thing on screen — the reported
  // bug. There is no in-process signal for "AppKit finished" (measured:
  // `enter-full-screen` arrives well before the animation ends, and closing
  // after it still orphaned an overlay), so the guard cannot be a wait. It is
  // `app.hide()`, which orders the overlay out along with everything else.
  it("hides the app, not the window, on the fullscreen path", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));
    win.emitLeaveFullScreen();
    timers.fireLast();
    assert.deepEqual(
      win.calls,
      [["setFullScreen", false], ["app.hide"]],
      "win.hide() here leaves an orphan overlay as the only thing on screen",
    );
  });

  // setFullScreen(false) is async on macOS: hiding on the next line is the bug.
  // This pins the ORDER, which is the entire fix — an implementation that hid
  // before the event would pass the "eventually hides" assertion above.
  it("orders the fullscreen exit strictly before the hide", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));
    win.emitLeaveFullScreen();
    timers.fireLast();
    assert.deepEqual(win.calls.map(([name]) => name), ["setFullScreen", "app.hide"]);
  });

  // AppKit re-orders the real window in ~25ms AFTER windowDidExitFullScreen as
  // the tail of its own transition. A hide issued synchronously inside the event
  // races that order-in, so it waits out a short settle delay.
  it("delays the hide after leave-full-screen by the settle interval", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));

    win.emitLeaveFullScreen();
    assert.deepEqual(win.calls, [["setFullScreen", false]], "no hide inside the event itself");
    assert.equal(timers.scheduled[0].cleared, true, "the backstop must be cleared on the event");
    const settle = timers.live();
    assert.equal(settle.length, 1, "exactly one settle timer is armed");
    assert.equal(settle[0].ms, POST_LEAVE_SETTLE_MS);
    assert.equal(settle[0].unrefed, true);

    settle[0].fn();
    assert.deepEqual(win.calls, [["setFullScreen", false], ["app.hide"]]);
  });

  // Scope guard. Windows/Linux fullscreen is a borderless maximized window with
  // no Space behind it, so the exit buys nothing there and costs something real:
  // the window would reopen WINDOWED, and the geometry listener (which persists
  // on leave-full-screen) would save it as windowed for the next launch too — a
  // visible regression on two platforms that never had this bug. There is no
  // AppKit overlay there either, so the hide stays window-level.
  it("does not touch fullscreen off macOS — a plain hide, as before", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    const result = hideToTray(win, { isMac: false, ...timers, ...makeAppHide(win) });

    assert.deepEqual(win.calls, [["hide"]], "no setFullScreen and no app.hide off darwin");
    assert.deepEqual(result, { ...idleResult, hidden: true });
    assert.equal(win.listeners.size, 0, "no listener armed off darwin");
    assert.equal(timers.scheduled.length, 0, "no backstop timer armed off darwin");
  });

  // ROOT CAUSE. AppKit abandons a fullscreen transition that a toggle interrupts,
  // and the transition's full-display snapshot overlay is then orphaned on screen
  // — no traffic lights, immovable, covering every other app. Its completion
  // callback is NOT the all-clear: measured on macOS 26, `enter-full-screen`
  // arrives while the Space animation is still running, and a close issued after
  // it still orphaned an overlay in about a third of runs (and the hide itself
  // was swallowed). Waiting for the window to be STILL for a grace period fixed
  // both (0 of 15 runs). So the exit is gated on quiet time, not on an event.
  it("waits for AppKit to be quiet before issuing the exit", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    let quiet = 0; // the transition is still moving
    const result = hideToTray(
      win,
      mac({ ...timers, ...makeAppHide(win), quietFor: () => quiet }),
    );

    assert.deepEqual(win.calls, [], "the exit must not be issued into a live transition");
    assert.equal(result.waitedForQuiet, true);
    assert.equal(result.deferred, true);

    // Still moving: the poll re-arms rather than exiting.
    timers.live().find((h) => h.ms <= TRANSITION_QUIET_MS).fn();
    assert.deepEqual(win.calls, []);

    quiet = TRANSITION_QUIET_MS; // AppKit has settled
    timers.live().find((h) => h.ms <= TRANSITION_QUIET_MS).fn();
    assert.deepEqual(win.calls, [["setFullScreen", false]]);

    win.emitLeaveFullScreen();
    timers.live().find((h) => h.ms === POST_LEAVE_SETTLE_MS).fn();
    assert.deepEqual(win.calls, [["setFullScreen", false], ["app.hide"]]);
  });

  // The gate must not become a new way to hang the close. With no probe (or one
  // that cannot answer) the exit goes out immediately, exactly as before.
  it("exits immediately when the quiet probe is absent or unusable", () => {
    for (const quietFor of [
      undefined,
      () => Infinity,
      () => {
        throw new Error("probe failed");
      },
    ]) {
      const timers = makeTimers();
      const win = makeWin({ fullScreen: true });
      const result = hideToTray(win, mac({ ...timers, ...makeAppHide(win), quietFor }));
      assert.deepEqual(win.calls, [["setFullScreen", false]]);
      assert.equal(result.waitedForQuiet, false);
      assert.equal(result.deferred, true);
    }
  });

  // A window that never goes quiet must still honour the close: the backstop
  // covers the whole wait, and it hides the app.
  it("hides the app when the window never goes quiet", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    const result = hideToTray(win, mac({ ...timers, ...makeAppHide(win), quietFor: () => 0 }));
    assert.deepEqual(win.calls, [], "no exit was ever issued");

    timers.scheduled.find((h) => h.ms === DEFAULT_LEAVE_TIMEOUT_MS + TRANSITION_QUIET_MS).fn();
    assert.deepEqual(win.calls, [["app.hide"]]);
    assert.equal(result.stalled, true);
    assert.equal(timers.live().filter((h) => h.ms <= TRANSITION_QUIET_MS).length, 0,
      "the quiet poll must be disarmed once the hide settles");
  });

  // The backstop. If AppKit never delivers `leave-full-screen` the close gesture
  // must still take effect — but the transition it belongs to has not finished,
  // and AppKit's full-display snapshot overlay may still be on screen. Hiding
  // the real WINDOW there leaves the user with only that overlay (the frozen
  // copy that blocks every other app). Hiding the APP orders the overlay out
  // too, so that is the only hide the backstop may perform.
  it("hides the app, never the window, if leave-full-screen never fires", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    const result = hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));

    assert.equal(timers.scheduled.length, 1);
    assert.equal(timers.scheduled[0].ms, DEFAULT_LEAVE_TIMEOUT_MS + TRANSITION_QUIET_MS);
    assert.deepEqual(win.calls, [["setFullScreen", false]]);
    assert.equal(result.stalled, false, "not stalled until the backstop actually runs");

    timers.fire();
    assert.deepEqual(win.calls, [["setFullScreen", false], ["app.hide"]]);
    assert.equal(result.stalled, true, "the caller's result is marked stalled in place");
  });

  // The other way the event can fail to land: the toggle was swallowed and the
  // window is still inside its Space. A window-level hide there is the original
  // orphaned-black-Space bug; hiding the whole app out of a fullscreen Space is
  // ordinary ⌘H behaviour and orphans nothing.
  it("hides the app when the exit never even started", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));
    win.stayFullScreen();

    timers.fire();
    assert.deepEqual(win.calls, [["setFullScreen", false], ["app.hide"]]);
  });

  // The backstop's log line is the only evidence a stalled exit leaves behind.
  it("logs the stalled exit with the timeout and the fullscreen flag", () => {
    const timers = makeTimers();
    const lines = [];
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac({ ...timers, ...makeAppHide(win), log: (l) => lines.push(l) }));
    timers.fire();
    assert.equal(lines.length, 1);
    assert.match(lines[0], /did not complete within 2000ms/);
    assert.match(lines[0], /isFullScreen=false/);
    assert.match(lines[0], /hiding the app/);
  });

  // `once` only self-removes when it FIRES, so the backstop path would otherwise
  // leave a listener armed for the life of the process, one per swallowed
  // transition, until MaxListenersExceededWarning.
  it("removes the leave-full-screen listener when the backstop wins", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));
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
    hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));

    const armed = win.listeners.get("leave-full-screen");
    win.emitLeaveFullScreen();
    assert.equal(timers.scheduled[0].cleared, true, "the backstop must be cleared on success");
    timers.scheduled.find((h) => h.ms === POST_LEAVE_SETTLE_MS).fn(); // the settle lands the hide
    timers.fire(); // fire the backstop anyway — a real timer could already be in flight
    armed(); // and re-enter through the listener the same way a stray event would

    assert.deepEqual(
      win.calls.filter(([n]) => n === "app.hide").length,
      1,
      "the stray event and the backstop must not hide a second time",
    );
    assert.deepEqual(win.calls, [["setFullScreen", false], ["app.hide"]]);
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
    hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));
    win.destroy();
    win.emitLeaveFullScreen();
    timers.fireLast();
    assert.deepEqual(win.calls, [["setFullScreen", false]], "no hide() on a destroyed window");
  });

  it("does not hide the app for a window destroyed before the backstop", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));
    win.destroy();
    timers.fire();
    assert.deepEqual(win.calls, [["setFullScreen", false]], "no app.hide() after destroy");
  });

  // If the terminal listener cannot be installed, the window is still known to
  // own a native Space. Falling back to win.hide() here would recreate the
  // original black-Space/overlay failure.
  it("hides the app when the fullscreen listener cannot be installed", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true, throwOnOnce: true });
    const result = hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));
    assert.deepEqual(win.calls, [["app.hide"]]);
    assert.equal(result.hidApp, true);
    assert.equal(result.hidden, false);
    assert.equal(shouldKeepAppHidden(win), true);
    assert.equal(cancelPendingTrayHide(win), true);
    timers.live().forEach((handle) => handle.fn());
    assert.deepEqual(win.calls, [["app.hide"]], "the cancelled re-assert must not fire");
  });

  // If the exit cannot even be started, nothing will ever fire the listener, so
  // falling through to the backstop would leave the window mapped for 2s.
  it("hides immediately when setFullScreen throws", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true, throwOnSetFullScreen: true });
    const result = hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));
    // The window is still inside its Space, so it must not be hidden on its own.
    assert.deepEqual(win.calls, [["app.hide"]]);
    assert.equal(result.hidApp, true);
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
    const win = makeWin({ fullScreen: true, destroyed: true });
    assert.deepEqual(hideToTray(win, mac()), idleResult);
    assert.deepEqual(win.calls, []);
    assert.deepEqual(hideToTray(null), idleResult);
    assert.deepEqual(hideToTray(undefined), idleResult);
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

  it("never throws when the app-level hide throws at the backstop", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    const result = hideToTray(
      win,
      mac({
        ...timers,
        hideAppFn: () => {
          throw new Error("app.hide failed");
        },
      }),
    );
    assert.doesNotThrow(() => timers.fire());
    assert.equal(result.stalled, true);
    assert.deepEqual(win.calls, [["setFullScreen", false]], "and no window hide() either");
  });

  // The default must follow the real platform, since window-lifecycle.js passes
  // no platform option.
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

  it("lets a show cancel a hide attached to a user-initiated exit", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: false });
    hideToTray(win, mac({ ...timers, transitionTarget: false }));
    const queuedLeave = win.listeners.get("leave-full-screen");
    assert.equal(typeof queuedLeave, "function");

    assert.equal(cancelPendingTrayHide(win), true);
    assert.equal(win.listeners.size, 0);
    queuedLeave();
    timers.scheduled.forEach((handle) => handle.fn());
    assert.deepEqual(win.calls, [], "the cancelled close must not hide after the show");
  });

  it("returns false when nothing is pending", () => {
    assert.equal(cancelPendingTrayHide(makeWin()), false);
    assert.equal(cancelPendingTrayHide(null), false);
    assert.equal(cancelPendingTrayHide(undefined), false);
  });

  // The settle delay after leave-full-screen is part of the deferral: a show
  // request landing inside it must still win, or the window the user just
  // summoned vanishes POST_LEAVE_SETTLE_MS later.
  it("disarms a hide that is waiting out the post-exit settle delay", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac(timers));
    win.emitLeaveFullScreen();
    assert.equal(timers.live().length, 1, "the settle timer is armed");

    assert.equal(cancelPendingTrayHide(win), true);
    assert.equal(timers.live().length, 0, "the settle timer must be cleared");
    timers.fireLast(); // a real timer could already be in flight
    assert.deepEqual(win.calls, [["setFullScreen", false]], "no hide after cancel");
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
    hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));
    win.emitLeaveFullScreen();
    timers.fireLast();
    assert.deepEqual(win.calls, [["setFullScreen", false], ["app.hide"]]);
    // The deferral is over, but the re-asserted hide is still pending and a show
    // has to disarm it, so the cancel reports true and nothing hides again.
    assert.equal(cancelPendingTrayHide(win), true);
    timers.live().forEach((h) => h.fn());
    assert.deepEqual(win.calls, [["setFullScreen", false], ["app.hide"]]);
    assert.equal(cancelPendingTrayHide(win), false, "a second cancel has nothing left");
  });

  it("keeps the close intent after both hide timers drain until a show clears it", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));
    assert.equal(shouldKeepAppHidden(win), true, "intent starts with the fullscreen close");

    win.emitLeaveFullScreen();
    timers.live().find((handle) => handle.ms === POST_LEAVE_SETTLE_MS).fn();
    timers.live().find((handle) => handle.ms === HIDE_REASSERT_MS).fn();
    assert.equal(
      shouldKeepAppHidden(win),
      true,
      "the four-second watchdog must still see the close after shorter timers finish",
    );

    assert.equal(cancelPendingTrayHide(win), true);
    assert.equal(shouldKeepAppHidden(win), false);
  });

  it("does not arm a second hide for a repeated close while one is pending", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: false });
    hideToTray(
      win,
      mac({ ...timers, ...makeAppHide(win), transitionTarget: false }),
    );
    const listeners = win.listeners.size;
    const scheduled = timers.scheduled.length;

    const duplicate = hideToTray(
      win,
      mac({ ...timers, ...makeAppHide(win), transitionTarget: false }),
    );
    assert.equal(duplicate.deferred, true);
    assert.equal(win.listeners.size, listeners);
    assert.equal(timers.scheduled.length, scheduled);
    assert.deepEqual(win.calls, [], "neither close may interrupt the active exit");

    win.emitLeaveFullScreen();
    timers.live().find((handle) => handle.ms === POST_LEAVE_SETTLE_MS).fn();
    assert.deepEqual(win.calls, [["app.hide"]], "the duplicate close must share one hide");
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

    win.stayFullScreen(); // the summoned-back window is still fullscreen
    hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));
    assert.deepEqual(win.calls, [["setFullScreen", false], ["setFullScreen", false]]);
    win.emitLeaveFullScreen();
    timers.fireLast();
    assert.deepEqual(win.calls, [
      ["setFullScreen", false],
      ["setFullScreen", false],
      ["app.hide"],
    ]);
  });

  // The setFullScreen-throw path settles synchronously inside hideToTray, so it
  // must not leave a cancellable entry behind.
  // The exit could not be started, so the app was hidden straight away. Nothing
  // is deferred, but the re-asserted hide still is, and a show must disarm it.
  it("still disarms the re-asserted hide when the exit could not be started", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true, throwOnSetFullScreen: true });
    hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));
    assert.deepEqual(win.calls, [["app.hide"]]);
    assert.equal(cancelPendingTrayHide(win), true);
    timers.live().forEach((h) => h.fn());
    assert.deepEqual(win.calls, [["app.hide"]], "the re-assert must not fire after a show");
    assert.equal(cancelPendingTrayHide(win), false);
  });

  // The re-assert exists because a hide issued while AppKit is still animating is
  // swallowed, leaving an orphan overlay on screen. Pin both the delay and that
  // it actually hides again.
  it("re-asserts the app hide once AppKit is certainly quiet", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    hideToTray(win, mac({ ...timers, ...makeAppHide(win) }));
    win.emitLeaveFullScreen();
    timers.scheduled.find((h) => h.ms === POST_LEAVE_SETTLE_MS).fn();
    assert.deepEqual(win.calls, [["setFullScreen", false], ["app.hide"]]);

    const reassert = timers.live().filter((h) => h.ms === HIDE_REASSERT_MS);
    assert.equal(reassert.length, 1);
    assert.equal(reassert[0].unrefed, true);
    reassert[0].fn();
    assert.deepEqual(win.calls, [["setFullScreen", false], ["app.hide"], ["app.hide"]]);
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
    assert.match(body, /hideToTray\(mainWindow, \{/);
    assert.match(body, /transitionTarget: fullScreenWatch \? fullScreenWatch\.pending\(\) : null/);
    assert.match(body, /exitSettlingFor: \(\) =>/);
    assert.doesNotMatch(
      body,
      /mainWindow\.hide\(\)/,
      "a direct hide() orphans the macOS fullscreen Space — go through hideToTray",
    );
    // The fullscreen tray hide is app-level, so a hidden app ignores a bare
    // win.show(): every user-intent show has to unhide the app first, or the
    // tray "Show" item and the summon hotkey silently do nothing.
    const showBody = WINDOW_LIFECYCLE_JS;
    assert.match(showBody, /function unhideApp\(\)/, "an app unhide helper must exist");
    const ownerShows = [
      ["showMainWindow", "function showMainWindow", "function activateMainWindow"],
      ["activateMainWindow", "function activateMainWindow", "function createTray"],
    ];
    for (const [name, startMarker, endMarker] of ownerShows) {
      const start = WINDOW_LIFECYCLE_JS.indexOf(startMarker);
      const end = WINDOW_LIFECYCLE_JS.indexOf(endMarker, start);
      assert.notEqual(start, -1, `could not locate ${name}`);
      assert.notEqual(end, -1, `could not bound ${name}`);
      const body = WINDOW_LIFECYCLE_JS.slice(start, end);
      assert.match(body, /unhideApp\(\)/, `${name} must unhide the app before showing`);
      assert.ok(
        body.indexOf("unhideApp()") < body.indexOf(".show()"),
        `${name} must unhide the app before win.show()`,
      );
    }
  });

  // A stalled fullscreen exit is invisible to every other handler in the shell
  // (fullscreen-transition-watch.js explains why), so the main window must be
  // watched from the moment it exists, and an exit stall must reach the one
  // repair that clears AppKit's abandoned overlay.
  it("watches the main window's fullscreen transitions and repairs a stalled exit", () => {
    assert.match(
      WINDOW_LIFECYCLE_JS,
      /require\("\.\/fullscreen-transition-watch"\)/,
      "window-lifecycle must own the watch",
    );
    const start = WINDOW_LIFECYCLE_JS.indexOf("function createWindow()");
    const end = WINDOW_LIFECYCLE_JS.indexOf("function showMainWindow(", start);
    assert.notEqual(start, -1, "could not locate createWindow");
    assert.notEqual(end, -1, "could not bound createWindow");
    const body = WINDOW_LIFECYCLE_JS.slice(start, end);
    assert.match(body, /watchFullScreenTransitions\(mainWindow, \{/);
    assert.match(body, /fullScreenWatch = watchFullScreenTransitions\(/, "the watch must be held");
    assert.match(body, /repairStalledFullScreenExit\(\{/);
    // Only an EXIT stall has a known overlay to clear; an enter stall is logged.
    assert.match(body, /if \(target\) return;/);
    // An abandoned transition orphans its overlay with no missing event to spot,
    // so the abort report must reach the same repair.
    assert.match(body, /onAbort: \(\{/);
    // Repairing while a close is in flight or already hidden must not re-show
    // the dismissed window. Pass a live probe because the intent can change
    // during the repair's own delayed unhide.
    assert.match(body, /keepHidden: \(\) => shouldKeepAppHidden\(mainWindow\)/);
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

  it("routes both tray show gestures through the app-unhiding helper", () => {
    // The menu item and the icon click are the same user intent; both must use
    // showMainWindow so they clear the durable close intent, call app.show(),
    // and only then surface the real window.
    const helper = WINDOW_LIFECYCLE_JS.match(
      /const showFromTray = \(\) => \{([\s\S]*?)\};/,
    );
    assert.ok(helper, "could not locate showFromTray");
    assert.match(helper[1], /showMainWindow\(\{ focus: true \}\)/);
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
    assert.match(
      IPC_REGISTRAR_JS,
      /showApp: \(\) => \{[\s\S]*?app\.show\(\)/,
      "the global summon hotkey must unhide the app before showing its target window",
    );

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
      assert.match(body, /unhideApp\(\)/, name + " must unhide the application");
      assert.ok(
        body.indexOf("cancelPendingTrayHide") < body.indexOf("unhideApp()")
          && body.indexOf("unhideApp()") < body.indexOf(".show()"),
        name + ": cancel, app.show, and win.show must stay in that order",
      );
    }
  });
});
