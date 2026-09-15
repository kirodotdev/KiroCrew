"use strict";

const { test } = require("node:test");
const assert = require("node:assert");
const { applyFocusModeChrome, REDECLARE_DRAG_REGIONS_JS } = require("../focus-chrome");

// A fake BaseWindow with the surface applyFocusModeChrome touches. `calls`
// records ORDER, which is the load-bearing property: the drag-region
// re-declaration must be issued AFTER setWindowButtonVisibility, because that
// native call is what drops the renderer's declared regions.
function fakeWin({ destroyed = false, fullscreen = false, withView = true } = {}) {
  const calls = [];
  const win = {
    calls,
    isDestroyed: () => destroyed,
    isFullScreen: () => fullscreen,
    setWindowButtonVisibility: (v) => calls.push(["buttons", v]),
  };
  if (withView) {
    win._mcView = {
      webContents: {
        executeJavaScript: (js) => {
          calls.push(["exec", js]);
          return Promise.resolve();
        },
      },
    };
  }
  return win;
}

test("re-declares drag regions AFTER the native button toggle, on both transitions", () => {
  for (const visible of [true, false]) {
    const win = fakeWin();
    assert.strictEqual(applyFocusModeChrome(win, visible, {}), true);
    const kinds = win.calls.map(([k]) => k);
    // Order is the contract: buttons first (the styleMask mutation that wipes
    // regions), then the renderer-side re-declaration.
    assert.deepStrictEqual(kinds, ["buttons", "exec"], `visible=${visible}`);
    assert.deepStrictEqual(win.calls[0], ["buttons", visible]);
    assert.strictEqual(win.calls[1][1], REDECLARE_DRAG_REGIONS_JS);
  }
});

test("re-positions the traffic lights only on show", () => {
  const positioned = [];
  const helpers = { positionTrafficLights: (w) => positioned.push(w) };

  const shown = fakeWin();
  applyFocusModeChrome(shown, true, helpers);
  assert.deepStrictEqual(positioned, [shown]);

  positioned.length = 0;
  applyFocusModeChrome(fakeWin(), false, helpers);
  assert.deepStrictEqual(positioned, []);
});

test("refuses destroyed and fullscreen windows without touching them", () => {
  for (const win of [fakeWin({ destroyed: true }), fakeWin({ fullscreen: true }), null, undefined]) {
    assert.strictEqual(applyFocusModeChrome(win, true, {}), false);
    if (win) assert.deepStrictEqual(win.calls, []);
  }
});

test("survives a window without a view, and an API-less window", () => {
  // No _mcView: the buttons still toggle; there is just no renderer to nudge.
  const bare = fakeWin({ withView: false });
  assert.strictEqual(applyFocusModeChrome(bare, true, {}), true);
  assert.deepStrictEqual(bare.calls.map(([k]) => k), ["buttons"]);

  // setWindowButtonVisibility throwing (older build) is contained, not fatal.
  const throwing = {
    isDestroyed: () => false,
    isFullScreen: () => false,
    setWindowButtonVisibility: () => { throw new Error("no api"); },
  };
  assert.strictEqual(applyFocusModeChrome(throwing, true, {}), false);
});

test("the injected snippet forces a layout read and cleans up after itself", () => {
  // The snippet is a string evaluated in the page; pin the two properties that
  // make it WORK rather than merely run: the forced layout read (without it the
  // style change may never produce a region re-send) and the removal (without
  // it every toggle leaks a drag-region element at the window origin).
  assert.match(REDECLARE_DRAG_REGIONS_JS, /void el\.offsetWidth/);
  assert.match(REDECLARE_DRAG_REGIONS_JS, /el\.remove\(\)/);
  assert.match(REDECLARE_DRAG_REGIONS_JS, /-webkit-app-region:drag/);
});
