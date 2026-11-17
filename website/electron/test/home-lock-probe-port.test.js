"use strict";

// probeLockHolderPid asks `kirocrew gateway-pid` about a port only when the
// caller names one: that is what lets the CLI name a Windows lock holder from
// the gateway's run record for the port, while the drain path keeps its
// lock-only question.

const path = require("node:path");
const { test } = require("node:test");
const assert = require("node:assert");

const { createHomeLock } = require("../runtime/gateway/home-lock");

function homeLock(calls) {
  return createHomeLock({
    fs: {},
    path,
    execFile(file, args, options, callback) {
      calls.push(args);
      callback(null, `${JSON.stringify({ holder: "process", pid: 4242, alive: true, source: "run_record" })}\n`);
    },
    app: { isPackaged: false },
    processObj: { platform: "linux", env: {} },
    home: "/virtual/home",
    port: 5476,
    isWindows: false,
    log: () => {},
    lsofHoldsFile: async () => false,
    snapshotGatewayPortPids: async () => [],
    resolveGatewayBin: () => "/opt/kc/bin/kirocrew",
  });
}

test("a probe that names a port passes it to gateway-pid", async () => {
  const calls = [];
  const result = await homeLock(calls).probeLockHolderPid({ port: 5476 });
  assert.deepStrictEqual(result, { verdict: "captured", pid: 4242 });
  assert.deepStrictEqual(calls, [["gateway-pid", "--port", "5476"]]);
});

test("a probe that names no port asks about the lock alone", async () => {
  const calls = [];
  await homeLock(calls).probeLockHolderPid();
  assert.deepStrictEqual(calls, [["gateway-pid"]]);
});
