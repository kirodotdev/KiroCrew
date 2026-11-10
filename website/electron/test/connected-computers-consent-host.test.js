"use strict";

// Issue 29949 (B13): the consent-only desktop host the app spawns for one share
// asked for from the local gateway's dashboard. Its hello says consent_only, it
// serves describe and share only, it is never respawned, and startAndWait
// reports the state its hello settled on. Not run locally (host rule), CI runs it.

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const {
  createConnectedComputersHost,
  EXIT_DISABLED,
  RESPAWN_DELAY_MS,
} = require("../runtime/gateway/connected-computers-host");

function fakeChild() {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.written = [];
  child.stdin = Object.assign(new EventEmitter(), {
    ended: false,
    write(chunk) { child.written.push(JSON.parse(String(chunk))); return true; },
    end() { this.ended = true; },
  });
  child.kill = () => {};
  child.reply = (obj) => child.stdout.emit("data", Buffer.from(JSON.stringify(obj) + "\n"));
  return child;
}

function harness({ instances = [], supported = true, spawnThrows = false } = {}) {
  const spawned = [];
  const timers = [];
  const host = createConnectedComputersHost({
    consentOnly: true,
    enabled: () => true,
    spawn: () => {
      if (spawnThrows) throw new Error("no binary");
      const child = fakeChild();
      spawned.push(child);
      return child;
    },
    resolveBin: () => "/opt/kc/bin/kirocrew",
    getInstances: () => instances,
    supported,
    setTimeoutFn: (fn, ms) => { const t = { fn, ms, cleared: false }; timers.push(t); return t; },
    clearTimeoutFn: (t) => { if (t) t.cleared = true; },
  });
  return { host, spawned, timers };
}

test("B13: the hello says consent_only, never revoke_saved, with no app connection needed", () => {
  const { host, spawned } = harness();
  void host.startAndWait();
  assert.equal(spawned.length, 1);
  const [hello] = spawned[0].written;
  assert.equal(hello.op, "hello");
  assert.equal(hello.consent_only, true);
  assert.equal("revoke_saved" in hello, false);
  assert.deepEqual(hello.instances, []);
});

test("startAndWait resolves the state the hello settled on, and requests are served", async () => {
  const { host, spawned } = harness();
  const ready = host.startAndWait();
  const child = spawned[0];
  child.reply({ id: child.written[0].id, ok: true, state: "consent", protection: "Secret Service" });
  assert.equal(await ready, "consent");
  assert.equal(host.protection(), "Secret Service");
  const pending = host.request("describe", { instance_id: "work-crew" });
  const sent = child.written[1];
  assert.equal(sent.op, "describe");
  child.reply({ id: sent.id, ok: true, consent: "t" });
  assert.deepEqual(await pending, { id: sent.id, ok: true, consent: "t" });
});

test("startAndWait reports disabled and a refused hello as failed", async () => {
  for (const [reply, expected] of [
    [{ ok: true, state: "disabled" }, "disabled"],
    [{ ok: false, code: "invalid_request" }, "failed"],
  ]) {
    const { host, spawned } = harness();
    const ready = host.startAndWait();
    spawned[0].reply({ id: spawned[0].written[0].id, ...reply });
    assert.equal(await ready, expected);
  }
});

test("startAndWait times out instead of waiting forever", async () => {
  const { host, timers } = harness();
  const ready = host.startAndWait({ timeoutMs: 1234 });
  const timer = timers.find((t) => t.ms === 1234);
  assert.ok(timer, "a bounded wait is armed");
  timer.fn();
  assert.equal(await ready, "timeout");
});

test("an exit is final: no respawn, and a waiter is told it stopped", async () => {
  const { host, spawned, timers } = harness();
  const ready = host.startAndWait();
  spawned[0].emit("exit", 1, null);
  assert.equal(await ready, "stopped");
  assert.equal(spawned.length, 1);
  assert.equal(timers.some((t) => t.ms === RESPAWN_DELAY_MS), false, "no respawn is scheduled");
  assert.equal(host.state(), "stopped");
});

test("a host exiting disabled after its hello stays disabled", async () => {
  const { host, spawned } = harness();
  const ready = host.startAndWait();
  spawned[0].reply({ id: spawned[0].written[0].id, ok: true, state: "disabled" });
  assert.equal(await ready, "disabled");
  spawned[0].emit("exit", EXIT_DISABLED, null);
  assert.equal(host.state(), "disabled");
  assert.equal(spawned.length, 1);
});

test("a binary that cannot start fails at once and is not retried", async () => {
  const { host, spawned } = harness({ spawnThrows: true });
  assert.equal(await host.startAndWait(), "failed");
  assert.equal(spawned.length, 0);
});

test("B06: off Linux nothing is spawned", async () => {
  const { host, spawned } = harness({ supported: false });
  assert.equal(await host.startAndWait(), "unavailable");
  assert.equal(spawned.length, 0);
});

test("requests are refused before the hello settles and after the host stops", async () => {
  const { host, spawned } = harness();
  void host.startAndWait();
  assert.deepEqual(await host.request("describe", {}), { ok: false, code: "not_running" });
  spawned[0].reply({ id: spawned[0].written[0].id, ok: true, state: "consent" });
  host.stop();
  assert.equal(spawned[0].stdin.ended, true);
  spawned[0].emit("exit", 0, null);
  assert.deepEqual(await host.request("describe", {}), { ok: false, code: "not_running" });
});
