"use strict";

const { test } = require("node:test");
const assert = require("node:assert/strict");

const { CLOSE_ACTION_KEY } = require("../close-action");
const { SHOW_TRAY_ICON_KEY, getTrayIcon, setTrayIcon } = require("../tray-icon");

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

const OFF_MAC = { isMac: false };
const MAC = { isMac: true };

test("an untouched store shows the icon, locked on while the close button hides the window", () => {
  assert.deepEqual(getTrayIcon(fakeStore(), OFF_MAC), { show: true, canHide: false });
  assert.deepEqual(getTrayIcon(fakeStore(), MAC), { show: true, canHide: true });
});

test("off macOS the icon can be hidden only while the close button quits", () => {
  const quits = fakeStore({ [CLOSE_ACTION_KEY]: "quit" });
  assert.deepEqual(setTrayIcon(quits, false, OFF_MAC), { show: false, canHide: true });
  assert.deepEqual(quits.writes, [[SHOW_TRAY_ICON_KEY, false]]);

  const hides = fakeStore({ [CLOSE_ACTION_KEY]: "background" });
  assert.deepEqual(setTrayIcon(hides, false, OFF_MAC), { show: true, canHide: false });
  assert.deepEqual(hides.writes, [], "a refused hide writes nothing");
});

test("macOS keeps the Dock, so the icon can be hidden whatever the close button does", () => {
  const store = fakeStore({ [CLOSE_ACTION_KEY]: "background" });
  assert.deepEqual(setTrayIcon(store, false, MAC), { show: false, canHide: true });
});

test("a stored hide shows the icon again as soon as the close button hides the window", () => {
  // The way back to a hidden window is never lost, whatever the file says.
  const store = fakeStore({ [CLOSE_ACTION_KEY]: "quit", [SHOW_TRAY_ICON_KEY]: false });
  assert.equal(getTrayIcon(store, OFF_MAC).show, false);
  store.data[CLOSE_ACTION_KEY] = "background";
  assert.deepEqual(getTrayIcon(store, OFF_MAC), { show: true, canHide: false });
});

test("only an exact false hides the icon", () => {
  for (const value of ["false", 0, null, "", "no", {}]) {
    const store = fakeStore({ [CLOSE_ACTION_KEY]: "quit", [SHOW_TRAY_ICON_KEY]: value });
    assert.equal(getTrayIcon(store, OFF_MAC).show, true, `${JSON.stringify(value)} must read as shown`);
    assert.equal(setTrayIcon(fakeStore({ [CLOSE_ACTION_KEY]: "quit" }), value, OFF_MAC).show, true);
  }
});

test("a write that changes nothing is skipped", () => {
  const store = fakeStore({ [CLOSE_ACTION_KEY]: "quit", [SHOW_TRAY_ICON_KEY]: true });
  setTrayIcon(store, true, OFF_MAC);
  assert.deepEqual(store.writes, []);
});
