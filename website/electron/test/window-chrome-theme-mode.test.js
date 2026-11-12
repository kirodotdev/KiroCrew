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
