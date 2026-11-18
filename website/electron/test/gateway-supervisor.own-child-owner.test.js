"use strict";

// Windows: the port owner probe recognises the gateway this app spawned even
// when its command line cannot be read. On current Windows releases WMIC is
// absent, and a busy host pushes the PowerShell query past its budget, so the
// read answers "". Shared fakes live in gateway-supervisor-test-helpers.js.

const { test } = require("node:test");
const assert = require("node:assert");

const { flush, harness } = require("./gateway-supervisor-test-helpers");

// The harness's spawnFn gives every child this pid.
const SPAWNED_PID = 1234;

// netstat names `state.holder` on :5476 (or nothing); every command read fails
// the way a timed-out PowerShell and a missing wmic.exe do.
function unreadableCommandExecFile(state) {
  return (file, _args, _options, callback) => {
    const tool = String(file).toLowerCase();
    if (tool.includes("netstat")) {
      callback(null, state.holder
        ? `  TCP    0.0.0.0:5476           0.0.0.0:0              LISTENING       ${state.holder}\r\n`
        : "", "");
      return;
    }
    if (tool.includes("powershell")) {
      callback(Object.assign(new Error("Command failed: powershell.exe"), { killed: true }));
      return;
    }
    if (tool.includes("wmic")) {
      callback(Object.assign(new Error("spawn wmic.exe ENOENT"), { code: "ENOENT" }));
      return;
    }
    callback(new Error(`unexpected command: ${file}`));
  };
}

function windowsHarness(state) {
  return harness({
    execFileFn: unreadableCommandExecFile(state),
    processRef: {
      platform: "win32",
      arch: "x64",
      env: { KIROCREW_HOME: "/virtual/kirocrew-home" },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("no such process"); },
    },
  });
}

test("win32: our spawned child is our gateway even when its command line cannot be read", async () => {
  const state = { holder: null };
  const { supervisor, spawnCalls } = windowsHarness(state);

  assert.strictEqual(await supervisor.start(), true);
  assert.strictEqual(spawnCalls.length, 1, "this process started the gateway");
  state.holder = SPAWNED_PID;

  assert.strictEqual(await supervisor.probePrimaryPortOwner(), "kirocrew");
});

test("win32: an unreadable holder that is not our live child stays unidentified", async () => {
  const state = { holder: SPAWNED_PID + 999 };
  const { supervisor } = windowsHarness(state);

  // Nothing spawned yet: no child of ours exists, whatever pid holds the port.
  assert.strictEqual(await supervisor.probePrimaryPortOwner(), "unidentified");

  state.holder = null;
  assert.strictEqual(await supervisor.start(), true);
  state.holder = SPAWNED_PID + 999;
  assert.strictEqual(await supervisor.probePrimaryPortOwner(), "unidentified", "another pid is not our child");
});

test("win32: once our child has exited its pid earns no trust", async () => {
  const state = { holder: null };
  const { supervisor, spawnCalls } = windowsHarness(state);

  assert.strictEqual(await supervisor.start(), true);
  const { child } = spawnCalls[0];
  child.exitCode = 1;
  child.emit("exit", 1, null);
  await flush();
  // The pid may be reused by anything once the child is gone.
  state.holder = SPAWNED_PID;

  assert.strictEqual(await supervisor.probePrimaryPortOwner(), "unidentified");
});
