"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createWindowChrome } = require("../runtime/window/chrome");

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

// The caption mode comes from the renderer's push, never from data-mode: with
// no push yet, a focus re-sync paints the mode the preference pins.
test("a focus re-sync with no pushed caption mode paints the own mode, not data-mode", async () => {
  const nativeTheme = {
    themeSource: "system",
    get shouldUseDarkColors() { return this.themeSource === "dark"; },
  };
  const symbols = [];
  const view = {
    webContents: {
      isDestroyed: () => false,
      getZoomFactor: () => 1,
      executeJavaScript: (code) => Promise.resolve(
        code.startsWith("JSON.stringify") ? JSON.stringify({ pref: "light", mode: "dark" }) : undefined,
      ),
    },
  };
  const win = {
    _mcView: view,
    isDestroyed: () => false,
    setTitleBarOverlay: (opts) => symbols.push(opts.symbolColor),
  };
  const chrome = createWindowChrome({
    BaseWindow: { getAllWindows: () => [win], getFocusedWindow: () => win },
    nativeTheme,
    screen: {},
    store: { get: () => null, set: () => {} },
    log: () => {},
    isMac: false,
    isWindows: true,
    windowForWebContents: () => win,
  });

  chrome.syncNativeTheme(view, win);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(nativeTheme.themeSource, "light");
  assert.deepEqual(symbols, ["#211d28"], "light own mode: dark glyphs, not the lent dark mode's light ones");
});

// A renderer pushes its painted mode (a Remote Crew's lent one included): only
// its own window repaints, and a zoom step or focus re-sync keeps that mode.
test("a pushed caption mode is kept per window by every painter", async () => {
  const nativeTheme = { themeSource: "light", shouldUseDarkColors: false };
  const makeWindow = (name, symbols) => {
    const webContents = {
      name,
      isDestroyed: () => false,
      getZoomFactor: () => 1,
      executeJavaScript: (code) => Promise.resolve(
        code.startsWith("JSON.stringify") ? JSON.stringify({ pref: "light", mode: "dark" }) : undefined,
      ),
    };
    return {
      _mcView: { webContents },
      isDestroyed: () => false,
      setTitleBarOverlay: (opts) => symbols.push(opts.symbolColor),
    };
  };
  const aSymbols = [];
  const bSymbols = [];
  const a = makeWindow("a", aSymbols);
  const b = makeWindow("b", bSymbols);
  const chrome = createWindowChrome({
    BaseWindow: { getAllWindows: () => [a, b], getFocusedWindow: () => a },
    nativeTheme,
    screen: {},
    store: { get: () => null, set: () => {} },
    log: () => {},
    isMac: false,
    isWindows: true,
    windowForWebContents: (wc) => [a, b].find((win) => win._mcView.webContents === wc) || null,
  });

  chrome.setTitlebarMode("dark", a._mcView.webContents);
  assert.deepEqual(aSymbols, ["#f4f0fa"], "the sender's window paints the pushed dark mode");
  assert.deepEqual(bSymbols, [], "another window is not repainted");

  chrome.reconcileZoomChrome(a);
  chrome.reconcileZoomChrome(b);
  assert.deepEqual(aSymbols, ["#f4f0fa", "#f4f0fa"], "a zoom step keeps the pushed mode");
  assert.deepEqual(bSymbols, ["#211d28"], "a window with no push takes the native theme");

  chrome.syncNativeTheme(a._mcView, a);
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(aSymbols.at(-1), "#f4f0fa", "a focus re-sync keeps the pushed mode");

  chrome.setTitlebarMode("", a._mcView.webContents);
  assert.equal(aSymbols.at(-1), "#211d28", "an unknown push falls back to the native theme");
  chrome.setTitlebarMode("dark", { name: "unknown" });
  assert.equal(aSymbols.length, 4, "a push from no dashboard window paints nothing");
});
