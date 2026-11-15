/**
 * The desktop window host: what an app's declaration buys, and nothing more.
 *
 * Driven with an injected Electron stand-in and a canned `/api/apps`, so each
 * tick is called directly. The negative half pins the seam's boundary: a page
 * can reach only its OWN app's declared windows, a sender the host did not
 * create is ignored, a chord another owner holds is never taken, a window is
 * never revealed before its page says ready or while it shows an error
 * document, and a probe that cannot be answered changes nothing.
 */

const test = require("node:test");
const assert = require("node:assert");

const {
  CHANNELS,
  createAppWindowHost,
  desktopWindowPath,
  sanitizeHitRegions,
} = require("../app-window-host");
const { createShortcutReservations } = require("../shell-shortcut-reservations");

const BACKEND = "http://127.0.0.1:5476";

function fakeElectron() {
  let nextId = 1;
  const created = [];
  const ipc = {};
  const registered = new Map();
  const screenEvents = {};
  const display = {
    id: 1,
    bounds: { x: 0, y: 0, width: 1440, height: 900 },
    workArea: { x: 0, y: 25, width: 1440, height: 875 },
  };
  let cursor = { x: 10, y: 10 };

  class FakeWindow {
    constructor(opts) {
      this.opts = opts;
      this.destroyed = false;
      this.visible = false;
      this.ignore = [];
      this.focusable = [];
      this.calls = [];
      this.events = {};
      this.loaded = [];
      const win = this;
      this.webContents = {
        id: nextId++,
        url: "",
        sent: [],
        events: {},
        zoom: 1,
        mainFrame: { main: true },
        on(ev, cb) { this.events[ev] = cb; },
        send(channel, ...args) { this.sent.push([channel, ...args]); },
        getURL() { return this.url; },
        getZoomFactor() { return this.zoom; },
        setWindowOpenHandler(fn) { win.openHandler = fn; },
      };
      created.push(this);
    }
    loadURL(url) { this.loaded.push(url); this.webContents.url = url; }
    setIgnoreMouseEvents(ignore, opts) { this.ignore.push([ignore, opts]); }
    setFocusable(f) { this.focusable.push(f); }
    setContentProtection(v) { this.calls.push(["contentProtection", v]); }
    setVisibleOnAllWorkspaces(v) { this.calls.push(["allWorkspaces", v]); }
    setAlwaysOnTop(v, level) { this.calls.push(["alwaysOnTop", v, level]); }
    showInactive() { this.visible = true; this.calls.push(["showInactive"]); }
    show() { this.visible = true; this.calls.push(["show"]); }
    hide() { this.visible = false; this.calls.push(["hide"]); }
    focus() { this.calls.push(["focus"]); }
    blur() { this.calls.push(["blur"]); }
    isVisible() { return this.visible; }
    isDestroyed() { return this.destroyed; }
    getBounds() { return { x: this.opts.x, y: this.opts.y, width: this.opts.width, height: this.opts.height }; }
    setBounds(b) { Object.assign(this.opts, b); }
    on(ev, cb) { this.events[ev] = cb; }
    destroy() {
      if (this.destroyed) return;
      this.destroyed = true;
      if (this.events.closed) this.events.closed();
    }
  }

  const electron = {
    BrowserWindow: FakeWindow,
    screen: {
      getCursorScreenPoint: () => cursor,
      getDisplayNearestPoint: () => display,
      getPrimaryDisplay: () => display,
      on(ev, cb) { screenEvents[ev] = cb; },
      removeListener(ev) { delete screenEvents[ev]; },
    },
    ipcMain: {
      on(channel, fn) { ipc[channel] = fn; },
      removeListener(channel) { delete ipc[channel]; },
      handle(channel, fn) { ipc[channel] = fn; },
      removeHandler(channel) { delete ipc[channel]; },
    },
    globalShortcut: {
      held: new Set(),
      isRegistered(acc) { return this.held.has(acc) || registered.has(acc); },
      register(acc, fn) { registered.set(acc, fn); return true; },
      unregister(acc) { registered.delete(acc); },
    },
  };
  return {
    electron,
    created,
    ipc,
    registered,
    setCursor(p) { cursor = p; },
  };
}

function appRow(name, desktop, extra = {}) {
  return { name, enabled: true, manifest: { name, desktop }, ...extra };
}

const OVERLAY_APP = {
  windows: [
    { id: "pet", entry: "desktop/pet.mjs", kind: "overlay", openOnEnable: true },
    { id: "panel", entry: "desktop/panel.mjs", kind: "panel" },
  ],
  shortcuts: [{ id: "toggle", accelerator: "CommandOrControl+Alt+P", window: "panel" }],
};

function makeHost(rows, overrides = {}) {
  const fake = fakeElectron();
  const state = { rows, status: 200, fetches: [] };
  const lines = [];
  const host = createAppWindowHost({
    electron: fake.electron,
    backendUrl: BACKEND,
    preloadPath: "/shell/app-window-preload.js",
    fetchGatewayAuth: async () => ({ value: "tok", viaCookie: false }),
    fetchImpl: async (url, init) => {
      state.fetches.push({ url, init });
      if (state.status instanceof Error) throw state.status;
      return { status: state.status, json: async () => state.rows };
    },
    log: (line) => lines.push(line),
    platform: "darwin",
    ...overrides,
  });
  return { host, fake, state, lines };
}

/** A page message as Electron delivers it, from the window's main frame. */
function fromWindow(win, frame) {
  return { sender: win.webContents, senderFrame: frame || win.webContents.mainFrame };
}

async function started(rows, overrides) {
  const ctx = makeHost(rows, overrides);
  await ctx.host.start();
  return ctx;
}

test("an openOnEnable overlay opens hidden, sandboxed, on the host's own route", async (t) => {
  const { host, fake, state } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  assert.match(state.fetches[0].url, /\/api\/apps\?token=tok$/);
  assert.strictEqual(fake.created.length, 1);
  const [win] = fake.created;
  assert.deepStrictEqual(win.loaded, [`${BACKEND}/popout/desktop/pets/pet?token=tok`]);
  assert.strictEqual(win.opts.show, false);
  assert.strictEqual(win.opts.transparent, true);
  assert.strictEqual(win.opts.alwaysOnTop, true);
  assert.deepStrictEqual(
    { x: win.opts.x, y: win.opts.y, width: win.opts.width, height: win.opts.height },
    { x: 0, y: 0, width: 1440, height: 900 },
  );
  const prefs = win.opts.webPreferences;
  assert.strictEqual(prefs.preload, "/shell/app-window-preload.js");
  assert.strictEqual(prefs.contextIsolation, true);
  assert.strictEqual(prefs.sandbox, true);
  assert.strictEqual(prefs.nodeIntegration, false);
  assert.strictEqual(prefs.webviewTag, false);
  assert.deepStrictEqual(prefs.additionalArguments, ["--kc-app-window=pets/pet/overlay/3/"]);
  assert.deepStrictEqual(win.ignore[0], [true, { forward: true }]);
  assert.deepStrictEqual(win.focusable, [false]);
  assert.strictEqual(win.isVisible(), false, "not revealed before the page says ready");
  assert.deepStrictEqual(win.openHandler(), { action: "deny" });
});

test("ready reveals the window; the declared shortcut is registered", async (t) => {
  const { host, fake } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  const [win] = fake.created;
  fake.ipc[CHANNELS.ready](fromWindow(win));
  assert.strictEqual(win.isVisible(), true);
  assert.ok(win.calls.some((c) => c[0] === "showInactive"), "an overlay never takes focus on reveal");
  assert.deepStrictEqual([...fake.registered.keys()], ["CommandOrControl+Alt+P"]);
});

test("a shortcut opens its window and is delivered once the page is ready", async (t) => {
  const { host, fake } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  fake.registered.get("CommandOrControl+Alt+P")();
  const panel = fake.created.find((w) => w.loaded[0].includes("/panel"));
  assert.ok(panel, "the shortcut's window was opened");
  assert.deepStrictEqual(panel.webContents.sent, []);
  fake.ipc[CHANNELS.ready](fromWindow(panel));
  assert.deepStrictEqual(panel.webContents.sent, [[CHANNELS.shortcut, "toggle"]]);
  assert.ok(panel.calls.some((c) => c[0] === "show"));
  fake.registered.get("CommandOrControl+Alt+P")();
  assert.deepStrictEqual(panel.webContents.sent.length, 2);
});

test("disabling the app closes its windows and releases its shortcuts", async (t) => {
  const { host, fake, state, lines } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  state.rows = [appRow("pets", OVERLAY_APP, { enabled: false })];
  await host.reconcileOnce();
  assert.ok(fake.created.every((w) => w.isDestroyed()));
  assert.strictEqual(fake.registered.size, 0);
  assert.strictEqual(host._records.size, 0);
  assert.ok(lines.some((l) => l === "app-window-host: pets/pet: opened"));
});

test("a changed declaration replaces the window it changed", async (t) => {
  const { host, fake, state } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  const next = JSON.parse(JSON.stringify(OVERLAY_APP));
  next.windows[0].entry = "desktop/pet2.mjs";
  state.rows = [appRow("pets", next)];
  await host.reconcileOnce();
  assert.strictEqual(fake.created.length, 2);
  assert.ok(fake.created[0].isDestroyed());
  assert.ok(!fake.created[1].isDestroyed());
});

test("a probe that cannot be answered leaves every window and shortcut as it is", async (t) => {
  const { host, fake, state } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  for (const status of [500, new Error("down")]) {
    state.status = status;
    await host.reconcileOnce();
    assert.ok(!fake.created[0].isDestroyed());
    assert.strictEqual(fake.registered.size, 1);
  }
  state.status = 200;
  state.rows = { unexpected: true };
  await host.reconcileOnce();
  assert.ok(!fake.created[0].isDestroyed());
});

test("a refused credential is re-resolved once, then left as unknown", async (t) => {
  let mints = 0;
  const { host, fake, state } = makeHost([appRow("pets", OVERLAY_APP)], {
    fetchGatewayAuth: async () => ({ value: `tok${++mints}`, viaCookie: false }),
  });
  t.after(() => host.stop());
  state.status = 401;
  await host.start();
  assert.strictEqual(fake.created.length, 0);
  assert.ok(state.fetches.length >= 2);
  assert.ok(mints >= 2);
});

test("a cookie credential loads the page bare and probes with the cookie", async (t) => {
  const { host, fake, state } = await started([appRow("pets", OVERLAY_APP)], {
    fetchGatewayAuth: async () => ({ value: "sess", viaCookie: true }),
    cookieHeader: (_url, value) => `mc_token_5476=${value}`,
  });
  t.after(() => host.stop());
  assert.strictEqual(state.fetches[0].url, `${BACKEND}/api/apps`);
  assert.deepStrictEqual(state.fetches[0].init.headers, { Cookie: "mc_token_5476=sess" });
  assert.deepStrictEqual(fake.created[0].loaded, [`${BACKEND}/popout/desktop/pets/pet`]);
});

// ── The boundary ──────────────────────────────────────────────────────────────

test("NEGATIVE: a page cannot open another app's window, or one its app does not declare", async (t) => {
  const other = { windows: [{ id: "spy", entry: "spy.mjs", kind: "overlay" }] };
  const { host, fake } = await started([appRow("pets", OVERLAY_APP), appRow("other", other)]);
  t.after(() => host.stop());
  const [pet] = fake.created;
  const before = fake.created.length;
  fake.ipc[CHANNELS.open](fromWindow(pet), "spy");
  fake.ipc[CHANNELS.open](fromWindow(pet), "other/spy");
  fake.ipc[CHANNELS.open](fromWindow(pet), "settings");
  fake.ipc[CHANNELS.open](fromWindow(pet), { id: "panel" });
  assert.strictEqual(fake.created.length, before);
  fake.ipc[CHANNELS.open](fromWindow(pet), "panel");
  assert.strictEqual(fake.created.length, before + 1);
  assert.match(fake.created.at(-1).loaded[0], /\/popout\/desktop\/pets\/panel\?/);
});

test("NEGATIVE: a sender the host did not create is ignored on every channel", async (t) => {
  const { host, fake } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  const [pet] = fake.created;
  const stranger = new fake.electron.BrowserWindow({ x: 0, y: 0, width: 1, height: 1 });
  stranger.webContents.url = `${BACKEND}${desktopWindowPath("pets", "pet")}`;
  const count = fake.created.length;
  for (const channel of Object.values(CHANNELS)) {
    if (fake.ipc[channel]) fake.ipc[channel](fromWindow(stranger), "panel");
  }
  assert.strictEqual(fake.created.length, count);
  assert.strictEqual(pet.isVisible(), false);
  assert.strictEqual(pet.isDestroyed(), false);
  assert.deepStrictEqual(stranger.calls, []);
});

test("NEGATIVE: a subframe, or a page that left its document, speaks for nobody", async (t) => {
  const { host, fake } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  const [pet] = fake.created;
  fake.ipc[CHANNELS.ready](fromWindow(pet, { main: false }));
  assert.strictEqual(pet.isVisible(), false);
  pet.webContents.url = "https://elsewhere.example/popout/desktop/pets/pet";
  fake.ipc[CHANNELS.ready](fromWindow(pet));
  assert.strictEqual(pet.isVisible(), false);
  pet.webContents.url = `${BACKEND}/popout/desktop/pets/panel`;
  fake.ipc[CHANNELS.ready](fromWindow(pet));
  assert.strictEqual(pet.isVisible(), false);
});

test("NEGATIVE: navigation away from the window's own document is refused", async (t) => {
  const { host, fake } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  const wc = fake.created[0].webContents;
  const attempt = (url) => {
    let prevented = false;
    wc.events["will-navigate"]({ preventDefault: () => { prevented = true; } }, url);
    return prevented;
  };
  assert.strictEqual(attempt(`${BACKEND}/`), true);
  assert.strictEqual(attempt(`${BACKEND}/popout/desktop/other/spy`), true);
  assert.strictEqual(attempt("https://evil.example/popout/desktop/pets/pet"), true);
  assert.strictEqual(attempt(`${BACKEND}/popout/desktop/pets/pet?x=1`), false);
  let webview = false;
  wc.events["will-attach-webview"]({ preventDefault: () => { webview = true; } });
  assert.strictEqual(webview, true);
});

test("NEGATIVE: a chord another owner holds is never taken, and is reported once", async (t) => {
  const fakeHeld = new Set(["CommandOrControl+Alt+P"]);
  const ctx = makeHost([appRow("pets", OVERLAY_APP)]);
  ctx.fake.electron.globalShortcut.held = fakeHeld;
  t.after(() => ctx.host.stop());
  await ctx.host.start();
  await ctx.host.reconcileOnce();
  assert.strictEqual(ctx.fake.registered.size, 0);
  assert.deepStrictEqual(
    ctx.lines.filter((l) => l.includes("unavailable")),
    ["app-window-host: pets: shortcut toggle unavailable"],
  );
});

test("NEGATIVE: a chord a shell owner reserves is never taken while that owner has it released", async (t) => {
  // The owner is configured for the chord but does not hold it right now (a
  // disabled built-in, or one between unregister and register on a rebind).
  const reservations = createShortcutReservations({ platform: "darwin" });
  reservations.reserve("shell-owner", ["Alt+CmdOrCtrl+p"]);
  const { host, fake, lines } = await started([appRow("pets", OVERLAY_APP)], { reservations });
  t.after(() => host.stop());
  await host.reconcileOnce();
  assert.strictEqual(fake.registered.size, 0);
  assert.deepStrictEqual(
    lines.filter((l) => l.includes("unavailable")),
    ["app-window-host: pets: shortcut toggle unavailable"],
  );
});

test("NEGATIVE: reserving a chord an app holds hands it back before the owner registers", async (t) => {
  const reservations = createShortcutReservations({ platform: "darwin" });
  const { host, fake } = await started([appRow("pets", OVERLAY_APP)], { reservations });
  t.after(() => host.stop());
  assert.deepStrictEqual([...fake.registered.keys()], ["CommandOrControl+Alt+P"]);
  // What a shell owner does on enable or rebind: reserve, then register at once,
  // with no host tick in between.
  reservations.reserve("shell-owner", ["CommandOrControl+Alt+P"]);
  const { globalShortcut } = fake.electron;
  assert.strictEqual(globalShortcut.isRegistered("CommandOrControl+Alt+P"), false);
  assert.strictEqual(globalShortcut.register("CommandOrControl+Alt+P", () => {}), true);
  // Once the owner moves off the chord the app gets it on the next tick.
  globalShortcut.unregister("CommandOrControl+Alt+P");
  reservations.reserve("shell-owner", []);
  await host.reconcileOnce();
  assert.deepStrictEqual([...fake.registered.keys()], ["CommandOrControl+Alt+P"]);
});

test("NEGATIVE: one app cannot take a chord another app already holds", async (t) => {
  const twin = {
    windows: [{ id: "w", entry: "w.mjs", kind: "panel" }],
    shortcuts: [{ id: "steal", accelerator: "Alt+CmdOrCtrl+p", window: "w" }],
  };
  const { host, fake } = await started([appRow("pets", OVERLAY_APP), appRow("twin", twin)]);
  t.after(() => host.stop());
  assert.deepStrictEqual([...fake.registered.keys()], ["CommandOrControl+Alt+P"]);
  fake.registered.get("CommandOrControl+Alt+P")();
  assert.ok(fake.created.some((w) => w.loaded[0].includes("/pets/panel")));
  assert.ok(!fake.created.some((w) => w.loaded[0].includes("/twin/")));
});

test("NEGATIVE: an app enabled later never takes a chord an earlier app holds", async (t) => {
  const twin = {
    windows: [{ id: "w", entry: "w.mjs", kind: "panel" }],
    shortcuts: [{ id: "steal", accelerator: "Alt+CmdOrCtrl+p", window: "w" }],
  };
  const { host, fake, state } = await started([appRow("zeta", OVERLAY_APP)]);
  t.after(() => host.stop());
  // `/api/apps` is name-sorted, so the newly enabled app comes first.
  state.rows = [appRow("alpha", twin), appRow("zeta", OVERLAY_APP)];
  await host.reconcileOnce();
  assert.deepStrictEqual([...fake.registered.keys()], ["CommandOrControl+Alt+P"]);
  fake.registered.get("CommandOrControl+Alt+P")();
  assert.ok(fake.created.some((w) => w.loaded[0].includes("/zeta/panel")));
  assert.ok(!fake.created.some((w) => w.loaded[0].includes("/alpha/")));
  // Once the holder lets the chord go, the other declaration gets it.
  state.rows = [appRow("alpha", twin)];
  await host.reconcileOnce();
  assert.deepStrictEqual([...fake.registered.keys()], ["Alt+CmdOrCtrl+p"]);
  fake.registered.get("Alt+CmdOrCtrl+p")();
  assert.ok(fake.created.some((w) => w.loaded[0].includes("/alpha/w")));
});

test("NEGATIVE: hit regions shape an overlay only, and a malformed list clicks through", async (t) => {
  const { host, fake } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  const [pet] = fake.created;
  fake.ipc[CHANNELS.ready](fromWindow(pet));
  fake.ipc[CHANNELS.hitRegions](fromWindow(pet), [{ x: 0, y: 0, width: 50, height: 50 }]);
  fake.setCursor({ x: 10, y: 10 });
  host._pollHitRegionsOnce();
  assert.strictEqual(pet.ignore.at(-1)[0], false);
  fake.setCursor({ x: 500, y: 500 });
  host._pollHitRegionsOnce();
  assert.deepStrictEqual(pet.ignore.at(-1), [true, { forward: true }]);
  fake.setCursor({ x: 10, y: 10 });
  fake.ipc[CHANNELS.hitRegions](fromWindow(pet), [{ x: 0, y: 0, width: Infinity, height: 1 }]);
  host._pollHitRegionsOnce();
  assert.deepStrictEqual(pet.ignore.at(-1), [true, { forward: true }]);

  fake.ipc[CHANNELS.open](fromWindow(pet), "panel");
  const panel = fake.created.at(-1);
  fake.ipc[CHANNELS.ready](fromWindow(panel));
  fake.ipc[CHANNELS.hitRegions](fromWindow(panel), [{ x: 0, y: 0, width: 50, height: 50 }]);
  fake.ipc[CHANNELS.focusable](fromWindow(panel), true);
  assert.deepStrictEqual(panel.ignore, []);
  assert.deepStrictEqual(panel.focusable, []);
});

test("hit regions are read in page pixels at the page's zoom", async (t) => {
  const { host, fake } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  const [pet] = fake.created;
  pet.webContents.zoom = 2;
  fake.ipc[CHANNELS.ready](fromWindow(pet));
  fake.ipc[CHANNELS.hitRegions](fromWindow(pet), [{ x: 100, y: 100, width: 10, height: 10 }]);
  fake.setCursor({ x: 210, y: 210 });
  host._pollHitRegionsOnce();
  assert.strictEqual(pet.ignore.at(-1)[0], false);
});

test("blanking an overlay stops the hit-region poll it no longer needs", async (t) => {
  const { host, fake } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  const started_ = [];
  const cleared = [];
  const realSetInterval = globalThis.setInterval;
  const realClearInterval = globalThis.clearInterval;
  t.mock.method(globalThis, "setInterval", (...args) => {
    const handle = realSetInterval(...args);
    started_.push(handle);
    return handle;
  });
  t.mock.method(globalThis, "clearInterval", (handle) => {
    cleared.push(handle);
    return realClearInterval(handle);
  });
  const [pet] = fake.created;
  fake.ipc[CHANNELS.ready](fromWindow(pet));
  fake.ipc[CHANNELS.hitRegions](fromWindow(pet), [{ x: 0, y: 0, width: 50, height: 50 }]);
  assert.strictEqual(started_.length, 1, "the poll starts once the overlay has regions");

  pet.webContents.events["did-navigate"]({}, pet.loaded[0], 403);

  assert.strictEqual(pet.isVisible(), false);
  assert.deepStrictEqual(cleared, started_, "the blanked overlay's poll is cleared");
});

test("NEGATIVE: an error document is never revealed, and a later tick reloads it", async (t) => {
  const { host, fake } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  const [pet] = fake.created;
  pet.webContents.events["did-navigate"]({}, pet.loaded[0], 403);
  fake.ipc[CHANNELS.ready](fromWindow(pet));
  assert.strictEqual(pet.isVisible(), false);
  await host.reconcileOnce();
  assert.strictEqual(pet.loaded.length, 2);
  pet.webContents.events["did-navigate"]({}, pet.loaded[1], 200);
  fake.ipc[CHANNELS.ready](fromWindow(pet));
  assert.strictEqual(pet.isVisible(), true);
});

test("close and a crashed page drop the window; the next tick reopens an openOnEnable one", async (t) => {
  const { host, fake } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  fake.ipc[CHANNELS.close](fromWindow(fake.created[0]));
  assert.ok(fake.created[0].isDestroyed());
  await host.reconcileOnce();
  assert.strictEqual(fake.created.length, 2);
  fake.created[1].webContents.events["render-process-gone"]();
  assert.ok(fake.created[1].isDestroyed());
  assert.strictEqual(host._records.size, 0);
});

test("suspend closes everything and releases shortcuts; resume reconciles again", async (t) => {
  const { host, fake } = await started([appRow("pets", OVERLAY_APP)]);
  t.after(() => host.stop());
  host.suspend();
  assert.ok(fake.created[0].isDestroyed());
  assert.strictEqual(fake.registered.size, 0);
  await host.reconcileOnce();
  assert.strictEqual(fake.created.length, 1, "a suspended host opens nothing");
  await host.resume();
  assert.strictEqual(fake.created.length, 2);
  assert.strictEqual(fake.registered.size, 1);
});

test("stop releases every shortcut and handler it added", async () => {
  const { host, fake } = await started([appRow("pets", OVERLAY_APP)]);
  host.stop();
  assert.strictEqual(fake.registered.size, 0);
  assert.deepStrictEqual(Object.keys(fake.ipc), []);
  assert.ok(fake.created.every((w) => w.isDestroyed()));
});

test("a panel is placed inside the work area of the display under the cursor", async (t) => {
  const panelOnly = {
    windows: [
      { id: "tr", entry: "a.mjs", kind: "panel", width: 300, height: 200, position: "top-right", openOnEnable: true },
      { id: "c", entry: "b.mjs", kind: "panel", width: 300, height: 200, position: "center", openOnEnable: true },
    ],
  };
  const { host, fake } = await started([appRow("cards", panelOnly)]);
  t.after(() => host.stop());
  const [tr, c] = fake.created;
  assert.deepStrictEqual(
    { x: tr.opts.x, y: tr.opts.y, width: tr.opts.width, height: tr.opts.height },
    { x: 1124, y: 41, width: 300, height: 200 },
  );
  assert.deepStrictEqual({ x: c.opts.x, y: c.opts.y }, { x: 570, y: 363 });
  assert.strictEqual(tr.opts.transparent, false);
  assert.ok(tr.calls.some((call) => call[0] === "alwaysOnTop" && call[2] === "floating"));
  fake.ipc[CHANNELS.ready](fromWindow(tr));
  assert.ok(tr.calls.some((call) => call[0] === "showInactive"), "openOnEnable takes no focus");
  assert.ok(!tr.calls.some((call) => call[0] === "show"));
});

test("sanitizeHitRegions keeps finite boxes only and caps the count", () => {
  assert.deepStrictEqual(sanitizeHitRegions([{ x: 1, y: 2, width: 3, height: 4, extra: 9 }]), [
    { x: 1, y: 2, width: 3, height: 4 },
  ]);
  assert.strictEqual(sanitizeHitRegions([{ x: 1, y: 2, width: -1, height: 4 }]), null);
  assert.strictEqual(sanitizeHitRegions([{ x: "1", y: 2, width: 3, height: 4 }]), null);
  assert.strictEqual(sanitizeHitRegions(Array.from({ length: 17 }, () => ({ x: 0, y: 0, width: 1, height: 1 }))), null);
  assert.strictEqual(sanitizeHitRegions("all"), null);
});

// ── Capabilities: declared AND approved, or refused ──────────────────────────

const PANEL_APP = { windows: [{ id: "panel", entry: "desktop/panel.mjs", kind: "panel", openOnEnable: true }] };

/** A row declaring *declared* capabilities, of which the owner approved *approved*. */
function capRow(name, declared, approved, desktop = PANEL_APP) {
  return appRow(name, desktop, {
    manifest: { name, desktop, permissions: { desktopCapabilities: declared } },
    ...(approved ? { desktopCapabilitiesGrant: { approved, staged: declared.filter((c) => !approved.includes(c)) } } : {}),
  });
}

function fakeLocalFiles(files) {
  const calls = [];
  return {
    calls,
    deps: {
      fs: {
        statSync: (p) => {
          if (!(p in files)) throw new Error("ENOENT");
          return { isFile: () => files[p] === "file" };
        },
        realpathSync: (p) => {
          if (!(p in files)) throw new Error("ENOENT");
          return p;
        },
      },
      path: require("path"),
      shell: { showItemInFolder: (p) => calls.push(["reveal", p]) },
      openPathHardened: async (_shell, p) => {
        calls.push(["open", p]);
        return "";
      },
    },
  };
}

test("NEGATIVE: a capability the app does not declare, or the owner did not approve, is refused", async (t) => {
  const captured = [];
  const files = fakeLocalFiles({ "/tmp/a.png": "file" });
  const { host, fake } = await started(
    [
      appRow("plain", PANEL_APP),
      capRow("staged", ["localFiles", "screenCapture", "gatewayChoice"], []),
      // A gateway that predates the grant publishes no approval at all.
      capRow("older", ["localFiles", "screenCapture", "gatewayChoice"], null),
    ],
    { registerCaptureSurface: (wc) => captured.push(wc.id), localFiles: files.deps },
  );
  t.after(() => host.stop());
  assert.strictEqual(fake.created.length, 3);
  for (const win of fake.created) {
    assert.match(win.opts.webPreferences.additionalArguments[0], /\/3\/$/, "opened with no capability");
    fake.ipc[CHANNELS.revealFile](fromWindow(win), "/tmp/a.png");
    assert.strictEqual(await fake.ipc[CHANNELS.openImage](fromWindow(win), "/tmp/a.png"), false);
    assert.strictEqual(await fake.ipc[CHANNELS.listGateways](fromWindow(win)), null);
    assert.deepStrictEqual(await fake.ipc[CHANNELS.chooseGateway](fromWindow(win), "crew"), { ok: false });
  }
  assert.deepStrictEqual(captured, [], "no window may be granted a screen");
  assert.deepStrictEqual(files.calls, []);
});

test("localFiles: reveal and open-image run the shared rules, for that app's windows only", async (t) => {
  const files = fakeLocalFiles({ "/tmp/a.png": "file", "/tmp/notes.txt": "file", "/tmp/dir": "dir" });
  const { host, fake } = await started(
    [capRow("files", ["localFiles"], ["localFiles"])],
    { localFiles: files.deps },
  );
  t.after(() => host.stop());
  const [win] = fake.created;
  assert.match(win.opts.webPreferences.additionalArguments[0], /\/3\/localFiles$/);
  fake.ipc[CHANNELS.revealFile](fromWindow(win), "/tmp/notes.txt");
  fake.ipc[CHANNELS.revealFile](fromWindow(win), "/tmp/dir");
  fake.ipc[CHANNELS.revealFile](fromWindow(win), 42);
  assert.strictEqual(await fake.ipc[CHANNELS.openImage](fromWindow(win), "/tmp/a.png"), true);
  assert.strictEqual(await fake.ipc[CHANNELS.openImage](fromWindow(win), "/tmp/notes.txt"), false, "not an image");
  assert.strictEqual(await fake.ipc[CHANNELS.openImage](fromWindow(win), "/tmp/gone.png"), false);
  assert.deepStrictEqual(files.calls, [["reveal", "/tmp/notes.txt"], ["open", "/tmp/a.png"]]);
  // A sender the host did not create gets nothing, whatever it asks.
  const stranger = { sender: { id: 999, getURL: () => win.webContents.url, mainFrame: {} } };
  fake.ipc[CHANNELS.revealFile](stranger, "/tmp/notes.txt");
  assert.strictEqual(await fake.ipc[CHANNELS.openImage](stranger, "/tmp/a.png"), false);
  assert.strictEqual(files.calls.length, 2);
});

test("screenCapture: only a window of an app holding it is registered as a capture surface", async (t) => {
  const captured = [];
  const { host, fake } = await started(
    [capRow("snips", ["screenCapture"], ["screenCapture"]), capRow("other", ["localFiles"], ["localFiles"])],
    { registerCaptureSurface: (wc, url) => captured.push([wc.id, url]) },
  );
  t.after(() => host.stop());
  const [snips] = fake.created;
  assert.deepStrictEqual(captured, [[snips.webContents.id, BACKEND]]);
});

test("withdrawing an approved capability closes the window opened with it", async (t) => {
  const { host, fake, state } = await started([capRow("files", ["localFiles"], ["localFiles"])]);
  t.after(() => host.stop());
  const [first] = fake.created;
  state.rows = [capRow("files", ["localFiles"], [])];
  await host.reconcileOnce();
  assert.strictEqual(first.isDestroyed(), true);
  const [, second] = fake.created;
  assert.match(second.opts.webPreferences.additionalArguments[0], /\/3\/$/, "reopened without it");
});

/** A router over the host gateway, its instance registry and one remote crew. */
function crewNetwork(state, crew) {
  return async (url, init = {}) => {
    state.fetches.push({ url, init });
    const u = new URL(url);
    const reply = (status, body) => ({ status, json: async () => body });
    if (u.origin === BACKEND && u.pathname === "/api/apps") return reply(200, state.rows);
    if (u.origin === BACKEND && u.pathname === "/api/instances") {
      if (crew.list instanceof Error) throw crew.list;
      return reply(200, { active: true, instances: crew.instances });
    }
    if (u.origin === BACKEND && u.pathname === "/api/instances/crew/connect" && init.method === "POST") {
      return reply(200, { state: "connected", local_port: 7778, token: crew.token });
    }
    if (u.origin === "http://localhost:7778" && u.pathname === "/api/apps") {
      if (crew.remote instanceof Error) throw crew.remote;
      const desktop = crew.desktop || PANEL_APP;
      // The app there declares and approves `crew.caps` (gatewayChoice unless a test says otherwise).
      const caps = crew.caps || ["gatewayChoice"];
      const declaredThere = crew.declared || caps;
      return reply(200, [{
        name: "pets",
        enabled: crew.enabled,
        manifest: { name: "pets", desktop, permissions: { desktopCapabilities: declaredThere } },
        desktopCapabilitiesGrant: { approved: caps, staged: declaredThere.filter((c) => !caps.includes(c)) },
      }]);
    }
    return reply(404, null);
  };
}

const CONNECTED = { id: "crew", name: "Build box", ssh_host: "secret.example", local_port: 7778, status: { state: "connected" } };

test("gatewayChoice: a chosen crew where the app is on gets the app's windows, and back again", async (t) => {
  const crew = { instances: [CONNECTED], token: "remote-tok", enabled: true };
  const ctx = makeHost([capRow("pets", ["gatewayChoice"], ["gatewayChoice"])]);
  const state = ctx.state;
  const fake = ctx.fake;
  const host = createAppWindowHost({
    electron: fake.electron,
    backendUrl: BACKEND,
    preloadPath: "/shell/app-window-preload.js",
    fetchGatewayAuth: async () => ({ value: "tok", viaCookie: false }),
    fetchImpl: crewNetwork(state, crew),
    log: () => {},
    platform: "darwin",
  });
  await host.start();
  t.after(() => host.stop());
  const [local] = fake.created;
  assert.deepStrictEqual(local.loaded, [`${BACKEND}/popout/desktop/pets/panel?token=tok`]);

  const listed = await fake.ipc[CHANNELS.listGateways](fromWindow(local));
  assert.deepStrictEqual(listed, {
    state: "ready",
    gateways: [{ id: "crew", name: "Build box", state: "connected", connected: true, appEnabled: true }],
    chosen: "self",
    showing: "self",
  });
  assert.ok(!JSON.stringify(listed).includes("secret.example"), "no host name reaches the page");

  const moved = await fake.ipc[CHANNELS.chooseGateway](fromWindow(local), "crew");
  assert.deepStrictEqual(moved, { ok: true, chosen: "crew", showing: "crew" });
  assert.strictEqual(local.isDestroyed(), true);
  const remote = fake.created[1];
  assert.deepStrictEqual(remote.loaded, ["http://localhost:7778/popout/desktop/pets/panel?token=remote-tok"]);
  // The remote window speaks for itself only while it holds the remote's own document.
  remote.webContents.url = remote.loaded[0];
  fake.ipc[CHANNELS.ready](fromWindow(remote));
  assert.strictEqual(remote.isVisible(), true);

  const back = await fake.ipc[CHANNELS.chooseGateway](fromWindow(remote), "self");
  assert.deepStrictEqual(back, { ok: true, chosen: "self", showing: "self" });
  assert.deepStrictEqual(fake.created[2].loaded, [`${BACKEND}/popout/desktop/pets/panel?token=tok`]);
});

test("gatewayChoice: a crew's window is served only what the app holds on BOTH gateways", async (t) => {
  const ALL = ["localFiles", "screenCapture", "gatewayChoice"];
  // The crew's copy declares all three but its owner approved only gatewayChoice.
  const crew = { instances: [CONNECTED], token: "remote-tok", enabled: true, declared: ALL, caps: ["gatewayChoice"] };
  const store = new Map([["appWindowGatewayChoices", { pets: "crew" }]]);
  const captured = [];
  const files = fakeLocalFiles({ "/tmp/a.png": "file" });
  const ctx = makeHost([capRow("pets", ALL, ALL)]);
  const host = createAppWindowHost({
    electron: ctx.fake.electron,
    backendUrl: BACKEND,
    preloadPath: "/shell/app-window-preload.js",
    fetchGatewayAuth: async () => ({ value: "tok", viaCookie: false }),
    fetchImpl: crewNetwork(ctx.state, crew),
    gatewayStore: { get: (k) => store.get(k), set: (k, v) => store.set(k, v) },
    registerCaptureSurface: (wc) => captured.push(wc.id),
    localFiles: files.deps,
    log: () => {},
    platform: "darwin",
  });
  await host.start();
  t.after(() => host.stop());
  const fake = ctx.fake;
  const [remote] = fake.created;
  assert.match(remote.loaded[0], /^http:\/\/localhost:7778\//);
  remote.webContents.url = remote.loaded[0];
  assert.match(remote.opts.webPreferences.additionalArguments[0], /\/3\/gatewayChoice$/, "only what the crew approved");
  assert.deepStrictEqual(captured, [], "the crew's bundle is granted no screen");
  fake.ipc[CHANNELS.revealFile](fromWindow(remote), "/tmp/a.png");
  assert.strictEqual(await fake.ipc[CHANNELS.openImage](fromWindow(remote), "/tmp/a.png"), false);
  assert.deepStrictEqual(files.calls, []);

  // The crew's owner approves localFiles there: the window reopens holding it.
  crew.caps = ["gatewayChoice", "localFiles"];
  host._targets.get("pets").at = 0;
  await host.reconcileOnce();
  assert.strictEqual(remote.isDestroyed(), true);
  const widened = fake.created[1];
  assert.match(widened.opts.webPreferences.additionalArguments[0], /\/3\/localFiles,gatewayChoice$/);

  // ...and withdraws it again: the window opened with it is closed.
  crew.caps = ["gatewayChoice"];
  host._targets.get("pets").at = 0;
  await host.reconcileOnce();
  assert.strictEqual(widened.isDestroyed(), true);
  assert.match(fake.created[2].opts.webPreferences.additionalArguments[0], /\/3\/gatewayChoice$/);
  assert.deepStrictEqual(captured, []);
});

test("gatewayChoice: a crew that has not approved gatewayChoice never takes the windows, so they can always come back", async (t) => {
  // The app is on there, but its owner there approved nothing (an older build publishes no grant).
  const crew = { instances: [CONNECTED], token: "remote-tok", enabled: true, declared: ["gatewayChoice"], caps: [] };
  const store = new Map([["appWindowGatewayChoices", { pets: "crew" }]]);
  const ctx = makeHost([capRow("pets", ["gatewayChoice"], ["gatewayChoice"])]);
  const host = createAppWindowHost({
    electron: ctx.fake.electron,
    backendUrl: BACKEND,
    preloadPath: "/shell/app-window-preload.js",
    fetchGatewayAuth: async () => ({ value: "tok", viaCookie: false }),
    fetchImpl: crewNetwork(ctx.state, crew),
    gatewayStore: { get: (k) => store.get(k), set: (k, v) => store.set(k, v) },
    log: () => {},
    platform: "darwin",
  });
  await host.start();
  t.after(() => host.stop());
  const fake = ctx.fake;
  const [local] = fake.created;
  assert.deepStrictEqual(local.loaded, [`${BACKEND}/popout/desktop/pets/panel?token=tok`], "the stored crew is refused");
  const listed = await fake.ipc[CHANNELS.listGateways](fromWindow(local));
  assert.strictEqual(listed.gateways[0].appEnabled, false, "the page is told it cannot move there");
  assert.deepStrictEqual(listed.showing, "self");
  const tried = await fake.ipc[CHANNELS.chooseGateway](fromWindow(local), "crew");
  assert.deepStrictEqual(tried, { ok: false, chosen: "crew", showing: "self" });
  assert.strictEqual(local.isDestroyed(), false, "the window stays here, still holding the way to choose");
  assert.deepStrictEqual(await fake.ipc[CHANNELS.chooseGateway](fromWindow(local), "self"), { ok: true, chosen: "self", showing: "self" });
});

test("gatewayChoice: a crew with the app off, or down, is a fact; a non-answer keeps the windows", async (t) => {
  const crew = { instances: [CONNECTED], token: "remote-tok", enabled: false };
  const store = new Map();
  const ctx = makeHost([capRow("pets", ["gatewayChoice"], ["gatewayChoice"])]);
  const host = createAppWindowHost({
    electron: ctx.fake.electron,
    backendUrl: BACKEND,
    preloadPath: "/shell/app-window-preload.js",
    fetchGatewayAuth: async () => ({ value: "tok", viaCookie: false }),
    fetchImpl: crewNetwork(ctx.state, crew),
    gatewayStore: { get: (k) => store.get(k), set: (k, v) => store.set(k, v) },
    log: () => {},
    platform: "darwin",
  });
  await host.start();
  t.after(() => host.stop());
  const fake = ctx.fake;
  const [local] = fake.created;
  const off = await fake.ipc[CHANNELS.chooseGateway](fromWindow(local), "crew");
  assert.deepStrictEqual(off, { ok: false, chosen: "crew", showing: "self" }, "the app is off there");
  assert.strictEqual(local.isDestroyed(), false, "nothing moved");
  assert.deepStrictEqual(store.get("appWindowGatewayChoices"), { pets: "crew" }, "the choice is kept for later");

  crew.enabled = true;
  host._targets.get("pets").at = 0;
  await host.reconcileOnce();
  const remote = fake.created[1];
  assert.match(remote.loaded[0], /^http:\/\/localhost:7778\//);

  // The crew stops answering: the windows stay where they are.
  crew.remote = new Error("tunnel restarting");
  host._targets.get("pets").at = 0;
  await host.reconcileOnce();
  assert.strictEqual(remote.isDestroyed(), false);
  assert.strictEqual(fake.created.length, 2);
});

test("gatewayChoice: a crew whose manifest lacks this computer's window keeps the windows here", async (t) => {
  // An older version there declares only "pet": its page would close "panel" on load.
  const crew = { instances: [CONNECTED], token: "remote-tok", enabled: true, desktop: { windows: [{ id: "pet", entry: "desktop/pet.mjs", kind: "panel" }] } };
  const ctx = makeHost([capRow("pets", ["gatewayChoice"], ["gatewayChoice"])]);
  const host = createAppWindowHost({
    electron: ctx.fake.electron,
    backendUrl: BACKEND,
    preloadPath: "/shell/app-window-preload.js",
    fetchGatewayAuth: async () => ({ value: "tok", viaCookie: false }),
    fetchImpl: crewNetwork(ctx.state, crew),
    log: () => {},
    platform: "darwin",
  });
  await host.start();
  t.after(() => host.stop());
  const fake = ctx.fake;
  const [local] = fake.created;
  const listed = await fake.ipc[CHANNELS.listGateways](fromWindow(local));
  assert.strictEqual(listed.gateways[0].appEnabled, false, "the page is told the app cannot show its windows there");
  const tried = await fake.ipc[CHANNELS.chooseGateway](fromWindow(local), "crew");
  assert.deepStrictEqual(tried, { ok: false, chosen: "crew", showing: "self" });
  assert.strictEqual(local.isDestroyed(), false, "the working window stays");
  assert.strictEqual(fake.created.length, 1);
});

const TWO_PANELS = {
  windows: [
    { id: "panel", entry: "desktop/panel.mjs", kind: "panel", openOnEnable: true },
    { id: "notes", entry: "desktop/notes.mjs", kind: "panel" },
  ],
};

/** "pets" shown from crew "crew" on port 7778, chosen before start. */
async function onCrew(t) {
  const crew = { instances: [CONNECTED], token: "remote-tok", enabled: true, desktop: TWO_PANELS };
  const store = new Map([["appWindowGatewayChoices", { pets: "crew" }]]);
  const ctx = makeHost([capRow("pets", ["gatewayChoice"], ["gatewayChoice"], TWO_PANELS)]);
  const host = createAppWindowHost({
    electron: ctx.fake.electron,
    backendUrl: BACKEND,
    preloadPath: "/shell/app-window-preload.js",
    fetchGatewayAuth: async () => ({ value: "tok", viaCookie: false }),
    fetchImpl: crewNetwork(ctx.state, crew),
    gatewayStore: { get: (k) => store.get(k), set: (k, v) => store.set(k, v) },
    log: () => {},
    platform: "darwin",
  });
  await host.start();
  t.after(() => host.stop());
  const [remote] = ctx.fake.created;
  assert.deepStrictEqual(remote.loaded, ["http://localhost:7778/popout/desktop/pets/panel?token=remote-tok"]);
  remote.webContents.url = remote.loaded[0];
  // The crew disconnects, freeing its port, and another crew is given it.
  crew.instances = [{ ...CONNECTED, status: { state: "disconnected" } }, { ...CONNECTED, id: "other", name: "Other" }];
  return { host, fake: ctx.fake, remote };
}

/** Every page URL a window after the first loaded. */
const laterLoads = (fake) => fake.created.slice(1).flatMap((w) => w.loaded);

test("gatewayChoice: a window opened on a reused crew target loads only if that crew still holds the port", async (t) => {
  const { host, fake, remote } = await onCrew(t);
  fake.ipc[CHANNELS.open](fromWindow(remote), "notes");
  const notes = fake.created[1];
  await Promise.all([...host._pendingLoads]);
  assert.deepStrictEqual(notes.loaded, [], "the crew's token never goes to the port's new owner");
  assert.strictEqual(notes.isDestroyed(), true);
  assert.strictEqual(host._targets.get("pets")?.instanceId ?? "self", "self", "the target is dropped");
  await host.reconcileOnce();
  assert.strictEqual(remote.isDestroyed(), true);
  assert.ok(laterLoads(fake).every((url) => url.startsWith(`${BACKEND}/`)), laterLoads(fake).join(" "));
  assert.ok(laterLoads(fake).includes(`${BACKEND}/popout/desktop/pets/panel?token=tok`), "the app is back on this computer");
});

test("gatewayChoice: a reused crew target is re-checked every tick, so its windows leave a port it gave up", async (t) => {
  const { host, fake, remote } = await onCrew(t);
  // Inside the reuse window: nothing re-resolves the target unless the tick re-checks it.
  await host.reconcileOnce();
  assert.strictEqual(remote.isDestroyed(), true);
  assert.deepStrictEqual(laterLoads(fake), [`${BACKEND}/popout/desktop/pets/panel?token=tok`]);
});

test("gatewayChoice: a window added here is checked against the crew inside the reuse window", async (t) => {
  // The crew still runs the older manifest: only "panel".
  const crew = { instances: [CONNECTED], token: "remote-tok", enabled: true, desktop: PANEL_APP };
  const store = new Map([["appWindowGatewayChoices", { pets: "crew" }]]);
  const ctx = makeHost([capRow("pets", ["gatewayChoice"], ["gatewayChoice"], PANEL_APP)]);
  const host = createAppWindowHost({
    electron: ctx.fake.electron,
    backendUrl: BACKEND,
    preloadPath: "/shell/app-window-preload.js",
    fetchGatewayAuth: async () => ({ value: "tok", viaCookie: false }),
    fetchImpl: crewNetwork(ctx.state, crew),
    gatewayStore: { get: (k) => store.get(k), set: (k, v) => store.set(k, v) },
    log: () => {},
    platform: "darwin",
  });
  await host.start();
  t.after(() => host.stop());
  const [remote] = ctx.fake.created;
  assert.match(remote.loaded[0], /^http:\/\/localhost:7778\//);
  // An update here adds "notes", opened on enable; the crew's target is seconds old.
  const NOTES_TOO = { windows: [PANEL_APP.windows[0], { id: "notes", entry: "desktop/notes.mjs", kind: "panel", openOnEnable: true }] };
  ctx.state.rows = [capRow("pets", ["gatewayChoice"], ["gatewayChoice"], NOTES_TOO)];
  await host.reconcileOnce();
  await Promise.all([...host._pendingLoads]);
  assert.strictEqual(host._targets.get("pets").instanceId, "self", "the crew lacks a window, so this computer serves them");
  assert.strictEqual(remote.isDestroyed(), true);
  const later = ctx.fake.created.slice(1).flatMap((w) => w.loaded);
  assert.ok(later.length > 0 && later.every((url) => url.startsWith(`${BACKEND}/`)), later.join(" "));
});

test("gatewayChoice: a stored crew that does not answer at start opens the windows on this computer", async (t) => {
  const crew = { instances: [CONNECTED], token: "remote-tok", enabled: true, remote: new Error("link restarting") };
  const store = new Map([["appWindowGatewayChoices", { pets: "crew" }]]);
  const ctx = makeHost([capRow("pets", ["gatewayChoice"], ["gatewayChoice"])]);
  const host = createAppWindowHost({
    electron: ctx.fake.electron,
    backendUrl: BACKEND,
    preloadPath: "/shell/app-window-preload.js",
    fetchGatewayAuth: async () => ({ value: "tok", viaCookie: false }),
    fetchImpl: crewNetwork(ctx.state, crew),
    gatewayStore: { get: (k) => store.get(k), set: (k, v) => store.set(k, v) },
    log: () => {},
    platform: "darwin",
  });
  await host.start();
  t.after(() => host.stop());
  const [local] = ctx.fake.created;
  assert.deepStrictEqual(local.loaded, [`${BACKEND}/popout/desktop/pets/panel?token=tok`]);
  // The crew answers on a later tick: the windows move there.
  delete crew.remote;
  await host.reconcileOnce();
  assert.strictEqual(local.isDestroyed(), true);
  assert.match(ctx.fake.created[1].loaded[0], /^http:\/\/localhost:7778\//);
});

// ── Off here, served by the crew chosen for it ───────────────────────────────

/** "pets" switched off on this computer's gateway, declaring *declared* and holding *approved* here. */
function offHere(declared = ["gatewayChoice"], approved = ["gatewayChoice"], desktop = PANEL_APP) {
  return { ...capRow("pets", declared, approved, desktop), enabled: false };
}

/** A host over *rows* and one crew, with "pets" pointed at that crew before start. */
async function choseCrew(t, rows, crew, overrides = {}) {
  const store = new Map([["appWindowGatewayChoices", { pets: "crew" }]]);
  const ctx = makeHost(rows);
  const host = createAppWindowHost({
    electron: ctx.fake.electron,
    backendUrl: BACKEND,
    preloadPath: "/shell/app-window-preload.js",
    fetchGatewayAuth: async () => ({ value: "tok", viaCookie: false }),
    fetchImpl: crewNetwork(ctx.state, crew),
    gatewayStore: { get: (k) => store.get(k), set: (k, v) => store.set(k, v) },
    log: () => {},
    platform: "darwin",
    ...overrides,
  });
  await host.start();
  t.after(() => host.stop());
  return { host, fake: ctx.fake, state: ctx.state, store };
}

test("off here: the crew chosen for the app serves its windows while the app is on there", async (t) => {
  const crew = { instances: [CONNECTED], token: "remote-tok", enabled: true };
  const { host, fake } = await choseCrew(t, [offHere()], crew);
  assert.strictEqual(fake.created.length, 1);
  const [remote] = fake.created;
  assert.deepStrictEqual(remote.loaded, ["http://localhost:7778/popout/desktop/pets/panel?token=remote-tok"]);
  // The crew switches it off too: a definite no, so nothing stays and nothing opens here.
  crew.enabled = false;
  host._targets.get("pets").at = 0;
  await host.reconcileOnce();
  assert.strictEqual(remote.isDestroyed(), true);
  assert.strictEqual(fake.created.length, 1, "this computer has the app off: no window here");
});

test("off here: a non-answer keeps an open crew window and never creates one", async (t) => {
  const crew = { instances: [CONNECTED], token: "remote-tok", enabled: true, remote: new Error("link restarting") };
  const { host, fake } = await choseCrew(t, [offHere()], crew);
  assert.strictEqual(fake.created.length, 0, "not knowing never opens a window for an app off here");
  delete crew.remote;
  await host.reconcileOnce();
  const [remote] = fake.created;
  assert.match(remote.loaded[0], /^http:\/\/localhost:7778\//);
  crew.remote = new Error("link restarting");
  host._targets.get("pets").at = 0;
  await host.reconcileOnce();
  assert.strictEqual(remote.isDestroyed(), false, "the open window is kept");
  // Once it is gone, a non-answer does not bring it back.
  remote.destroy();
  host._targets.get("pets").at = 0;
  await host.reconcileOnce();
  assert.strictEqual(fake.created.length, 1);
});

test("off here: a non-answer keeps only the windows still open; none is created until the crew answers", async (t) => {
  const DECLARED = {
    windows: [
      { id: "a", entry: "desktop/a.mjs", kind: "panel", openOnEnable: true },
      { id: "b", entry: "desktop/b.mjs", kind: "panel", openOnEnable: true },
      { id: "bg", entry: "desktop/bg.mjs", kind: "background" },
    ],
    shortcuts: [{ id: "show-b", accelerator: "CommandOrControl+Alt+B", window: "b" }],
  };
  const clock = { now: 1_000_000 };
  const crew = { instances: [CONNECTED], token: "remote-tok", enabled: true, desktop: DECLARED };
  const { host, fake } = await choseCrew(
    t, [offHere(["gatewayChoice"], ["gatewayChoice"], DECLARED)], crew, { now: () => clock.now },
  );
  const [a, b] = fake.created;
  assert.strictEqual(fake.created.length, 2);
  fake.ipc[CHANNELS.open](fromWindow(a), "bg");
  await Promise.all([...host._pendingLoads]);
  const bg = fake.created[2];
  assert.match(bg.loaded[0], /^http:\/\/localhost:7778\/popout\/desktop\/pets\/bg\?/);

  // The crew stops answering while one of the windows is closed.
  b.destroy();
  crew.remote = new Error("link restarting");
  host._targets.get("pets").at = 0;
  await host.reconcileOnce();
  assert.strictEqual(fake.created.length, 3, "an openOnEnable window is not reopened");
  assert.strictEqual(a.isDestroyed(), false, "the open window is kept");
  fake.registered.get("CommandOrControl+Alt+B")();
  fake.ipc[CHANNELS.open](fromWindow(a), "b");
  bg.webContents.events["render-process-gone"]();
  assert.strictEqual(fake.created.length, 3, "neither a press, open() nor a background reopen creates one");

  // Core's registry does not answer the reused target's check either.
  host._targets.get("pets").at = clock.now;
  crew.list = new Error("core busy");
  await host.reconcileOnce();
  assert.strictEqual(fake.created.length, 3);
  assert.strictEqual(a.isDestroyed(), false);

  // The crew answers: what is declared is opened from it again, the background page too.
  delete crew.list;
  delete crew.remote;
  host._targets.get("pets").at = 0;
  await host.reconcileOnce();
  const reopened = fake.created.slice(3);
  assert.deepStrictEqual(reopened.map((w) => w.loaded[0].split("?")[0]).sort(), [
    "http://localhost:7778/popout/desktop/pets/b",
    "http://localhost:7778/popout/desktop/pets/bg",
  ]);
});

test("off here: a background page that dies while the crew is silent comes back when it answers, and not after a no", async (t) => {
  const DECLARED = {
    windows: [
      { id: "a", entry: "desktop/a.mjs", kind: "panel", openOnEnable: true },
      { id: "bg", entry: "desktop/bg.mjs", kind: "background" },
    ],
  };
  const reopenedBackground = (fake, from) => fake.created.slice(from)
    .filter((w) => w.loaded[0].startsWith("http://localhost:7778/popout/desktop/pets/bg?"));
  for (const answer of ["yes", "no"]) {
    const clock = { now: 1_000_000 };
    const crew = { instances: [CONNECTED], token: "remote-tok", enabled: true, desktop: DECLARED };
    const { host, fake } = await choseCrew(
      t, [offHere(["gatewayChoice"], ["gatewayChoice"], DECLARED)], crew, { now: () => clock.now },
    );
    const [a] = fake.created;
    fake.ipc[CHANNELS.open](fromWindow(a), "bg");
    await Promise.all([...host._pendingLoads]);
    const bg = fake.created[1];
    // The background page is the app's last window when the crew goes quiet.
    a.destroy();
    crew.remote = new Error("link restarting");
    host._targets.get("pets").at = 0;
    await host.reconcileOnce();
    assert.strictEqual(bg.isDestroyed(), false, answer);
    bg.webContents.events["render-process-gone"]();
    // A second silent tick, with no window of the app left open.
    host._targets.get("pets").at = 0;
    await host.reconcileOnce();
    assert.strictEqual(fake.created.length, 2, `${answer}: a non-answer opens nothing`);

    delete crew.remote;
    if (answer === "no") crew.enabled = false;
    host._targets.get("pets").at = 0;
    await host.reconcileOnce();
    if (answer === "yes") {
      assert.strictEqual(reopenedBackground(fake, 2).length, 1, "the crew answers: the background page is reopened");
      continue;
    }
    assert.strictEqual(fake.created.length, 2, "a definite no reopens nothing");
    // The no dropped the pending reopen: the crew turning it back on opens only openOnEnable windows.
    crew.enabled = true;
    host._targets.get("pets").at = 0;
    await host.reconcileOnce();
    assert.strictEqual(reopenedBackground(fake, 2).length, 0, "the dropped reopen does not come back");
  }
});

test("off here: switching the app off keeps the windows a crew serves; choosing this computer closes them", async (t) => {
  const crew = { instances: [CONNECTED], token: "remote-tok", enabled: true };
  const { host, fake, state, store } = await choseCrew(t, [capRow("pets", ["gatewayChoice"], ["gatewayChoice"])], crew);
  const [remote] = fake.created;
  assert.match(remote.loaded[0], /^http:\/\/localhost:7778\//);
  remote.webContents.url = remote.loaded[0];
  state.rows = [offHere()];
  await host.reconcileOnce();
  assert.strictEqual(remote.isDestroyed(), false, "a local switch-off does not take a crew's windows away");
  assert.strictEqual(fake.created.length, 1);
  const back = await fake.ipc[CHANNELS.chooseGateway](fromWindow(remote), "self");
  assert.deepStrictEqual(back, { ok: true, chosen: "self", showing: "self" });
  assert.strictEqual(remote.isDestroyed(), true);
  assert.strictEqual(fake.created.length, 1, "this computer has the app off: nothing opens here");
  assert.deepStrictEqual(store.get("appWindowGatewayChoices"), {});
});

test("NEGATIVE: an app off here that does not hold gatewayChoice here is served nothing, whatever crew is stored", async (t) => {
  for (const [declared, approved] of [[["gatewayChoice"], []], [["gatewayChoice"], null], [["localFiles"], ["localFiles"]]]) {
    const crew = { instances: [CONNECTED], token: "remote-tok", enabled: true };
    const { fake, state } = await choseCrew(t, [offHere(declared, approved)], crew);
    assert.strictEqual(fake.created.length, 0, JSON.stringify([declared, approved]));
    assert.ok(
      state.fetches.every((f) => !f.url.includes("/api/instances") && !f.url.startsWith("http://localhost:7778")),
      "no crew is even asked",
    );
  }
});

test("NEGATIVE: an app off here with no desktop block, or one skipped at boot, is served nothing", async (t) => {
  const crew = { instances: [CONNECTED], token: "remote-tok", enabled: true };
  const plain = { name: "pets", enabled: false, manifest: { name: "pets", permissions: { desktopCapabilities: ["gatewayChoice"] } }, desktopCapabilitiesGrant: { approved: ["gatewayChoice"], staged: [] } };
  for (const row of [plain, { ...offHere(), unavailable: { reason: "OSError: denied" } }]) {
    const { fake } = await choseCrew(t, [row], crew);
    assert.strictEqual(fake.created.length, 0);
  }
});
