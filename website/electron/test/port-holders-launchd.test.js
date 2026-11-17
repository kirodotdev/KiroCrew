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

// `launchctl print` output listing `jobs` as running services, the shape every
// domain answers with in these tests.
const listing = (jobs) => `\tservices = {\n${jobs.map((pid) => `\t\t ${pid} - \tjob.${pid}\n`).join("")}\t}\n`;

test("isLaunchdJob answers true for a job, false for any other pid, null when unreadable", async () => {
  const listed = holders((_file, _args, _options, callback) => callback(null, listing([8108]), ""));
  assert.strictEqual(await listed.isLaunchdJob(8108), true);
  assert.strictEqual(await listed.isLaunchdJob("8108"), true, "a pid read from ps compares as a number");
  assert.strictEqual(await listed.isLaunchdJob(4242), false);
  const unreadable = holders((_file, _args, _options, callback) => callback(new Error("boom"), "", ""));
  assert.strictEqual(await unreadable.isLaunchdJob(8108), null);
});

// The force-stop recovery runs: lsof names `pid` until it is signalled, ps
// reports a Kiro Crew gateway with launchd (PID 1) as its parent, and launchctl
// lists `jobs`.
function forceStopHolders({ platform = "darwin", jobs = [] } = {}) {
  const signals = [];
  const launchctl = [];
  let held = true;
  const owner = createPortHolders({
    fs: {},
    os,
    path,
    execFile(file, args, _options, callback) {
      if (file === "/bin/launchctl") {
        launchctl.push(args[1]);
        callback(null, listing(jobs), "");
      } else if (file === "/bin/ps") {
        callback(null, args.includes("ppid=") ? "1\n" : "python -m kiro_crew gateway --port 5476\n", "");
      } else {
        callback(null, held ? "4242\n" : "", "");
      }
    },
    processObj: {
      platform,
      getuid: () => 501,
      kill(pid, signal) {
        signals.push([pid, signal]);
        held = false;
      },
    },
    dirname: "/virtual",
    isWindows: false,
    log: () => {},
    getSpawnedExecutablePaths: () => [],
  });
  return { owner, signals, launchctl };
}

test("macOS force-stop evicts an orphan launchd runs no job for", async () => {
  const { owner, signals } = forceStopHolders({ jobs: [8108] });
  const result = await owner.forceStopGatewayPort(5476);
  assert.deepStrictEqual(signals, [[4242, "SIGKILL"]]);
  assert.strictEqual(result.freed, true);
  assert.strictEqual(result.serviceHolder, false);
});

test("macOS force-stop leaves a gateway launchd runs as a job alone", async () => {
  const { owner, signals } = forceStopHolders({ jobs: [8108, 4242] });
  const result = await owner.forceStopGatewayPort(5476);
  assert.deepStrictEqual(signals, []);
  assert.strictEqual(result.serviceHolder, true);
  assert.strictEqual(result.freed, false);
});

test("off macOS a PID-1 gateway stays service-managed and launchctl is never run", async () => {
  const { owner, signals, launchctl } = forceStopHolders({ platform: "linux" });
  const result = await owner.forceStopGatewayPort(5476);
  assert.deepStrictEqual(signals, []);
  assert.strictEqual(result.serviceHolder, true);
  assert.deepStrictEqual(launchctl, []);
});
