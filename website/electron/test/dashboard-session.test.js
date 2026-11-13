"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { openDashboardSession } = require("../dashboard-session");
const { hideToTray, shouldKeepAppHidden } = require("../hide-to-tray");

const BASE = "http://127.0.0.1:7777";

// A fullscreen dashboard window with the members hideToTray and
// openDashboardSession touch; `calls` records them in order.
function makeDashboardWindow() {
  const calls = [];
  const listeners = new Map();
  let fullScreen = true;
  const wc = {
    isDestroyed: () => false,
    getURL: () => `${BASE}/chat?token=t`,
    send: (channel, route) => calls.push(["send", channel, route]),
  };
  return {
    calls,
    _mcView: { webContents: wc },
    isDestroyed: () => false,
    isFullScreen: () => fullScreen,
    setFullScreen: (v) => {
      calls.push(["setFullScreen", v]);
      fullScreen = v;
    },
    once: (event, fn) => listeners.set(event, fn),
    off: (event, fn) => {
      if (listeners.get(event) === fn) listeners.delete(event);
    },
    hide: () => calls.push(["hide"]),
    isMinimized: () => false,
    restore: () => calls.push(["restore"]),
    show: () => calls.push(["show"]),
    focus: () => calls.push(["focus"]),
    leaveFullScreen: () => {
      const fn = listeners.get("leave-full-screen");
      if (fn) fn();
    },
  };
}

function makeTimers() {
  const scheduled = [];
  return {
    setTimeoutFn: (fn) => {
      const handle = { fn, cleared: false, unref() { return this; } };
      scheduled.push(handle);
      return handle;
    },
    clearTimeoutFn: (handle) => {
      if (handle) handle.cleared = true;
    },
    // Fire every timer still armed, as the event loop would once they are due.
    fireLive: () => {
      for (const handle of scheduled.slice()) {
        if (!handle.cleared) {
          handle.cleared = true;
          handle.fn();
        }
      }
    },
  };
}

describe("openDashboardSession", () => {
  it("wins over a pending close-to-tray, so the opened session stays on screen", () => {
    const win = makeDashboardWindow();
    const timers = makeTimers();
    hideToTray(win, {
      isMac: true,
      ...timers,
      hideAppFn: () => win.calls.push(["app.hide"]),
    });
    const order = [];

    const opened = openDashboardSession(win, BASE, "slot-1", {
      showApp: () => order.push("showApp"),
      focusApp: () => order.push("focusApp"),
    });
    win.leaveFullScreen();
    timers.fireLive();

    assert.equal(opened, true);
    assert.equal(shouldKeepAppHidden(win), false, "the show must clear the hide intent");
    assert.ok(!win.calls.some(([call]) => call === "app.hide" || call === "hide"),
      "the app was hidden again after the user opened the session");
    assert.deepEqual(order, ["showApp", "focusApp"]);
    assert.deepEqual(win.calls.at(-1), ["send", "navigate", "/chat?sid=slot-1"]);
  });

  it("runs no surface hook when it refuses a session the window cannot show", () => {
    const win = makeDashboardWindow();
    win._mcView.webContents.getURL = () => "file:///loading.html";
    const order = [];

    const opened = openDashboardSession(win, BASE, "slot-1", {
      showApp: () => order.push("showApp"),
      focusApp: () => order.push("focusApp"),
    });

    assert.equal(opened, false);
    assert.deepEqual(order, []);
    assert.deepEqual(win.calls, []);
  });
});
