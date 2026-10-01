"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const { createTunnelKeeper, RESPAWN_DELAY_MS } = require("../tunnel-keeper");

function fakeStore(remoteHosts) {
  const data = { remoteHosts };
  return { get: (key) => data[key], set: (key, value) => { data[key] = value; } };
}

function fakeChild() {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.stdin = Object.assign(new EventEmitter(), { ended: false, end() { this.ended = true; } });
  child.signals = [];
  child.kill = (signal) => { child.signals.push(signal); };
  return child;
}

function harness({ remoteHosts, isWindows = false } = {}) {
  const spawned = [];
  const timers = [];
  const store = fakeStore(remoteHosts);
  const keeper = createTunnelKeeper({
    store,
    port: 5477,
    spawn: (bin, args, options) => {
      const child = fakeChild();
      spawned.push({ bin, args, options, child });
      return child;
    },
    resolveBin: () => "/app/backend-dist/bin/kirocrew",
    isWindows,
    setTimeoutFn: (fn, ms) => { const t = { fn, ms }; timers.push(t); return t; },
    clearTimeoutFn: (t) => { const i = timers.indexOf(t); if (i >= 0) timers.splice(i, 1); },
  });
  return { keeper, spawned, timers, store };
}

const OPTED_IN = { 5477: { host: "devbox.example.com", binPath: "kirocrew", remotePort: "5476", manageTunnel: true } };

test("an opted-in crew gets a keeper for its own port pair, with a stdin lifeline", () => {
  const { keeper, spawned } = harness({ remoteHosts: OPTED_IN });
  assert.equal(keeper.start(), true);
  assert.equal(spawned.length, 1);
  const { bin, args, options } = spawned[0];
  assert.equal(bin, "/app/backend-dist/bin/kirocrew");
  assert.deepEqual(args, [
    "desktop", "tunnel",
    "--host", "devbox.example.com",
    "--local-port", "5477",
    "--remote-port", "5476",
  ]);
  assert.equal(options.stdio[0], "pipe");
  // A second start while the keeper runs must not spawn another forward.
  keeper.start();
  assert.equal(spawned.length, 1);
});

test("without the opt-in, on Windows, or with no crew, nothing is spawned", () => {
  const notOptedIn = { 5477: { ...OPTED_IN[5477], manageTunnel: false } };
  for (const setup of [
    { remoteHosts: notOptedIn },
    { remoteHosts: OPTED_IN, isWindows: true },
    { remoteHosts: {} },
  ]) {
    const { keeper, spawned } = harness(setup);
    assert.equal(keeper.start(), false);
    assert.equal(spawned.length, 0);
  }
});

test("an unsafe stored host is refused rather than handed to ssh", () => {
  const { keeper, spawned } = harness({
    remoteHosts: { 5477: { host: "-oProxyCommand=evil", manageTunnel: true } },
  });
  assert.equal(keeper.start(), false);
  assert.equal(spawned.length, 0);
});

test("a keeper that dies on its own is respawned after the delay", () => {
  const { keeper, spawned, timers } = harness({ remoteHosts: OPTED_IN });
  keeper.start();
  spawned[0].child.emit("exit", 1, null);
  assert.equal(timers.length, 1);
  assert.equal(timers[0].ms, RESPAWN_DELAY_MS);
  timers.shift().fn();
  assert.equal(spawned.length, 2);
});

test("a spawn that fails asynchronously (error, no exit) is respawned too", () => {
  const { keeper, spawned, timers } = harness({ remoteHosts: OPTED_IN });
  keeper.start();
  spawned[0].child.emit("error", Object.assign(new Error("spawn kirocrew ENOENT"), { code: "ENOENT" }));
  assert.equal(timers.length, 1);
  timers.shift().fn();
  assert.equal(spawned.length, 2);
});

test("a crew that opted out since the last start has its forward stopped", () => {
  const { keeper, spawned, store } = harness({ remoteHosts: OPTED_IN });
  keeper.start();
  const { child } = spawned[0];
  store.set("remoteHosts", { 5477: { ...OPTED_IN[5477], manageTunnel: false } });
  assert.equal(keeper.start(), false);
  assert.deepEqual(child.signals, ["SIGTERM"]);
  assert.equal(spawned.length, 1);
});

test("an edited host replaces the running forward instead of retrying the old one", () => {
  const { keeper, spawned, store } = harness({ remoteHosts: OPTED_IN });
  keeper.start();
  const first = spawned[0].child;
  store.set("remoteHosts", { 5477: { ...OPTED_IN[5477], host: "newbox.example.com" } });
  assert.equal(keeper.start(), true);
  assert.deepEqual(first.signals, ["SIGTERM"]);
  assert.equal(spawned.length, 2);
  assert.equal(spawned[1].args[spawned[1].args.indexOf("--host") + 1], "newbox.example.com");
  // Unchanged coordinates leave the running forward alone.
  keeper.start();
  assert.equal(spawned.length, 2);
});

test("stop closes the lifeline, signals the keeper, and nothing respawns", () => {
  const { keeper, spawned, timers } = harness({ remoteHosts: OPTED_IN });
  keeper.start();
  const { child } = spawned[0];
  keeper.stop();
  assert.equal(child.stdin.ended, true);
  assert.deepEqual(child.signals, ["SIGTERM"]);
  child.emit("exit", null, "SIGTERM");
  assert.equal(timers.length, 0);
  assert.equal(spawned.length, 1);
});

test("restart on wake replaces the keeper once, without a stray respawn", () => {
  const { keeper, spawned, timers } = harness({ remoteHosts: OPTED_IN });
  keeper.start();
  const first = spawned[0].child;
  keeper.restart();
  assert.deepEqual(first.signals, ["SIGTERM"]);
  assert.equal(spawned.length, 2);
  // The replaced keeper's own exit arrives late and must not schedule another.
  first.emit("exit", null, "SIGTERM");
  assert.equal(timers.length, 0);
  assert.equal(spawned.length, 2);
});

test("restart before any start is a no-op", () => {
  const { keeper, spawned } = harness({ remoteHosts: OPTED_IN });
  keeper.restart();
  assert.equal(spawned.length, 0);
});
