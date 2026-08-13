const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { exitImmersiveModes } = require("../blocking-prompt");

// Fake BrowserWindow that records setter calls and can report any subset of the
// immersive modes as currently on. Only the members exitImmersiveModes touches
// are implemented.
function makeWin({ fullScreen = false, simpleFullScreen = false, kiosk = false, destroyed = false } = {}) {
  const calls = [];
  return {
    calls,
    isDestroyed: () => destroyed,
    isFullScreen: () => fullScreen,
    isSimpleFullScreen: () => simpleFullScreen,
    isKiosk: () => kiosk,
    setFullScreen: (v) => calls.push(["setFullScreen", v]),
    setSimpleFullScreen: (v) => calls.push(["setSimpleFullScreen", v]),
    setKiosk: (v) => calls.push(["setKiosk", v]),
  };
}

describe("exitImmersiveModes", () => {
  // Regression guard for the un-closable token screen: token-prompt.html loads
  // into the fullscreen MAIN window, where the traffic lights are hidden and the
  // page has no exit. Showing it MUST first drop fullscreen so the window is
  // dismissable again. Before the fix main.js loaded the page without this, so
  // the only escape was force-killing the process.
  it("drops native fullscreen when the window is fullscreen", () => {
    const win = makeWin({ fullScreen: true });
    const changed = exitImmersiveModes(win);
    assert.deepEqual(win.calls, [["setFullScreen", false]]);
    assert.equal(changed.fullScreen, true);
  });

  it("also clears simple-fullscreen and kiosk (they hide chrome too)", () => {
    const win = makeWin({ simpleFullScreen: true, kiosk: true });
    const changed = exitImmersiveModes(win);
    assert.deepEqual(win.calls, [["setSimpleFullScreen", false], ["setKiosk", false]]);
    assert.equal(changed.fullScreen, true);
    assert.equal(changed.kiosk, true);
  });

  // Non-destructive: a windowed (already-dismissable) prompt must not be poked,
  // so we never disturb a normal window's state on the common path.
  it("is a no-op when no immersive mode is active", () => {
    const win = makeWin({});
    const changed = exitImmersiveModes(win);
    assert.deepEqual(win.calls, []);
    assert.deepEqual(changed, { fullScreen: false, kiosk: false });
  });

  it("does nothing to a destroyed window", () => {
    const win = makeWin({ fullScreen: true, destroyed: true });
    const changed = exitImmersiveModes(win);
    assert.deepEqual(win.calls, []);
    assert.deepEqual(changed, { fullScreen: false, kiosk: false });
  });

  it("tolerates a window missing the immersive-mode API", () => {
    assert.deepEqual(exitImmersiveModes(null), { fullScreen: false, kiosk: false });
    assert.deepEqual(exitImmersiveModes({}), { fullScreen: false, kiosk: false });
  });
});
