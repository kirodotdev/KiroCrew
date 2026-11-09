"use strict";

// Issue 29949 (G1/G2): the desktop app supervises this computer's local
// controller when no local gateway runs. Authored before the module; not run
// locally (host rule), CI runs it.

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const fs = require("node:fs");
const path = require("node:path");

const {
  createConnectedComputersHost,
  HOST_ARGS,
  EXIT_DISABLED,
  EXIT_ELSEWHERE,
  RESPAWN_DELAY_MS,
  MAX_PENDING,
  MAX_STDOUT_BUFFER,
} = require("../runtime/gateway/connected-computers-host");

const OWNER_PATH = path.join(__dirname, "..", "runtime", "gateway", "connected-computers-host.js");

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
  child.signals = [];
  child.kill = (signal) => { child.signals.push(signal); };
  child.reply = (obj) => child.stdout.emit("data", Buffer.from(JSON.stringify(obj) + "\n"));
  return child;
}

function harness({ isWindows = false, instances = [{ id: "desktop-7778" }] } = {}) {
  const spawned = [];
  const timers = [];
  const states = [];
  const host = createConnectedComputersHost({
    spawn: (bin, args, opts) => {
      const child = fakeChild();
      spawned.push({ bin, args, opts, child });
      return child;
    },
    resolveBin: () => "/opt/kc/bin/kirocrew",
    getEnv: () => ({ PATH: "/usr/bin" }),
    getInstances: () => instances,
    isWindows,
    onState: (state) => states.push(state),
    setTimeoutFn: (fn, ms) => { const t = { fn, ms, cleared: false }; timers.push(t); return t; },
    clearTimeoutFn: (t) => { if (t) t.cleared = true; },
  });
  return { host, spawned, timers, states };
}

test("G1: start spawns the fixed desktop-host subcommand with pipes and sends hello first", () => {
  const { host, spawned } = harness();
  assert.equal(host.start(), true);
  assert.equal(spawned.length, 1);
  assert.deepEqual(spawned[0].args, HOST_ARGS);
  assert.deepEqual(HOST_ARGS, ["connected-computer", "desktop-host"]);
  assert.deepEqual(spawned[0].opts.stdio, ["pipe", "pipe", "pipe"]);
  const [hello] = spawned[0].child.written;
  assert.equal(hello.op, "hello");
  assert.deepEqual(hello.instances, [{ id: "desktop-7778" }]);
});

test("G1: the hello reply sets the state the menu reads", () => {
  const { host, spawned, states } = harness();
  host.start();
  const { child } = spawned[0];
  child.reply({ id: child.written[0].id, ok: true, state: "running", protection: "login Keychain" });
  assert.equal(host.state(), "running");
  assert.equal(host.protection(), "login Keychain");
  assert.ok(states.includes("running"));
});

test("B06: Windows never spawns a host and reports unavailable", () => {
  const { host, spawned } = harness({ isWindows: true });
  assert.equal(host.start(), false);
  assert.equal(spawned.length, 0);
  assert.equal(host.state(), "unavailable");
});

test("no remote crew configured: nothing is spawned", () => {
  const { host, spawned } = harness({ instances: [] });
  assert.equal(host.start(), false);
  assert.equal(spawned.length, 0);
});

test("requests carry ids and resolve with the matching reply", async () => {
  const { host, spawned } = harness();
  host.start();
  const { child } = spawned[0];
  child.reply({ id: child.written[0].id, ok: true, state: "running" });
  const pending = host.request("status", {});
  const sent = child.written[1];
  assert.equal(sent.op, "status");
  child.reply({ id: sent.id, ok: true, grants: [] });
  assert.deepEqual(await pending, { id: sent.id, ok: true, grants: [] });
});

test("a reply split across chunks is reassembled", async () => {
  const { host, spawned } = harness();
  host.start();
  const { child } = spawned[0];
  child.reply({ id: child.written[0].id, ok: true, state: "running" });
  const pending = host.request("status", {});
  const line = JSON.stringify({ id: child.written[1].id, ok: true, grants: [] }) + "\n";
  child.stdout.emit("data", Buffer.from(line.slice(0, 5)));
  child.stdout.emit("data", Buffer.from(line.slice(5)));
  assert.equal((await pending).ok, true);
});

test("a request before the host runs never reaches it", async () => {
  const { host } = harness();
  assert.deepEqual(await host.request("share", {}), { ok: false, code: "not_running" });
});

test("pending requests are bounded; one past the bound is refused, not queued", async () => {
  const { host, spawned } = harness();
  host.start();
  const { child } = spawned[0];
  child.reply({ id: child.written[0].id, ok: true, state: "running" });
  for (let n = 0; n < MAX_PENDING; n++) host.request("status", {});
  assert.deepEqual(await host.request("status", {}), { ok: false, code: "busy" });
  assert.equal(child.written.length, 1 + MAX_PENDING);
});

test("a request that times out resolves with timeout, never rejects", async () => {
  const { host, spawned, timers } = harness();
  host.start();
  const { child } = spawned[0];
  child.reply({ id: child.written[0].id, ok: true, state: "running" });
  const pending = host.request("status", {}, { timeoutMs: 1000 });
  const timer = timers.find((t) => t.ms === 1000);
  timer.fn();
  assert.deepEqual(await pending, { ok: false, code: "timeout" });
});

test("B08: an exit answers every waiting request with host_exited", async () => {
  const { host, spawned } = harness();
  host.start();
  const { child } = spawned[0];
  child.reply({ id: child.written[0].id, ok: true, state: "running" });
  const pending = host.request("share", {});
  child.emit("exit", 1, null);
  assert.deepEqual(await pending, { ok: false, code: "host_exited" });
});

test("FLAG: exit code 3 means the switch is off; no respawn", () => {
  const { host, spawned, timers } = harness();
  host.start();
  spawned[0].child.emit("exit", EXIT_DISABLED, null);
  assert.equal(host.state(), "disabled");
  assert.equal(timers.filter((t) => t.ms === RESPAWN_DELAY_MS).length, 0);
});

test("G2: exit code 4 means a local gateway runs sharing; no respawn", () => {
  const { host, spawned, timers } = harness();
  host.start();
  spawned[0].child.emit("exit", EXIT_ELSEWHERE, null);
  assert.equal(host.state(), "elsewhere");
  assert.equal(timers.filter((t) => t.ms === RESPAWN_DELAY_MS).length, 0);
});

test("G2: an unexpected exit respawns after the delay, and the respawn says hello again", () => {
  const { host, spawned, timers } = harness();
  host.start();
  spawned[0].child.emit("exit", 1, null);
  const timer = timers.find((t) => t.ms === RESPAWN_DELAY_MS);
  assert.ok(timer, "a respawn is scheduled");
  timer.fn();
  assert.equal(spawned.length, 2);
  assert.equal(spawned[1].child.written[0].op, "hello");
});

test("G2: repeated crashes stop respawning and report failed", () => {
  const { host, spawned, timers } = harness();
  host.start();
  for (let n = 0; n < 10; n++) {
    const last = spawned[spawned.length - 1];
    last.child.emit("exit", 1, null);
    const timer = timers.filter((t) => t.ms === RESPAWN_DELAY_MS && !t.fired && !t.cleared).pop();
    if (!timer) break;
    timer.fired = true;
    timer.fn();
  }
  assert.equal(host.state(), "failed");
  assert.ok(spawned.length <= 6);
});

test("G2: stop ends the stdin lifeline first, then SIGTERM after the grace", () => {
  const { host, spawned, timers } = harness();
  host.start();
  const { child } = spawned[0];
  host.stop();
  assert.equal(child.stdin.ended, true);
  assert.deepEqual(child.signals, []);
  const grace = timers.find((t) => t.ms > RESPAWN_DELAY_MS);
  grace.fn();
  assert.deepEqual(child.signals, ["SIGTERM"]);
  // A stopped host is never respawned.
  child.emit("exit", 0, null);
  assert.equal(timers.filter((t) => t.ms === RESPAWN_DELAY_MS).length, 0);
});

test("an endless stdout line kills the host instead of buffering it", () => {
  const { host, spawned } = harness();
  host.start();
  const { child } = spawned[0];
  child.stdout.emit("data", Buffer.alloc(MAX_STDOUT_BUFFER + 1, 120));
  assert.deepEqual(child.signals, ["SIGTERM"]);
});

test("syncInstances re-sends the configured crews to a running host", () => {
  const instances = [{ id: "desktop-7778" }];
  const { host, spawned } = harness({ instances });
  host.start();
  const { child } = spawned[0];
  child.reply({ id: child.written[0].id, ok: true, state: "running" });
  instances.push({ id: "desktop-7779" });
  host.syncInstances();
  const last = child.written[child.written.length - 1];
  assert.equal(last.op, "instances");
  assert.deepEqual(last.items.map((i) => i.id), ["desktop-7778", "desktop-7779"]);
});

test("the owner follows the runtime-owner rules: no electron, fs or child_process", () => {
  const source = fs.readFileSync(OWNER_PATH, "utf8");
  for (const mod of ["electron", "fs", "child_process", "http", "net"]) {
    assert.ok(!new RegExp(`require\\("(node:)?${mod}"\\)`).test(source), `${mod} must be injected`);
  }
  assert.ok(!/__dirname/.test(source));
});

// -- instancesFromStore: the app's own connections, in the host's hello shape ----

const { instancesFromStore, MAX_INSTANCES } = require("../runtime/gateway/connected-computers-host");

function storeOf(remoteHosts) {
  return { get: (key) => (key === "remoteHosts" ? remoteHosts : undefined) };
}

test("B13: connections come from the app's store, keyed by their local port", () => {
  const items = instancesFromStore(storeOf({
    7778: { host: "clouddesk", binPath: "~/.local/bin/kirocrew", remotePort: "5476", defaultName: "Cloud" },
  }));
  assert.deepEqual(items, [{
    id: "desktop-7778",
    name: "Cloud",
    ssh_host: "clouddesk",
    remote_bin: "~/.local/bin/kirocrew",
    remote_port: 5476,
    local_port: 7778,
  }]);
});

test("B13: an entry the host would refuse is left out, the rest kept", () => {
  const items = instancesFromStore(storeOf({
    7001: { host: "-oProxyCommand=evil", binPath: "" },
    7002: { host: "ok-host", binPath: "$(evil)" },
    7003: { host: "ok-host", binPath: "", remotePort: "5476x" },
    "7004-old": { host: "ok-host", binPath: "" },
    7005: { defaultName: "title only" },
    7006: { host: "user@good.example", binPath: "", remotePort: "" },
  }));
  assert.deepEqual(items.map((i) => i.id), ["desktop-7006"]);
  assert.equal(items[0].remote_port, 0);
  assert.equal(items[0].name, "user@good.example");
});

test("never more connections than the host accepts in one hello", () => {
  const hosts = {};
  for (let n = 0; n < MAX_INSTANCES + 4; n++) hosts[String(6000 + n)] = { host: "h", binPath: "" };
  assert.equal(instancesFromStore(storeOf(hosts)).length, MAX_INSTANCES);
});

test("an empty or missing store names nothing", () => {
  assert.deepEqual(instancesFromStore(storeOf(undefined)), []);
  assert.deepEqual(instancesFromStore(null), []);
});

