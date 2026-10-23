const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { armSwipeHistory, browserPanelUnderCursor, historyStepForSwipe, HISTORY_STEP_CHANNEL } = require("../swipe-history");

// With Logitech's Options software installed, macOS hands a mouse's Back and
// Forward buttons to apps as swipe events. Electron performs no navigation for
// them and only emits the window's `swipe` event, so the shell relays each one
// to the dashboard as a single history step.

/** The slice of a BaseWindow and its dashboard webContents the relay touches. */
function makeFakeWindow() {
  const handlers = {};
  const sent = [];
  const win = {
    on(event, fn) {
      handlers[event] = fn;
    },
  };
  const wc = {
    send(channel, ...args) {
      sent.push([channel, ...args]);
    },
  };
  const swipe = (direction) => handlers.swipe({}, direction);
  return { win, wc, sent, swipe, handlers };
}

describe("historyStepForSwipe", () => {
  it("maps left to Back and right to Forward", () => {
    assert.equal(historyStepForSwipe("left"), -1);
    assert.equal(historyStepForSwipe("right"), 1);
  });

  it("gives no step for vertical swipes or anything else", () => {
    for (const direction of ["up", "down", "", undefined, null, 1, "LEFT", "toString", "__proto__"]) {
      assert.equal(historyStepForSwipe(direction), 0, `direction ${String(direction)}`);
    }
  });
});

describe("armSwipeHistory", () => {
  it("relays a left swipe as Back and a right swipe as Forward", () => {
    const fake = makeFakeWindow();
    armSwipeHistory(fake.win, fake.wc);
    fake.swipe("left");
    fake.swipe("right");
    assert.deepEqual(fake.sent, [
      [HISTORY_STEP_CHANNEL, -1],
      [HISTORY_STEP_CHANNEL, 1],
    ]);
  });

  it("sends nothing for a vertical swipe", () => {
    const fake = makeFakeWindow();
    armSwipeHistory(fake.win, fake.wc);
    fake.swipe("up");
    fake.swipe("down");
    assert.deepEqual(fake.sent, []);
  });

  it("sends nothing once the window is torn down", () => {
    const fake = makeFakeWindow();
    armSwipeHistory(fake.win, fake.wc, { isAlive: () => false });
    fake.swipe("left");
    assert.deepEqual(fake.sent, []);
  });

  it("tolerates a webContents that throws mid-teardown", () => {
    const fake = makeFakeWindow();
    fake.wc.send = () => {
      throw new Error("Object has been destroyed");
    };
    armSwipeHistory(fake.win, fake.wc);
    assert.doesNotThrow(() => fake.swipe("left"));
  });

  it("steps the browser panel under the cursor instead of the dashboard", () => {
    const fake = makeFakeWindow();
    const page = makeFakePage({ canGoBack: true, canGoForward: true });
    armSwipeHistory(fake.win, fake.wc, { panelUnderCursor: () => page.wc });
    fake.swipe("left");
    fake.swipe("right");
    assert.deepEqual(page.steps, ["back", "forward"]);
    assert.deepEqual(fake.sent, [], "the dashboard must not move for a press over the panel");
  });

  it("does nothing over a browser panel with nowhere to go", () => {
    const fake = makeFakeWindow();
    const page = makeFakePage({ canGoBack: false, canGoForward: false });
    armSwipeHistory(fake.win, fake.wc, { panelUnderCursor: () => page.wc });
    fake.swipe("left");
    fake.swipe("right");
    assert.deepEqual(page.steps, []);
    assert.deepEqual(fake.sent, []);
  });
});

/** A browser panel's page: a webContents with its own navigation history. */
function makeFakePage({ canGoBack, canGoForward, destroyed = false }) {
  const steps = [];
  const wc = {
    isDestroyed: () => destroyed,
    navigationHistory: {
      canGoBack: () => canGoBack,
      canGoForward: () => canGoForward,
      goBack: () => steps.push("back"),
      goForward: () => steps.push("forward"),
    },
  };
  return { wc, steps };
}

/** A window whose content area starts at (100, 50) on screen, holding the
 *  given panels; each panel is `{ visible, bounds, wc }`. */
function makePanelWindow(panels) {
  const entries = new Map(
    panels.map((p, i) => [
      `panel-${i}`,
      { manager: { getState: () => ({ visible: p.visible, bounds: p.bounds }), getWebContents: () => p.wc } },
    ]),
  );
  return { _mcBrowserPanels: entries, getContentBounds: () => ({ x: 100, y: 50, width: 1200, height: 800 }) };
}

describe("browserPanelUnderCursor", () => {
  const bounds = { x: 800, y: 40, width: 400, height: 700 };
  const page = makeFakePage({ canGoBack: true, canGoForward: true }).wc;

  it("finds the visible panel under the cursor, in content-area coordinates", () => {
    const win = makePanelWindow([{ visible: true, bounds, wc: page }]);
    // Screen (900, 90) is content (800, 40): the panel's top-left corner.
    assert.equal(browserPanelUnderCursor(win, { x: 900, y: 90 }), page);
    assert.equal(browserPanelUnderCursor(win, { x: 1299, y: 789 }), page);
  });

  it("answers null outside every panel, at the far edges included", () => {
    const win = makePanelWindow([{ visible: true, bounds, wc: page }]);
    assert.equal(browserPanelUnderCursor(win, { x: 899, y: 90 }), null);
    assert.equal(browserPanelUnderCursor(win, { x: 1300, y: 90 }), null);
    assert.equal(browserPanelUnderCursor(win, { x: 900, y: 790 }), null);
    // Screen coordinates inside the panel's rect, read without the content
    // offset, are not inside it.
    assert.equal(browserPanelUnderCursor(win, { x: 850, y: 60 }), null);
  });

  it("skips a hidden panel and a destroyed page", () => {
    const destroyed = makeFakePage({ canGoBack: true, canGoForward: true, destroyed: true }).wc;
    assert.equal(browserPanelUnderCursor(makePanelWindow([{ visible: false, bounds, wc: page }]), { x: 1000, y: 300 }), null);
    assert.equal(browserPanelUnderCursor(makePanelWindow([{ visible: true, bounds, wc: destroyed }]), { x: 1000, y: 300 }), null);
    assert.equal(browserPanelUnderCursor(makePanelWindow([{ visible: true, bounds, wc: null }]), { x: 1000, y: 300 }), null);
  });

  it("answers null for a window with no panels or no cursor", () => {
    assert.equal(browserPanelUnderCursor({ getContentBounds: () => ({ x: 0, y: 0 }) }, { x: 1, y: 1 }), null);
    assert.equal(browserPanelUnderCursor(makePanelWindow([{ visible: true, bounds, wc: page }]), null), null);
  });
});

describe("swipe relay wiring", () => {
  const read = (rel) => fs.readFileSync(path.join(__dirname, "..", rel), "utf8");

  it("every dashboard window arms the relay on its dashboard webContents", () => {
    const src = read("window-lifecycle.js");
    assert.match(src, /require\("\.\/swipe-history"\)/);
    assert.match(src, /armSwipeHistory\(win, view\.webContents,/);
    // A swipe has no position: the cursor decides between a panel and the dashboard.
    assert.match(src, /panelUnderCursor: \(\) => browserPanelUnderCursor\(win, screen\.getCursorScreenPoint\(\)\)/);
  });

  it("the preload listens on the channel the relay sends", () => {
    const src = read("preload.js");
    assert.ok(
      src.includes(`ipcRenderer.on("${HISTORY_STEP_CHANNEL}"`),
      `preload.js must listen on ${HISTORY_STEP_CHANNEL}, or every step is a silent no-op`,
    );
  });
});
