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
      this.webContents = {
        once: (name, fn) => { this.events[`wc:${name}`] = fn; },
        on: (name, fn) => { this.events[`wc:${name}`] = fn; },
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
    destroy() { this.destroyed = true; }
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
