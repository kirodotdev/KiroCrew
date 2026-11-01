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

function windowsHolders(execFileFn) {
  return createPortHolders({
    fs, os, path,
    execFile: execFileFn,
    processObj: { resourcesPath: "", env: {} },
    dirname: "/virtual/electron",
    isWindows: true,
    log: () => {},
    getSpawnedExecutablePaths: () => [],
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
