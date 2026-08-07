/**
 * The main-process half of cursor-hitbox forwarding.
 *
 * The overlay covers the whole display and is click-through by default. The
 * renderer reports the companion's, bubble's and menu's rects; the main process
 * polls the cursor and toggles THIS window's ignore-mouse itself. These tests pin
 * that toggle: which rects make the window accept input, that the cursor is
 * converted through the window's own bounds, and that `forward: true` is always
 * kept so the poll can keep seeing the cursor.
 *
 * `refreshOverlayInput` takes the cursor as an argument, so the toggle is exercised
 * directly without the live 60fps interval or a real `screen`.
 */

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");
const Module = require("module");

/** Minimal Electron stand-in — enough to import the module and observe toggles. */
function stubElectron() {
  const created = [];
  const ipcHandlers = {};

  class FakeWindow {
    constructor(opts) {
      this.opts = opts;
      this.destroyed = false;
      this.ignoreMouse = null;
      this._events = {};
      created.push(this);
    }
    setFocusable() {}
    setAcceptFirstMouse() {}
    setIgnoreMouseEvents(ignore, opts) { this.ignoreMouse = { ignore, opts }; }
    setVisibleOnAllWorkspaces() {}
    loadURL() {}
    once(ev, cb) { this._events[ev] = cb; }
    on(ev, cb) { this._events[ev] = cb; }
    showInactive() {}
    isDestroyed() { return this.destroyed; }
    destroy() { this.destroyed = true; }
    // The overlay lives at its display's origin; the poll converts screen->local
    // through this, not through display.bounds.
    getBounds() { return { x: this.opts.x, y: this.opts.y, width: this.opts.width, height: this.opts.height }; }
  }

  const electron = {
    BrowserWindow: FakeWindow,
    screen: {
      getAllDisplays: () => [
        { id: 1, bounds: { x: 0, y: 0, width: 1440, height: 900 } },
        { id: 2, bounds: { x: 1440, y: 0, width: 1920, height: 1080 } },
      ],
      // Not used by the direct toggle tests, but present so the poll never throws.
      getCursorScreenPoint: () => ({ x: -1, y: -1 }),
    },
    ipcMain: { on: (ch, cb) => { ipcHandlers[ch] = cb; } },
    contextBridge: { exposeInMainWorld: () => {} },
    ipcRenderer: { send: () => {} },
  };
  electron.BrowserWindow.fromWebContents = (sender) => sender && sender.__win;

  const realResolve = Module._resolveFilename;
  Module._resolveFilename = function (request, ...rest) {
    if (request === "electron") return "electron";
    return realResolve.call(this, request, ...rest);
  };
  require.cache.electron = { id: "electron", filename: "electron", loaded: true, exports: electron };

  return {
    created,
    ipcHandlers,
    FakeWindow,
    restore() {
      Module._resolveFilename = realResolve;
      delete require.cache.electron;
    },
  };
}

function loadOverlay() {
  const dir = path.join(__dirname, "..");
  for (const f of ["petOverlay.js", "pageUrl.js"]) {
    delete require.cache[require.resolve(path.join(dir, f))];
  }
  return require(path.join(dir, "petOverlay.js"));
}

// ── pure geometry ───────────────────────────────────────────────────────────

test("pointInRect includes the edges and excludes points outside", () => {
  const stub = stubElectron();
  try {
    const { pointInRect } = loadOverlay();
    const r = { x: 10, y: 20, w: 100, h: 50 };
    assert.strictEqual(pointInRect(r, 60, 40), true, "centre is in");
    assert.strictEqual(pointInRect(r, 10, 20), true, "top-left corner is in (inclusive)");
    assert.strictEqual(pointInRect(r, 110, 70), true, "bottom-right corner is in (inclusive)");
    assert.strictEqual(pointInRect(r, 9, 40), false, "just left is out");
    assert.strictEqual(pointInRect(r, 111, 40), false, "just right is out");
    assert.strictEqual(pointInRect(null, 60, 40), false, "a null rect is never hit");
  } finally {
    stub.restore();
  }
});

test("cursorHitsWindow is true inside ANY reported rect and false otherwise", () => {
  const stub = stubElectron();
  try {
    const { cursorHitsWindow } = loadOverlay();
    const boxes = {
      pet: { x: 100, y: 100, w: 128, h: 128 },
      bubble: { x: 90, y: 0, w: 240, h: 60 },
      menu: { x: 400, y: 300, w: 160, h: 80 },
    };
    assert.strictEqual(cursorHitsWindow(boxes, 150, 150), true, "in the companion");
    assert.strictEqual(cursorHitsWindow(boxes, 120, 20), true, "in the bubble");
    assert.strictEqual(cursorHitsWindow(boxes, 450, 320), true, "in the menu");
    assert.strictEqual(cursorHitsWindow(boxes, 700, 700), false, "empty desktop");
    assert.strictEqual(cursorHitsWindow(null, 150, 150), false, "no hitboxes reported yet");
  } finally {
    stub.restore();
  }
});

// ── the toggle ────────────────────────────────────────────────────────────

test("with no hitbox reported the overlay stays click-through", () => {
  const stub = stubElectron();
  try {
    const overlay = loadOverlay();
    const win = new stub.FakeWindow({ x: 0, y: 0, width: 1440, height: 900 });
    const ignore = overlay.refreshOverlayInput(win, { x: 200, y: 200 });
    assert.strictEqual(ignore, true, "nothing to accept => click-through");
    assert.deepStrictEqual(win.ignoreMouse, { ignore: true, opts: { forward: true } });
  } finally {
    stub.restore();
  }
});

test("the overlay accepts input over the companion and refuses it elsewhere", () => {
  const stub = stubElectron();
  try {
    const overlay = loadOverlay();
    const win = new stub.FakeWindow({ x: 0, y: 0, width: 1440, height: 900 });
    overlay.setWindowHitbox(win, { x: 100, y: 100, w: 128, h: 128 }, null);

    overlay.refreshOverlayInput(win, { x: 150, y: 150 });
    assert.deepStrictEqual(
      win.ignoreMouse,
      { ignore: false, opts: { forward: true } },
      "cursor over the companion => accept input, forwarding still on",
    );

    overlay.refreshOverlayInput(win, { x: 800, y: 800 });
    assert.deepStrictEqual(
      win.ignoreMouse,
      { ignore: true, opts: { forward: true } },
      "cursor off the companion => back to click-through",
    );
  } finally {
    stub.restore();
  }
});

test("the cursor is converted through the overlay's OWN bounds, not the origin", () => {
  const stub = stubElectron();
  try {
    const overlay = loadOverlay();
    // Overlay on the second display, whose origin is (1440,0).
    const win = new stub.FakeWindow({ x: 1440, y: 0, width: 1920, height: 1080 });
    overlay.setWindowHitbox(win, { x: 10, y: 10, w: 50, h: 50 }, null);

    // Screen point (1455,25) => local (15,25), inside the 10..60 box.
    overlay.refreshOverlayInput(win, { x: 1455, y: 25 });
    assert.strictEqual(win.ignoreMouse.ignore, false, "local conversion lands inside the box");

    // The same LOCAL coordinates on the primary origin would be off this display.
    overlay.refreshOverlayInput(win, { x: 15, y: 25 });
    assert.strictEqual(win.ignoreMouse.ignore, true, "a screen point on another display is out");
  } finally {
    stub.restore();
  }
});

test("the bubble rect is clickable too", () => {
  const stub = stubElectron();
  try {
    const overlay = loadOverlay();
    const win = new stub.FakeWindow({ x: 0, y: 0, width: 1440, height: 900 });
    overlay.setWindowHitbox(win, { x: 100, y: 300, w: 128, h: 128 }, { x: 80, y: 180, w: 240, h: 80 });
    overlay.refreshOverlayInput(win, { x: 120, y: 200 });
    assert.strictEqual(win.ignoreMouse.ignore, false, "cursor over the bubble accepts input");
  } finally {
    stub.restore();
  }
});

test("the menu rect is one of the reported hitboxes and preserves the pet rect", () => {
  const stub = stubElectron();
  try {
    const overlay = loadOverlay();
    const win = new stub.FakeWindow({ x: 0, y: 0, width: 1440, height: 900 });
    overlay.setWindowHitbox(win, { x: 100, y: 100, w: 128, h: 128 }, null);
    overlay.setWindowMenuHitbox(win, { x: 500, y: 400, w: 160, h: 90 });

    overlay.refreshOverlayInput(win, { x: 550, y: 440 });
    assert.strictEqual(win.ignoreMouse.ignore, false, "cursor over the menu accepts input");

    // Setting the menu must not have clobbered the companion's own rect.
    overlay.refreshOverlayInput(win, { x: 150, y: 150 });
    assert.strictEqual(win.ignoreMouse.ignore, false, "the companion is still clickable");

    // And clearing the menu (null) drops just that region.
    overlay.setWindowMenuHitbox(win, null);
    overlay.refreshOverlayInput(win, { x: 550, y: 440 });
    assert.strictEqual(win.ignoreMouse.ignore, true, "menu region is click-through once closed");
  } finally {
    stub.restore();
  }
});

// ── IPC routing ───────────────────────────────────────────────────────────

test("the hitbox IPC routes a renderer's report to its own overlay", () => {
  const stub = stubElectron();
  try {
    const overlay = loadOverlay();
    overlay.setOverlayTarget("http://localhost:5476", "");
    overlay.openPetWindow();
    overlay.registerOverlayIpc();

    const win = stub.created[0];
    // Sender identity resolves to this window via the stub's fromWebContents.
    const sender = { __win: win };

    stub.ipcHandlers["crew-companion:update-hitbox"](
      { sender },
      { x: 100, y: 100, w: 128, h: 128 },
      null,
    );
    overlay.refreshOverlayInput(win, { x: 150, y: 150 });
    assert.strictEqual(win.ignoreMouse.ignore, false, "reported pet rect makes the companion clickable");

    stub.ipcHandlers["crew-companion:menu-hitbox"]({ sender }, { x: 500, y: 400, w: 160, h: 90 });
    overlay.refreshOverlayInput(win, { x: 550, y: 440 });
    assert.strictEqual(win.ignoreMouse.ignore, false, "reported menu rect makes the menu clickable");

    overlay.stopHitboxPoll();
    overlay.closePetWindow();
  } finally {
    stub.restore();
  }
});
