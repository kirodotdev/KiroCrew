/**
 * panelWindow — the Mochi chat panel window and the pet→panel IPC.
 *
 * These pin the wiring that replaced the broken generic `contextMenuAction`
 * relay: the pet overlay is a SEPARATE window, so pet-menu items that show
 * panel content (Memories, Settings) reach across via named channels that
 * open/focus the panel and tell its renderer which view to show, and Dashboard
 * opens the gateway origin in the system browser rather than inside the panel.
 */

const test = require("node:test");
const assert = require("node:assert");
const path = require("path");
const Module = require("module");

/** Minimal Electron stand-in — enough to observe what the module does. */
function stubElectron() {
  const created = [];
  const ipcHandlers = {};
  const appHandlers = {};
  const openedExternal = [];
  const revealed = [];

  class FakeWebContents {
    constructor() {
      this.events = {};
      this.sent = [];
      this._loading = true; // a freshly-loaded window is loading until did-finish-load
      this.destroyed = false;
      this.crashed = false; // flipped by tests to simulate a dead renderer
      this.reloaded = 0;
    }
    once(name, fn) { this.events[`once:${name}`] = fn; }
    on(name, fn) { this.events[name] = fn; }
    send(channel, ...args) { this.sent.push({ channel, args }); }
    setWindowOpenHandler() {}
    isLoading() { return this._loading; }
    isDestroyed() { return this.destroyed; }
    isCrashed() { return this.crashed; }
    reload() { this.reloaded += 1; this._loading = true; }
    emitOnce(name, ...args) { this.events[`once:${name}`]?.(...args); }
    emit(name, ...args) { this.events[name]?.(...args); }
  }

  class FakeWindow {
    constructor(opts) {
      this.opts = opts;
      this.destroyed = false;
      this.visible = false;
      this.focused = false;
      this.loadedUrl = null;
      this.events = {};
      this._bounds = { x: 100, y: 200, width: 320, height: 470 };
      this.webContents = new FakeWebContents();
      created.push(this);
    }
    loadURL(u) { this.loadedUrl = u; }
    setVisibleOnAllWorkspaces() {}
    setAlwaysOnTop() {}
    once(name, fn) { this.events[name] = fn; }
    on(name, fn) { this.events[name] = fn; }
    show() { this.visible = true; }
    hide() { this.visible = false; }
    focus() { this.focused = true; }
    isVisible() { return this.visible; }
    isDestroyed() { return this.destroyed; }
    destroy() { this.destroyed = true; }
    getBounds() { return { ...this._bounds }; }
    setBounds(b) { this._bounds = { ...this._bounds, ...b }; }
    emit(name, ...args) { this.events[name]?.(...args); }
    // Emit a close event with a real preventDefault so the hidden-singleton
    // interception can be observed.
    emitClose() {
      const e = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
      this.events["close"]?.(e);
      return e;
    }
  }

  return {
    created,
    ipcHandlers,
    appHandlers,
    openedExternal,
    revealed,
    electron: {
      app: { on: (name, fn) => { appHandlers[name] = fn; } },
      BrowserWindow: FakeWindow,
      ipcMain: { on: (channel, fn) => { ipcHandlers[channel] = fn; } },
      screen: {
        getPrimaryDisplay: () => ({ workArea: { x: 0, y: 0, width: 1440, height: 900 } }),
        // set-width consults the display the panel is on so a growing panel can
        // be pulled back inside the work area.
        getDisplayMatching: () => ({ workArea: { x: 0, y: 0, width: 1440, height: 900 } }),
      },
      shell: {
        openExternal: (u) => { openedExternal.push(u); },
        showItemInFolder: (p) => { revealed.push(p); },
      },
    },
  };
}

function loadModule() {
  const stub = stubElectron();
  const modPath = path.join(__dirname, "..", "panelWindow.js");
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

const BASE = "http://localhost:6777";

test("bindPanelIpc registers the pet→panel channels (idempotently)", () => {
  const { mod, ipcHandlers } = loadModule();
  mod.bindPanelIpc(BASE);
  mod.bindPanelIpc(BASE); // second call must not throw or re-bind
  for (const ch of [
    "mochi-pet:open-chat",
    "mochi-pet:open-memories",
    "mochi-panel:open-dashboard",
    "mochi-panel:close",
  ]) {
    assert.strictEqual(typeof ipcHandlers[ch], "function", `missing handler: ${ch}`);
  }
  // Settings is its OWN window (mochi/settingsWindow.js owns this channel). Two
  // listeners would open the window AND flip a panel view.
  assert.strictEqual(ipcHandlers["mochi-pet:open-settings"], undefined);
});

test("the panel is a non-activating panel on macOS only", () => {
  const { mod, ipcHandlers, created } = loadModule();
  mod.bindPanelIpc(BASE);
  ipcHandlers["mochi-pet:open-chat"]();

  assert.strictEqual(created.length, 1, "should create the panel window");
  const { type } = created[0].opts;
  if (process.platform === "darwin") {
    // NSWindowStyleMaskNonactivatingPanel. Without it, focusing the panel
    // activates the app, and the shell's app.on("activate") pulls a
    // deliberately-hidden dashboard window back up -- clicking the pet would
    // reopen the whole app instead of just the chat panel. Asserted on the
    // shipped option because dropping it regresses that SILENTLY.
    assert.strictEqual(type, "panel");
  } else {
    // `type` legal values are per-platform: "panel" is not one of Linux's
    // (desktop/dock/toolbar/splash/notification) nor Windows' (toolbar), and
    // the panel IS reachable off macOS via the toggle shortcut.
    assert.strictEqual(type, undefined);
  }
});

test("open-memories opens the panel and shows the memories view", () => {
  const { mod, ipcHandlers, created } = loadModule();
  mod.bindPanelIpc(BASE);
  ipcHandlers["mochi-pet:open-memories"]();

  assert.strictEqual(created.length, 1, "should create the panel window");
  const win = created[0];
  assert.match(win.loadedUrl, /app-windows\/mochi\/panel\.html$/);
  // Fresh window is still loading, so the view message must wait for load.
  assert.strictEqual(win.webContents.sent.length, 0, "must not send before load");
  win.webContents.emitOnce("did-finish-load");
  assert.deepStrictEqual(
    win.webContents.sent.map((s) => s.channel),
    ["mochi-panel:show-memories"],
    "sends the view channel once the page has loaded",
  );
});

test("the panel never shows a settings view (settings is its own window)", () => {
  const { mod, ipcHandlers, created } = loadModule();
  mod.bindPanelIpc(BASE);
  ipcHandlers["mochi-pet:open-memories"]();
  const win = created[0];
  win.webContents.emitOnce("did-finish-load");
  assert.ok(
    !win.webContents.sent.some((s) => s.channel === "mochi-panel:show-settings"),
    "the in-panel settings overlay was removed; nothing may request it",
  );
});

test("a view switch on an already-loaded panel sends immediately", () => {
  const { mod, ipcHandlers, created } = loadModule();
  mod.bindPanelIpc(BASE);
  // First open + finish loading so the panel persists (it hides, never closes).
  ipcHandlers["mochi-pet:open-memories"]();
  const win = created[0];
  win.webContents.emitOnce("did-finish-load");
  win.webContents._loading = false;
  win.webContents.sent.length = 0;
  win.hide();

  // Re-open: listeners are live, so no did-finish-load is needed this time.
  ipcHandlers["mochi-pet:open-memories"]();
  assert.strictEqual(created.length, 1, "must reuse the hidden panel, not stack a new one");
  assert.deepStrictEqual(
    win.webContents.sent.map((s) => s.channel),
    ["mochi-panel:show-memories"],
  );
});

test("open-dashboard opens the gateway origin in the system browser", () => {
  const { mod, ipcHandlers, openedExternal, created } = loadModule();
  mod.bindPanelIpc(BASE);
  ipcHandlers["mochi-panel:open-dashboard"]();
  assert.deepStrictEqual(openedExternal, [BASE], "must shell.openExternal the gateway origin");
  assert.strictEqual(created.length, 0, "dashboard must NOT open inside a panel window");
});

test("open-dashboard refuses a non-http base url", () => {
  // Defence in depth: main supplies the URL, but never hand a non-web scheme
  // to shell.openExternal.
  const { mod, ipcHandlers, openedExternal } = loadModule();
  mod.bindPanelIpc("file:///etc/passwd");
  ipcHandlers["mochi-panel:open-dashboard"]();
  assert.deepStrictEqual(openedExternal, []);
});

test("showPanelView tolerates a window torn down before the send", () => {
  const { mod, ipcHandlers, created } = loadModule();
  mod.bindPanelIpc(BASE);
  ipcHandlers["mochi-pet:open-memories"]();
  const win = created[0];
  win.destroy();
  assert.doesNotThrow(() => win.webContents.emitOnce("did-finish-load"));
  assert.strictEqual(win.webContents.sent.length, 0);
});

test("a user close HIDES the panel, never destroys it (hidden singleton)", () => {
  // The WS session + chat history must survive a close, so close -> hide.
  const { mod, created } = loadModule();
  const win = mod.openPanelWindow(BASE);
  win.webContents.emit("did-finish-load"); // becomes visible (registered via .on)
  assert.strictEqual(win.isVisible(), true);

  const e = win.emitClose();
  assert.strictEqual(e.defaultPrevented, true, "close must be intercepted");
  assert.strictEqual(win.isDestroyed(), false, "must NOT destroy on a user close");
  assert.strictEqual(win.isVisible(), false, "must hide instead");
});

test("during app quit the close is allowed through (real teardown)", () => {
  const { mod, created, appHandlers } = loadModule();
  const win = mod.openPanelWindow(BASE);
  win.webContents.emit("did-finish-load");
  appHandlers["before-quit"]?.(); // app is quitting now
  const e = win.emitClose();
  assert.strictEqual(e.defaultPrevented, false, "quit must not intercept the close");
});

test("reconcile HIDES the panel on disable and RESTORES it on re-enable", () => {
  const { mod, created } = loadModule();
  const win = mod.openPanelWindow(BASE);
  win.webContents.emit("did-finish-load");
  win._bounds = { x: 300, y: 400, width: 360, height: 500 }; // user moved/resized it
  assert.strictEqual(win.isVisible(), true);

  // Disable: hidden, not destroyed, geometry preserved.
  mod.hidePanelOnDisable();
  assert.strictEqual(win.isVisible(), false);
  assert.strictEqual(win.isDestroyed(), false);

  // Re-enable: same window shown again (bounds survive because it was hidden,
  // not recreated).
  mod.restorePanelOnEnable(BASE);
  assert.strictEqual(created.length, 1, "must reuse the hidden window");
  assert.strictEqual(win.isVisible(), true);
  assert.deepStrictEqual(win.getBounds(), { x: 300, y: 400, width: 360, height: 500 });
});

test("restore is a no-op when the panel was not visible at disable", () => {
  const { mod, created } = loadModule();
  const win = mod.openPanelWindow(BASE);
  win.webContents.emit("did-finish-load");
  win.emitClose(); // user dismissed it (hidden)
  assert.strictEqual(win.isVisible(), false);

  mod.hidePanelOnDisable(); // no-op, panel already hidden
  mod.restorePanelOnEnable(BASE);
  assert.strictEqual(win.isVisible(), false, "must not resurrect a user-dismissed panel");
});

test("repeated disable ticks do not clobber the remembered visibility", () => {
  const { mod } = loadModule();
  const win = mod.openPanelWindow(BASE);
  win.webContents.emit("did-finish-load");
  mod.hidePanelOnDisable(); // records visible=true, hides
  mod.hidePanelOnDisable(); // second tick: no-op, must NOT record false
  mod.restorePanelOnEnable(BASE);
  assert.strictEqual(win.isVisible(), true, "must still restore after repeated ticks");
});

test("set-width changes the width, clamped, keeping height and Y", () => {
  const { mod, ipcHandlers, created } = loadModule();
  mod.bindPanelIpc(BASE);
  const win = mod.openPanelWindow(BASE);
  win._bounds = { x: 300, y: 400, width: 320, height: 470 };

  ipcHandlers["mochi-panel:set-width"](null, 500);
  assert.deepStrictEqual(win.getBounds(), { x: 300, y: 400, width: 500, height: 470 });

  // Clamp: an absurd value is capped, not applied verbatim.
  ipcHandlers["mochi-panel:set-width"](null, 999999);
  assert.strictEqual(win.getBounds().width, 1200, "over-max clamps to the ceiling");
  ipcHandlers["mochi-panel:set-width"](null, 10);
  assert.strictEqual(win.getBounds().width, 260, "under-min clamps to the floor");
  // Y + height untouched throughout. X is NOT asserted here: the 1200px clamp
  // above exceeds the remaining room at x=300 on a 1440 work area, so the
  // edge-fit correctly pulled it left — see the next test for that behaviour.
  assert.deepStrictEqual(
    { y: win.getBounds().y, height: win.getBounds().height },
    { y: 400, height: 470 },
  );
});

test("set-width pulls a right-docked panel back so the new rail stays visible", () => {
  const { mod, ipcHandlers } = loadModule();
  mod.bindPanelIpc(BASE);
  const win = mod.openPanelWindow(BASE);
  // Panel parked at the right edge of a 1440-wide work area (320 + 20 margin).
  win._bounds = { x: 1100, y: 400, width: 320, height: 470 };

  ipcHandlers["mochi-panel:set-width"](null, 600);
  assert.deepStrictEqual(
    win.getBounds(),
    { x: 840, y: 400, width: 600, height: 470 },
    "the panel grows LEFTWARD when it would otherwise run off the display",
  );
});

// ── Crash self-heal (Problem 1) ────────────────────────────────────────────

/** Open the panel and drive it to visible, returning the live window. */
function openVisible(mod) {
  const win = mod.openPanelWindow(BASE);
  win.webContents.emit("did-finish-load"); // registered via .on → becomes visible
  return win;
}

/** Emit render-process-gone on a window's webContents. */
function crash(win, reason = "crashed", exitCode = 133) {
  win.webContents.emit("render-process-gone", {}, { reason, exitCode });
}

test("a crashed renderer (while visible) is torn down and the panel recreated", () => {
  const { mod, created } = loadModule();
  const logs = [];
  mod.setPanelLogger((l) => logs.push(l));
  const win = openVisible(mod);

  crash(win, "crashed", 133);

  assert.strictEqual(win.isDestroyed(), true, "dead window must be destroyed, not kept as a zombie");
  assert.strictEqual(created.length, 2, "panel must be recreated after the crash");
  assert.strictEqual(created[1].isDestroyed(), false, "the recreated window is live");
  assert.match(created[1].loadedUrl, /app-windows\/mochi\/panel\.html$/);
  // The cause is logged (reason + exitCode) so the next run has a breadcrumb.
  assert.ok(logs.some((l) => /renderer gone/.test(l) && /crashed/.test(l) && /133/.test(l)), logs);
});

test("console warnings/errors are forwarded to the gateway log; info is not", () => {
  const { mod } = loadModule();
  const logs = [];
  mod.setPanelLogger((l) => logs.push(l));
  const win = openVisible(mod);

  win.webContents.emit("console-message", {}, 3, "TypeError: boom", 42, "app.js");
  win.webContents.emit("console-message", {}, 1, "just info", 1, "app.js");

  assert.ok(logs.some((l) => /console \[error\]/.test(l) && /boom/.test(l) && /app\.js:42/.test(l)), logs);
  assert.ok(!logs.some((l) => /just info/.test(l)), "info-level messages must be dropped");
});

test("an unresponsive renderer is reloaded, not left frozen", () => {
  const { mod } = loadModule();
  const win = openVisible(mod);
  win.webContents.emit("unresponsive");
  assert.strictEqual(win.webContents.reloaded, 1, "unresponsive must trigger a reload");
  assert.strictEqual(win.isDestroyed(), false, "a hang is not a death — do not destroy");
});

test("our own teardown (clean-exit / quit) never recreates the panel", () => {
  // clean-exit reason: not a fault.
  {
    const { mod, created } = loadModule();
    const win = openVisible(mod);
    crash(win, "clean-exit", 0);
    assert.strictEqual(win.isDestroyed(), true);
    assert.strictEqual(created.length, 1, "clean-exit must not recreate");
  }
  // Actual crash but during app quit: still no recreate.
  {
    const { mod, created, appHandlers } = loadModule();
    const win = openVisible(mod);
    appHandlers["before-quit"]?.();
    crash(win, "crashed", 1);
    assert.strictEqual(created.length, 1, "a crash during quit must not recreate");
  }
});

test("a crash while HIDDEN does not pop the panel back on screen", () => {
  const { mod, created } = loadModule();
  const win = openVisible(mod);
  win.hide(); // user put it away
  crash(win, "crashed", 133);
  assert.strictEqual(win.isDestroyed(), true, "the dead hidden window is still cleaned up");
  assert.strictEqual(created.length, 1, "a hidden panel is recreated on next open, not now");
});

test("the crash-loop guard stops recreating after repeated crashes", () => {
  const { mod, created } = loadModule();
  mod.setPanelLogger(() => {});
  let win = openVisible(mod);
  // MAX_PANEL_CRASHES = 3 recreations, then give up.
  for (let i = 0; i < 3; i++) {
    crash(win, "crashed", 133);
    win = created[created.length - 1];
    win.webContents.emit("did-finish-load"); // make the recreated window visible
  }
  const countBeforeGiveUp = created.length; // 1 original + 3 recreations = 4
  assert.strictEqual(countBeforeGiveUp, 4);
  crash(win, "crashed", 133); // 4th crash within the window
  assert.strictEqual(created.length, 4, "must stop recreating once the crash-loop cap is hit");
});

test("openPanelWindow rebuilds instead of show()-ing a renderer-gone window", () => {
  const { mod, created } = loadModule();
  const win = openVisible(mod);
  // Simulate a dead renderer whose render-process-gone event has NOT yet fired
  // (the window object is still around, isDestroyed() false).
  win.hide();
  win.webContents.crashed = true;

  const reopened = mod.openPanelWindow(BASE);
  assert.notStrictEqual(reopened, win, "must not hand back the renderer-gone window");
  assert.strictEqual(win.isDestroyed(), true, "the stale window is torn down");
  assert.strictEqual(created.length, 2, "a fresh window is built");
});

test("isPanelWindowOpen reports false for a renderer-gone window", () => {
  const { mod } = loadModule();
  const win = openVisible(mod);
  assert.strictEqual(mod.isPanelWindowOpen(), true);
  win.webContents.crashed = true;
  assert.strictEqual(mod.isPanelWindowOpen(), false, "a dead renderer is not 'open'");
});

test("hidePanelWindow hides without arming reconcile restore-intent", () => {
  // A hideAll hide must NOT be undone by the next enabled reconcile tick, so it
  // must not set the wasVisibleBeforeHide flag restorePanelOnEnable keys off.
  const { mod } = loadModule();
  const win = openVisible(mod);
  const wasVisible = mod.hidePanelWindow();
  assert.strictEqual(wasVisible, true);
  assert.strictEqual(win.isVisible(), false);

  mod.restorePanelOnEnable(BASE); // simulates a reconcile tick
  assert.strictEqual(win.isVisible(), false, "reconcile must not resurrect a hideAll-hidden panel");
});

// Both of these take an argument straight from page content, so the guard IS the
// feature: without it, chat content could point the shell at any path on disk or
// launch an arbitrary URL handler.
test("reveal-file only accepts an existing regular file", () => {
  const { mod, ipcHandlers, revealed } = loadModule();
  mod.bindPanelIpc(BASE);
  const reveal = ipcHandlers["mochi-panel:reveal-file"];
  assert.strictEqual(typeof reveal, "function");
  // Nonexistent paths, non-strings and empty strings are dropped silently — a
  // renderer must not learn from the shell whether a path exists.
  assert.doesNotThrow(() => reveal({}, "/definitely/not/a/real/path-xyz"));
  assert.doesNotThrow(() => reveal({}, ""));
  assert.doesNotThrow(() => reveal({}, 42));
  assert.deepStrictEqual(revealed, [], "nothing invalid reached the shell");
  reveal({}, __filename);
  assert.deepStrictEqual(revealed, [__filename], "an existing file is accepted");
});

test("open-external accepts http(s) only", () => {
  const { mod, ipcHandlers, openedExternal } = loadModule();
  mod.bindPanelIpc(BASE);
  const open = ipcHandlers["mochi-panel:open-external"];
  open({}, "https://example.com/a");
  open({}, "http://example.com/b");
  assert.deepStrictEqual(openedExternal, ["https://example.com/a", "http://example.com/b"]);
  // Anything else would turn "open a link" into "launch an arbitrary handler
  // with an attacker-chosen argument".
  for (const bad of ["file:///etc/passwd", "javascript:alert(1)", "mailto:a@b.c", "", 7]) {
    open({}, bad);
  }
  assert.strictEqual(openedExternal.length, 2, "no non-http scheme may reach the shell");
});

/**
 * Widening the panel for a dock must keep it on screen.
 *
 * The handler used to preserve X unconditionally, so opening Pins or the watch
 * list on a panel that already sat near the right edge pushed the new rail past
 * the display. Nothing errors in that state — the rail is simply not visible —
 * so the geometry is pinned here.
 */
test("panelLeftForWidth pulls a growing panel back inside the work area", () => {
  const { mod } = loadModule();
  const wa = { x: 0, y: 0, width: 1440, height: 900 };

  // Fits: X must not move. A panel that jumps left on every dock toggle is its
  // own bug.
  assert.strictEqual(mod.panelLeftForWidth(100, 600, wa), 100);
  // Exactly flush still fits.
  assert.strictEqual(mod.panelLeftForWidth(840, 600, wa), 840);
  // Overflows: pull back so the right edge lands on the work-area edge.
  assert.strictEqual(mod.panelLeftForWidth(1200, 600, wa), 840);
  // Wider than the display: clamp at the left edge rather than going negative.
  assert.strictEqual(mod.panelLeftForWidth(300, 2000, wa), 0);
  // Non-zero work-area origin (secondary display / menu bar offsets).
  assert.strictEqual(mod.panelLeftForWidth(1500, 600, { x: 1000, y: 0, width: 1000, height: 900 }), 1400);
});
