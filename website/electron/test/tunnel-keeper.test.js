"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");

const {
  createTunnelKeeper,
  RESPAWN_DELAY_MS,
  PANE_FORWARD_WAIT_MS,
  MAX_PANE_PORTS,
} = require("../tunnel-keeper");

function fakeStore(remoteHosts) {
  const data = { remoteHosts };
  return { get: (key) => data[key], set: (key, value) => { data[key] = value; } };
}

function fakeChild() {
  const child = new EventEmitter();
  child.stdout = new EventEmitter();
  child.stderr = new EventEmitter();
  child.stdin = Object.assign(new EventEmitter(), {
    ended: false,
    writes: [],
    end() { this.ended = true; },
    write(line) { this.writes.push(line); },
  });
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

test("a crew that did not opt in gets a forward once its port is found unanswered", () => {
  const notOptedIn = { 5477: { ...OPTED_IN[5477], manageTunnel: false } };
  const { keeper, spawned, timers } = harness({ remoteHosts: notOptedIn });
  assert.equal(keeper.startForUnansweredPort(), true);
  assert.equal(spawned.length, 1);
  assert.deepEqual(spawned[0].args.slice(2), [
    "--host", "devbox.example.com", "--local-port", "5477", "--remote-port", "5476",
  ]);
  // It lasts for the launch: a wake's restart and an edit's re-sync keep it.
  keeper.restart();
  assert.equal(keeper.start(), true);
  assert.equal(spawned.length, 2);
  // Until stop: the next start reads the stored choice again.
  keeper.stop();
  assert.equal(keeper.start(), false);
  assert.equal(spawned.length, 2);
  assert.equal(timers.length, 0);
});

test("an unanswered port with no crew, an unsafe host, or on Windows gets nothing", () => {
  for (const setup of [
    { remoteHosts: {} },
    { remoteHosts: { 5477: { host: "-oProxyCommand=evil" } } },
    { remoteHosts: OPTED_IN, isWindows: true },
  ]) {
    const { keeper, spawned } = harness(setup);
    assert.equal(keeper.startForUnansweredPort(), false);
    assert.equal(keeper.start(), false, "a refused fallback does not linger");
    assert.equal(spawned.length, 0);
  }
});

test("a save that leaves the option unticked ends a launch-time fallback", () => {
  const notOptedIn = { 5477: { ...OPTED_IN[5477], manageTunnel: false } };
  const { keeper, spawned, timers, store } = harness({ remoteHosts: notOptedIn });
  assert.equal(keeper.startForUnansweredPort(), true);
  const { child } = spawned[0];
  assert.equal(keeper.applySavedChoice(), false, "the saved false is applied");
  assert.deepEqual(child.signals, ["SIGTERM"]);
  assert.equal(keeper.start(), false, "and the fallback does not come back on the next start");
  assert.equal(spawned.length, 1);
  assert.equal(timers.length, 0);
  // A save that ticks the option keeps a forward, now as the user's own choice.
  store.set("remoteHosts", OPTED_IN);
  assert.equal(keeper.applySavedChoice(), true);
  assert.equal(spawned.length, 2);
});

test("a save in this launch keeps the fallback from reopening a forward it left unticked", () => {
  const notOptedIn = { 5477: { ...OPTED_IN[5477], manageTunnel: false } };
  const { keeper, spawned } = harness({ remoteHosts: notOptedIn });
  assert.equal(keeper.applySavedChoice(), false);
  // Retry probes the port again and finds it unanswered.
  assert.equal(keeper.startForUnansweredPort(), false);
  assert.equal(keeper.start(), false);
  assert.equal(spawned.length, 0);
});

test("a refused fallback leaves no flag behind for a crew stored later", () => {
  // Refused because no crew is stored; the flag must reset with the refusal,
  // or a crew saved unticked afterwards would get a forward it never asked for.
  const { keeper, spawned, store } = harness({ remoteHosts: {} });
  assert.equal(keeper.startForUnansweredPort(), false);
  store.set("remoteHosts", { 5477: { ...OPTED_IN[5477], manageTunnel: false } });
  assert.equal(keeper.start(), false);
  assert.equal(spawned.length, 0);
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

/** What the keeper prints on stdout, as one chunk per call. */
function report(child, ...lines) {
  child.stdout.emit("data", Buffer.from(lines.map((l) => `${l}\n`).join("")));
}

test("a window's pane ports are forwarded through the keeper and answered once it reports", async () => {
  const { keeper, spawned } = harness({ remoteHosts: OPTED_IN });
  keeper.start();
  const { child } = spawned[0];
  const answer = keeper.forwardPanePorts("win-1", [7779, 7778]);
  assert.deepEqual(child.stdin.writes, ["forward 7778\n", "forward 7779\n"]);
  report(child, "tunnel: connected", "tunnel: pane 7778 connected");
  report(child, "tunnel: pane 7779 down (connection refused); retrying in 1s");
  assert.deepEqual(await answer, { 7778: "up", 7779: "down" });
  // A port already reported on is answered at once and not asked for again.
  assert.deepEqual(await keeper.forwardPanePorts("win-1", [7778]), { 7778: "up" });
  assert.deepEqual(child.stdin.writes.slice(2), ["drop 7779\n"]);
});

test("with no managed forward for the launch port the answer is null", async () => {
  const notOptedIn = { 5477: { ...OPTED_IN[5477], manageTunnel: false } };
  const { keeper, spawned } = harness({ remoteHosts: notOptedIn });
  keeper.start();
  assert.equal(await keeper.forwardPanePorts("win-1", [7778]), null);
  assert.equal(spawned.length, 0);
});

test("ports a window showed before the crew opted in are asked for once its keeper starts", async () => {
  const notOptedIn = { 5477: { ...OPTED_IN[5477], manageTunnel: false } };
  const { keeper, spawned, store } = harness({ remoteHosts: notOptedIn });
  keeper.start();
  assert.equal(await keeper.forwardPanePorts("win-1", [7778]), null);
  assert.equal(await keeper.forwardPanePorts("win-2", [7779]), null);
  keeper.releasePaneForwards("win-2");
  store.set("remoteHosts", OPTED_IN); // ticked in the settings, no reload
  keeper.start();
  assert.deepEqual(spawned[0].child.stdin.writes, ["forward 7778\n"]);
});

test("only valid pane ports are asked for, never the dashboard's own", async () => {
  const { keeper, spawned } = harness({ remoteHosts: OPTED_IN });
  keeper.start();
  const { child } = spawned[0];
  keeper.forwardPanePorts("win-1", [7778, 7778, 5477, 0, 70000, 1.5, "7780", null]);
  assert.deepEqual(child.stdin.writes, ["forward 7778\n"]);
  const many = Array.from({ length: MAX_PANE_PORTS + 5 }, (_v, i) => 20000 + i);
  keeper.forwardPanePorts("win-2", many);
  assert.equal(child.stdin.writes.filter((w) => w.startsWith("forward")).length, MAX_PANE_PORTS + 1);
});

test("a port another window still shows survives one window's release", () => {
  const { keeper, spawned } = harness({ remoteHosts: OPTED_IN });
  keeper.start();
  const { child } = spawned[0];
  keeper.forwardPanePorts("win-1", [7778]);
  keeper.forwardPanePorts("win-2", [7778, 7779]);
  assert.deepEqual(child.stdin.writes, ["forward 7778\n", "forward 7779\n"]);
  keeper.releasePaneForwards("win-2");
  assert.deepEqual(child.stdin.writes.slice(2), ["drop 7779\n"]);
  keeper.forwardPanePorts("win-1", []);
  assert.deepEqual(child.stdin.writes.slice(3), ["drop 7778\n"]);
});

test("a replacement keeper is asked again for every port still shown", async () => {
  const { keeper, spawned } = harness({ remoteHosts: OPTED_IN });
  keeper.start();
  const first = spawned[0].child;
  keeper.forwardPanePorts("win-1", [7778]);
  report(first, "tunnel: pane 7778 connected");
  keeper.restart(); // the laptop woke from sleep
  const second = spawned[1].child;
  assert.deepEqual(second.stdin.writes, ["forward 7778\n"]);
  // What the old keeper said is not an answer about the new forward.
  const answer = keeper.forwardPanePorts("win-1", [7778]);
  report(first, "tunnel: pane 7778 down (exited); retrying in 1s");
  report(second, "tunnel: pane 7778 connected");
  assert.deepEqual(await answer, { 7778: "up" });
});

test("a report split across two chunks is still read", async () => {
  const { keeper, spawned } = harness({ remoteHosts: OPTED_IN });
  keeper.start();
  const { child } = spawned[0];
  const answer = keeper.forwardPanePorts("win-1", [7778]);
  child.stdout.emit("data", Buffer.from("tunnel: pane 77"));
  child.stdout.emit("data", Buffer.from("78 connected\n"));
  assert.deepEqual(await answer, { 7778: "up" });
});

test("only a well-formed pane report answers a pane", async () => {
  const { keeper, spawned } = harness({ remoteHosts: OPTED_IN });
  keeper.start();
  const { child } = spawned[0];
  let settled = null;
  const answer = keeper.forwardPanePorts("win-1", [7778]).then((value) => (settled = value));
  report(
    child,
    "tunnel: pane 7778",
    "tunnel: pane 7778 connecting",
    "tunnel: pane 7778connected",
    "tunnel: pane 77x8 connected",
    "tunnel: pane 0007778 connected",
    "tunnel: pane \uFF17\uFF17\uFF17\uFF18 connected", // full-width digits
    "tunnel:  pane 7778 connected",
  );
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(settled, null, "a malformed line must not answer the pane");
  report(child, "tunnel: pane 7778 refused (not a pane port)");
  assert.deepEqual(await answer, { 7778: "down" });
});

test("a pane the keeper never reports on loads after the wait", async () => {
  const { keeper, timers } = harness({ remoteHosts: OPTED_IN });
  keeper.start();
  const answer = keeper.forwardPanePorts("win-1", [7778]);
  const wait = timers.find((t) => t.ms === PANE_FORWARD_WAIT_MS);
  assert.ok(wait, "an unanswered pane must not wait forever");
  wait.fn();
  assert.deepEqual(await answer, { 7778: "down" });
});

test("stopping the keeper answers every waiting pane at once", async () => {
  const { keeper, timers } = harness({ remoteHosts: OPTED_IN });
  keeper.start();
  const answer = keeper.forwardPanePorts("win-1", [7778]);
  keeper.stop();
  assert.deepEqual(await answer, { 7778: "down" });
  assert.equal(timers.length, 0, "the wait's timer is cleared with it");
});
