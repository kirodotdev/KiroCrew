"use strict";

// Wedge recovery on Windows fails closed when it cannot read the port: a probe
// error means "restart required", never a blind respawn. One slow netstat spawn
// on a busy host used to be enough to reach that dead end even though the kill
// had already worked. These tests pin that a transient failure is retried and
// that a probe that never succeeds still fails closed.

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test } = require("node:test");
const assert = require("node:assert");

const { createPortHolders } = require("../runtime/gateway/port-holders");
const { windowsListenPidsRetrying } = require("../windows-port");

// The netstat-retry cases below run on the gateway's real port number, so they
// take a bind check that answers "free" rather than binding that port on the
// test host. The empty-owner cases pass `realBindCheck` to keep the module's own
// check, run against a real socket. `sleep` returns at once: the wait between
// bind checks is counted, not slept.
function windowsHolders(execFileFn, {
  realBindCheck = false,
  isPortBindable = async () => true,
  sleep = async () => {},
  log = () => {},
} = {}) {
  return createPortHolders({
    fs, os, path,
    execFile: execFileFn,
    processObj: { resourcesPath: "", env: {} },
    dirname: "/virtual/electron",
    isWindows: true,
    log,
    getSpawnedExecutablePaths: () => [],
    sleep,
    ...(realBindCheck ? {} : { isPortBindable }),
  });
}

// netstat answers from a script: an Error entry fails that call, a string is
// its stdout. Any other tool is unexpected, because a free port needs no
// identity lookup and no kill.
function scriptedNetstat(script) {
  const calls = [];
  const execFileFn = (file, args, options, callback) => {
    if (!String(file).toLowerCase().includes("netstat")) {
      callback(new Error(`unexpected command: ${file}`));
      return;
    }
    calls.push(options);
    const step = script[Math.min(calls.length - 1, script.length - 1)];
    if (step instanceof Error) callback(step, "");
    else callback(null, step, "");
  };
  return { calls, execFileFn };
}

function timedOut() {
  return Object.assign(new Error("Command failed: C:\\Windows\\System32\\netstat.exe -ano\n"), {
    killed: true, signal: "SIGTERM", code: null,
  });
}

test("Windows recovery retries a netstat that timed out once, then sees the port free", async () => {
  const { calls, execFileFn } = scriptedNetstat([timedOut(), ""]);
  const result = await windowsHolders(execFileFn).forceStopGatewayPort(5476);
  assert.strictEqual(calls.length, 2);
  assert.strictEqual(result.freed, true);
  assert.ok(!result.probeFailed, "a probe that succeeded on retry is not a failed probe");
});

test("Windows recovery still fails closed when netstat never answers", async () => {
  const { calls, execFileFn } = scriptedNetstat([timedOut()]);
  const result = await windowsHolders(execFileFn).forceStopGatewayPort(5476);
  assert.strictEqual(calls.length, 3, "bounded: three attempts, then give up");
  assert.strictEqual(result.freed, false);
  assert.strictEqual(result.probeFailed, true);
});

test("windowsListenPidsRetrying bounds each attempt and says how the last one failed", async () => {
  const seen = [];
  const sleeps = [];
  const logs = [];
  await assert.rejects(
    windowsListenPidsRetrying(5476, {
      timeoutMs: 1234,
      retryDelayMs: 7,
      sleep: async (ms) => { sleeps.push(ms); },
      log: (line) => logs.push(line),
      listenPids: async (_port, options) => { seen.push(options.timeoutMs); throw timedOut(); },
    }),
    /netstat failed 3 times on :5476; last: timed out after 1234ms/
  );
  assert.deepStrictEqual(seen, [1234, 1234, 1234]);
  assert.deepStrictEqual(sleeps, [7, 7], "no wait after the final attempt");
  assert.strictEqual(logs.length, 3);
});

test("windowsListenPidsRetrying names a non-timeout exit code", async () => {
  const exited = Object.assign(new Error("Command failed: netstat.exe -ano"), { code: 1 });
  await assert.rejects(
    windowsListenPidsRetrying(5476, {
      attempts: 1,
      listenPids: async () => { throw exited; },
    }),
    /last: Command failed: netstat\.exe -ano \(exit 1\)/
  );
});

test("windowsListenPidsRetrying returns the first successful answer", async () => {
  let calls = 0;
  const pids = await windowsListenPidsRetrying(5476, {
    sleep: async () => {},
    listenPids: async () => { calls += 1; if (calls === 1) throw timedOut(); return [4242]; },
  });
  assert.deepStrictEqual(pids, [4242]);
  assert.strictEqual(calls, 2);
});

// --- An empty netstat answer is checked with a real bind ---------------------
//
// netstat can leave a held port out of its table: no row at all, a LISTENING row
// whose foreign endpoint is not the wildcard, or a row with pid 0. Each parses to
// "no owner". Recovery must not read that as "free" while a bind is refused.

const net = require("node:net");
const { WINDOWS_LOCAL_GATEWAY_WAIT_MS } = require("../gateway-wait");

function listenOn(host) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.once("error", reject);
    server.listen({ port: 0, host }, () => resolve(server));
  });
}

function closeServer(server) {
  return new Promise((resolve) => server.close(() => resolve()));
}

function bindRefused(port, host) {
  return new Promise((resolve) => {
    const probe = net.createServer();
    probe.once("error", (e) => resolve(e.code === "EADDRINUSE"));
    probe.listen({ port, host, exclusive: true }, () => probe.close(() => resolve(false)));
  });
}

const UNLISTED_SHAPES = {
  "no row for the port": () => "",
  "a LISTENING row with a non-wildcard foreign endpoint": (port) =>
    `  TCP    127.0.0.1:${port}         127.0.0.1:0            LISTENING       4242\r\n`,
  "a LISTENING row with pid 0": (port) =>
    `  TCP    0.0.0.0:${port}           0.0.0.0:0              LISTENING       0\r\n`,
};

for (const [shape, row] of Object.entries(UNLISTED_SHAPES)) {
  test(`Windows recovery does not call a held loopback port free when netstat shows ${shape}`, async () => {
    const held = await listenOn("127.0.0.1");
    const port = held.address().port;
    try {
      const logs = [];
      const { execFileFn } = scriptedNetstat([row(port)]);
      const sleeps = [];
      const result = await windowsHolders(execFileFn, {
        realBindCheck: true,
        sleep: async (ms) => { sleeps.push(ms); },
        log: (l) => logs.push(l),
      }).forceStopGatewayPort(port);
      assert.strictEqual(await bindRefused(port, "127.0.0.1"), true, "the port really is held");
      assert.strictEqual(result.freed, false);
      assert.strictEqual(result.ownerUnknown, true);
      assert.strictEqual(result.killed, 0);
      assert.strictEqual(sleeps.reduce((a, b) => a + b, 0), WINDOWS_LOCAL_GATEWAY_WAIT_MS, "the refusal is waited out first");
      assert.ok(logs.some((l) => l.includes(`no LISTEN owner found on :${port} but a bind is refused`)), logs.join("\n"));
    } finally {
      await closeServer(held);
    }
  });
}

// The re-probe after a kill, or after skipping a foreign holder, can drop the
// row too. Both end on the same bind check as an empty first probe.
const { forceStopPort } = require("../gateway-stop");

function scriptedForceStop({ command, confirms }) {
  const answers = [[4242], []];
  const checks = [];
  return {
    checks,
    run: () => forceStopPort(5476, {
      getListenPids: async () => answers.shift() ?? [],
      getCommand: async () => command,
      kill: async () => {},
      sleep: async () => {},
      isKirocrew: (cmd) => cmd === "kirocrew gateway --port 5476",
      failClosedOnProbeError: true,
      confirmFree: async (port) => { checks.push(port); return confirms; },
    }),
  };
}

for (const [label, command] of [
  ["after killing our own gateway", "kirocrew gateway --port 5476"],
  ["after skipping a foreign holder", "ssh -L 5476:localhost:5476 build-host"],
]) {
  test(`an empty re-probe ${label} is checked with a bind before it reads as free`, async () => {
    const refused = scriptedForceStop({ command, confirms: false });
    const held = await refused.run();
    assert.deepStrictEqual(refused.checks, [5476]);
    assert.strictEqual(held.freed, false);
    assert.strictEqual(held.ownerUnknown, true);

    const bindable = scriptedForceStop({ command, confirms: true });
    const free = await bindable.run();
    assert.deepStrictEqual(bindable.checks, [5476]);
    assert.strictEqual(free.freed, true);
    assert.ok(!free.ownerUnknown);
  });
}

test("a port still listed after the kill is not bind-checked", async () => {
  const checks = [];
  const result = await forceStopPort(5476, {
    getListenPids: async () => [4242],
    getCommand: async () => "ssh -L 5476:localhost:5476 build-host",
    kill: async () => {},
    sleep: async () => {},
    confirmFree: async (port) => { checks.push(port); return true; },
  });
  assert.deepStrictEqual(checks, [], "a listed owner already answers the question");
  assert.strictEqual(result.freed, false);
  assert.strictEqual(result.foreignHolder, true);
});

test("Windows recovery still calls a genuinely free port free, with no kill", async () => {
  const probe = await listenOn("127.0.0.1");
  const port = probe.address().port;
  await closeServer(probe);
  const { calls, execFileFn } = scriptedNetstat([""]);
  const result = await windowsHolders(execFileFn, { realBindCheck: true }).forceStopGatewayPort(port);
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(result.freed, true);
  assert.strictEqual(result.killed, 0);
  assert.ok(!result.ownerUnknown);
  assert.strictEqual(await bindRefused(port, "127.0.0.1"), false, "the bind check released the port");
});

test("Windows recovery fails closed when the bind check itself throws", async () => {
  const { execFileFn } = scriptedNetstat([""]);
  const result = await windowsHolders(execFileFn, {
    isPortBindable: async () => { throw new Error("socket table unavailable"); },
  }).forceStopGatewayPort(5476);
  assert.strictEqual(result.freed, false);
  assert.strictEqual(result.ownerUnknown, true);
});

// --- A refused bind with no owner is waited out before it fails closed -------
//
// A gateway that served connections leaves its port refusing a new bind for a
// while after it exits, with no LISTEN row. Recovery waits for the port to free
// and only reports `ownerUnknown` when the wait runs out.

function refusingThen(freeAfter) {
  const checks = [];
  return {
    checks,
    confirmFree: async (port) => { checks.push(port); return checks.length > freeAfter; },
  };
}

test("an ownerless port that frees during the wait is free, and the wait is announced once", async () => {
  const bind = refusingThen(3);
  const sleeps = [];
  let waits = 0;
  const result = await forceStopPort(5476, {
    getListenPids: async () => [],
    getCommand: async () => "",
    kill: async () => {},
    sleep: async (ms) => { sleeps.push(ms); },
    failClosedOnProbeError: true,
    confirmFree: bind.confirmFree,
    confirmFreeWaitMs: 10_000,
    onConfirmFreeWait: () => { waits += 1; },
  });
  assert.strictEqual(result.freed, true);
  assert.ok(!result.ownerUnknown);
  assert.strictEqual(bind.checks.length, 4, "three refusals, then the bind that succeeds");
  assert.deepStrictEqual(sleeps, [1000, 1000, 1000]);
  assert.strictEqual(waits, 1);
});

test("an ownerless port still refused when the wait runs out reports ownerUnknown", async () => {
  const bind = refusingThen(Infinity);
  const sleeps = [];
  let waits = 0;
  const result = await forceStopPort(5476, {
    getListenPids: async () => [],
    getCommand: async () => "",
    kill: async () => {},
    sleep: async (ms) => { sleeps.push(ms); },
    failClosedOnProbeError: true,
    confirmFree: bind.confirmFree,
    confirmFreeWaitMs: 5000,
    onConfirmFreeWait: () => { waits += 1; },
  });
  assert.strictEqual(result.freed, false);
  assert.strictEqual(result.ownerUnknown, true);
  assert.strictEqual(bind.checks.length, 6, "one check at the start and one after each wait");
  assert.strictEqual(sleeps.reduce((a, b) => a + b, 0), 5000);
  assert.strictEqual(waits, 1);
});

test("an empty re-probe after a kill waits for a refused bind too", async () => {
  const answers = [[4242], []];
  const bind = refusingThen(2);
  const result = await forceStopPort(5476, {
    getListenPids: async () => answers.shift() ?? [],
    getCommand: async () => "kirocrew gateway --port 5476",
    kill: async () => {},
    sleep: async () => {},
    isKirocrew: (cmd) => cmd === "kirocrew gateway --port 5476",
    failClosedOnProbeError: true,
    confirmFree: bind.confirmFree,
    confirmFreeWaitMs: 10_000,
  });
  assert.strictEqual(result.freed, true);
  assert.strictEqual(result.killed, 1);
  assert.strictEqual(bind.checks.length, 3);
});

test("Windows recovery waits the Windows gateway start budget and tells the caller it is waiting", async () => {
  const { execFileFn } = scriptedNetstat([""]);
  const bind = refusingThen(Infinity);
  const sleeps = [];
  let waits = 0;
  const result = await windowsHolders(execFileFn, {
    isPortBindable: bind.confirmFree,
    sleep: async (ms) => { sleeps.push(ms); },
  }).forceStopGatewayPort(5476, { onBindWait: () => { waits += 1; } });
  assert.strictEqual(result.ownerUnknown, true);
  assert.strictEqual(sleeps.reduce((a, b) => a + b, 0), WINDOWS_LOCAL_GATEWAY_WAIT_MS);
  assert.strictEqual(waits, 1);
});

test("an aborted bind wait stops at once, binds no more and is not announced", async () => {
  const bind = refusingThen(Infinity);
  let waits = 0;
  let aborted = false;
  const result = await forceStopPort(5476, {
    getListenPids: async () => [],
    getCommand: async () => "",
    kill: async () => {},
    sleep: async () => { aborted = true; },
    failClosedOnProbeError: true,
    confirmFree: bind.confirmFree,
    confirmFreeWaitMs: 10_000,
    onConfirmFreeWait: () => { waits += 1; },
    isAborted: () => aborted,
  });
  assert.strictEqual(result.freed, false);
  assert.strictEqual(bind.checks.length, 1, "no bind after the abort");
  assert.strictEqual(waits, 1);

  const before = refusingThen(Infinity);
  let announced = 0;
  await forceStopPort(5476, {
    getListenPids: async () => [],
    getCommand: async () => "",
    kill: async () => {},
    sleep: async () => {},
    confirmFree: before.confirmFree,
    confirmFreeWaitMs: 10_000,
    onConfirmFreeWait: () => { announced += 1; },
    isAborted: () => true,
  });
  assert.strictEqual(before.checks.length, 0, "an already-aborted wait binds nothing");
  assert.strictEqual(announced, 0, "and paints nothing");
});
