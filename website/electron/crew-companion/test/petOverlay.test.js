/**
 * The companion's window lifecycle.
 *
 * Electron is STUBBED — these pin the logic that decides whether a window should
 * exist, not the compositor. The rules under test are the ones that produce visible
 * bugs when broken: a failed probe must not tear the companion down, the overlay
 * must refuse input by default, and enable/disable must be idempotent.
 */

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");
const Module = require("module");

/** Minimal Electron stand-in — enough to observe what the modules do. */
function stubElectron() {
  const created = [];
  const ipcHandlers = {};

  class FakeWindow {
    constructor(opts) {
      this.opts = opts;
      this.destroyed = false;
      this.ignoreMouse = null;
      this.focusable = true;
      this.workspaces = null;
      this.loadedUrl = "";
      this.shown = false;
      this._events = {};
      created.push(this);
    }
    setFocusable(v) { this.focusable = v; }
    setAcceptFirstMouse() {}
    setIgnoreMouseEvents(ignore, opts) { this.ignoreMouse = { ignore, opts }; }
    setVisibleOnAllWorkspaces(v, opts) { this.workspaces = { v, opts }; }
    loadURL(u) { this.loadedUrl = u; }
    once(ev, cb) { this._events[ev] = cb; }
    on(ev, cb) { this._events[ev] = cb; }
    showInactive() { this.shown = true; }
    isDestroyed() { return this.destroyed; }
    destroy() { this.destroyed = true; }
  }

  const electron = {
    BrowserWindow: FakeWindow,
    screen: {
      getAllDisplays: () => [
        { id: 1, bounds: { x: 0, y: 0, width: 1440, height: 900 } },
        { id: 2, bounds: { x: 1440, y: 0, width: 1920, height: 1080 } },
      ],
    },
    ipcMain: { on: (ch, cb) => { ipcHandlers[ch] = cb; } },
    contextBridge: { exposeInMainWorld: () => {} },
    ipcRenderer: { send: () => {} },
  };
  electron.BrowserWindow.fromWebContents = () => created[created.length - 1];

  const realResolve = Module._resolveFilename;
  Module._resolveFilename = function (request, ...rest) {
    if (request === "electron") return "electron";
    return realResolve.call(this, request, ...rest);
  };
  require.cache.electron = { id: "electron", filename: "electron", loaded: true, exports: electron };

  return {
    created,
    ipcHandlers,
    restore() {
      Module._resolveFilename = realResolve;
      delete require.cache.electron;
    },
  };
}

function loadModules() {
  const dir = path.join(__dirname, "..");
  for (const f of ["petOverlay.js", "index.js", "pageUrl.js"]) {
    delete require.cache[require.resolve(path.join(dir, f))];
  }
  return {
    overlay: require(path.join(dir, "petOverlay.js")),
    index: require(path.join(dir, "index.js")),
    pageUrl: require(path.join(dir, "pageUrl.js")),
  };
}

/**
 * Let the reconcile that `initCrewCompanion` fires on entry actually finish.
 *
 * `reconcileOnce` is guarded by an in-flight flag, so awaiting a second call
 * returns immediately while the first is still running — an assertion placed
 * straight after `init` races the probe and passes for the wrong reason. Found
 * exactly that way: a deliberately broken guard still passed until this wait
 * existed.
 */
async function settle() {
  for (let i = 0; i < 20; i++) await new Promise((r) => setTimeout(r, 10));
}

// ── the overlay window ──────────────────────────────────────────────────────

test("opens one overlay per display, covering each display's full bounds", () => {
  const stub = stubElectron();
  try {
    const { overlay } = loadModules();
    overlay.setOverlayTarget("http://localhost:5476", "");
    overlay.openPetWindow();

    assert.strictEqual(overlay.petWindowCount(), 2, "one per display");
    const [a, b] = stub.created;
    assert.strictEqual(a.opts.width, 1440);
    assert.strictEqual(b.opts.x, 1440, "second overlay sits on the second display");
  } finally {
    stub.restore();
  }
});

test("the overlay refuses mouse input by default, with forwarding on", () => {
  const stub = stubElectron();
  try {
    const { overlay } = loadModules();
    overlay.setOverlayTarget("http://localhost:5476", "");
    overlay.openPetWindow();

    // The window covers the whole desktop. Accepting input by default would make
    // the machine unclickable; `forward` is what still lets the renderer see the
    // cursor so it can tell when the pointer is over the companion.
    const win = stub.created[0];
    assert.deepStrictEqual(win.ignoreMouse, { ignore: true, opts: { forward: true } });
  } finally {
    stub.restore();
  }
});

test("the overlay is transparent, frameless, always on top and not focusable", () => {
  const stub = stubElectron();
  try {
    const { overlay } = loadModules();
    overlay.setOverlayTarget("http://localhost:5476", "");
    overlay.openPetWindow();
    const win = stub.created[0];

    assert.strictEqual(win.opts.transparent, true);
    assert.strictEqual(win.opts.frame, false);
    assert.strictEqual(win.opts.alwaysOnTop, true);
    assert.strictEqual(win.opts.skipTaskbar, true);
    assert.strictEqual(win.focusable, false);
    // The companion animates continuously in a never-focusable window, which
    // Chromium would otherwise throttle to a stall.
    assert.strictEqual(win.opts.webPreferences.backgroundThrottling, false);
    // Follows the user across spaces and over full-screen apps.
    assert.deepStrictEqual(win.workspaces, { v: true, opts: { visibleOnFullScreen: true } });
  } finally {
    stub.restore();
  }
});

test("opening twice does not create a second overlay per display", () => {
  const stub = stubElectron();
  try {
    const { overlay } = loadModules();
    overlay.setOverlayTarget("http://localhost:5476", "");
    overlay.openPetWindow();
    overlay.openPetWindow();
    assert.strictEqual(overlay.petWindowCount(), 2, "idempotent");
  } finally {
    stub.restore();
  }
});

test("closing is idempotent and leaves nothing behind", () => {
  const stub = stubElectron();
  try {
    const { overlay } = loadModules();
    overlay.setOverlayTarget("http://localhost:5476", "");
    overlay.openPetWindow();
    overlay.closePetWindow();
    overlay.closePetWindow();
    assert.strictEqual(overlay.petWindowCount(), 0);
    assert.ok(stub.created.every((w) => w.destroyed));
  } finally {
    stub.restore();
  }
});

test("no overlay is opened before a gateway origin is known", () => {
  const stub = stubElectron();
  try {
    const { overlay } = loadModules();
    overlay.setOverlayTarget("", "");
    overlay.openPetWindow();
    assert.strictEqual(overlay.petWindowCount(), 0, "deferred, not opened at a blank URL");
  } finally {
    stub.restore();
  }
});

// ── the page URL ────────────────────────────────────────────────────────────

test("the page URL mirrors the file layout, and omits an empty credential", () => {
  const stub = stubElectron();
  try {
    const { pageUrl } = loadModules();
    assert.strictEqual(
      pageUrl.companionPageUrl("http://localhost:5476", "pet.html"),
      "http://localhost:5476/app-windows/crew-companion/pet.html",
    );
    assert.strictEqual(
      pageUrl.companionPageUrl("http://localhost:5476/", "pet.html", "abc"),
      "http://localhost:5476/app-windows/crew-companion/pet.html?token=abc",
    );
  } finally {
    stub.restore();
  }
});

// ── the reconcile rule ──────────────────────────────────────────────────────

test("an inconclusive probe leaves the windows exactly as they are", async () => {
  const stub = stubElectron();
  try {
    const { overlay, index } = loadModules();
    overlay.setOverlayTarget("http://localhost:5476", "cred");
    overlay.openPetWindow();
    const before = overlay.petWindowCount();

    // No gateway is listening, so the probe cannot answer: that is UNKNOWN, and
    // treating it as "disabled" is what makes the companion appear to crash and
    // reappear every few seconds during an ordinary restart.
    index.initCrewCompanion({
      backendUrl: "http://127.0.0.1:9",
      fetchLocalToken: async () => "cred",
      glog: () => {},
    });
    await settle();

    assert.strictEqual(overlay.petWindowCount(), before, "unknown must not tear down");
    index.shutdownCrewCompanion();
  } finally {
    stub.restore();
  }
});

test("an inconclusive probe does not OPEN a companion either", async () => {
  const stub = stubElectron();
  try {
    const { overlay, index } = loadModules();
    overlay.setOverlayTarget("http://localhost:5476", "cred");
    // Nothing open yet, and the probe cannot answer.
    assert.strictEqual(overlay.petWindowCount(), 0);

    index.initCrewCompanion({
      backendUrl: "http://127.0.0.1:9",
      fetchLocalToken: async () => "cred",
      glog: () => {},
    });
    await settle();

    // This is the direction the three-state rule actually guards in this
    // implementation: teardown is already safe because "disabled" is matched
    // explicitly, but falling through on unknown would put a companion on screen
    // for an app nobody has enabled. Verified by reverting: deleting the
    // `state === "unknown"` early return makes this fail with 2 windows.
    assert.strictEqual(
      overlay.petWindowCount(),
      0,
      "unknown must not summon a companion for a possibly-disabled app",
    );
    index.shutdownCrewCompanion();
  } finally {
    stub.restore();
  }
});

test("no credential is unknown, not disabled", async () => {
  const stub = stubElectron();
  try {
    const { overlay, index } = loadModules();
    overlay.setOverlayTarget("http://localhost:5476", "cred");
    overlay.openPetWindow();

    index.initCrewCompanion({
      backendUrl: "http://localhost:5476",
      fetchLocalToken: async () => "", // cannot ask
      glog: () => {},
    });
    await settle();
    assert.strictEqual(overlay.petWindowCount(), 2, "kept, because we could not ask");
    index.shutdownCrewCompanion();
  } finally {
    stub.restore();
  }
});

test("shutdown closes every overlay", async () => {
  const stub = stubElectron();
  try {
    const { overlay, index } = loadModules();
    overlay.setOverlayTarget("http://localhost:5476", "cred");
    overlay.openPetWindow();
    index.initCrewCompanion({
      backendUrl: "http://127.0.0.1:9",
      fetchLocalToken: async () => "cred",
      glog: () => {},
    });
    index.shutdownCrewCompanion();
    assert.strictEqual(overlay.petWindowCount(), 0);
  } finally {
    stub.restore();
  }
});

test("the overlay registers the cursor-hitbox channels the renderer reports to", () => {
  const stub = stubElectron();
  try {
    const { overlay } = loadModules();
    overlay.registerOverlayIpc();

    // The renderer reports the companion's/bubble's rects and the menu's rect; the
    // main process polls the cursor and toggles ignore-mouse itself. Both channels
    // must be listened for or the reports are silent no-ops.
    assert.ok(
      stub.ipcHandlers["crew-companion:update-hitbox"],
      "pet/bubble hitbox channel must be registered",
    );
    assert.ok(
      stub.ipcHandlers["crew-companion:menu-hitbox"],
      "menu hitbox channel must be registered",
    );

    // The removed pointer-toggle round-trip must be gone.
    assert.strictEqual(
      stub.ipcHandlers["crew-companion:interactive"],
      undefined,
      "the pointer-enter/leave toggle was replaced by the hitbox poll",
    );

    overlay.stopHitboxPoll();
  } finally {
    stub.restore();
  }
});
