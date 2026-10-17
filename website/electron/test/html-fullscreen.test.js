const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { attachHtmlFullScreen } = require("../html-fullscreen");

/**
 * Window stub recording every setFullScreen call.
 *
 * `setFullScreen` is deliberately ASYNC-like: it records the call and flips the
 * reported state, but does NOT fire `leave-full-screen`. Tests fire that
 * separately via `_transitionDone()`, which is what macOS does after its exit
 * animation — the gap this bridge has to survive.
 */
function winStub({ fullScreen = false, destroyed = false } = {}) {
  const calls = [];
  const listeners = new Map();
  return {
    calls,
    isDestroyed: () => destroyed,
    isFullScreen: () => fullScreen,
    setFullScreen: (v) => { calls.push(v); fullScreen = v; },
    on: (event, fn) => { listeners.set(event, fn); },
    // test-only: the native transition finished
    _transitionDone: () => listeners.get("leave-full-screen")?.(),
    // test-only: the user hit the native fullscreen control
    _userSetsFullScreen: (v) => { fullScreen = v; },
    _hasWinListener: (event) => listeners.has(event),
  };
}

/** WebContents stub that lets a test fire the two DOM-fullscreen events. */
function wcStub() {
  const listeners = new Map();
  return {
    on: (event, fn) => { listeners.set(event, fn); },
    enter: () => listeners.get("enter-html-full-screen")?.(),
    leave: () => listeners.get("leave-html-full-screen")?.(),
    has: (event) => listeners.has(event),
  };
}

describe("attachHtmlFullScreen — DOM fullscreen drives the window", () => {
  it("subscribes to both DOM-fullscreen events and the window transition", () => {
    const wc = wcStub();
    const win = winStub();
    attachHtmlFullScreen({ win, webContents: wc });
    assert.equal(wc.has("enter-html-full-screen"), true);
    assert.equal(wc.has("leave-html-full-screen"), true);
    assert.equal(win._hasWinListener("leave-full-screen"), true);
  });

  it("raises the window when a page element goes fullscreen", () => {
    // THE BUG: nothing listened to these events, so the <video> became
    // :fullscreen inside a WebContentsView clamped to the un-fullscreened
    // window's content rect — the fullscreen button looked dead.
    const win = winStub({ fullScreen: false });
    const wc = wcStub();
    attachHtmlFullScreen({ win, webContents: wc });
    wc.enter();
    assert.deepEqual(win.calls, [true]);
  });

  it("lowers the window again on exit", () => {
    const win = winStub({ fullScreen: false });
    const wc = wcStub();
    attachHtmlFullScreen({ win, webContents: wc });
    wc.enter();
    wc.leave();
    assert.deepEqual(win.calls, [true, false]);
  });

  it("KEEPS ownership across the async exit transition", () => {
    // setFullScreen(false) is asynchronous on macOS. Clearing the claim inline
    // would leave a gap where isFullScreen() is still true but the bridge no
    // longer owns it, and a persist landing in that gap writes the transient
    // fullscreen to disk — exactly what the flag exists to prevent.
    const win = winStub({ fullScreen: false });
    const wc = wcStub();
    const bridge = attachHtmlFullScreen({ win, webContents: wc });
    wc.enter();
    assert.equal(bridge.raisedWindow(), true);
    wc.leave();
    assert.equal(bridge.raisedWindow(), true, "still owned until the transition lands");
    win._transitionDone();
    assert.equal(bridge.raisedWindow(), false, "ownership ends with the transition");
  });

  it("does NOT un-fullscreen a window the USER had already fullscreened", () => {
    // Watching a video must not silently drop the user out of fullscreen when it
    // ends. The bridge only lowers what it raised.
    const win = winStub({ fullScreen: true });
    const wc = wcStub();
    const bridge = attachHtmlFullScreen({ win, webContents: wc });
    wc.enter();
    assert.deepEqual(win.calls, [], "already fullscreen — nothing to raise");
    assert.equal(bridge.raisedWindow(), false);
    wc.leave();
    assert.deepEqual(win.calls, [], "must not lower a user-raised fullscreen");
  });

  it("releases ownership when the USER exits a bridge-raised fullscreen", () => {
    // User hits the native control mid-playback: ownership ends there, so the
    // later leave-html-full-screen must not try to lower an already-normal window.
    const win = winStub({ fullScreen: false });
    const wc = wcStub();
    const bridge = attachHtmlFullScreen({ win, webContents: wc });
    wc.enter();
    win._userSetsFullScreen(false);
    win._transitionDone();
    assert.equal(bridge.raisedWindow(), false);
    win.calls.length = 0;
    wc.leave();
    assert.deepEqual(win.calls, [], "nothing to lower — the user already did");
  });

  it("re-decides ownership on a second fullscreen cycle", () => {
    const win = winStub({ fullScreen: false });
    const wc = wcStub();
    const bridge = attachHtmlFullScreen({ win, webContents: wc });
    wc.enter();
    wc.leave();
    win._transitionDone();
    // Second cycle starting from a window the user has since fullscreened.
    win._userSetsFullScreen(true);
    win.calls.length = 0;
    wc.enter();
    assert.equal(bridge.raisedWindow(), false, "not ours this time");
    wc.leave();
    assert.deepEqual(win.calls, []);
  });

  it("is a no-op on a destroyed window", () => {
    // Chromium can dispatch after teardown; every other window-lifecycle.js
    // listener guards the same way, and setFullScreen on a destroyed window
    // throws in Electron.
    const win = winStub({ destroyed: true });
    const wc = wcStub();
    attachHtmlFullScreen({ win, webContents: wc });
    wc.enter();
    wc.leave();
    assert.deepEqual(win.calls, []);
  });
});

describe("window lifecycle actually attaches the bridge", () => {
  // The defect was absent WIRING, so the unit tests above would all pass while
  // the app stayed broken if the window owner never called it. This pins the
  // call site at the boundary that creates and persists dashboard windows.
  const WINDOW_LIFECYCLE_JS = fs.readFileSync(
    path.join(__dirname, "..", "window-lifecycle.js"),
    "utf8",
  );

  it("requires and calls attachHtmlFullScreen for the dashboard view", () => {
    assert.match(WINDOW_LIFECYCLE_JS, /require\("\.\/html-fullscreen"\)/);
    assert.match(
      WINDOW_LIFECYCLE_JS,
      /_mcHtmlFullScreen = attachHtmlFullScreen\(\{\s*win,\s*webContents:\s*view\.webContents,?\s*\}\)/,
    );
  });

  it("excludes a bridge-raised fullscreen from persisted window state", () => {
    // Without this the bridge corrupts the restore preference: quitting or
    // crashing while a video is fullscreen would persist fullScreen:true and
    // relaunch into a fullscreen Space the user never chose. Pinned as a source
    // contract because the two halves live in different files and only their
    // COMPOSITION is the fix.
    assert.match(
      WINDOW_LIFECYCLE_JS,
      /captureWindowState\(mainWindow,\s*\{[\s\S]*?transientFullScreen:\s*mainWindow\?\._mcHtmlFullScreen\?\.raisedWindow\(\)\s*===\s*true,?[\s\S]*?\}\)/,
    );
  });
});
