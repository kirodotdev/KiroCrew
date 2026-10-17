const { test } = require("node:test");
const assert = require("node:assert");
const {
  titleBarOverlayOptions,
  paintTitleBarOverlay,
  paintAllTitleBarOverlays,
  SYMBOL_DARK,
  SYMBOL_LIGHT,
  OVERLAY_BACKGROUND,
} = require("../windows-titlebar");

/** An overlay-capable window that records what it was painted with. */
function overlayWindow(zoom = 1) {
  const win = {
    calls: [],
    isDestroyed: () => false,
    setTitleBarOverlay(opts) { this.calls.push(opts); },
  };
  if (zoom !== null) {
    win._mcView = { webContents: { isDestroyed: () => false, getZoomFactor: () => zoom } };
  }
  return win;
}

/** A plain framed window, like the gateway-error dialog: Electron throws. */
function framedWindow() {
  return {
    isDestroyed: () => false,
    setTitleBarOverlay() { throw new Error("window has no title bar overlay"); },
  };
}

test("paints a transparent overlay with theme-appropriate symbol color", () => {
  assert.deepStrictEqual(titleBarOverlayOptions("dark", 1, 42), {
    color: OVERLAY_BACKGROUND,
    symbolColor: SYMBOL_DARK,
    height: 42,
  });
  assert.strictEqual(titleBarOverlayOptions("light", 1, 42).symbolColor, SYMBOL_LIGHT);
  // The background never carries the theme — the header paints the strip.
  assert.strictEqual(titleBarOverlayOptions("light", 1, 42).color, "#00000000");
});

test("scales the overlay height with the renderer zoom factor", () => {
  // The height is physical px, so it must track zoom or the native buttons stop
  // being centered in the header row.
  assert.strictEqual(titleBarOverlayOptions("dark", 1.5, 42).height, 63);
  assert.strictEqual(titleBarOverlayOptions("dark", 0.9, 42).height, 38);
});

test("falls back to 1x for a missing or nonsensical zoom factor", () => {
  for (const zoom of [undefined, null, NaN, Infinity, 0, -2]) {
    assert.strictEqual(titleBarOverlayOptions("dark", zoom, 42).height, 42);
  }
});

test("reads the zoom factor from the window's own view", () => {
  const win = overlayWindow(2);
  assert.strictEqual(paintTitleBarOverlay(win, "dark", 42), true);
  assert.deepStrictEqual(win.calls, [{
    color: OVERLAY_BACKGROUND,
    symbolColor: SYMBOL_DARK,
    height: 84,
  }]);
});

test("treats a window with no view as 1x rather than skipping it", () => {
  // A dashboard window can be painted before setupWindowContents assigns
  // _mcView; it still needs the right symbol color.
  const win = overlayWindow(null);
  assert.strictEqual(paintTitleBarOverlay(win, "light", 42), true);
  assert.strictEqual(win.calls[0].height, 42);
  assert.strictEqual(win.calls[0].symbolColor, SYMBOL_LIGHT);
});

test("reports a framed window as skipped instead of throwing", () => {
  assert.strictEqual(paintTitleBarOverlay(framedWindow(), "dark", 42), false);
});

test("skips a destroyed window and one with no overlay API at all", () => {
  const destroyed = { isDestroyed: () => true, setTitleBarOverlay() { throw new Error("gone"); } };
  assert.strictEqual(paintTitleBarOverlay(destroyed, "dark", 42), false);
  assert.strictEqual(paintTitleBarOverlay({}, "dark", 42), false);
  assert.strictEqual(paintTitleBarOverlay(null, "dark", 42), false);
});

test("a throwing window does NOT abort the windows after it", () => {
  // The regression this guards: a theme switch with a modal open used to throw
  // partway through the loop, leaving every later window painted for the OLD
  // theme — a half-repainted app.
  const before = overlayWindow();
  const after = overlayWindow();
  const result = paintAllTitleBarOverlays([before, framedWindow(), after], "dark", 42);

  assert.deepStrictEqual(result, { painted: 2, skipped: 1 });
  assert.strictEqual(before.calls.length, 1);
  assert.strictEqual(after.calls.length, 1, "window after the throwing one must still repaint");
  assert.strictEqual(after.calls[0].symbolColor, SYMBOL_DARK);
});

test("tolerates an empty or absent window list", () => {
  assert.deepStrictEqual(paintAllTitleBarOverlays([], "dark", 42), { painted: 0, skipped: 0 });
  assert.deepStrictEqual(paintAllTitleBarOverlays(undefined, "dark", 42), { painted: 0, skipped: 0 });
});
