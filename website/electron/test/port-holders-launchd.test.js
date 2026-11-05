"use strict";

// The macOS launchd job probe that tells a LaunchAgent gateway apart from an
// orphan the previous app left behind. Both have PPID 1; only the agent is a job.

const { test } = require("node:test");
const assert = require("node:assert");
const os = require("os");
const path = require("path");

const { createPortHolders, launchdServicePids } = require("../runtime/gateway/port-holders");

const LISTING = [
  "gui/501 = {",
  "\ttype = login",
  "\tservices = {",
  "\t\t   14317      - \tapplication.com.example.editor.1.2",
  "\t\t       0      - \tcom.apple.unmountassistant.useragent",
  "\t\t    8108     78 \tdev.kirocrew.gateway",
  "\t}",
  "\tunmanaged processes = {",
  "\t\t   9999      - \tnot-a-service",
  "\t}",
  "}",
].join("\n");

test("launchdServicePids reads the running job pids of the services block only", () => {
  assert.deepStrictEqual([...launchdServicePids(LISTING)].sort((a, b) => a - b), [8108, 14317]);
});

test("launchdServicePids returns null for a listing it does not understand", () => {
  assert.strictEqual(launchdServicePids(""), null);
  assert.strictEqual(launchdServicePids("gui/501 = {\n\ttype = login\n}"), null);
  assert.strictEqual(launchdServicePids("\tservices = {\n\t}\n"), null, "no rows is not an empty job list");
});

function holders(execFile, processObj = { platform: "darwin", getuid: () => 501 }) {
  return createPortHolders({
    fs: {},
    os,
    path,
    execFile,
    processObj,
    dirname: "/virtual",
    isWindows: false,
    log: () => {},
    getSpawnedExecutablePaths: () => [],
  });
}

test("launchdJobPids unions the gui, user and system domains", async () => {
  const calls = [];
  const { launchdJobPids } = holders((file, args, _options, callback) => {
    calls.push([file, ...args]);
    const pid = { "gui/501": 11, "user/501": 22, system: 33 }[args[1]];
    callback(null, `\tservices = {\n\t\t ${pid} - \tjob.${pid}\n\t}\n`, "");
  });
  assert.deepStrictEqual([...(await launchdJobPids())].sort((a, b) => a - b), [11, 22, 33]);
  assert.deepStrictEqual(calls, [
    ["/bin/launchctl", "print", "gui/501"],
    ["/bin/launchctl", "print", "user/501"],
    ["/bin/launchctl", "print", "system"],
  ]);
});

test("launchdJobPids is null when any domain cannot be read", async () => {
  const { launchdJobPids } = holders((_file, args, _options, callback) => {
    if (args[1] === "system") callback(new Error("boom"), "", "");
    else callback(null, LISTING, "");
  });
  assert.strictEqual(await launchdJobPids(), null);
});

test("launchdJobPids is null when the user id cannot be read", async () => {
  let runs = 0;
  const { launchdJobPids } = holders(
    () => { runs += 1; },
    { platform: "darwin", getuid: () => { throw new Error("no uid"); } },
  );
  assert.strictEqual(await launchdJobPids(), null);
  assert.strictEqual(runs, 0);
});
