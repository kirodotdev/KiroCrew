/**
 * avatarWindow — the first-run avatar picker.
 *
 * The window is what makes the choice unavoidable: it opens when Mochi is
 * enabled but no avatar is stored, and the pet stays closed until a choice is
 * saved (the avatar decides the personality, so a pet without one would run its
 * first turns as a generic companion). These tests pin the guards around that.
 */

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");
const Module = require("module");

/** Minimal Electron stand-in — enough to observe what the module does. */
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
        once: (name, fn) => {
          this.events[`wc:${name}`] = fn;
        },
        on: (name, fn) => {
          this.events[`wc:${name}`] = fn;
        },
      };
      created.push(this);
    }
    loadURL(u) {
      this.loadedUrl = u;
    }
    setAlwaysOnTop(flag, level) {
      this.alwaysOnTopFlag = flag;
      this.alwaysOnTopLevel = level;
    }
    once(name, fn) {
      this.events[name] = fn;
    }
    on(name, fn) {
      this.events[name] = fn;
    }
    show() {
      this.visible = true;
    }
    focus() {
      this.focused = true;
    }
    isVisible() {
      return this.visible;
    }
    isDestroyed() {
      return this.destroyed;
    }
    destroy() {
      this.destroyed = true;
    }
    emit(name, ...args) {
      this.events[name]?.(...args);
    }
  }

  return {
    created,
    ipcHandlers,
    electron: {
      BrowserWindow: FakeWindow,
      ipcMain: {
        on: (channel, fn) => {
          ipcHandlers[channel] = fn;
        },
      },
    },
  };
}

/** Load a fresh copy of the module with Electron stubbed. */
function loadModule() {
  const stub = stubElectron();
  const modPath = path.join(__dirname, "..", "avatarWindow.js");
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

test("opens an opaque, centered, gallery-aligned modal-panel window", () => {
  const { mod, created } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:5476");

  assert.strictEqual(created.length, 1);
  const win = created[0];
  const opts = win.opts;
  // Opaque: it holds real UI, unlike the pet overlay.
  assert.notStrictEqual(opts.transparent, true);
  assert.ok(opts.backgroundColor, "needs a background or text sits on the desktop");
  assert.strictEqual(opts.center, true);
  // Geometry aligned to the original gallery window.
  assert.strictEqual(opts.width, 800);
  assert.strictEqual(opts.height, 600);
  assert.strictEqual(opts.minWidth, 600);
  assert.strictEqual(opts.minHeight, 400);
  assert.strictEqual(opts.resizable, true);
  // alwaysOnTop at the modal-panel level, matching galleryWindowManager.ts.
  assert.strictEqual(opts.alwaysOnTop, true);
  assert.strictEqual(win.alwaysOnTopLevel, "modal-panel");
});

test("loads the avatar entry point", () => {
  const { mod, created } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  assert.match(created[0].loadedUrl, /app-windows\/mochi\/avatar\.html$/);
});

test("is a singleton — a second open focuses rather than duplicating", () => {
  const { mod, created } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  mod.openAvatarWindow("http://127.0.0.1:5476");
  assert.strictEqual(created.length, 1, "must not stack pickers");
  assert.strictEqual(created[0].focused, true);
});

test("reveals on did-finish-load, not only ready-to-show", () => {
  // ready-to-show has proven unreliable for the Mochi windows; a picker that
  // never appears blocks the entire first-run flow.
  const { mod, created } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  const win = created[0];
  assert.strictEqual(win.visible, false);
  win.events["wc:did-finish-load"]();
  assert.strictEqual(win.visible, true);
});

test("a saved choice closes the window", () => {
  const { mod, created, ipcHandlers } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  ipcHandlers["mochi-avatar:chosen"]();
  assert.strictEqual(created[0].destroyed, true);
  assert.strictEqual(mod.isAvatarWindowOpen(), false);
});

test("does not re-open after a choice was made", () => {
  // Otherwise the 5s reconcile loop would re-summon it every tick.
  const { mod, created, ipcHandlers } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  ipcHandlers["mochi-avatar:chosen"]();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  assert.strictEqual(created.length, 1);
});

test("disabling Mochi resets the guard so re-enabling asks again", () => {
  const { mod, created, ipcHandlers } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  ipcHandlers["mochi-avatar:chosen"]();
  mod.resetAvatarChoiceGuard();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  assert.strictEqual(created.length, 2, "re-enable with no avatar must ask again");
});

test("a crashed renderer releases the handle for a fresh window", () => {
  const { mod, created } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  created[0].events["wc:render-process-gone"]();
  assert.strictEqual(mod.isAvatarWindowOpen(), false);
  mod.openAvatarWindow("http://127.0.0.1:5476");
  assert.strictEqual(created.length, 2);
});

test("closing when never opened is a no-op", () => {
  const { mod } = loadModule();
  assert.doesNotThrow(() => mod.closeAvatarWindow());
});

test("a user-initiated open reverses an earlier choice", () => {
  // The choiceMade guard exists to stop the 5s reconcile loop re-summoning the
  // window — it must NOT stop the user changing their mind via right-click.
  const { mod, created, ipcHandlers } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  ipcHandlers["mochi-avatar:chosen"]();
  assert.strictEqual(created[0].destroyed, true);

  ipcHandlers["mochi-avatar:open"]();
  assert.strictEqual(created.length, 2, "right-click > Avatars must re-open");
  assert.strictEqual(mod.isAvatarWindowOpen(), true);
});

test("a user-initiated open remembers the base url", () => {
  // The renderer has no business knowing the gateway origin, so the window
  // reuses the one the reconcile loop already gave it.
  const { mod, created, ipcHandlers } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:6777");
  ipcHandlers["mochi-avatar:chosen"]();
  ipcHandlers["mochi-avatar:open"]();
  assert.match(created[1].loadedUrl, /^http:\/\/127\.0\.0\.1:6777\//);
});

test("a dismissed picker is NOT re-opened by the reconcile loop", () => {
  // Without this the 5s loop re-summons it on the next tick, so dismissing the
  // window just makes it flash back — the app pesters the user until they pick.
  const { mod, created } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  created[0].emit("closed"); // user closed it without choosing

  mod.openAvatarWindow("http://127.0.0.1:5476"); // next reconcile tick
  assert.strictEqual(created.length, 1, "reconcile must respect the dismissal");
});

test("a dismissed picker still re-opens on user request", () => {
  const { mod, created, ipcHandlers } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  created[0].emit("closed");

  ipcHandlers["mochi-avatar:open"]();
  assert.strictEqual(created.length, 2, "right-click > Avatars must win");
});

test("disabling the app clears a dismissal too", () => {
  // Re-enabling with no avatar should ask again, even if the picker was
  // dismissed during the previous enable.
  const { mod, created } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  created[0].emit("closed");
  mod.resetAvatarChoiceGuard();

  mod.openAvatarWindow("http://127.0.0.1:5476");
  assert.strictEqual(created.length, 2);
});

test("closing after a saved choice is not treated as a dismissal", () => {
  const { mod, created, ipcHandlers } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  ipcHandlers["mochi-avatar:chosen"]();
  created[0].emit("closed"); // real Electron fires this after destroy()

  // choiceMade already stops re-opening; the point is that a later
  // resetAvatarChoiceGuard (app disabled) still works cleanly.
  mod.resetAvatarChoiceGuard();
  mod.openAvatarWindow("http://127.0.0.1:5476");
  assert.strictEqual(created.length, 2);
});

test("avatar IPC listeners exist before any window was ever opened", () => {
  // Registered EAGERLY at module load. Previously they were bound only inside
  // openAvatarWindow, so with an avatar already set at startup the window was
  // never opened and 'mochi-avatar:open' had no listener — the right-click >
  // Avatars menu silently did nothing.
  const { ipcHandlers } = loadModule();
  assert.strictEqual(typeof ipcHandlers["mochi-avatar:open"], "function");
  assert.strictEqual(typeof ipcHandlers["mochi-avatar:chosen"], "function");
});

test("reconcile does NOT close a user-opened avatar window", () => {
  const { mod, created, ipcHandlers } = loadModule();
  mod.setAvatarBaseUrl("http://127.0.0.1:5476");
  ipcHandlers["mochi-avatar:open"](); // user opened it (right-click > Avatars)
  assert.strictEqual(mod.isAvatarWindowOpen(), true);

  mod.closeAvatarWindowFromReconcile();
  assert.strictEqual(mod.isAvatarWindowOpen(), true, "a user-opened window must survive reconcile");
  assert.strictEqual(created[0].destroyed, false);
});

test("reconcile DOES close a window it opened itself", () => {
  const { mod, created } = loadModule();
  mod.openAvatarWindow("http://127.0.0.1:5476"); // reconcile open (not user-initiated)
  assert.strictEqual(mod.isAvatarWindowOpen(), true);
  mod.closeAvatarWindowFromReconcile();
  assert.strictEqual(mod.isAvatarWindowOpen(), false, "a reconcile-opened window is fair game");
  assert.strictEqual(created[0].destroyed, true);
});

test("setAvatarBaseUrl lets a user open load the right origin with no window yet", () => {
  // The picker was never opened at startup (avatar already set), so lastBaseUrl
  // would be empty; the reconcile loop seeds it each tick.
  const { mod, created, ipcHandlers } = loadModule();
  mod.setAvatarBaseUrl("http://127.0.0.1:6777");
  ipcHandlers["mochi-avatar:open"]();
  assert.match(created[0].loadedUrl, /^http:\/\/127\.0\.0\.1:6777\/app-windows\/mochi\/avatar\.html$/);
});
