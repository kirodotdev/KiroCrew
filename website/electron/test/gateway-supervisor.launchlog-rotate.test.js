"use strict";

const { EventEmitter } = require("node:events");
const { test } = require("node:test");
const assert = require("node:assert");

const {
  rejectingHttp,
  switchableHttp,
  harness,
  ownerProbe,
  OWN_GATEWAY_COMMAND,
} = require("./gateway-supervisor-test-helpers");

const LAUNCH_LOG = "/virtual/logs/gateway-launch.log";
const PREVIOUS_LOG = "/virtual/logs/gateway-launch.previous.log";

// An fsMod that records renameSync so a test can see whether the launch log was
// rotated, and reports the launch log as over the 10 MiB rotation cap so a
// rotation that is reached actually renames. Everything else the spawn and
// adoption paths touch is a benign stub.
function recordingFs() {
  const renames = [];
  return {
    renames,
    constants: { X_OK: 1 },
    mkdirSync() {},
    accessSync() {},
    existsSync() { return true; },
    openSync() { return 41; },
    closeSync() {},
    readFileSync() { return ""; },
    statSync(file) {
      if (file === LAUNCH_LOG) return { size: 64 * 1024 * 1024 };
      throw Object.assign(new Error("no such file"), { code: "ENOENT" });
    },
    renameSync(from, to) { renames.push([from, to]); },
  };
}

test("a fresh gateway spawn rotates the launch log before opening its fd", async () => {
  const fsMod = recordingFs();
  // Rejecting http: nothing holds the port, so start() spawns a fresh gateway.
  const { supervisor, spawnCalls } = harness({ fsMod, httpMod: rejectingHttp() });

  assert.strictEqual(await supervisor.start(), true);
  assert.strictEqual(spawnCalls.length, 1, "a fresh gateway was spawned");
  assert.deepStrictEqual(
    fsMod.renames,
    [[LAUNCH_LOG, PREVIOUS_LOG]],
    "the fresh spawn rotates the launch log exactly once, before its writer opens the fd",
  );
});

test("reusing a surviving gateway does not rotate the launch log", async () => {
  const fsMod = recordingFs();
  // A healthy gateway for this data folder already answers on the port, so
  // resolveGatewayConflict adopts it and start() returns before spawnGateway.
  const httpState = {
    status: 200,
    body: JSON.stringify({ ok: true, app: "kirocrew", version: "0.6.0" }),
  };
  const { supervisor, spawnCalls } = harness({
    fsMod,
    httpMod: switchableHttp(httpState),
    execFileFn: ownerProbe(OWN_GATEWAY_COMMAND),
  });

  assert.strictEqual(await supervisor.start(), true);
  assert.strictEqual(spawnCalls.length, 0, "the surviving gateway was reused, not respawned");
  assert.deepStrictEqual(
    fsMod.renames,
    [],
    "a reused gateway is still writing to the launch log, so it must not be rotated out from under it",
  );
});
