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
  assert.deepStrictEqual(prefs.additionalArguments, ["--kc-app-window=pets/pet/overlay/1"]);
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
