/**
 * The desktop window host's per-machine half: per-platform shortcut defaults, the
 * user's rebinds and the hand-over of an app's own reserved chords.
 *
 * Driven with an injected Electron stand-in and a canned `/api/apps`. The negative
 * half pins the boundary: a page rebinds only its own app's declared shortcuts,
 * only while the user acts in it, only within the manifest's chord rules and only
 * onto a chord the user pressed there; a refused rebind keeps the chord the app
 * held; a chord a shell owner reserves is handed over only to the app that owner is.
 */

const test = require("node:test");
const assert = require("node:assert");

const { APP_WINDOW_BRIDGE_LEVEL, CHANNELS, createAppWindowHost } = require("../app-window-host");
const { BINDINGS_KEY } = require("../app-window-prefs");
const { createShortcutReservations } = require("../shell-shortcut-reservations");

const BACKEND = "http://127.0.0.1:5476";

function fakeElectron() {
  let nextId = 1;
  const created = [];
  const ipc = {};
  const registered = new Map();
  const display = {
    id: 1,
    bounds: { x: 0, y: 0, width: 1440, height: 900 },
    workArea: { x: 0, y: 25, width: 1440, height: 875 },
  };

  class FakeWindow {
    constructor(opts) {
      this.opts = opts;
      this.destroyed = false;
      this.events = {};
      this.loaded = [];
      const win = this;
      this.webContents = {
        id: nextId++,
        url: "",
        sent: [],
        events: {},
        mainFrame: { main: true },
        session: { fetch: async () => ({ arrayBuffer: async () => new ArrayBuffer(0) }) },
        isDestroyed: () => win.destroyed,
        loadURL: (url) => win.loadURL(url),
        on(ev, cb) { this.events[ev] = cb; },
        send(channel, ...args) { this.sent.push([channel, ...args]); },
        getURL() { return this.url; },
        getZoomFactor() { return 1; },
        setWindowOpenHandler(fn) { win.openHandler = fn; },
      };
      created.push(this);
    }
    loadURL(url) { this.loaded.push(url); this.webContents.url = url; }
    setIgnoreMouseEvents() {}
    setFocusable() {}
    setContentProtection() {}
    setVisibleOnAllWorkspaces() {}
    setAlwaysOnTop() {}
    showInactive() {}
    show() {}
    hide() {}
    focus() {}
    blur() {}
    isVisible() { return false; }
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
      getCursorScreenPoint: () => ({ x: 10, y: 10 }),
      getDisplayNearestPoint: () => display,
      getPrimaryDisplay: () => display,
      on() {},
      removeListener() {},
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
      register(acc, fn) {
        if (this.held.has(acc)) return false;
        registered.set(acc, fn);
        return true;
      },
      unregister(acc) { registered.delete(acc); },
    },
  };
  return { electron, created, ipc, registered };
}

function mapStore(initial = {}) {
  const map = new Map(Object.entries(initial));
  return { get: (k) => map.get(k), set: (k, v) => map.set(k, v), map };
}

function appRow(name, desktop, extra = {}) {
  return { name, enabled: true, manifest: { name, desktop }, ...extra };
}

/** Mac and elsewhere differ, as a desktop pet's defaults do. */
const SHORTCUT_APP = {
  windows: [
    { id: "pet", entry: "desktop/pet.mjs", kind: "overlay", openOnEnable: true },
    { id: "settings", entry: "desktop/settings.mjs", kind: "panel", openOnEnable: true },
  ],
  shortcuts: [
    {
      id: "toggle-window",
      accelerator: "Alt+Shift+M",
      platformAccelerators: { macos: "CommandOrControl+Shift+M" },
      window: "pet",
    },
    { id: "hide-all", accelerator: "Alt+Shift+H", platformAccelerators: { macos: "CommandOrControl+Shift+H" }, window: "pet" },
  ],
};

const OTHER_APP = {
  windows: [{ id: "panel", entry: "desktop/panel.mjs", kind: "panel", openOnEnable: true }],
  shortcuts: [{ id: "go", accelerator: "CommandOrControl+Alt+G", window: "panel" }],
};

async function started(rows, overrides = {}) {
  const fake = fakeElectron();
  const state = { rows };
  const lines = [];
  let clock = 1_000_000;
  const host = createAppWindowHost({
    electron: fake.electron,
    backendUrl: BACKEND,
    preloadPath: "/shell/app-window-preload.js",
    fetchGatewayAuth: async () => ({ value: "tok", viaCookie: false }),
    fetchImpl: async () => ({ status: 200, json: async () => state.rows }),
    log: (line) => lines.push(line),
    platform: "darwin",
    now: () => clock,
    ...overrides,
  });
  await host.start();
  return { host, fake, state, lines, advance: (ms) => { clock += ms; } };
}

function fromWindow(win) {
  return { sender: win.webContents, senderFrame: win.webContents.mainFrame };
}

function windowOf(fake, app, id) {
  return fake.created.find((w) => !w.isDestroyed() && w.loaded[0].includes(`/popout/desktop/${app}/${id}`));
}

/** A key press the shell itself sees in *win*: a person acting in it. */
function press(win) {
  win.webContents.events["input-event"]({}, { type: "rawKeyDown" });
}

/** The key-down Electron's `before-input-event` reports for *accelerator* on macOS. */
function keyDown(accelerator) {
  const parts = accelerator.split("+");
  const key = parts.pop();
  const mods = new Set(parts.map((p) => p.toLowerCase()));
  const has = (...names) => names.some((n) => mods.has(n));
  const code = /^[A-Za-z]$/.test(key) ? `Key${key.toUpperCase()}` : /^[0-9]$/.test(key) ? `Digit${key}` : key;
  return {
    type: "keyDown",
    key,
    code,
    meta: has("commandorcontrol", "cmdorctrl", "command", "cmd", "super"),
    control: has("control", "ctrl"),
    alt: has("alt", "option"),
    shift: has("shift"),
  };
}

/** The user presses *accelerator*'s keys in *win*, as a Settings page recording them sees. */
function pressChord(win, accelerator) {
  press(win);
  win.webContents.events["before-input-event"]({}, keyDown(accelerator));
}

/** A rebind the way a Settings page makes one: on the user's own key press of the chord. */
function rebind(fake, win, id, accelerator) {
  if (typeof accelerator === "string" && accelerator) pressChord(win, accelerator);
  else press(win);
  return fake.ipc[CHANNELS.setShortcut](fromWindow(win), id, accelerator);
}

test("the bridge is level 3", () => {
  assert.strictEqual(APP_WINDOW_BRIDGE_LEVEL, 3);
});

test("a shortcut binds at its declared default for this platform", async (t) => {
  for (const [platform, expected] of [
    ["darwin", ["CommandOrControl+Shift+M", "CommandOrControl+Shift+H"]],
    ["linux", ["Alt+Shift+M", "Alt+Shift+H"]],
    ["win32", ["Alt+Shift+M", "Alt+Shift+H"]],
  ]) {
    const { host, fake } = await started([appRow("pets", SHORTCUT_APP)], { platform });
    t.after(() => host.stop());
    assert.deepStrictEqual([...fake.registered.keys()], expected, platform);
  }
});

test("a page lists its own app's shortcuts with their defaults and state", async (t) => {
  const { host, fake } = await started([appRow("pets", SHORTCUT_APP), appRow("other", OTHER_APP)]);
  t.after(() => host.stop());
  const settings = windowOf(fake, "pets", "settings");
  const listed = await fake.ipc[CHANNELS.shortcuts](fromWindow(settings));
  assert.deepStrictEqual(listed, [
    {
      id: "toggle-window", window: "pet", defaultAccelerator: "CommandOrControl+Shift+M",
      accelerator: "CommandOrControl+Shift+M", custom: false, registered: true,
    },
    {
      id: "hide-all", window: "pet", defaultAccelerator: "CommandOrControl+Shift+H",
      accelerator: "CommandOrControl+Shift+H", custom: false, registered: true,
    },
  ]);
  const other = windowOf(fake, "other", "panel");
  const theirs = await fake.ipc[CHANNELS.shortcuts](fromWindow(other));
  assert.deepStrictEqual(theirs.map((s) => s.id), ["go"], "another app sees only its own");
});

test("a rebind moves the chord, persists per app on this computer and survives a restart", async (t) => {
  const prefsStore = mapStore();
  const { host, fake } = await started([appRow("pets", SHORTCUT_APP)], { prefsStore });
  const settings = windowOf(fake, "pets", "settings");
  const answer = await rebind(fake, settings, "toggle-window", "Alt+Shift+CommandOrControl+J");
  assert.strictEqual(answer.ok, true);
  assert.ok(!fake.registered.has("CommandOrControl+Shift+M"), "the old chord is released");
  assert.ok(fake.registered.has("Alt+Shift+CommandOrControl+J"));
  assert.deepStrictEqual(answer.shortcuts[0], {
    id: "toggle-window", window: "pet", defaultAccelerator: "CommandOrControl+Shift+M",
    accelerator: "Alt+Shift+CommandOrControl+J", custom: true, registered: true,
  });
  assert.deepStrictEqual(prefsStore.get(BINDINGS_KEY), { pets: { "toggle-window": "Alt+Shift+CommandOrControl+J" } });
  // The new chord reaches the declared window.
  fake.registered.get("Alt+Shift+CommandOrControl+J")();
  host.stop();

  const again = await started([appRow("pets", SHORTCUT_APP)], { prefsStore });
  t.after(() => again.host.stop());
  assert.deepStrictEqual([...again.fake.registered.keys()].sort(), [
    "Alt+Shift+CommandOrControl+J", "CommandOrControl+Shift+H",
  ]);
});

test("an empty chord unbinds a shortcut and null returns it to its default", async (t) => {
  const { host, fake } = await started([appRow("pets", SHORTCUT_APP)]);
  t.after(() => host.stop());
  const settings = windowOf(fake, "pets", "settings");
  const unbound = await rebind(fake, settings, "hide-all", "");
  assert.strictEqual(unbound.ok, true);
  assert.ok(!fake.registered.has("CommandOrControl+Shift+H"));
  assert.deepStrictEqual(
    unbound.shortcuts[1],
    { id: "hide-all", window: "pet", defaultAccelerator: "CommandOrControl+Shift+H", accelerator: "", custom: true, registered: false },
  );
  const reset = await rebind(fake, settings, "hide-all", null);
  assert.strictEqual(reset.ok, true);
  assert.ok(fake.registered.has("CommandOrControl+Shift+H"));
  assert.strictEqual(reset.shortcuts[1].custom, false);
});

test("NEGATIVE: a rebind needs the user acting in the window, never a timer or a background page", async (t) => {
  const prefsStore = mapStore();
  const { host, fake, advance } = await started([appRow("pets", SHORTCUT_APP)], { prefsStore });
  t.after(() => host.stop());
  const settings = windowOf(fake, "pets", "settings");
  const noPress = await fake.ipc[CHANNELS.setShortcut](fromWindow(settings), "toggle-window", "Alt+Shift+CommandOrControl+J");
  assert.deepStrictEqual(noPress, { ok: false });
  press(settings);
  advance(5_000);
  const stale = await fake.ipc[CHANNELS.setShortcut](fromWindow(settings), "toggle-window", "Alt+Shift+CommandOrControl+J");
  assert.deepStrictEqual(stale, { ok: false }, "a press more than 2 s ago no longer counts");
  // One press answers one request.
  press(settings);
  assert.strictEqual((await fake.ipc[CHANNELS.setShortcut](fromWindow(settings), "hide-all", "")).ok, true);
  assert.deepStrictEqual(await fake.ipc[CHANNELS.setShortcut](fromWindow(settings), "toggle-window", ""), { ok: false });
  assert.ok(fake.registered.has("CommandOrControl+Shift+M"));
  // A sender this host did not create is no window of any app.
  const stranger = { sender: { id: 999, getURL: () => "", mainFrame: {} } };
  assert.deepStrictEqual(await fake.ipc[CHANNELS.setShortcut](stranger, "toggle-window", ""), { ok: false });
  assert.strictEqual(await fake.ipc[CHANNELS.shortcuts](stranger), null);
});

test("NEGATIVE: a background page never rebinds, whatever input it sees", async (t) => {
  const BACKGROUND_APP = {
    windows: [{ id: "brain", entry: "desktop/brain.mjs", kind: "background", openOnEnable: true }],
    shortcuts: [{ id: "go", accelerator: "CommandOrControl+Alt+B", window: "brain" }],
  };
  const { host, fake } = await started([appRow("bg", BACKGROUND_APP)]);
  t.after(() => host.stop());
  const brain = windowOf(fake, "bg", "brain");
  assert.deepStrictEqual(await rebind(fake, brain, "go", ""), { ok: false });
  assert.ok(fake.registered.has("CommandOrControl+Alt+B"));
  assert.strictEqual((await fake.ipc[CHANNELS.shortcuts](fromWindow(brain)))[0].registered, true, "it may still read them");
});

test("NEGATIVE: a rebind is refused beyond what the app declares and the chord rules allow", async (t) => {
  const reservations = createShortcutReservations({ platform: "darwin" });
  reservations.reserve("shell:summon", ["CommandOrControl+Alt+K"]);
  const { host, fake } = await started(
    [appRow("pets", SHORTCUT_APP), appRow("other", OTHER_APP)],
    { reservations },
  );
  t.after(() => host.stop());
  const settings = windowOf(fake, "pets", "settings");
  const before = [...fake.registered.keys()].sort();
  // Another app's shortcut, or one nobody declares.
  assert.strictEqual((await rebind(fake, settings, "go", "Alt+Shift+CommandOrControl+J")).reason, "undeclared");
  assert.strictEqual((await rebind(fake, settings, "nope", "Alt+Shift+CommandOrControl+J")).reason, "undeclared");
  // A single-modifier chord (copy), or garbage.
  assert.strictEqual((await rebind(fake, settings, "toggle-window", "CommandOrControl+C")).reason, "refused");
  assert.strictEqual((await rebind(fake, settings, "toggle-window", "Shift+Alt+?")).reason, "refused");
  // A chord another of its own shortcuts is on.
  assert.strictEqual((await rebind(fake, settings, "toggle-window", "Shift+CommandOrControl+H")).reason, "in-use");
  // A chord a shell owner reserves, another app holds, or another program holds.
  assert.strictEqual((await rebind(fake, settings, "toggle-window", "CommandOrControl+Alt+K")).reason, "unavailable");
  assert.strictEqual((await rebind(fake, settings, "toggle-window", "CommandOrControl+Alt+G")).reason, "unavailable");
  fake.electron.globalShortcut.held.add("Alt+Shift+CommandOrControl+Z");
  assert.strictEqual((await rebind(fake, settings, "toggle-window", "Alt+Shift+CommandOrControl+Z")).reason, "unavailable");
  // Every refusal left every binding as it was.
  assert.deepStrictEqual([...fake.registered.keys()].sort(), before);
  const listed = await fake.ipc[CHANNELS.shortcuts](fromWindow(settings));
  assert.ok(listed.every((s) => !s.custom && s.registered));
  assert.strictEqual(fake.registered.has("CommandOrControl+Alt+G"), true, "the other app keeps its chord");
});

test("NEGATIVE: a rebind lands only on a chord the user pressed in that window", async (t) => {
  const prefsStore = mapStore();
  const { host, fake, advance } = await started([appRow("pets", SHORTCUT_APP)], { prefsStore });
  t.after(() => host.stop());
  const settings = windowOf(fake, "pets", "settings");
  const pet = windowOf(fake, "pets", "pet");
  const ask = (accelerator) => fake.ipc[CHANNELS.setShortcut](fromWindow(settings), "toggle-window", accelerator);
  const reason = async (accelerator) => (await ask(accelerator)).reason;
  // A click, then a chord the page picked itself.
  settings.webContents.events["input-event"]({}, { type: "mouseDown" });
  assert.strictEqual(await reason("Alt+Shift+CommandOrControl+T"), "not-pressed");
  // The user pressed another chord.
  pressChord(settings, "Alt+Shift+CommandOrControl+J");
  assert.strictEqual(await reason("Alt+Shift+CommandOrControl+T"), "not-pressed");
  // The chord, pressed in another of the app's windows.
  pet.webContents.events["before-input-event"]({}, keyDown("Alt+Shift+CommandOrControl+T"));
  press(settings);
  assert.strictEqual(await reason("Alt+Shift+CommandOrControl+T"), "not-pressed");
  // The chord, pressed more than 2 s before the request.
  pressChord(settings, "Alt+Shift+CommandOrControl+T");
  advance(2_001);
  press(settings);
  assert.strictEqual(await reason("Alt+Shift+CommandOrControl+T"), "not-pressed");
  // The chord, already spent by one request.
  pressChord(settings, "Alt+Shift+CommandOrControl+T");
  assert.strictEqual(await reason("CommandOrControl+C"), "refused");
  press(settings);
  assert.strictEqual(await reason("Alt+Shift+CommandOrControl+T"), "not-pressed");
  // A key repeat or a release spells no chord.
  press(settings);
  settings.webContents.events["before-input-event"]({}, { ...keyDown("Alt+Shift+CommandOrControl+T"), isAutoRepeat: true });
  settings.webContents.events["before-input-event"]({}, { ...keyDown("Alt+Shift+CommandOrControl+T"), type: "keyUp" });
  assert.strictEqual(await reason("Alt+Shift+CommandOrControl+T"), "not-pressed");
  assert.ok(fake.registered.has("CommandOrControl+Shift+M"), "every refusal left the default bound");
  assert.strictEqual(prefsStore.get(BINDINGS_KEY), undefined);

  // The chord the user pressed, in either spelling a recorder reads (position or character).
  pressChord(settings, "Alt+Shift+CommandOrControl+T");
  assert.strictEqual((await ask("Shift+Alt+CmdOrCtrl+T")).ok, true);
  press(settings);
  settings.webContents.events["before-input-event"]({}, { type: "keyDown", code: "KeyQ", key: "a", meta: true, alt: true });
  assert.strictEqual((await ask("CommandOrControl+Alt+A")).ok, true);
  assert.ok(fake.registered.has("CommandOrControl+Alt+A"));
  // Its declared default, "" and null need no chord: the publisher signed the default.
  press(settings);
  assert.strictEqual((await ask("Shift+CommandOrControl+M")).ok, true);
  press(settings);
  assert.strictEqual((await ask("")).ok, true);
  press(settings);
  assert.strictEqual((await ask(null)).ok, true);
  assert.ok(fake.registered.has("CommandOrControl+Shift+M"));
});

test("an app's own reserved chords are handed over to the host serving that app, and to no other", async (t) => {
  const reservations = createShortcutReservations({ platform: "darwin" });
  reservations.reserve("pets", ["CommandOrControl+Shift+M"], { app: true });
  reservations.reserve("shell:summon", ["CommandOrControl+Shift+H"]);
  const { host, fake, lines } = await started([appRow("pets", SHORTCUT_APP)], { reservations });
  t.after(() => host.stop());
  assert.ok(fake.registered.has("CommandOrControl+Shift+M"), "its own shell code's reservation is handed over");
  assert.ok(!fake.registered.has("CommandOrControl+Shift+H"), "a shell owner that is no app is never");
  assert.ok(lines.includes("app-window-host: pets: shortcut hide-all unavailable"));
});

test("NEGATIVE: another app is refused a chord an app's shell code reserves", async (t) => {
  const reservations = createShortcutReservations({ platform: "darwin" });
  reservations.reserve("pets", ["CommandOrControl+Alt+G"], { app: true });
  const { host, fake } = await started([appRow("other", OTHER_APP)], { reservations });
  t.after(() => host.stop());
  assert.ok(!fake.registered.has("CommandOrControl+Alt+G"));
  // Reserved without `app`, the owner's name is just a name: no hand-over.
  reservations.reserve("other", ["CommandOrControl+Alt+G"]);
  await host.reconcileOnce();
  assert.ok(!fake.registered.has("CommandOrControl+Alt+G"));
});

/** An app declaring *accelerator* by default, for the chord another app may free. */
function appOn(accelerator) {
  return {
    windows: [{ id: "panel", entry: "desktop/panel.mjs", kind: "panel", openOnEnable: true }],
    shortcuts: [{ id: "go", accelerator, window: "panel" }],
  };
}

test("NEGATIVE: a refused rebind or reset keeps the chord the app held, never hands it to another app", async (t) => {
  const custom = "Alt+Shift+CommandOrControl+J";
  const prefsStore = mapStore({ [BINDINGS_KEY]: { pets: { "toggle-window": custom } } });
  const { host, fake } = await started([appRow("pets", SHORTCUT_APP), appRow("waiting", appOn(custom))], { prefsStore });
  t.after(() => host.stop());
  const settings = windowOf(fake, "pets", "settings");
  const holder = () => host._liveShortcuts.get("Alt+CommandOrControl+Shift+J")?.app;
  assert.strictEqual(holder(), "pets", "the app holding the chord first keeps it");
  // Its default, or a chord the user pressed, held by another program.
  fake.electron.globalShortcut.held.add("CommandOrControl+Shift+M");
  fake.electron.globalShortcut.held.add("Alt+Shift+CommandOrControl+Z");
  assert.strictEqual((await rebind(fake, settings, "toggle-window", null)).reason, "unavailable");
  assert.strictEqual((await rebind(fake, settings, "toggle-window", "Alt+Shift+CommandOrControl+Z")).reason, "unavailable");
  assert.strictEqual(holder(), "pets", "the refused move left the chord with the app");
  assert.deepStrictEqual(prefsStore.get(BINDINGS_KEY), { pets: { "toggle-window": custom } });
  const listed = await fake.ipc[CHANNELS.shortcuts](fromWindow(settings));
  assert.deepStrictEqual([listed[0].accelerator, listed[0].registered], [custom, true]);
  // A move that registers frees the chord, and the waiting app then takes it.
  fake.electron.globalShortcut.held.delete("CommandOrControl+Shift+M");
  assert.strictEqual((await rebind(fake, settings, "toggle-window", null)).ok, true);
  assert.strictEqual(holder(), "waiting");
  assert.ok(fake.registered.has("CommandOrControl+Shift+M"));
});

test("NEGATIVE: respelling a held chord keeps it with the app, never hands it to another declaring it", async (t) => {
  const chord = "Alt+Shift+CommandOrControl+M";
  const { host, fake, state } = await started([appRow("beta", appOn(chord))]);
  t.after(() => host.stop());
  const holder = () => host._liveShortcuts.get("Alt+CommandOrControl+Shift+M")?.app;
  // An app enabled later, listed first, declaring the same chord.
  state.rows = [appRow("alpha", appOn(chord)), appRow("beta", appOn(chord))];
  await host.reconcileOnce();
  assert.strictEqual(holder(), "beta");
  const panel = windowOf(fake, "beta", "panel");
  const answer = await rebind(fake, panel, "go", "Shift+Alt+CmdOrCtrl+M");
  assert.strictEqual(answer.ok, true);
  assert.strictEqual(holder(), "beta", "the same chord in another spelling stays with its holder");
  assert.strictEqual(answer.shortcuts[0].registered, true);
});

test("an app's bindings outlive a listing without it, as its data dir outlives an uninstall", async (t) => {
  const prefsStore = mapStore();
  const { host, fake, state } = await started([appRow("pets", SHORTCUT_APP)], { prefsStore });
  t.after(() => host.stop());
  const settings = windowOf(fake, "pets", "settings");
  await rebind(fake, settings, "hide-all", "");
  // Core skips a damaged record from the list and keeps `data/` on an uninstall.
  state.rows = [];
  await host.reconcileOnce();
  state.rows = [appRow("pets", SHORTCUT_APP)];
  await host.reconcileOnce();
  assert.deepStrictEqual(prefsStore.get(BINDINGS_KEY), { pets: { "hide-all": "" } });
  assert.ok(!fake.registered.has("CommandOrControl+Shift+H"));
});
