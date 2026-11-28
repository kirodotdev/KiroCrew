// The Windows wedge kill tree-kills the gateway this app spawned without first
// reading its command line: that read can time out on a busy machine, and a
// refusal then killed the parent alone, leaving its ssh tunnel and app backend
// running.
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");

const { createShutdown } = require("../runtime/gateway/shutdown");

function windowsShutdown({ windowsTaskkillTreeFn, logs }) {
  return createShutdown({
    fs,
    path,
    http: {},
    processObj: process,
    port: 5476,
    backendUrl: "http://127.0.0.1:5476",
    isWindows: true,
    log: (line) => logs.push(line),
    setTimeoutFn: setTimeout,
    getGatewayProcess: () => null,
    setGatewayProcess: () => {},
    setSpawnedExecutablePaths: () => {},
    getRunLocalGateway: () => true,
    getGatewayOwnership: () => "spawned",
    isTrustedWindowsGatewayCommand: () => false,
    windowsTaskkillTreeFn,
  });
}

function fakeChild() {
  return {
    pid: 30060,
    exitCode: null,
    signalCode: null,
    signals: [],
    kill(signal) { this.signals.push(signal); },
  };
}

test("the Windows wedge kill tree-kills the spawned child with no identity check", async () => {
  const treeKills = [];
  const logs = [];
  const child = fakeChild();
  const { killGatewayProcessTree } = windowsShutdown({
    windowsTaskkillTreeFn: async (pid) => { treeKills.push(pid); },
    logs,
  });
  await killGatewayProcessTree(child, "SIGKILL");
  assert.deepStrictEqual(treeKills, [30060]);
  assert.deepStrictEqual(child.signals, [], "the tree kill must not degrade to a single-pid kill");
  assert.deepStrictEqual(logs, []);
});

test("a failed Windows tree kill still falls back to killing the pid", async () => {
  const logs = [];
  const child = fakeChild();
  const { killGatewayProcessTree } = windowsShutdown({
    windowsTaskkillTreeFn: async () => { throw new Error("taskkill exited 1"); },
    logs,
  });
  await killGatewayProcessTree(child, "SIGKILL");
  assert.deepStrictEqual(child.signals, ["SIGKILL"]);
  assert.match(logs.join("\n"), /tree kill failed \(taskkill exited 1\)/);
});

test("an exited child is not tree-killed", async () => {
  const treeKills = [];
  const child = fakeChild();
  child.exitCode = 0;
  const { killGatewayProcessTree } = windowsShutdown({
    windowsTaskkillTreeFn: async (pid) => { treeKills.push(pid); },
    logs: [],
  });
  await killGatewayProcessTree(child, "SIGKILL");
  assert.deepStrictEqual(treeKills, []);
  assert.deepStrictEqual(child.signals, []);
});

test("a child a signal already killed is not tree-killed", async () => {
  // exitCode stays null after a signal death, and the pid may then be reused.
  const treeKills = [];
  const child = fakeChild();
  child.signalCode = "SIGKILL";
  const { killGatewayProcessTree } = windowsShutdown({
    windowsTaskkillTreeFn: async (pid) => { treeKills.push(pid); },
    logs: [],
  });
  await killGatewayProcessTree(child, "SIGKILL");
  assert.deepStrictEqual(treeKills, []);
  assert.deepStrictEqual(child.signals, []);
});
