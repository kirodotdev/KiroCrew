"use strict";

// session-lock.js: relaying the OS lock to every live dashboard view.

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const { SESSION_LOCKED_CHANNEL, dashboardContents, watchSessionLock } = require("../session-lock");

function fakeContents({ destroyed = false, throws = false } = {}) {
  const sent = [];
  return {
    sent,
    isDestroyed: () => destroyed,
    send: (channel) => {
      if (throws) throw new Error("gone");
      sent.push(channel);
    },
  };
}

function fakeWindow(wc, { destroyed = false } = {}) {
  return { isDestroyed: () => destroyed, _mcView: wc ? { webContents: wc } : undefined };
}

test("tells every live dashboard view when the session locks", () => {
  const power = new EventEmitter();
  const a = fakeContents();
  const b = fakeContents();
  const logs = [];
  watchSessionLock(power, () => [fakeWindow(a), fakeWindow(b)], (line) => logs.push(line));
  power.emit("lock-screen");
  assert.deepEqual(a.sent, [SESSION_LOCKED_CHANNEL]);
  assert.deepEqual(b.sent, [SESSION_LOCKED_CHANNEL]);
  assert.match(logs[0], /told 2 dashboard view/);
});

test("reaches a window opened after the relay was armed", () => {
  const power = new EventEmitter();
  const windows = [];
  watchSessionLock(power, () => windows);
  const late = fakeContents();
  windows.push(fakeWindow(late));
  power.emit("lock-screen");
  assert.deepEqual(late.sent, [SESSION_LOCKED_CHANNEL]);
});

test("skips non-dashboard and destroyed windows, and one failing view does not stop the rest", () => {
  const power = new EventEmitter();
  const live = fakeContents();
  const broken = fakeContents({ throws: true });
  const windows = [
    fakeWindow(null),
    fakeWindow(fakeContents(), { destroyed: true }),
    fakeWindow(fakeContents({ destroyed: true })),
    fakeWindow(broken),
    fakeWindow(live),
  ];
  const logs = [];
  watchSessionLock(power, () => windows, (line) => logs.push(line));
  power.emit("lock-screen");
  assert.deepEqual(live.sent, [SESSION_LOCKED_CHANNEL]);
  assert.match(logs[0], /told 1 dashboard view/);
});

test("dashboardContents keeps only live dashboard views", () => {
  const live = fakeContents();
  assert.deepEqual(dashboardContents([fakeWindow(live), fakeWindow(null), null]), [live]);
  assert.deepEqual(dashboardContents(undefined), []);
});

test("does nothing on other power events, and stops relaying once disarmed", () => {
  const power = new EventEmitter();
  const wc = fakeContents();
  const stop = watchSessionLock(power, () => [fakeWindow(wc)]);
  power.emit("resume");
  power.emit("unlock-screen");
  assert.deepEqual(wc.sent, []);
  stop();
  power.emit("lock-screen");
  assert.deepEqual(wc.sent, []);
  assert.equal(power.listenerCount("lock-screen"), 0);
});
