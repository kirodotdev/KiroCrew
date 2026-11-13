"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");

const {
  MINIMIZE_TO_TRAY_KEY,
  createMinimizeHandler,
  createMinimizeToTray,
  getMinimizeToTray,
  hasTray,
  setMinimizeToTray,
} = require("../minimize-to-tray");

function fakeStore(initial = {}) {
  const data = { ...initial };
  const writes = [];
  return {
    data,
    writes,
    get: (key) => data[key],
    set(key, value) {
      data[key] = value;
      writes.push([key, value]);
    },
  };
}

const TRAY = { trayAvailable: true, trayShown: true };
const NO_TRAY = { trayAvailable: false, trayShown: true };
const ICON_HIDDEN = { trayAvailable: true, trayShown: false };

test("an untouched store is off, and offered only where a tray can bring the window back", () => {
  assert.deepEqual(getMinimizeToTray(fakeStore(), TRAY), { on: false, offered: true, canEnable: true });
  assert.deepEqual(getMinimizeToTray(fakeStore(), NO_TRAY), { on: false, offered: false, canEnable: false });
});

test("it turns on only while a tray is available and its icon shows", () => {
  const store = fakeStore();
  assert.deepEqual(setMinimizeToTray(store, true, TRAY), { on: true, offered: true, canEnable: true });
  assert.deepEqual(store.writes, [[MINIMIZE_TO_TRAY_KEY, true]]);

  for (const o of [NO_TRAY, ICON_HIDDEN]) {
    const refused = fakeStore();
    assert.equal(setMinimizeToTray(refused, true, o).on, false);
    assert.deepEqual(refused.writes, [], "a refused turn-on writes nothing");
  }
});

test("a stored choice is off while the tray icon is hidden, and returns with the icon", () => {
  const store = fakeStore({ [MINIMIZE_TO_TRAY_KEY]: true });
  assert.deepEqual(getMinimizeToTray(store, ICON_HIDDEN), { on: false, offered: true, canEnable: false });
  assert.equal(getMinimizeToTray(store, TRAY).on, true);
});

test("only an exact true turns it on, and turning it off is always allowed", () => {
  for (const value of ["true", 1, {}, "yes"]) {
    assert.equal(getMinimizeToTray(fakeStore({ [MINIMIZE_TO_TRAY_KEY]: value }), TRAY).on, false);
    assert.equal(setMinimizeToTray(fakeStore(), value, TRAY).on, false);
  }
  const store = fakeStore({ [MINIMIZE_TO_TRAY_KEY]: true });
  assert.equal(setMinimizeToTray(store, false, ICON_HIDDEN).on, false);
  assert.deepEqual(store.writes, [[MINIMIZE_TO_TRAY_KEY, false]]);
  setMinimizeToTray(store, false, TRAY);
  assert.equal(store.writes.length, 1, "a write that changes nothing is skipped");
});

test("Windows always has a tray and macOS is never offered, without asking anything", async () => {
  const run = () => assert.fail("no process is started");
  assert.equal(await hasTray("win32", run), true);
  assert.equal(await hasTray("darwin", run), false);
});

const SYSTEM_DBUS_SEND = "/usr/bin/dbus-send";
const inSystemDir = (name) => (name === "dbus-send" ? SYSTEM_DBUS_SEND : null);

test("on Linux a tray is there only when a StatusNotifier host is registered", async () => {
  const answering = (err, stdout) => (file, args, opts, cb) => {
    assert.equal(file, SYSTEM_DBUS_SEND, "the resolved absolute path is run, never a PATH lookup");
    assert.ok(args.includes("string:IsStatusNotifierHostRegistered"));
    assert.ok(opts.timeout > 0, "a hung bus cannot hold the answer");
    cb(err, stdout);
  };
  assert.equal(await hasTray("linux", answering(null, "   variant       boolean true\n"), inSystemDir), true);
  assert.equal(await hasTray("linux", answering(null, "   variant       boolean false\n"), inSystemDir), false);
  // GNOME with no tray extension has no watcher, so the call fails.
  assert.equal(await hasTray("linux", answering(new Error("ServiceUnknown"), ""), inSystemDir), false);
});

test("on Linux with no dbus-send in a system directory there is no tray, and nothing is started", async () => {
  const asked = [];
  const run = () => assert.fail("no process is started");
  const notFound = (name) => {
    asked.push(name);
    return null;
  };
  assert.equal(await hasTray("linux", run, notFound), false);
  assert.deepEqual(asked, ["dbus-send"]);
});

// A main window that records what the handler did, minimized until restored.
function fakeWindow() {
  const calls = [];
  return {
    calls,
    minimized: true,
    isDestroyed: () => false,
    isMinimized() { return this.minimized; },
    hide: () => calls.push("hide"),
  };
}

// Run one minimize through the handler and wait for its tray check.
async function minimizeWith({ stored = true, available = true, shown = true, restoredMeanwhile = false } = {}) {
  const win = fakeWindow();
  let probes = 0;
  await createMinimizeHandler({
    win,
    store: fakeStore({ [MINIMIZE_TO_TRAY_KEY]: stored }),
    trayShown: () => shown,
    trayAvailable: async () => {
      probes += 1;
      if (restoredMeanwhile) win.minimized = false;
      return available;
    },
    log: () => {},
  })();
  return { win, probes };
}

test("a minimize hides the window to the tray when the switch is on and a tray is there", async () => {
  assert.deepEqual((await minimizeWith()).win.calls, ["hide"]);
});

test("otherwise the window minimizes to the taskbar as usual", async () => {
  for (const [why, o] of [["no tray", { available: false }], ["icon hidden", { shown: false }]]) {
    assert.deepEqual((await minimizeWith(o)).win.calls, [], why);
  }
  const off = await minimizeWith({ stored: false });
  assert.equal(off.probes, 0, "the desktop is not asked while the switch is off");
  assert.deepEqual(off.win.calls, []);
});

test("a window restored while the tray is being checked stays on screen", async () => {
  assert.deepEqual((await minimizeWith({ restoredMeanwhile: true })).win.calls, []);
});

test("the main process's facade reads the tray live and logs each write", async () => {
  const store = fakeStore();
  const lines = [];
  let shown = true;
  const facade = createMinimizeToTray({
    store,
    platform: "linux",
    trayShown: () => shown,
    trayAvailable: async () => true,
    log: (line) => lines.push(line),
  });
  assert.deepEqual(await facade.set(true), { on: true, offered: true, canEnable: true });
  shown = false;
  assert.deepEqual(await facade.get(), { on: false, offered: true, canEnable: false });
  assert.deepEqual(lines, ["minimizeToTray set to true"]);

  const win = fakeWindow();
  shown = true;
  await facade.minimizeHandler(win)();
  assert.deepEqual(win.calls, ["hide"]);
});
