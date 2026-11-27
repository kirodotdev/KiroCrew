"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");

const { focusHandBackMessage } = require("../browser-view");
const { attachBrowserPanels } = require("../runtime/window/browser-panels");

// One panel in a stub window, its embedded page off screen and holding focus.
function hiddenFocusedPanel(launchLog) {
  const page = { focused: false, handlers: {} };
  const dashboardFocus = [];
  const win = {
    isDestroyed: () => false,
    isFocused: () => true,
    getContentBounds: () => ({ width: 1280, height: 860 }),
    contentView: { addChildView() {}, removeChildView() {} },
  };
  const view = {
    webContents: { isDestroyed: () => false, focus: () => dashboardFocus.push(1), send() {} },
  };
  class WebContentsView {
    constructor() {
      this.webContents = {
        loadURL() {},
        isFocused: () => page.focused,
        on: (name, fn) => { page.handlers[name] = fn; },
      };
    }
    setBounds() {}
    setVisible() {}
  }
  attachBrowserPanels(win, view, { WebContentsView, shell: {}, partition: "persist:test", log: launchLog });
  const { manager } = win._mcBrowserPanel("p1");
  manager.open("http://127.0.0.1:5173/");
  page.focused = true;
  return { manager, pageLoaded: () => page.handlers["did-stop-loading"](), dashboardFocus };
}

describe("browser panel focus hand-back line", () => {
  it("goes to the launch log at most once a minute per trigger, and to the console every time", (t) => {
    t.mock.timers.enable({ apis: ["Date"], now: 0 });
    const warn = t.mock.method(console, "warn", () => {});
    const launched = [];
    const { manager, pageLoaded, dashboardFocus } = hiddenFocusedPanel((line) => launched.push(line));

    // A hidden page that keeps taking focus back, then the panel's tab goes inactive.
    pageLoaded();
    pageLoaded();
    pageLoaded();
    manager.setInactive(true);
    assert.deepEqual(launched, [focusHandBackMessage("hidden-view-load"), focusHandBackMessage("hide")]);
    assert.equal(warn.mock.callCount(), 4, "the console line is unchanged: one per hand-back");
    assert.equal(dashboardFocus.length, 4, "every hand-back still runs");

    t.mock.timers.tick(59_999);
    pageLoaded();
    assert.equal(launched.length, 2);
    t.mock.timers.tick(1);
    pageLoaded();
    assert.deepEqual(launched.slice(2), [focusHandBackMessage("hidden-view-load")]);
  });

  it("still reaches the console, and the hand-back still runs, when the launch log throws", (t) => {
    const warn = t.mock.method(console, "warn", () => {});
    const { pageLoaded, dashboardFocus } = hiddenFocusedPanel(() => { throw new Error("log gone"); });
    pageLoaded();
    assert.equal(warn.mock.callCount(), 1);
    assert.equal(dashboardFocus.length, 1);
  });
});
