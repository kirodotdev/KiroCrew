const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  watchFullScreenTransitions,
  repairStalledFullScreenExit,
  TRANSITION_TIMEOUT_MS,
  REPAIR_UNHIDE_DELAY_MS,
} = require("../fullscreen-transition-watch");

// Fake BaseWindow: an event emitter whose isFullScreen() flips when the test
// says the style mask flipped. Electron fires `resize` at the START of a native
// fullscreen transition with isFullScreen() already reporting the target state;
// the terminal enter/leave event arrives ~0.5s later — or, in the bug this
// module exists for, never.
function makeWin({ fullScreen = false, visible = true } = {}) {
  const listeners = new Map();
  let destroyed = false;
  const win = {
    calls: [],
    isDestroyed: () => destroyed,
    isFullScreen: () => fullScreen,
    isVisible: () => visible,
    show: () => win.calls.push("show"),
    on: (ev, fn) => {
      if (!listeners.has(ev)) listeners.set(ev, new Set());
      listeners.get(ev).add(fn);
    },
    off: (ev, fn) => {
      listeners.get(ev)?.delete(fn);
    },
    emit: (ev) => {
      for (const fn of [...(listeners.get(ev) || [])]) fn();
    },
    listenerCount: (ev) => (listeners.get(ev) || new Set()).size,
    // Test-only controls.
    setFlag: (v) => {
      fullScreen = v;
    },
    setVisible: (v) => {
      visible = v;
    },
    destroy: () => {
      destroyed = true;
    },
    // What AppKit does when a transition starts: flip the flag and resize.
    beginTransition(target) {
      fullScreen = target;
      win.emit("resize");
    },
    finishTransition(target) {
      win.emit(target ? "enter-full-screen" : "leave-full-screen");
    },
  };
  return win;
}

function makeTimers() {
  const scheduled = [];
  let clock = 1000;
  return {
    scheduled,
    now: () => clock,
    advance: (ms) => {
      clock += ms;
    },
    setTimeoutFn: (fn, ms) => {
      const h = { fn, ms, cleared: false, unrefed: false, unref() { this.unrefed = true; return this; } };
      scheduled.push(h);
      return h;
    },
    clearTimeoutFn: (h) => {
      if (h) h.cleared = true;
    },
    live: () => scheduled.filter((h) => !h.cleared),
    fireLive: () => {
      for (const h of scheduled.filter((x) => !x.cleared)) {
        h.cleared = true;
        h.fn();
      }
    },
  };
}

const mac = (extra = {}) => ({ isMac: true, ...extra });

describe("watchFullScreenTransitions", () => {
  // The healthy path must be silent: a transition that completes is not a
  // stall, whichever direction it goes and whoever started it.
  it("stays silent when the terminal event follows the resize", () => {
    const timers = makeTimers();
    const stalls = [];
    const win = makeWin();
    const watch = watchFullScreenTransitions(win, mac({ ...timers, onStall: (i) => stalls.push(i) }));

    win.beginTransition(true);
    assert.equal(watch.pending(), true, "an enter is in flight");
    assert.equal(timers.live().length, 1);
    assert.equal(timers.live()[0].ms, TRANSITION_TIMEOUT_MS);
    win.finishTransition(true);
    assert.equal(watch.pending(), null);
    assert.equal(timers.live().length, 0, "the timer is disarmed by the event");

    win.beginTransition(false);
    win.finishTransition(false);
    timers.fireLive();
    assert.deepEqual(stalls, []);
  });

  it("distinguishes an in-flight exit from its post-terminal tail", () => {
    const timers = makeTimers();
    const win = makeWin({ fullScreen: true });
    const watch = watchFullScreenTransitions(win, mac(timers));

    assert.equal(watch.exitSettlingFor(), Infinity, "no exit has completed yet");
    win.beginTransition(false);
    assert.equal(watch.pending(), false);
    assert.equal(
      watch.exitSettlingFor(),
      Infinity,
      "the pending target, not the tail probe, owns the active exit",
    );

    timers.advance(125);
    win.finishTransition(false);
    assert.equal(watch.pending(), null);
    assert.equal(watch.exitSettlingFor(), 0);
    timers.advance(300);
    assert.equal(watch.exitSettlingFor(), 300);
  });

  it("reports each newly armed transition target exactly once", () => {
    const timers = makeTimers();
    const arms = [];
    const win = makeWin();
    watchFullScreenTransitions(win, mac({ ...timers, onArm: (info) => arms.push(info) }));

    win.emit("resize"); // ordinary resize
    win.beginTransition(true);
    win.emit("resize"); // repeated resize while the same transition is in flight
    win.finishTransition(true);
    win.beginTransition(false);

    assert.deepEqual(arms, [{ target: true }, { target: false }]);
  });

  // The signature of the reported bug: the style mask flipped to windowed (the
  // resize saw isFullScreen() === false) but windowDidExitFullScreen never came.
  it("reports an exit that never completes, with the state it left behind", () => {
    const timers = makeTimers();
    const stalls = [];
    const win = makeWin({ fullScreen: true });
    watchFullScreenTransitions(win, mac({ ...timers, onStall: (i) => stalls.push(i) }));

    win.beginTransition(false);
    timers.advance(TRANSITION_TIMEOUT_MS);
    timers.fireLive();

    assert.equal(stalls.length, 1);
    assert.deepEqual(stalls[0], {
      target: false,
      fullScreen: false,
      visible: true,
      elapsedMs: TRANSITION_TIMEOUT_MS,
    });
  });

  it("reports an enter that never completes as an enter", () => {
    const timers = makeTimers();
    const stalls = [];
    const win = makeWin({ fullScreen: false });
    watchFullScreenTransitions(win, mac({ ...timers, onStall: (i) => stalls.push(i) }));
    win.beginTransition(true);
    timers.fireLive();
    assert.equal(stalls.length, 1);
    assert.equal(stalls[0].target, true);
    assert.equal(stalls[0].fullScreen, true);
  });

  // Ordinary resizes never flip the flag and must never arm anything: a
  // watchdog that fired on window drags would repair windows that are fine.
  it("ignores resizes that do not change the fullscreen flag", () => {
    const timers = makeTimers();
    const win = makeWin();
    const watch = watchFullScreenTransitions(win, mac(timers));
    win.emit("resize");
    win.emit("resize");
    assert.equal(watch.pending(), null);
    assert.equal(timers.scheduled.length, 0);
  });

  // The user can reverse a transition before it completes (green control twice).
  // A flip back to the confirmed state means nothing is in flight; a flip to a
  // new target re-arms with one timer.
  it("reports the abandoned transition when one reverses to the baseline", () => {
    const timers = makeTimers();
    const stalls = [];
    const aborts = [];
    const win = makeWin();
    const watch = watchFullScreenTransitions(
      win,
      mac({ ...timers, onStall: (i) => stalls.push(i), onAbort: (i) => aborts.push(i) }),
    );
    win.beginTransition(true);
    win.beginTransition(false); // back to the confirmed windowed state
    assert.equal(watch.pending(), null);
    assert.equal(timers.live().length, 0, "the abandoned enter's timer is cleared");
    // The abandoned enter's overlay is orphaned on screen, and the replacement
    // transition will deliver its terminal event normally, so this report is the
    // only signal anything went wrong.
    assert.equal(aborts.length, 1);
    assert.equal(aborts[0].target, true, "the ENTER was the transition abandoned");

    win.finishTransition(true); // baseline is now fullscreen
    win.beginTransition(false);
    win.beginTransition(false); // a repeated resize in flight keeps the one timer
    assert.equal(watch.pending(), false);
    assert.equal(timers.live().length, 1);
    win.finishTransition(false);
    timers.fireLive();
    assert.deepEqual(stalls, []);
    assert.equal(aborts.length, 1, "a completed transition is not an abort");
  });

  // The abort report is for an ABANDONED transition only. A resize that does not
  // change the flag while nothing is in flight is ordinary window resizing and
  // must stay silent, or every drag of the window edge would trigger a repair
  // that hides and unhides the whole application.
  it("does not report an abort for a resize with no transition in flight", () => {
    const timers = makeTimers();
    const aborts = [];
    const win = makeWin();
    watchFullScreenTransitions(win, mac({ ...timers, onAbort: (i) => aborts.push(i) }));
    win.emit("resize");
    win.emit("resize");
    assert.deepEqual(aborts, []);
  });

  // A stall must be reported once. The stale timer of a superseded arm must not
  // fire a second report for a transition the window is no longer in.
  it("reports each stall once and re-baselines to the observed state", () => {
    const timers = makeTimers();
    const stalls = [];
    const win = makeWin({ fullScreen: true });
    const watch = watchFullScreenTransitions(win, mac({ ...timers, onStall: (i) => stalls.push(i) }));
    win.beginTransition(false);
    timers.fireLive();
    assert.equal(stalls.length, 1);
    assert.equal(watch.pending(), null);
    // The window is windowed now; a windowed resize is not a new transition.
    win.emit("resize");
    assert.equal(watch.pending(), null);
    // But a genuine new transition is watched again.
    win.beginTransition(true);
    assert.equal(watch.pending(), true);
  });

  // A terminal event without a preceding resize (constructor `fullscreen: true`,
  // a transition that started before the watch attached) must simply update the
  // baseline, not be mistaken for a stall or leave a timer behind.
  it("accepts a terminal event with no armed transition", () => {
    const timers = makeTimers();
    const win = makeWin();
    const watch = watchFullScreenTransitions(win, mac(timers));
    win.setFlag(true);
    win.finishTransition(true);
    assert.equal(watch.pending(), null);
    assert.equal(timers.scheduled.length, 0);
    // Baseline is now fullscreen: a resize while still fullscreen is not a transition.
    win.emit("resize");
    assert.equal(watch.pending(), null);
  });

  // A destroyed window has no transition to report; a late timer must not
  // touch it (Electron throws on a destroyed window).
  it("does not report a stall for a window destroyed in flight", () => {
    const timers = makeTimers();
    const stalls = [];
    const win = makeWin({ fullScreen: true });
    watchFullScreenTransitions(win, mac({ ...timers, onStall: (i) => stalls.push(i) }));
    win.beginTransition(false);
    win.destroy();
    timers.fireLive();
    assert.deepEqual(stalls, []);
  });

  it("disposes on closed and on dispose(), removing every listener", () => {
    const timers = makeTimers();
    const win = makeWin();
    const watch = watchFullScreenTransitions(win, mac(timers));
    for (const ev of ["resize", "enter-full-screen", "leave-full-screen", "closed"]) {
      assert.equal(win.listenerCount(ev), 1, ev);
    }
    win.beginTransition(true);
    win.emit("closed");
    for (const ev of ["resize", "enter-full-screen", "leave-full-screen", "closed"]) {
      assert.equal(win.listenerCount(ev), 0, ev + " must be removed");
    }
    assert.equal(timers.live().length, 0, "the in-flight timer is cleared");
    assert.equal(watch.pending(), null);
    assert.doesNotThrow(() => watch.dispose());
  });

  // Timers must never be the reason the process lingers on quit.
  it("unrefs its timer", () => {
    const timers = makeTimers();
    const win = makeWin();
    watchFullScreenTransitions(win, mac(timers));
    win.beginTransition(true);
    assert.equal(timers.scheduled[0].unrefed, true);
  });

  // Windows/Linux fullscreen has no AppKit transition and no overlay; the watch
  // must attach nothing there so a resize never arms a repair.
  // `quietFor()` is what the close path gates its exit on, because AppKit's own
  // completion event fires while it is still working. Every fullscreen-related
  // event must therefore refresh it.
  it("reports how long the window has been still", () => {
    const timers = makeTimers();
    let clock = 1000;
    const win = makeWin();
    const watch = watchFullScreenTransitions(
      win,
      mac({ ...timers, now: () => clock }),
    );
    assert.equal(watch.quietFor(), 0);
    clock += 500;
    assert.equal(watch.quietFor(), 500);

    win.beginTransition(true); // a resize refreshes it
    assert.equal(watch.quietFor(), 0);
    clock += 300;
    win.finishTransition(true); // so does the terminal event
    assert.equal(watch.quietFor(), 0);
    clock += 900;
    assert.equal(watch.quietFor(), 900);
  });

  it("is a no-op off macOS", () => {
    const timers = makeTimers();
    const win = makeWin();
    const watch = watchFullScreenTransitions(win, { isMac: false, ...timers });
    win.beginTransition(true);
    assert.equal(win.listenerCount("resize"), 0);
    assert.equal(timers.scheduled.length, 0);
    assert.equal(watch.pending(), null);
    assert.equal(watch.exitSettlingFor(), Infinity);
  });

  it("is a no-op for a missing, destroyed or non-emitter window", () => {
    assert.doesNotThrow(() => watchFullScreenTransitions(null, mac()).dispose());
    const dead = makeWin();
    dead.destroy();
    assert.equal(watchFullScreenTransitions(dead, mac()).pending(), null);
    assert.equal(watchFullScreenTransitions({ isDestroyed: () => false }, mac()).pending(), null);
  });
});

describe("repairStalledFullScreenExit", () => {
  function makeApp() {
    const calls = [];
    return { calls, hide: () => calls.push("app.hide"), show: () => calls.push("app.show") };
  }

  // The verified repair: hiding the APP orders out AppKit's abandoned overlay as
  // well; unhiding brings back only real windows. The unhide is delayed because
  // AppKit tears the hidden overlay down asynchronously.
  it("cycles app.hide/app.show and re-shows a window that was visible", () => {
    const timers = makeTimers();
    const app = makeApp();
    const win = makeWin({ visible: true });
    const result = repairStalledFullScreenExit({ app, win, isMac: true, setTimeoutFn: timers.setTimeoutFn });

    assert.deepEqual(app.calls, ["app.hide"], "unhide must not follow on the same tick");
    assert.deepEqual(result, { hidden: true, unhideScheduled: true });
    assert.equal(timers.scheduled[0].ms, REPAIR_UNHIDE_DELAY_MS);
    assert.equal(timers.scheduled[0].unrefed, true);

    timers.fireLive();
    assert.deepEqual(app.calls, ["app.hide", "app.show"]);
    assert.deepEqual(win.calls, ["show"]);
  });

  // A window the user dismissed (close-to-tray) must stay dismissed: the point
  // of the repair there is only to take the overlay down with it. The next Dock
  // click unhides the app and finds the real window alone.
  it("stops after app.hide when the window was already hidden", () => {
    const timers = makeTimers();
    const app = makeApp();
    const win = makeWin({ visible: false });
    const result = repairStalledFullScreenExit({ app, win, isMac: true, setTimeoutFn: timers.setTimeoutFn });
    assert.deepEqual(app.calls, ["app.hide"]);
    assert.deepEqual(result, { hidden: true, unhideScheduled: false });
    assert.equal(timers.scheduled.length, 0);
  });

  // A close-to-tray hide is DEFERRED behind the fullscreen exit, so the window is
  // still visible when the repair runs on that path. Visibility alone therefore
  // cannot tell "the user is toggling fullscreen" from "the user is closing the
  // window", and unhiding on the close path would re-surface the window the user
  // just dismissed. `keepHidden` carries that intent in.
  it("keeps the app hidden when a close gesture is still in flight", () => {
    const timers = makeTimers();
    const app = makeApp();
    const win = makeWin({ visible: true });
    const result = repairStalledFullScreenExit({
      app,
      win,
      isMac: true,
      keepHidden: true,
      setTimeoutFn: timers.setTimeoutFn,
    });
    assert.deepEqual(app.calls, ["app.hide"], "the overlay goes, the window stays dismissed");
    assert.deepEqual(result, { hidden: true, unhideScheduled: false });
    assert.deepEqual(win.calls, []);
    assert.equal(timers.scheduled.length, 0);
  });

  it("re-checks a dynamic close intent before the delayed unhide", () => {
    const timers = makeTimers();
    const app = makeApp();
    const win = makeWin({ visible: true });
    let keepHidden = false;
    const result = repairStalledFullScreenExit({
      app,
      win,
      isMac: true,
      keepHidden: () => keepHidden,
      setTimeoutFn: timers.setTimeoutFn,
    });
    assert.equal(result.unhideScheduled, true);
    keepHidden = true; // a close gesture arrived during the 350ms repair delay
    timers.fireLive();
    assert.deepEqual(app.calls, ["app.hide"]);
    assert.deepEqual(win.calls, [], "the delayed repair must not fight the later close");
  });

  it("fails closed when the dynamic close-intent probe throws", () => {
    const timers = makeTimers();
    const app = makeApp();
    const win = makeWin({ visible: true });
    const result = repairStalledFullScreenExit({
      app,
      win,
      isMac: true,
      keepHidden: () => {
        throw new Error("intent unavailable");
      },
      setTimeoutFn: timers.setTimeoutFn,
    });
    assert.deepEqual(app.calls, ["app.hide"]);
    assert.equal(result.unhideScheduled, false);
  });

  it("does not unhide a window destroyed during the delay", () => {
    const timers = makeTimers();
    const app = makeApp();
    const win = makeWin({ visible: true });
    repairStalledFullScreenExit({ app, win, isMac: true, setTimeoutFn: timers.setTimeoutFn });
    win.destroy();
    timers.fireLive();
    assert.deepEqual(app.calls, ["app.hide"]);
    assert.deepEqual(win.calls, []);
  });

  it("is a no-op off macOS and without an app", () => {
    const app = makeApp();
    const idle = { hidden: false, unhideScheduled: false };
    assert.deepEqual(repairStalledFullScreenExit({ app, win: makeWin(), isMac: false }), idle);
    assert.deepEqual(app.calls, []);
    assert.deepEqual(repairStalledFullScreenExit({ app: null, win: makeWin(), isMac: true }), idle);
    assert.deepEqual(repairStalledFullScreenExit({ app, win: null, isMac: true }), idle);
  });

  it("never throws when app.hide throws", () => {
    const app = {
      hide: () => {
        throw new Error("hide failed");
      },
      show: () => {},
    };
    const result = repairStalledFullScreenExit({ app, win: makeWin(), isMac: true });
    assert.deepEqual(result, { hidden: false, unhideScheduled: false });
  });
});
