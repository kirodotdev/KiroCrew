/**
 * settingsWindow — Mochi's Settings window.
 *
 * Settings used to be an overlay rendered inside the 320px chat panel; it is now
 * its own window again, matching the original app. These tests pin the parts that
 * broke on the sibling windows: the geometry we took from the original, the
 * single-instance focus, the EAGER ipc registration (lazy registration is what
 * left the Avatars window unreachable), and that a disable does not orphan an
 * always-on-top form over the desktop.
 */

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");
const Module = require("module");

function stubElectron() {
  const created = [];
  const ipcHandlers = {};

  class FakeWindow {
    constructor(opts) {
      this.opts = opts;
      this.destroyed = false;
      this.visible = false;
      this.focused = false;
      this.loadedUrl = null;
      this.events = {};
      this.wcSent = [];
      this.webContents = {
        once: (name, fn) => { this.events[`wc:${name}`] = fn; },
        on: (name, fn) => { this.events[`wc:${name}`] = fn; },
        send: (channel, ...args) => { this.wcSent.push({ channel, args }); },
      };
      created.push(this);
    }
    loadURL(u) { this.loadedUrl = u; }
    setAlwaysOnTop(flag, level) { this.alwaysOnTopFlag = flag; this.alwaysOnTopLevel = level; }
    once(name, fn) { this.events[name] = fn; }
    on(name, fn) { this.events[name] = fn; }
    show() { this.visible = true; }
    focus() { this.focused = true; }
    isVisible() { return this.visible; }
    isDestroyed() { return this.destroyed; }
    // Real Electron emits `closed` after destroy(); the module's pending-state
    // cleanup hangs off that event, so the fake must be faithful here.
    destroy() { this.destroyed = true; this.events["closed"]?.(); }
    emit(name, ...args) { this.events[name]?.(...args); }
  }

  return {
    created,
    ipcHandlers,
    electron: {
      BrowserWindow: FakeWindow,
      ipcMain: { on: (channel, fn) => { ipcHandlers[channel] = fn; } },
    },
  };
}

function loadModule() {
  const stub = stubElectron();
  const modPath = path.join(__dirname, "..", "settingsWindow.js");
  delete require.cache[require.resolve(modPath)];
  const origLoad = Module._load;
  Module._load = function (request, parent, isMain) {
    if (request === "electron") return stub.electron;
    return origLoad(request, parent, isMain);
  };
  try {
    return { mod: require(modPath), ...stub };
  } finally {
    Module._load = origLoad;
  }
}

test("uses the two-column settings window geometry, opaque and modal-panel", () => {
  const { mod, created } = loadModule();
  mod.openSettingsWindow("http://127.0.0.1:5476");

  assert.strictEqual(created.length, 1);
  const win = created[0];
  // Wider than upstream ON PURPOSE: the panel is a section rail plus a content
  // column, and upstream's 360px floor squeezed the content column until rows
  // clipped mid-word.
  assert.strictEqual(win.opts.width, 580);
  assert.strictEqual(win.opts.height, 620);
  assert.strictEqual(win.opts.minWidth, 480);
  assert.strictEqual(win.opts.minHeight, 420);
  assert.strictEqual(win.opts.center, true);
  // Opaque: a form over the desktop must not be see-through.
  assert.notStrictEqual(win.opts.transparent, true);
  assert.strictEqual(win.opts.backgroundColor, "#1e1e2e");
  assert.strictEqual(win.alwaysOnTopLevel, "modal-panel");
  assert.match(win.loadedUrl, /\/app-windows\/mochi\/settings\.html$/);
});

test("is a singleton: a second open focuses the existing window", () => {
  const { mod, created } = loadModule();
  mod.openSettingsWindow("http://127.0.0.1:5476");
  mod.openSettingsWindow("http://127.0.0.1:5476");

  assert.strictEqual(created.length, 1);
  assert.strictEqual(created[0].focused, true);
});

test("registers its open channel at MODULE LOAD, before any window exists", () => {
  const { ipcHandlers, created } = loadModule();
  // No open call yet — the listener must already be there, otherwise the pet's
  // right-click Settings goes nowhere (the bug the Avatars window had).
  assert.strictEqual(created.length, 0);
  assert.strictEqual(typeof ipcHandlers["mochi-pet:open-settings"], "function");
  assert.strictEqual(typeof ipcHandlers["mochi-settings:close"], "function");
});

test("the open channel works after only setSettingsBaseUrl (no prior open)", () => {
  const { mod, ipcHandlers, created } = loadModule();
  mod.setSettingsBaseUrl("http://127.0.0.1:5476");
  ipcHandlers["mochi-pet:open-settings"]();

  assert.strictEqual(created.length, 1);
  assert.match(created[0].loadedUrl, /\/app-windows\/mochi\/settings\.html$/);
});

test("renderer close request destroys the window", () => {
  const { mod, ipcHandlers, created } = loadModule();
  mod.openSettingsWindow("http://127.0.0.1:5476");
  ipcHandlers["mochi-settings:close"]();

  assert.strictEqual(created[0].destroyed, true);
  assert.strictEqual(mod.isSettingsWindowOpen(), false);
});

test("closeSettingsWindow is safe when nothing was ever opened", () => {
  const { mod } = loadModule();
  mod.closeSettingsWindow();
  assert.strictEqual(mod.isSettingsWindowOpen(), false);
});

test("reveals itself on did-finish-load even if ready-to-show never fires", () => {
  const { mod, created } = loadModule();
  mod.openSettingsWindow("http://127.0.0.1:5476");
  const win = created[0];
  assert.strictEqual(win.visible, false);
  win.events["wc:did-finish-load"]();
  assert.strictEqual(win.visible, true);
});

// ── Native close guard ──────────────────────────────────────────────────────
//
// The Settings renderer stages every control until Save, so the native close
// button must run the same Unsaved Changes guard as the in-panel Cancel. These
// pin the seam: intercept-and-ask instead of destroy, one pending request at a
// time, a sender-bound acknowledgement, and a bounded force-close fallback so
// a wedged renderer can never leave an unclosable always-on-top window.

function fakeCloseEvent() {
  return {
    prevented: false,
    preventDefault() { this.prevented = true; },
  };
}

/** Open the settings window and simulate the renderer finishing its load —
 *  the precondition for the close guard to be armed. */
function openLoadedWindow(mod, created) {
  mod.openSettingsWindow("http://127.0.0.1:5476");
  const win = created[created.length - 1];
  win.events["wc:did-finish-load"]();
  return win;
}

test("a close before the renderer has loaded is NOT intercepted", () => {
  // Pre-load there is no subscriber and nothing staged; a send to a frameless
  // renderer is dropped silently, so intercepting here would stall the close
  // for the full ack timeout. The default (destroy) must proceed instead.
  const { mod, created } = loadModule();
  mod.openSettingsWindow("http://127.0.0.1:5476");
  const win = created[0];

  const event = fakeCloseEvent();
  win.emit("close", event);

  assert.strictEqual(event.prevented, false);
  assert.strictEqual(win.wcSent.length, 0);
});

test("native close asks the renderer instead of destroying", () => {
  const { mod, created } = loadModule();
  const win = openLoadedWindow(mod, created);

  const event = fakeCloseEvent();
  win.emit("close", event);

  assert.strictEqual(event.prevented, true);
  assert.strictEqual(win.destroyed, false);
  assert.deepStrictEqual(
    win.wcSent.map((s) => s.channel),
    ["mochi-settings:close-request"],
  );
});

test("repeated native close clicks do not queue duplicate requests", () => {
  const { mod, created } = loadModule();
  const win = openLoadedWindow(mod, created);

  win.emit("close", fakeCloseEvent());
  const second = fakeCloseEvent();
  win.emit("close", second);

  // Still intercepted (the window must not die), but only ONE request went out.
  assert.strictEqual(second.prevented, true);
  assert.strictEqual(win.wcSent.length, 1);
});

test("an acknowledged request disarms the force-close fallback", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { mod, created, ipcHandlers } = loadModule();
  const win = openLoadedWindow(mod, created);

  win.emit("close", fakeCloseEvent());
  ipcHandlers["mochi-settings:close-request-ack"]({ sender: win.webContents });
  t.mock.timers.tick(mod.CLOSE_ACK_TIMEOUT_MS + 1);

  // The renderer took over (it shows the dialog or closes itself); the shell
  // must not yank the window out from under it.
  assert.strictEqual(win.destroyed, false);
});

test("after an acknowledgement, the next close click sends a fresh request", () => {
  const { mod, created, ipcHandlers } = loadModule();
  const win = openLoadedWindow(mod, created);

  win.emit("close", fakeCloseEvent());
  ipcHandlers["mochi-settings:close-request-ack"]({ sender: win.webContents });
  win.emit("close", fakeCloseEvent());

  assert.strictEqual(win.wcSent.length, 2);
});

test("the acknowledgement is sender-bound: another webContents cannot clear it", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { mod, created, ipcHandlers } = loadModule();
  const win = openLoadedWindow(mod, created);

  win.emit("close", fakeCloseEvent());
  // An ack from some OTHER window's renderer must not disarm the fallback.
  ipcHandlers["mochi-settings:close-request-ack"]({ sender: { not: "ours" } });
  t.mock.timers.tick(mod.CLOSE_ACK_TIMEOUT_MS + 1);

  assert.strictEqual(win.destroyed, true);
});

test("a silent renderer is force-closed after the timeout", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { mod, created } = loadModule();
  const win = openLoadedWindow(mod, created);

  win.emit("close", fakeCloseEvent());
  assert.strictEqual(win.destroyed, false);
  t.mock.timers.tick(mod.CLOSE_ACK_TIMEOUT_MS + 1);

  assert.strictEqual(win.destroyed, true);
  assert.strictEqual(mod.isSettingsWindowOpen(), false);
});

test("a renderer that cannot be reached is force-closed immediately", () => {
  const { mod, created } = loadModule();
  const win = openLoadedWindow(mod, created);
  win.webContents.send = () => {
    throw new Error("webContents destroyed");
  };

  win.emit("close", fakeCloseEvent());

  assert.strictEqual(win.destroyed, true);
  assert.strictEqual(mod.isSettingsWindowOpen(), false);
});

test("the renderer's own close channel still destroys during a pending request", () => {
  // Save/Discard resolve the guard by closing through mochi-settings:close;
  // that path must stay an unconditional destroy, never re-enter the guard.
  const { mod, created, ipcHandlers } = loadModule();
  const win = openLoadedWindow(mod, created);

  win.emit("close", fakeCloseEvent());
  ipcHandlers["mochi-settings:close"]();

  assert.strictEqual(win.destroyed, true);
  assert.strictEqual(mod.isSettingsWindowOpen(), false);
});

test("a pending request from a destroyed window cannot leak into its successor", (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const { mod, created, ipcHandlers } = loadModule();
  const first = openLoadedWindow(mod, created);

  // Request in flight on the first window, then it is torn down explicitly.
  first.emit("close", fakeCloseEvent());
  ipcHandlers["mochi-settings:close"]();

  // A new window's close must send its own request (pending state was cleared).
  const second = openLoadedWindow(mod, created);
  second.emit("close", fakeCloseEvent());
  assert.strictEqual(second.wcSent.length, 1);

  // And the FIRST window's late ack must not disarm the second's fallback.
  ipcHandlers["mochi-settings:close-request-ack"]({ sender: first.webContents });
  t.mock.timers.tick(mod.CLOSE_ACK_TIMEOUT_MS + 1);
  assert.strictEqual(second.destroyed, true);
});
