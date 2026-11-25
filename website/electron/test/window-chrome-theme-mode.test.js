"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createWindowChrome } = require("../runtime/window/chrome");
const { SYMBOL_DARK, SYMBOL_LIGHT } = require("../windows-titlebar");

// themeSource is process-wide: a dashboard window in the background must not
// re-pin the appearance of the dashboard window that has focus.
test("a background dashboard window's theme push is dropped while another dashboard window has focus", () => {
  const nativeTheme = { themeSource: "system", shouldUseDarkColors: false };
  const dashboard = (webContents) => ({ _mcView: { webContents }, isDestroyed: () => false });
  const aContents = { id: "a" };
  const bContents = { id: "b" };
  const a = dashboard(aContents);
  const b = dashboard(bContents);
  const companion = { isDestroyed: () => false };
  let focused = a;
  const chrome = createWindowChrome({
    BaseWindow: { getAllWindows: () => [a, b, companion], getFocusedWindow: () => focused },
    nativeTheme,
    screen: {},
    store: { get: () => null, set: () => {} },
    log: () => {},
    isMac: false,
    isWindows: false,
    windowForWebContents: (wc) => [a, b].find((win) => win._mcView.webContents === wc) || null,
  });

  chrome.setThemeMode("dark", bContents);
  assert.equal(nativeTheme.themeSource, "system", "a background window cannot re-pin the focused one");
  chrome.setThemeMode("light", aContents);
  assert.equal(nativeTheme.themeSource, "light", "the focused window's own push applies");
  focused = companion;
  chrome.setThemeMode("dark", bContents);
  assert.equal(nativeTheme.themeSource, "dark", "a focused companion window does not hold the appearance");
  focused = null;
  chrome.setThemeMode("light", bContents);
  assert.equal(nativeTheme.themeSource, "light", "with no focused window the push applies");
});

// The glyph colour titleBarOverlayOptions paints: light glyphs on a dark strip.
const LIGHT_GLYPHS = SYMBOL_DARK;
const DARK_GLYPHS = SYMBOL_LIGHT;

function windowsChrome({ windows, nativeTheme, owner }) {
  return createWindowChrome({
    BaseWindow: { getAllWindows: () => windows, getFocusedWindow: () => windows[0] },
    nativeTheme,
    screen: {},
    store: { get: () => null, set: () => {} },
    log: () => {},
    isMac: false,
    isWindows: true,
    windowForWebContents: owner,
  });
}

function captionWindow(dataset) {
  const symbols = [];
  const webContents = {
    isDestroyed: () => false,
    getZoomFactor: () => 1,
    executeJavaScript: (code) => Promise.resolve(
      code.startsWith("JSON.stringify") ? JSON.stringify(dataset) : undefined,
    ),
  };
  return {
    symbols,
    view: { webContents },
    win: {
      _mcView: { webContents },
      isDestroyed: () => false,
      setTitleBarOverlay: (opts) => symbols.push(opts.symbolColor),
    },
  };
}

// A dashboard painting a Remote Crew's lent mode pushes it; its own window keeps
// it through a focus re-sync and a zoom step, and no other window takes it.
test("the caption buttons keep the mode their own window's dashboard pushed (#40254)", async () => {
  const nativeTheme = {
    themeSource: "light",
    get shouldUseDarkColors() { return this.themeSource === "dark"; },
  };
  const a = captionWindow({ pref: "light", mode: "dark" });
  const b = captionWindow({ pref: "light", mode: "light" });
  const chrome = windowsChrome({
    windows: [a.win, b.win],
    nativeTheme,
    owner: (wc) => [a.win, b.win].find((w) => w._mcView.webContents === wc) || null,
  });

  chrome.setTitlebarMode("dark", a.view.webContents);
  assert.equal(a.symbols.at(-1), LIGHT_GLYPHS, "the sender's window paints the pushed mode");
  assert.deepEqual(b.symbols, [], "another window is not painted with it");

  chrome.syncNativeTheme(a.view, a.win);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(nativeTheme.themeSource, "light", "the native theme keeps the own mode");
  assert.equal(a.symbols.at(-1), LIGHT_GLYPHS, "a focus re-sync keeps the pushed mode");

  chrome.reconcileZoomChrome(a.win);
  assert.equal(a.symbols.at(-1), LIGHT_GLYPHS, "a zoom step keeps the pushed mode");
  chrome.reconcileZoomChrome(b.win);
  assert.equal(b.symbols.at(-1), DARK_GLYPHS, "a window with no push follows the native theme");

  chrome.setTitlebarMode("light", a.view.webContents);
  assert.equal(a.symbols.at(-1), DARK_GLYPHS, "the own mode comes back with the next push");
});

// A push no window owns (no sender) repaints every window, as before.
test("a push without a known sender repaints every window", () => {
  const nativeTheme = { themeSource: "system", shouldUseDarkColors: false };
  const a = captionWindow({});
  const b = captionWindow({});
  const chrome = windowsChrome({ windows: [a.win, b.win], nativeTheme, owner: () => null });
  chrome.setTitlebarMode("dark");
  assert.equal(a.symbols.at(-1), LIGHT_GLYPHS);
  assert.equal(b.symbols.at(-1), LIGHT_GLYPHS);
});
