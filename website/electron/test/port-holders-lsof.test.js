"use strict";

// lsofListenPids: an empty answer means "nobody listens", so only a run that
// actually finished may produce one. A run that could not start or was killed
// at its timeout is unknown and must reject.

const { test } = require("node:test");
const assert = require("node:assert");
const os = require("os");
const path = require("path");

const { createPortHolders } = require("../runtime/gateway/port-holders");

function holders(execFile) {
  return createPortHolders({
    fs: {},
    os,
    path,
    execFile,
    processObj: { platform: "linux", getuid: () => 1000 },
    dirname: "/virtual",
    isWindows: false,
    log: () => {},
    getSpawnedExecutablePaths: () => [],
  });
}

test("lsofListenPids reads the listening pids lsof prints", async () => {
  const { lsofListenPids } = holders((_file, _args, _options, callback) => callback(null, "4321\n8765\n", ""));
  assert.deepStrictEqual(await lsofListenPids(5476), [4321, 8765]);
});

test("lsofListenPids treats lsof's no-match exit as a free port", async () => {
  const noMatch = Object.assign(new Error("Command failed"), { code: 1, killed: false, signal: null });
  const { lsofListenPids } = holders((_file, _args, _options, callback) => callback(noMatch, "", ""));
  assert.deepStrictEqual(await lsofListenPids(5476), []);
});

test("lsofListenPids rejects when lsof is killed at its timeout", async () => {
  const timedOut = Object.assign(new Error("Command failed"), { code: null, killed: true, signal: "SIGTERM" });
  const { lsofListenPids } = holders((_file, _args, _options, callback) => callback(timedOut, "", ""));
  await assert.rejects(lsofListenPids(5476));
});

// Recovery cannot ask a missing lsof who holds the port, so a real bind
// decides: a held port goes to the restart-required dialog, a free one is
// reported freed so recovery starts the gateway again. `sleep` returns at once:
// the wait between bind checks is counted, not slept.
test("forceStopGatewayPort settles a failed lsof probe with a bind", async () => {
  const missing = Object.assign(new Error("spawn lsof ENOENT"), { code: "ENOENT" });
  const recovery = (bindable) => createPortHolders({
    fs: { existsSync: () => false },
    os,
    path,
    execFile: (_file, _args, _options, callback) => callback(missing, "", ""),
    processObj: { platform: "linux", getuid: () => 1000 },
    dirname: "/virtual",
    isWindows: false,
    log: () => {},
    getSpawnedExecutablePaths: () => [],
    isPortBindable: async () => bindable,
    sleep: async () => {},
  });

  const held = await recovery(false).forceStopGatewayPort(5476);
  assert.strictEqual(held.freed, false);
  assert.strictEqual(held.probeFailed, true);

  const free = await recovery(true).forceStopGatewayPort(5476);
  assert.strictEqual(free.freed, true);
  assert.ok(!free.probeFailed);
});
