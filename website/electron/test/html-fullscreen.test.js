const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const { attachHtmlFullScreen } = require("../html-fullscreen");

/**
 * Window stub that records every setFullScreen call.
 *
 * The tracker must never call it: Electron raises and lowers the window itself
 * for DOM fullscreen, and a second caller leaves a <video> stuck fullscreen.
 * `_fire` delivers a window event; `_setFullScreen` flips the reported state the
 * way a native transition does, without firing anything.
 */
function winStub({ fullScreen = false, destroyed = false } = {}) {
  const calls = [];
  const listeners = new Map();
  return {
    calls,
    isDestroyed: () => destroyed,
    isFullScreen: () => fullScreen,
    setFullScreen: (v) => { calls.push(v); fullScreen = v; },
    on: (event, fn) => { listeners.set(event, [...(listeners.get(event) ?? []), fn]); },
    _setFullScreen: (v) => { fullScreen = v; },
    _fire: (event) => { for (const fn of listeners.get(event) ?? []) fn(); },
    _hasListener: (event) => listeners.has(event),
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

// The tracker settles a window `enter-full-screen` on the next event-loop turn,
// once the native call that raised the window has emitted both events.
const settle = () => new Promise((resolve) => setImmediate(resolve));
// Electron emits each native event in its own callback scope, which drains the
// process.nextTick queue before the next emit.
const drainTicks = () => new Promise((resolve) => process.nextTick(resolve));

/** Electron raises the window, then emits enter-html-full-screen (Windows, Linux). */
async function pageRaisesSynchronously(win, wc) {
  win._setFullScreen(true);
  win._fire("enter-full-screen");
  await drainTicks();
  wc.enter();
  await settle();
}

/** Electron emits enter-html-full-screen, then the window lands later (macOS). */
async function pageRaisesAsynchronously(win, wc) {
  wc.enter();
  win._setFullScreen(true);
  win._fire("enter-full-screen");
  await settle();
}

/** The window finishes leaving fullscreen. */
function windowLeaves(win) {
  win._setFullScreen(false);
  win._fire("leave-full-screen");
}

describe("attachHtmlFullScreen — who raised the window", () => {
  it("subscribes to the DOM-fullscreen entry and the window's own transitions", () => {
    const wc = wcStub();
    const win = winStub();
    attachHtmlFullScreen({ win, webContents: wc });
    assert.equal(wc.has("enter-html-full-screen"), true);
    assert.equal(win._hasListener("enter-full-screen"), true);
    assert.equal(win._hasListener("leave-full-screen"), true);
  });

  it("never drives the window itself, on enter or on exit", async () => {
    // Electron raises and lowers the window for DOM fullscreen. A second
    // setFullScreen caller races its exit: the window drops out of fullscreen
    // but the <video> stays document.fullscreenElement, covering the whole view
    // with Escape no longer reaching it.
    const win = winStub();
    const wc = wcStub();
    attachHtmlFullScreen({ win, webContents: wc });
    await pageRaisesAsynchronously(win, wc);
    wc.leave();
    windowLeaves(win);
    assert.deepEqual(win.calls, []);
  });

  for (const [label, raise] of [
    ["macOS order (html event first)", pageRaisesAsynchronously],
    ["Windows/Linux order (window event first)", pageRaisesSynchronously],
  ]) {
    it(`claims a window the page raised, until the window leaves fullscreen — ${label}`, async () => {
      // The exit is asynchronous on macOS. Clearing the claim on the DOM event
      // would leave a gap where isFullScreen() is still true but nothing owns it,
      // and a persist landing there writes the transient fullscreen to disk.
      const win = winStub();
      const wc = wcStub();
      const bridge = attachHtmlFullScreen({ win, webContents: wc });
      await raise(win, wc);
      assert.equal(bridge.raisedWindow(), true);
      wc.leave();
      assert.equal(bridge.raisedWindow(), true, "still owned until the transition lands");
      windowLeaves(win);
      assert.equal(bridge.raisedWindow(), false, "ownership ends with the transition");
    });
  }

  it("does not claim a window the USER fullscreened before the page asked", async () => {
    const win = winStub();
    const wc = wcStub();
    const bridge = attachHtmlFullScreen({ win, webContents: wc });
    win._setFullScreen(true);
    win._fire("enter-full-screen");
    await settle();
    assert.equal(bridge.raisedWindow(), false, "the user's own fullscreen is not transient");
    wc.enter();
    assert.equal(bridge.raisedWindow(), false);
    wc.leave();
    assert.equal(bridge.raisedWindow(), false);
    assert.deepEqual(win.calls, []);
  });

  it("never claims a fullscreen the user just chose, even before it settles", () => {
    // Persistence reads raisedWindow() synchronously inside the window's own
    // enter-full-screen emit. An unclaimed-by-default tracker answers false there;
    // one that derives the claim from "fullscreen and not yet user-owned" answers
    // true and stores fullScreen:false for the user's own choice.
    const win = winStub();
    const wc = wcStub();
    const bridge = attachHtmlFullScreen({ win, webContents: wc });
    win._setFullScreen(true);
    let readInsideEmit = null;
    win.on("enter-full-screen", () => { readInsideEmit = bridge.raisedWindow(); });
    win._fire("enter-full-screen");
    assert.equal(readInsideEmit, false);
    assert.equal(bridge.raisedWindow(), false);
  });

  it("does not claim the user's re-entry while an element is still fullscreen", async () => {
    // The page raised the window, the user left fullscreen natively while the
    // element stayed :fullscreen, then re-entered. That entry is the user's, so
    // a later page request must not claim it.
    const win = winStub();
    const wc = wcStub();
    const bridge = attachHtmlFullScreen({ win, webContents: wc });
    await pageRaisesAsynchronously(win, wc);
    windowLeaves(win);
    win._setFullScreen(true);
    win._fire("enter-full-screen");
    await settle();
    assert.equal(bridge.raisedWindow(), false);
    wc.enter();
    assert.equal(bridge.raisedWindow(), false, "the user's fullscreen is not the page's");
  });

  it("does not claim a window that was already fullscreen when attached", () => {
    const win = winStub({ fullScreen: true });
    const wc = wcStub();
    const bridge = attachHtmlFullScreen({ win, webContents: wc });
    wc.enter();
    assert.equal(bridge.raisedWindow(), false);
  });

  it("releases the claim when the USER exits a page-raised fullscreen", async () => {
    const win = winStub();
    const wc = wcStub();
    const bridge = attachHtmlFullScreen({ win, webContents: wc });
    await pageRaisesAsynchronously(win, wc);
    windowLeaves(win);
    assert.equal(bridge.raisedWindow(), false);
  });

  it("re-decides ownership on a second fullscreen cycle", async () => {
    const win = winStub();
    const wc = wcStub();
    const bridge = attachHtmlFullScreen({ win, webContents: wc });
    await pageRaisesSynchronously(win, wc);
    wc.leave();
    windowLeaves(win);
    // Second cycle starting from a window the user has since fullscreened.
    win._setFullScreen(true);
    win._fire("enter-full-screen");
    await settle();
    wc.enter();
    assert.equal(bridge.raisedWindow(), false, "not ours this time");
  });

  it("answers false on a destroyed window", () => {
    const win = winStub({ fullScreen: true, destroyed: true });
    const wc = wcStub();
    const bridge = attachHtmlFullScreen({ win, webContents: wc });
    wc.enter();
    assert.equal(bridge.raisedWindow(), false);
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

  it("persists entering fullscreen after the debounce, once ownership has settled", () => {
    // Ownership is only readable after both of Electron's events for a raise have
    // landed, and their order differs by platform; a synchronous persist inside the
    // window's enter-full-screen emit can store a page-raised fullscreen as the
    // user's choice.
    assert.match(WINDOW_LIFECYCLE_JS, /mainWindow\.on\("enter-full-screen",\s*persistDebounced\)/);
    assert.doesNotMatch(WINDOW_LIFECYCLE_JS, /mainWindow\.on\("enter-full-screen",\s*persist\)/);
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
