// The wedge kill must reach a gateway that a launcher shim forked.
//
// The desktop's own child can be a shim that forks the real gateway instead of
// exec'ing it. Signalling only the child left that gateway alive, re-parented
// to init, holding the port and gateway.lock.
const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawn, execFile } = require("child_process");
const { createPortHolders, descendantPids } = require("../runtime/gateway/port-holders");

function holdersWith(execFileFn) {
  return createPortHolders({
    fs, os, path,
    execFile: execFileFn,
    processObj: { resourcesPath: "" },
    dirname: "/virtual/electron",
    isWindows: false,
    log: () => {},
    getSpawnedExecutablePaths: () => [],
  });
}

test("descendantPids walks the whole tree, deepest first, without the root", () => {
  const table = "  1     0\n 100    1\n 200  100\n 300  200\n 301  200\n 999    1\n";
  assert.deepStrictEqual(descendantPids(100, table), [300, 301, 200]);
  assert.deepStrictEqual(descendantPids(999, table), []);
  assert.deepStrictEqual(descendantPids(100, ""), []);
});

test("descendantPids ignores malformed rows and never returns init", () => {
  const table = "garbage\n 100 1\n 1 100\n 200 100 extra\n\n";
  assert.deepStrictEqual(descendantPids(100, table), [200]);
});

test("posixDescendantPids asks ps for the full pid/ppid table", async () => {
  let seen = null;
  const holders = holdersWith((file, args, _options, callback) => {
    seen = [file, args];
    callback(null, " 10 1\n 11 10\n 12 11\n");
  });
  assert.deepStrictEqual(await holders.posixDescendantPids(10), [12, 11]);
  assert.deepStrictEqual(seen, ["/bin/ps", ["-A", "-o", "pid=,ppid="]]);
});

test("posixDescendantPids returns [] when ps cannot run", async () => {
  const holders = holdersWith((_file, _args, _options, callback) => callback(new Error("EPERM"), ""));
  assert.deepStrictEqual(await holders.posixDescendantPids(4242), []);
});

test("posixDescendantPids returns [] when ps never exits", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const holders = holdersWith(() => {});
  const pending = holders.posixDescendantPids(4242);
  t.mock.timers.tick(6000);
  assert.deepStrictEqual(await pending, []);
});

test("posixDescendantPids finds a gateway a shim forked, on a real process tree", {
  skip: process.platform === "win32",
}, async () => {
  // shim (our child) -> gateway (its child). The shim does NOT exec.
  const gatewayCode = "setInterval(()=>{}, 1e9)";
  const shimCode = "const c=require('child_process').spawn(process.execPath,['-e',"
    + JSON.stringify(gatewayCode) + "],{stdio:'ignore'});"
    + "console.log(c.pid); setInterval(()=>{}, 1e9);";
  const shim = spawn(process.execPath, ["-e", shimCode]);
  const gatewayPid = await new Promise((resolve) => {
    shim.stdout.once("data", (data) => resolve(parseInt(String(data), 10)));
  });
  try {
    const found = await holdersWith(execFile).posixDescendantPids(shim.pid);
    assert.ok(found.includes(gatewayPid), `expected ${gatewayPid} in ${found}`);
  } finally {
    try { process.kill(gatewayPid, "SIGKILL"); } catch { /* already gone */ }
    shim.kill("SIGKILL");
  }
});
