/**
 * The desktop window host's multi-window seams: one avatar across every display,
 * a background page that outlives them, and the app's windows talking through
 * the host and nothing else.
 *
 * Driven with a two-display Electron stand-in, a canned `/api/apps` and an
 * injected drag timer, so each drag tick is called directly. The negative half
 * pins the boundary: only the copy hosting the avatar starts a drag, a message
 * never reaches another app or an undeclared window, a background page cannot
 * raise the dashboard or open a link, and only an https link reaches the OS.
 */

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { CHANNELS, MAX_MESSAGE_CHARS, createAppWindowHost } = require("../app-window-host");
const { MAX_SPAN_INBOX, clampBox, sanitizeDragBox } = require("../app-window-span");
const { placeBesideAnchor } = require("../app-window-placement");

const BACKEND = "http://127.0.0.1:5476";
const LEFT = { id: 1, bounds: { x: 0, y: 0, width: 1000, height: 800 }, workArea: { x: 0, y: 25, width: 1000, height: 775 } };
const RIGHT = { id: 2, bounds: { x: 1000, y: 0, width: 1000, height: 800 }, workArea: { x: 1000, y: 0, width: 1000, height: 800 } };

function fakeElectron() {
  let nextId = 1;
  const created = [];
  const ipc = {};
  const invoke = {};
  const screenEvents = {};
  let displays = [LEFT, RIGHT];
  let cursor = { x: 10, y: 10 };

  class FakeWindow {
    constructor(opts) {
      this.opts = opts;
      this.destroyed = false;
      this.visible = false;
      this.ignore = [];
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
    loadURL(url) { this.loaded.push(url); this.webContents.url = url.split("?")[0]; }
    setIgnoreMouseEvents(ignore) { this.ignore.push(ignore); }
    setFocusable() {}
    setContentProtection(v) { this.calls.push(["contentProtection", v]); }
    setVisibleOnAllWorkspaces(v) { this.calls.push(["allWorkspaces", v]); }
    setAlwaysOnTop(v, level) { this.calls.push(["alwaysOnTop", v, level]); }
    showInactive() { this.visible = true; this.calls.push(["showInactive"]); }
    show() { this.visible = true; this.calls.push(["show"]); }
    hide() { this.visible = false; }
    focus() { this.focused = true; this.calls.push(["focus"]); }
    blur() { this.focused = false; }
    isFocused() { return this.focused === true; }
    isVisible() { return this.visible; }
    isDestroyed() { return this.destroyed; }
    getBounds() { return { x: this.opts.x, y: this.opts.y, width: this.opts.width, height: this.opts.height }; }
    setBounds(b) { Object.assign(this.opts, b); this.calls.push(["setBounds", { ...b }]); }
    on(ev, cb) { this.events[ev] = cb; }
    destroy() {
      if (this.destroyed) return;
      this.destroyed = true;
      if (this.events.closed) this.events.closed();
    }
  }

  const containing = (p) => displays.find((d) => (
    p.x >= d.bounds.x && p.x < d.bounds.x + d.bounds.width && p.y >= d.bounds.y && p.y < d.bounds.y + d.bounds.height
  )) || displays[0];

  const electron = {
    BrowserWindow: FakeWindow,
    screen: {
      getCursorScreenPoint: () => cursor,
      getAllDisplays: () => displays,
      getDisplayNearestPoint: (p) => containing(p),
      getPrimaryDisplay: () => displays[0],
      on(ev, cb) { screenEvents[ev] = cb; },
      removeListener(ev) { delete screenEvents[ev]; },
    },
    ipcMain: {
      on(channel, fn) { ipc[channel] = fn; },
      removeListener(channel) { delete ipc[channel]; },
      handle(channel, fn) { invoke[channel] = fn; },
      removeHandler(channel) { delete invoke[channel]; },
    },
    globalShortcut: {
      isRegistered: () => false,
      register: () => true,
      unregister() {},
    },
  };
  return {
    electron,
    created,
    ipc,
    invoke,
    screenEvents,
    setCursor(p) { cursor = p; },
    setDisplays(list) { displays = list; },
  };
}

function fakeTimers() {
  const intervals = new Set();
  return {
    timers: {
      setInterval: (fn) => { const h = { fn }; intervals.add(h); return h; },
      clearInterval: (h) => intervals.delete(h),
      setTimeout: () => ({}),
      clearTimeout: () => {},
    },
    tick() { for (const h of [...intervals]) h.fn(); },
    running: () => intervals.size,
  };
}

const COMPANION = {
  windows: [
    { id: "pet", entry: "pet.mjs", kind: "overlay", displays: "all", openOnEnable: true },
    { id: "brain", entry: "brain.mjs", kind: "background", openOnEnable: true },
    { id: "panel", entry: "panel.mjs", kind: "panel", width: 384, height: 404, transparent: true, gutter: 32 },
    { id: "gallery", entry: "gallery.mjs", kind: "panel", width: 808, height: 608, position: "center", transparent: true },
  ],
};

function appRow(name, desktop) {
  return { name, enabled: true, manifest: { name, desktop } };
}

async function started(rows, overrides = {}, cursor = null, displays = null) {
  const fake = fakeElectron();
  if (cursor) fake.setCursor(cursor);
  if (displays) fake.setDisplays(displays);
  const clock = fakeTimers();
  const state = { rows, now: 1_000_000 };
  const calls = { sessions: [], links: [] };
  const lines = [];
  const host = createAppWindowHost({
    electron: fake.electron,
    backendUrl: BACKEND,
    preloadPath: "/shell/app-window-preload.js",
    fetchGatewayAuth: async () => ({ value: "tok", viaCookie: false }),
    fetchImpl: async () => ({ status: 200, json: async () => state.rows }),
    log: (line) => lines.push(line),
    platform: "darwin",
    now: () => state.now,
    timers: clock.timers,
    showDashboardSession: (slot) => { calls.sessions.push(slot); return true; },
    openExternal: (url) => calls.links.push(url),
    ...overrides,
  });
  await host.start();
  return { host, fake, clock, state, calls, lines };
}

function fromWindow(win) {
  return { sender: win.webContents, senderFrame: win.webContents.mainFrame };
}

/** The live windows whose page is the app window *id*. */
function windowsOf(fake, app, id) {
  return fake.created.filter((w) => !w.destroyed && w.webContents.url === `${BACKEND}/popout/desktop/${app}/${id}`);
}

function sentOn(win, channel) {
  return win.webContents.sent.filter((m) => m[0] === channel).map((m) => m.slice(1));
}

function ready(fake, win) {
  fake.ipc[CHANNELS.ready](fromWindow(win));
}

/** A click the shell itself sees in *win* (Electron's input-event): a person acting in it. */
function press(win, type = "mouseDown") {
  win.webContents.events["input-event"]({}, { type });
}

function tmpFile(t) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "kc-span-"));
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }));
  return path.join(dir, "displays.json");
}

test("a spanning overlay opens one copy per display and hosts the avatar on the cursor's", async (t) => {
  const { host, fake } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const pets = windowsOf(fake, "pets", "pet");
  assert.deepStrictEqual(pets.map((w) => w.getBounds()), [LEFT.bounds, RIGHT.bounds]);
  for (const win of pets) ready(fake, win);
  assert.deepStrictEqual(sentOn(pets[0], CHANNELS.active), [[{ active: true }]]);
  assert.deepStrictEqual(sentOn(pets[1], CHANNELS.active), [[{ active: false }]]);
  assert.ok(pets.every((w) => w.isVisible()), "every copy is revealed; the inactive one draws nothing");
});

test("the remembered display hosts the avatar, wherever the cursor is", async (t) => {
  const file = tmpFile(t);
  fs.writeFileSync(file, JSON.stringify({ "pets/pet": 2, "other/x": "junk" }));
  const { host, fake } = await started([appRow("pets", COMPANION)], { displayMemoryPath: file });
  t.after(() => host.stop());
  const [left, right] = windowsOf(fake, "pets", "pet");
  ready(fake, left);
  ready(fake, right);
  assert.deepStrictEqual(sentOn(left, CHANNELS.active), [[{ active: false }]]);
  assert.deepStrictEqual(sentOn(right, CHANNELS.active), [[{ active: true }]]);
});

test("a drag across a display edge hands the avatar over and remembers where it went", async (t) => {
  const file = tmpFile(t);
  const { host, fake, clock } = await started([appRow("pets", COMPANION)], { displayMemoryPath: file }, { x: 1500, y: 400 });
  t.after(() => host.stop());
  const [left, right] = windowsOf(fake, "pets", "pet");
  ready(fake, left);
  ready(fake, right);
  assert.deepStrictEqual(sentOn(right, CHANNELS.active), [[{ active: true }]]);

  // Only the copy hosting the avatar may start a drag.
  fake.ipc[CHANNELS.beginDrag](fromWindow(left), { offsetX: 10, offsetY: 10, width: 128, height: 128 });
  assert.strictEqual(clock.running(), 0);

  fake.setCursor({ x: 1500, y: 400 });
  press(right);
  fake.ipc[CHANNELS.beginDrag](fromWindow(right), { offsetX: 10, offsetY: 20, width: 128, height: 128 });
  assert.strictEqual(clock.running(), 1);
  assert.deepStrictEqual([left.ignore.at(-1), right.ignore.at(-1)], [false, false], "every copy takes the release");
  assert.deepStrictEqual(sentOn(left, CHANNELS.drag), [["start"]]);
  clock.tick();
  assert.deepStrictEqual(sentOn(right, CHANNELS.drag).at(-1), ["move", 490, 380]);

  fake.setCursor({ x: 300, y: 200 });
  clock.tick();
  assert.deepStrictEqual(sentOn(right, CHANNELS.active).at(-1), [{ active: false }]);
  assert.deepStrictEqual(sentOn(left, CHANNELS.active).at(-1), [{ active: true, x: 290, y: 180, dragging: true }]);
  assert.deepStrictEqual(JSON.parse(fs.readFileSync(file, "utf-8")), { "pets/pet": 1 });
  if (process.platform !== "win32") assert.strictEqual(fs.statSync(file).mode & 0o777, 0o600);

  // The release can be seen by any copy, and the avatar settles where it is.
  fake.setCursor({ x: 5, y: 790 });
  fake.ipc[CHANNELS.endDrag](fromWindow(right));
  assert.strictEqual(clock.running(), 0);
  assert.deepStrictEqual(sentOn(left, CHANNELS.drag).at(-1), ["end", -5, 672]);
  assert.deepStrictEqual([left.ignore.at(-1), right.ignore.at(-1)], [true, true]);
});

test("only the copy hosting the avatar takes the pointer over its regions", async (t) => {
  const { host, fake } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const [left, right] = windowsOf(fake, "pets", "pet");
  ready(fake, left);
  ready(fake, right);
  const regions = [{ x: 0, y: 0, width: 200, height: 200 }];
  fake.ipc[CHANNELS.hitRegions](fromWindow(left), regions);
  fake.ipc[CHANNELS.hitRegions](fromWindow(right), regions);
  fake.setCursor({ x: 50, y: 50 });
  host._pollHitRegionsOnce();
  assert.strictEqual(left.ignore.at(-1), false, "the avatar's copy takes the pointer");
  fake.setCursor({ x: 1050, y: 50 });
  host._pollHitRegionsOnce();
  assert.strictEqual(right.ignore.at(-1), true, "a copy without the avatar clicks through");
});

test("a display with no copy yet keeps the avatar on its own display, clamped", async (t) => {
  const { host, fake, clock } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const [left] = windowsOf(fake, "pets", "pet");
  ready(fake, left);
  press(left);
  fake.ipc[CHANNELS.beginDrag](fromWindow(left), { offsetX: 0, offsetY: 0, width: 100, height: 100 });
  const plugged = { id: 3, bounds: { x: 0, y: 800, width: 1000, height: 800 }, workArea: { x: 0, y: 800, width: 1000, height: 800 } };
  fake.setDisplays([LEFT, RIGHT, plugged]);
  fake.setCursor({ x: 400, y: 1200 });
  clock.tick();
  assert.deepStrictEqual(sentOn(left, CHANNELS.drag).at(-1), ["move", 400, 700]);
  assert.ok(!sentOn(left, CHANNELS.active).some(([s]) => s.active === false));
});

test("messages to a spanning window wait for the avatar's copy and follow the hand-off", async (t) => {
  const { host, fake, clock } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const [brain] = windowsOf(fake, "pets", "brain");
  const [left, right] = windowsOf(fake, "pets", "pet");
  ready(fake, brain);
  fake.ipc[CHANNELS.send](fromWindow(brain), "pet", { bubble: "first" });
  ready(fake, right);
  assert.deepStrictEqual(sentOn(right, CHANNELS.message), [], "an inactive copy is not the avatar");
  ready(fake, left);
  assert.deepStrictEqual(sentOn(left, CHANNELS.message), [["brain", { bubble: "first" }]]);
  const order = left.webContents.sent.map((m) => m[0]).filter((c) => c === CHANNELS.active || c === CHANNELS.message);
  assert.deepStrictEqual(order, [CHANNELS.active, CHANNELS.message], "told it hosts the avatar first");

  press(left);
  fake.ipc[CHANNELS.beginDrag](fromWindow(left), { offsetX: 0, offsetY: 0, width: 100, height: 100 });
  fake.setCursor({ x: 1500, y: 100 });
  clock.tick();
  fake.ipc[CHANNELS.send](fromWindow(brain), "pet", { bubble: "second" });
  assert.deepStrictEqual(sentOn(right, CHANNELS.message), [["brain", { bubble: "second" }]]);
  assert.strictEqual(sentOn(left, CHANNELS.message).length, 1);

  // The overlay answers the background page, and the inbox is bounded.
  fake.ipc[CHANNELS.send](fromWindow(right), "brain", { action: "dismiss" });
  assert.deepStrictEqual(sentOn(brain, CHANNELS.message), [["pet", { action: "dismiss" }]]);
  assert.ok(MAX_SPAN_INBOX >= 8);
});

test("a message to a spanning window that is closed is dropped, not delivered on reopen", async (t) => {
  const { host, fake } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const [brain] = windowsOf(fake, "pets", "brain");
  ready(fake, brain);
  fake.ipc[CHANNELS.close](fromWindow(brain), "pet");
  assert.deepStrictEqual(windowsOf(fake, "pets", "pet"), []);
  fake.ipc[CHANNELS.send](fromWindow(brain), "pet", { stale: true });
  await host.reconcileOnce();
  for (const win of windowsOf(fake, "pets", "pet")) ready(fake, win);
  assert.ok(windowsOf(fake, "pets", "pet").every((w) => sentOn(w, CHANNELS.message).length === 0));
});

test("a message never reaches another app, an undeclared window, or past the size cap", async (t) => {
  const OTHER = { windows: [{ id: "pet", entry: "pet.mjs", kind: "overlay", openOnEnable: true }] };
  const { host, fake } = await started([appRow("pets", COMPANION), appRow("other", OTHER)]);
  t.after(() => host.stop());
  const [brain] = windowsOf(fake, "pets", "brain");
  const [otherPet] = windowsOf(fake, "other", "pet");
  ready(fake, brain);
  ready(fake, otherPet);
  for (const win of windowsOf(fake, "pets", "pet")) ready(fake, win);
  fake.ipc[CHANNELS.send](fromWindow(otherPet), "brain", { forged: true });
  fake.ipc[CHANNELS.send](fromWindow(brain), "nope", { lost: true });
  fake.ipc[CHANNELS.send](fromWindow(brain), "pet", "x".repeat(MAX_MESSAGE_CHARS));
  fake.ipc[CHANNELS.send]({ sender: { id: 999, getURL: () => "" } }, "brain", { stranger: true });
  assert.deepStrictEqual(sentOn(brain, CHANNELS.message), []);
  assert.ok(windowsOf(fake, "pets", "pet").every((w) => sentOn(w, CHANNELS.message).length === 0));
  assert.deepStrictEqual(sentOn(otherPet, CHANNELS.message), []);

  // A broadcast reaches every other window of the same app, every copy included.
  fake.ipc[CHANNELS.broadcast](fromWindow(brain), { appearance: "changed" });
  for (const win of windowsOf(fake, "pets", "pet")) {
    assert.deepStrictEqual(sentOn(win, CHANNELS.message), [["brain", { appearance: "changed" }]]);
  }
  assert.deepStrictEqual(sentOn(brain, CHANNELS.message), []);
  assert.deepStrictEqual(sentOn(otherPet, CHANNELS.message), []);
});

test("a background page is never shown, and is reopened at once when it dies", async (t) => {
  const { host, fake, state } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const [brain] = windowsOf(fake, "pets", "brain");
  assert.strictEqual(brain.opts.show, false);
  assert.strictEqual(brain.opts.webPreferences.backgroundThrottling, false);
  assert.ok(brain.calls.some((c) => c[0] === "contentProtection" && c[1] === true));
  ready(fake, brain);
  assert.strictEqual(brain.isVisible(), false);

  brain.webContents.events["render-process-gone"]();
  const [second] = windowsOf(fake, "pets", "brain");
  assert.ok(second && second !== brain, "reopened at once");

  // Dying again at once is left to the tick, so a crash loop runs at tick cadence.
  second.webContents.events["render-process-gone"]();
  assert.deepStrictEqual(windowsOf(fake, "pets", "brain"), []);
  state.now += 6_000;
  await host.reconcileOnce();
  assert.strictEqual(windowsOf(fake, "pets", "brain").length, 1);

  // Disabled: closed, and not reopened.
  state.rows = [{ ...appRow("pets", COMPANION), enabled: false }];
  await host.reconcileOnce();
  assert.strictEqual(windowsOf(fake, "pets", "brain").length, 0);
  assert.strictEqual(fake.created.filter((w) => !w.destroyed).length, 0);
});

test("a panel opened beside an anchor places its card there and says which side", async (t) => {
  const { host, fake } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const [left] = windowsOf(fake, "pets", "pet");
  ready(fake, left);
  left.webContents.zoom = 2;
  press(left);
  fake.ipc[CHANNELS.open](fromWindow(left), "panel", { anchor: { x: 400, y: 100, width: 64, height: 64 } });
  const [panel] = windowsOf(fake, "pets", "panel");
  // The anchor is (800, 200, 128x128) on screen: right of the midline, so the
  // 320x340 card goes to its left, and the window adds the 32px gutter round it.
  const expected = placeBesideAnchor({ x: 800, y: 200, width: 128, height: 128 }, { width: 320, height: 340 }, LEFT.workArea);
  assert.strictEqual(expected.side, "left");
  assert.deepStrictEqual(panel.getBounds(), {
    x: expected.rect.x - 32, y: expected.rect.y - 32, width: 384, height: 404,
  });
  assert.strictEqual(panel.opts.hasShadow, false, "a transparent panel draws its own shadow");
  assert.ok(panel.calls.some((c) => c[0] === "allWorkspaces"), "it follows the avatar across workspaces");
  ready(fake, panel);
  assert.ok(panel.calls.some((c) => c[0] === "focus"), "a user opened it, so it takes focus");
  assert.deepStrictEqual(sentOn(panel, CHANNELS.placed), [[{ side: "left" }]]);
  assert.deepStrictEqual(sentOn(left, CHANNELS.windowChange), [["panel", true]]);

  // Opened again from elsewhere: re-placed and brought forward, not rebuilt.
  press(left);
  fake.ipc[CHANNELS.open](fromWindow(left), "panel", { anchor: { x: 10, y: 100, width: 64, height: 64 } });
  assert.deepStrictEqual(windowsOf(fake, "pets", "panel"), [panel]);
  assert.deepStrictEqual(sentOn(panel, CHANNELS.placed).at(-1), [{ side: "right" }]);
  assert.deepStrictEqual(sentOn(left, CHANNELS.windowChange).at(-1), ["panel", true]);

  // Closed by the overlay: the overlay hears it.
  fake.ipc[CHANNELS.close](fromWindow(left), "panel");
  assert.deepStrictEqual(windowsOf(fake, "pets", "panel"), []);
  assert.deepStrictEqual(sentOn(left, CHANNELS.windowChange).at(-1), ["panel", false]);
});

test("a page that loads later hears which of its app's windows are already open", async (t) => {
  const { host, fake } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const [brain] = windowsOf(fake, "pets", "brain");
  ready(fake, brain);
  fake.ipc[CHANNELS.open](fromWindow(windowsOf(fake, "pets", "pet")[0]), "gallery");
  const [gallery] = windowsOf(fake, "pets", "gallery");
  ready(fake, gallery);
  const [left] = windowsOf(fake, "pets", "pet");
  ready(fake, left);
  const told = sentOn(left, CHANNELS.windowChange).map(([id, open]) => `${id}:${open}`).sort();
  assert.deepStrictEqual(told, ["brain:true", "gallery:true"]);
});

test("closeAll closes every window of the app, and the next tick reopens what is declared on enable", async (t) => {
  const { host, fake } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const [left] = windowsOf(fake, "pets", "pet");
  ready(fake, left);
  fake.ipc[CHANNELS.closeAll](fromWindow(left));
  assert.strictEqual(fake.created.filter((w) => !w.destroyed).length, 0);
  await host.reconcileOnce();
  assert.strictEqual(windowsOf(fake, "pets", "pet").length, 2);
  assert.strictEqual(windowsOf(fake, "pets", "brain").length, 1);
});

test("removing the avatar's display hands it to a surviving copy", async (t) => {
  const { host, fake } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const [left, right] = windowsOf(fake, "pets", "pet");
  ready(fake, left);
  ready(fake, right);
  fake.setDisplays([RIGHT]);
  fake.screenEvents["display-removed"]();
  assert.strictEqual(left.destroyed, true);
  assert.deepStrictEqual(sentOn(right, CHANNELS.active).at(-1), [{ active: true }]);
  fake.setDisplays([LEFT, RIGHT]);
  fake.screenEvents["display-added"]();
  assert.strictEqual(windowsOf(fake, "pets", "pet").length, 2, "a plugged display gets a copy");
  assert.deepStrictEqual(sentOn(right, CHANNELS.active).at(-1), [{ active: true }], "the avatar stays put");
});

test("removing the only display a spanning overlay was on moves it to the display that remains", async (t) => {
  const INTERNAL = { id: 3, bounds: { x: 0, y: 0, width: 1280, height: 800 }, workArea: { x: 0, y: 0, width: 1280, height: 800 } };
  const { host, fake } = await started([appRow("pets", COMPANION)], {}, { x: 1500, y: 100 }, [RIGHT]);
  t.after(() => host.stop());
  const [brain] = windowsOf(fake, "pets", "brain");
  ready(fake, brain);
  const [only] = windowsOf(fake, "pets", "pet");
  ready(fake, only);
  // Undocked: the internal display is already listed when the external one goes.
  fake.setDisplays([INTERNAL]);
  fake.screenEvents["display-removed"]();
  const after = windowsOf(fake, "pets", "pet");
  assert.strictEqual(after.length, 1);
  assert.deepStrictEqual(after[0].getBounds(), INTERNAL.bounds);
  assert.ok(!sentOn(brain, CHANNELS.windowChange).some(([id, open]) => id === "pet" && !open), "never reported closed");
  ready(fake, after[0]);
  assert.deepStrictEqual(sentOn(after[0], CHANNELS.active), [[{ active: true }]]);
});

test("a copy plugged in mid-drag takes the pointer and hears the drag start", async (t) => {
  const { host, fake } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const [left] = windowsOf(fake, "pets", "pet");
  ready(fake, left);
  press(left);
  fake.ipc[CHANNELS.beginDrag](fromWindow(left), { offsetX: 0, offsetY: 0, width: 100, height: 100 });
  const plugged = { id: 3, bounds: { x: 0, y: 800, width: 1000, height: 800 }, workArea: { x: 0, y: 800, width: 1000, height: 800 } };
  fake.setDisplays([LEFT, RIGHT, plugged]);
  fake.screenEvents["display-added"]();
  const fresh = windowsOf(fake, "pets", "pet").find((w) => w.getBounds().y === 800);
  assert.ok(fresh, "the plugged display gets a copy");
  assert.strictEqual(fresh.ignore.at(-1), false, "it takes the release");
  ready(fake, fresh);
  assert.deepStrictEqual(sentOn(fresh, CHANNELS.drag), [["start"]]);
});

test("a copy that clears its regions mid-drag still takes the release when the avatar comes back", async (t) => {
  const { host, fake, clock } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const [left, right] = windowsOf(fake, "pets", "pet");
  ready(fake, left);
  ready(fake, right);
  press(left);
  fake.ipc[CHANNELS.beginDrag](fromWindow(left), { offsetX: 0, offsetY: 0, width: 100, height: 100 });
  fake.setCursor({ x: 1500, y: 400 });
  clock.tick();
  // The page that lost the avatar hides it and reports no regions.
  fake.ipc[CHANNELS.hitRegions](fromWindow(left), []);
  assert.strictEqual(left.ignore.at(-1), false, "the drag keeps the pointer");
  host._pollHitRegionsOnce();
  fake.setCursor({ x: 300, y: 200 });
  clock.tick();
  assert.deepStrictEqual(sentOn(left, CHANNELS.active).at(-1), [{ active: true, x: 300, y: 200, dragging: true }]);
  assert.strictEqual(left.ignore.at(-1), false, "the release can land on it");
  fake.ipc[CHANNELS.endDrag](fromWindow(left));
  assert.deepStrictEqual([left.ignore.at(-1), right.ignore.at(-1)], [true, true]);
});

test("a page cannot start a drag or pass as acted in without a press the shell saw", async (t) => {
  const { host, fake, clock, calls, state } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const [left, right] = windowsOf(fake, "pets", "pet");
  ready(fake, left);
  ready(fake, right);
  const link = fake.ipc[CHANNELS.openExternal];
  const show = fake.invoke[CHANNELS.showSession];
  // A page's timer: it reports regions covering everything, so the poll makes
  // it take the pointer wherever the cursor is, but nobody pressed anything.
  fake.ipc[CHANNELS.hitRegions](fromWindow(left), [{ x: -50_000, y: -50_000, width: 100_000, height: 100_000 }]);
  host._pollHitRegionsOnce();
  assert.strictEqual(left.ignore.at(-1), false);
  fake.ipc[CHANNELS.beginDrag](fromWindow(left), { offsetX: 0, offsetY: 0, width: 100, height: 100 });
  assert.strictEqual(clock.running(), 0, "no drag starts");
  assert.strictEqual(right.ignore.at(-1), true, "the other display still clicks through");
  link(fromWindow(left), "https://example.com/timer");
  assert.strictEqual(show(fromWindow(left), "slot-0"), false);
  assert.deepStrictEqual([calls.links, calls.sessions], [[], []]);

  // A press the shell saw: one request is answered, then the next needs another.
  press(left);
  link(fromWindow(left), "https://example.com/pressed");
  link(fromWindow(left), "https://example.com/again");
  // The same click's release, and a held key's repeats, are not another press.
  press(left, "mouseUp");
  link(fromWindow(left), "https://example.com/release");
  left.webContents.events["input-event"]({}, { type: "rawKeyDown", isAutoRepeat: true });
  link(fromWindow(left), "https://example.com/repeat");
  assert.deepStrictEqual(calls.links, ["https://example.com/pressed"]);
  // A press too long ago does not count.
  press(left, "rawKeyDown");
  state.now += 2_001;
  assert.strictEqual(show(fromWindow(left), "slot-1"), false);
  // A drag the user starts makes every copy take the pointer; that is not a press.
  press(left);
  fake.ipc[CHANNELS.beginDrag](fromWindow(left), { offsetX: 0, offsetY: 0, width: 100, height: 100 });
  assert.strictEqual(clock.running(), 1);
  assert.strictEqual(right.ignore.at(-1), false);
  link(fromWindow(right), "https://example.com/other-copy");
  assert.deepStrictEqual(calls.links, ["https://example.com/pressed"]);
});

test("a drag released over a copy still loading settles the avatar there once it is ready", async (t) => {
  const { host, fake, clock } = await started([appRow("pets", COMPANION)], {}, { x: 1500, y: 400 });
  t.after(() => host.stop());
  const [left, right] = windowsOf(fake, "pets", "pet");
  ready(fake, right);
  press(right);
  fake.ipc[CHANNELS.beginDrag](fromWindow(right), { offsetX: 10, offsetY: 20, width: 128, height: 128 });
  fake.setCursor({ x: 300, y: 200 });
  clock.tick();
  fake.setCursor({ x: 5, y: 790 });
  fake.ipc[CHANNELS.endDrag](fromWindow(right));
  assert.deepStrictEqual(sentOn(left, CHANNELS.drag), [], "a page that is not ready hears nothing yet");
  ready(fake, left);
  assert.deepStrictEqual(sentOn(left, CHANNELS.active), [[{ active: true, x: -5, y: 672 }]]);
});

test("a background page opened on demand that dies twice at once is reopened by the next tick", async (t) => {
  const ON_DEMAND = {
    windows: [
      { id: "pet", entry: "pet.mjs", kind: "overlay", openOnEnable: true },
      { id: "brain", entry: "brain.mjs", kind: "background" },
    ],
  };
  const { host, fake, state } = await started([appRow("pets", ON_DEMAND)]);
  t.after(() => host.stop());
  /** The pet window, ready. */
  const pet = () => {
    const [win] = windowsOf(fake, "pets", "pet");
    if (!sentOn(win, CHANNELS.windowChange).length && !win.isVisible()) ready(fake, win);
    return win;
  };
  const crashTwice = () => {
    state.now += 10_000;
    fake.ipc[CHANNELS.open](fromWindow(pet()), "brain");
    windowsOf(fake, "pets", "brain")[0].webContents.events["render-process-gone"]();
    windowsOf(fake, "pets", "brain")[0].webContents.events["render-process-gone"]();
    assert.deepStrictEqual(windowsOf(fake, "pets", "brain"), []);
  };
  crashTwice();
  await host.reconcileOnce();
  assert.strictEqual(windowsOf(fake, "pets", "brain").length, 1);
  fake.ipc[CHANNELS.close](fromWindow(pet()), "brain");

  // Closed on purpose while a reopen is due: the tick leaves it closed.
  crashTwice();
  fake.ipc[CHANNELS.close](fromWindow(pet()), "brain");
  await host.reconcileOnce();
  assert.deepStrictEqual(windowsOf(fake, "pets", "brain"), []);

  // Every window of the app closed while a reopen is due: the same.
  crashTwice();
  fake.ipc[CHANNELS.closeAll](fromWindow(pet()));
  await host.reconcileOnce();
  assert.deepStrictEqual(windowsOf(fake, "pets", "brain"), []);
  assert.strictEqual(windowsOf(fake, "pets", "pet").length, 1, "the tick reopens what is declared on enable");
});

test("an anchored panel is no bigger than the work area it opens on", async (t) => {
  const BIG = {
    windows: [
      { id: "pet", entry: "pet.mjs", kind: "overlay", openOnEnable: true },
      { id: "panel", entry: "panel.mjs", kind: "panel", width: 1600, height: 1200, transparent: true, gutter: 24 },
    ],
  };
  const { host, fake } = await started([appRow("pets", BIG)]);
  t.after(() => host.stop());
  const [pet] = windowsOf(fake, "pets", "pet");
  ready(fake, pet);
  fake.ipc[CHANNELS.open](fromWindow(pet), "panel", { anchor: { x: 100, y: 100, width: 64, height: 64 } });
  const [panel] = windowsOf(fake, "pets", "panel");
  const b = panel.getBounds();
  assert.deepStrictEqual([b.width, b.height], [LEFT.workArea.width, LEFT.workArea.height]);
});

test("an anchored panel with no gutter as big as the work area keeps its card inside it", async (t) => {
  const BIG = {
    windows: [
      { id: "pet", entry: "pet.mjs", kind: "overlay", openOnEnable: true },
      { id: "panel", entry: "panel.mjs", kind: "panel", width: 1600, height: 1200 },
    ],
  };
  const { host, fake } = await started([appRow("pets", BIG)]);
  t.after(() => host.stop());
  const [pet] = windowsOf(fake, "pets", "pet");
  ready(fake, pet);
  fake.ipc[CHANNELS.open](fromWindow(pet), "panel", { anchor: { x: 100, y: 100, width: 64, height: 64 } });
  const [panel] = windowsOf(fake, "pets", "panel");
  // LEFT's work area is 1000x775 at (0,25); the card keeps 8 px from each edge.
  assert.deepStrictEqual(panel.getBounds(), { x: 8, y: 33, width: 984, height: 759 });
});

test("only a window the user is acting in may raise the dashboard or open an https link", async (t) => {
  const { host, fake, calls } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const [left] = windowsOf(fake, "pets", "pet");
  const [brain] = windowsOf(fake, "pets", "brain");
  const show = fake.invoke[CHANNELS.showSession];
  ready(fake, left);
  ready(fake, brain);
  // Not before the user presses anything in it.
  assert.strictEqual(show(fromWindow(left), "slot-0"), false);
  press(left);
  assert.strictEqual(show(fromWindow(left), "slot-1"), true);
  press(brain);
  assert.strictEqual(show(fromWindow(brain), "slot-2"), false, "never a background page");
  press(left);
  assert.strictEqual(show(fromWindow(left), 42), false);
  press(left);
  assert.strictEqual(show(fromWindow(left), "s".repeat(600)), false);
  assert.deepStrictEqual(calls.sessions, ["slot-1"]);

  const link = fake.ipc[CHANNELS.openExternal];
  for (const url of ["https://petdex.dev/path?q=1", "http://example.com", "file:///etc/passwd", "not a url"]) {
    press(left);
    link(fromWindow(left), url);
  }
  link(fromWindow(brain), "https://example.com");
  assert.deepStrictEqual(calls.links, ["https://petdex.dev/path?q=1"]);

  // A panel, once the user presses a key in it.
  press(left);
  fake.ipc[CHANNELS.open](fromWindow(left), "gallery");
  const [gallery] = windowsOf(fake, "pets", "gallery");
  ready(fake, gallery);
  link(fromWindow(gallery), "https://example.com/no-press");
  press(gallery, "rawKeyDown");
  link(fromWindow(gallery), "https://example.com/pressed");
  assert.deepStrictEqual(calls.links, ["https://petdex.dev/path?q=1", "https://example.com/pressed"]);
});

test("a background page opens a window without taking the user's focus, and without an anchor", async (t) => {
  const { host, fake } = await started([appRow("pets", COMPANION)]);
  t.after(() => host.stop());
  const [brain] = windowsOf(fake, "pets", "brain");
  ready(fake, brain);
  fake.ipc[CHANNELS.open](fromWindow(brain), "gallery");
  const [gallery] = windowsOf(fake, "pets", "gallery");
  ready(fake, gallery);
  assert.ok(gallery.calls.some((c) => c[0] === "showInactive"));
  assert.ok(!gallery.calls.some((c) => c[0] === "focus"));
  // Its page is never on screen, so a box in it places nothing.
  fake.ipc[CHANNELS.open](fromWindow(brain), "panel", { anchor: { x: 0, y: 0, width: 16, height: 16 } });
  const [panel] = windowsOf(fake, "pets", "panel");
  ready(fake, panel);
  assert.deepStrictEqual(sentOn(panel, CHANNELS.placed), []);
  assert.ok(!panel.calls.some((c) => c[0] === "allWorkspaces"));
});

test("a drag box and a clamp are what the page may ask for", () => {
  assert.deepStrictEqual(sanitizeDragBox({ offsetX: 1, offsetY: 2, width: 3, height: 4 }), { offsetX: 1, offsetY: 2, width: 3, height: 4 });
  assert.strictEqual(sanitizeDragBox({ offsetX: 1, offsetY: 2, width: 0, height: 4 }), null);
  assert.strictEqual(sanitizeDragBox({ offsetX: Infinity, offsetY: 2, width: 3, height: 4 }), null);
  assert.strictEqual(sanitizeDragBox({ offsetX: 1, offsetY: 2, width: 99_999, height: 4 }), null);
  assert.strictEqual(sanitizeDragBox(null), null);
  const area = { width: 1000, height: 800 };
  const avatar = { width: 128, height: 128 };
  assert.deepStrictEqual(clampBox(-500, -10, avatar, area), { x: -64, y: 0 });
  assert.deepStrictEqual(clampBox(5000, 5000, avatar, area), { x: 936, y: 672 });
});
