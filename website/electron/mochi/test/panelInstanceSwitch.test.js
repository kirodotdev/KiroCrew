/**
 * Panel behaviour when the pet switches to another instance.
 *
 * Two rules here are load-bearing and both fail SILENTLY if broken — the panel
 * still appears, it is just attached to the wrong gateway, and nothing on screen
 * says so (the panel header deliberately does not name the instance):
 *
 *  1. the panel must be DESTROYED, not hidden — a hidden window is still live, so
 *     the next open would show() it, still rendering the old instance's chat slot;
 *  2. the pet-menu IPC handlers must read the CURRENT origin, not the one they
 *     were first bound with. That binding is one-shot by design, so a captured
 *     origin would send "open chat" to the old instance forever.
 */
const test = require("node:test");
const assert = require("node:assert");
const Module = require("node:module");

/** Load panelWindow.js with electron stubbed, capturing the ipcMain handlers. */
function loadPanelWindow() {
  const handlers = new Map();
  const opened = [];
  const loaded = [];
  const win = {
    destroyed: false,
    visible: true,
    isDestroyed() { return this.destroyed; },
    isVisible() { return this.visible; },
    hide() { this.visible = false; },
    show() { this.visible = true; },
    destroy() { this.destroyed = true; },
    focus() {},
    loadURL(url) { loaded.push(url); },
    on() {}, once() {},
    setBounds() {}, getBounds() { return { x: 0, y: 0, width: 0, height: 0 }; },
    setVisibleOnAllWorkspaces() {}, setAlwaysOnTop() {}, setMenu() {},
    webContents: {
      on() {}, send() {}, setWindowOpenHandler() {}, isDestroyed: () => false,
      isCrashed: () => false, getURL: () => loaded[loaded.length - 1] || "",
    },
  };
  const externals = [];

  const origLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === "electron") {
      return {
        app: { on() {}, getPath: () => "/tmp" },
        screen: {
          getPrimaryDisplay: () => ({ id: 1, workArea: { x: 0, y: 0, width: 1440, height: 900 } }),
          getAllDisplays: () => [{ id: 1, workArea: { x: 0, y: 0, width: 1440, height: 900 } }],
          on() {},
        },
        shell: { openExternal: (u) => externals.push(u) },
        ipcMain: { on: (ch, fn) => handlers.set(ch, fn), handle: (ch, fn) => handlers.set(ch, fn) },
        BrowserWindow: function () { opened.push(win); return win; },
      };
    }
    return origLoad(request, parent, isMain);
  };
  try {
    delete require.cache[require.resolve("../panelWindow")];
    const mod = require("../panelWindow");
    return { mod, handlers, win, externals, loaded };
  } finally {
    Module._load = origLoad;
  }
}

test("dropPanelForInstanceSwitch DESTROYS the panel, so it cannot be reused on the old origin", () => {
  const { mod, win } = loadPanelWindow();
  // Reach the module's panel handle the way the shell does: open it once.
  mod.openPanelWindow("http://localhost:5476");
  assert.strictEqual(win.destroyed, false, "precondition: a live panel");

  mod.dropPanelForInstanceSwitch();
  assert.strictEqual(win.destroyed, true, "a hidden-but-live panel would be reused on show()");
});

test("dropPanelForInstanceSwitch remembers an OPEN panel so it returns on the NEW origin", () => {
  const { mod, win, loaded } = loadPanelWindow();
  mod.openPanelWindow("http://localhost:5476");
  assert.deepStrictEqual(loaded, ["http://localhost:5476/app-windows/mochi/panel.html"]);
  win.visible = true;

  mod.dropPanelForInstanceSwitch();
  mod.restorePanelOnEnable("http://localhost:7778", "tok");

  // The whole point: the reopen must hit the NEW gateway, carrying its token.
  // A hidden-and-shown panel would have loaded nothing new at all.
  assert.strictEqual(loaded.length, 2, "the panel must load again, not just show()");
  assert.strictEqual(loaded[1], "http://localhost:7778/app-windows/mochi/panel.html?token=tok");
});

test("a CLOSED panel stays closed across a switch", () => {
  const { mod, win, loaded } = loadPanelWindow();
  mod.openPanelWindow("http://localhost:5476");
  win.visible = false; // the user had put it away

  mod.dropPanelForInstanceSwitch();
  mod.restorePanelOnEnable("http://localhost:7778", "tok");

  assert.strictEqual(win.destroyed, true);
  assert.strictEqual(loaded.length, 1, "switching must not surface a panel the user had closed");
});

test("dropPanelForInstanceSwitch is safe with no panel at all", () => {
  const { mod } = loadPanelWindow();
  mod.dropPanelForInstanceSwitch(); // must not throw
});

test("setPanelTarget redirects the pet-menu dashboard action to the CURRENT origin", () => {
  const { mod, handlers, externals } = loadPanelWindow();
  mod.bindPanelIpc("http://localhost:5476");

  const openDashboard = handlers.get("mochi-panel:open-dashboard");
  assert.ok(openDashboard, "the dashboard channel must be bound");

  openDashboard();
  assert.deepStrictEqual(externals, ["http://localhost:5476"]);

  // Switch instance. The IPC binding is one-shot, so this is the ONLY way the
  // handler can learn the new origin.
  mod.setPanelTarget("http://localhost:7778", "tok");
  openDashboard();
  assert.deepStrictEqual(
    externals,
    ["http://localhost:5476", "http://localhost:7778"],
    "a captured origin would keep opening the old instance forever",
  );
});
