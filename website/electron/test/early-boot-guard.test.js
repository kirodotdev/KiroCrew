"use strict";

const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const { installEarlyBootGuard, failEarlyBoot } = require("../early-boot-guard");

const DRIVER = path.join(__dirname, "early-boot-driver.js");

/** Fake app/dialog/glog that record call order, with an optional throwing dialog. */
function makeDeps({ dialogThrows = false } = {}) {
  const calls = [];
  const logged = [];
  return {
    calls,
    logged,
    deps: {
      app: {
        exit(code) {
          calls.push(["exit", code]);
        },
      },
      dialog: {
        showErrorBox(title, content) {
          calls.push(["showErrorBox", title, content]);
          if (dialogThrows) throw new Error("no display");
        },
      },
      glog(line) {
        logged.push(line);
        calls.push(["glog"]);
      },
      logPath: () => "/var/log/fake/gateway-launch.log",
    },
  };
}

describe("installEarlyBootGuard (unit)", () => {
  it("logs the stack, shows the error box, then exits 1 on uncaughtException", () => {
    const emitter = new EventEmitter();
    const { calls, logged, deps } = makeDeps();
    installEarlyBootGuard({ ...deps, emitter });

    const boom = new Error("boom");
    emitter.emit("uncaughtException", boom);

    assert.equal(logged.length, 1);
    assert.match(logged[0], /early boot failed \(uncaughtException\)/);
    assert.ok(logged[0].includes(boom.stack), "the log line carries the full stack");

    assert.deepEqual(calls.map((c) => c[0]), ["glog", "showErrorBox", "exit"]);
    const [, title, content] = calls[1];
    assert.equal(title, "Kiro Crew failed to start");
    assert.ok(content.includes("boom"), "the dialog names the error");
    assert.ok(content.includes(deps.logPath()), "the dialog tells the user where the log is");
    assert.deepEqual(calls[2], ["exit", 1]);
  });

  it("handles unhandledRejection the same way, including a non-Error reason", () => {
    const emitter = new EventEmitter();
    const { calls, logged, deps } = makeDeps();
    installEarlyBootGuard({ ...deps, emitter });

    emitter.emit("unhandledRejection", "plain string reason");

    assert.match(logged[0], /early boot failed \(unhandledRejection\): plain string reason/);
    assert.deepEqual(calls.map((c) => c[0]), ["glog", "showErrorBox", "exit"]);
    assert.deepEqual(calls.at(-1), ["exit", 1]);
  });

  it("a dialog that throws does not prevent exit(1)", () => {
    const emitter = new EventEmitter();
    const { calls, deps } = makeDeps({ dialogThrows: true });
    installEarlyBootGuard({ ...deps, emitter });

    assert.doesNotThrow(() => emitter.emit("uncaughtException", new Error("boom")));
    assert.deepEqual(calls.map((c) => c[0]), ["glog", "showErrorBox", "exit"]);
    assert.deepEqual(calls.at(-1), ["exit", 1]);
  });

  it("a logger that throws does not prevent the dialog or the exit", () => {
    const emitter = new EventEmitter();
    const { calls, deps } = makeDeps();
    deps.glog = () => {
      throw new Error("disk full");
    };
    installEarlyBootGuard({ ...deps, emitter });

    assert.doesNotThrow(() => emitter.emit("uncaughtException", new Error("boom")));
    assert.deepEqual(calls.map((c) => c[0]), ["showErrorBox", "exit"]);
  });

  it("reports only the first failure in full; later ones are logged and re-exit", () => {
    const emitter = new EventEmitter();
    const { calls, logged, deps } = makeDeps();
    installEarlyBootGuard({ ...deps, emitter });

    emitter.emit("uncaughtException", new Error("first"));
    emitter.emit("uncaughtException", new Error("second"));

    const boxes = calls.filter((c) => c[0] === "showErrorBox");
    assert.equal(boxes.length, 1, "one error box, not one per exception");
    assert.equal(logged.length, 2);
    assert.match(logged[1], /early boot failed again \(uncaughtException\)/);
    assert.equal(calls.filter((c) => c[0] === "exit").length, 2);
  });

  it("the returned function removes both listeners", () => {
    const emitter = new EventEmitter();
    const { calls, deps } = makeDeps();
    const release = installEarlyBootGuard({ ...deps, emitter });

    assert.equal(emitter.listenerCount("uncaughtException"), 1);
    assert.equal(emitter.listenerCount("unhandledRejection"), 1);
    release();
    assert.equal(emitter.listenerCount("uncaughtException"), 0);
    assert.equal(emitter.listenerCount("unhandledRejection"), 0);

    // The keep-alive semantics of the post-ready net depend on the guard being
    // gone: an exception after release must not reach exit(1) through it.
    emitter.on("uncaughtException", () => {});
    emitter.emit("uncaughtException", new Error("post-ready"));
    assert.deepEqual(calls, []);
  });

  it("defaults to the real process emitter", () => {
    const before = process.listenerCount("uncaughtException");
    const { deps } = makeDeps();
    const release = installEarlyBootGuard(deps);
    try {
      assert.equal(process.listenerCount("uncaughtException"), before + 1);
    } finally {
      release();
    }
    assert.equal(process.listenerCount("uncaughtException"), before);
  });

  it("failEarlyBoot tolerates a missing or throwing logPath", () => {
    const { calls, deps } = makeDeps();
    failEarlyBoot({ ...deps, logPath: undefined }, "test", new Error("x"));
    assert.deepEqual(calls.map((c) => c[0]), ["glog", "showErrorBox", "exit"]);
    assert.ok(!calls[1][2].includes("A log was written to"), "no path line without a logPath");

    const second = makeDeps();
    failEarlyBoot(
      { ...second.deps, logPath: () => { throw new Error("no logs dir"); } },
      "test",
      new Error("y"),
    );
    assert.deepEqual(second.calls.map((c) => c[0]), ["glog", "showErrorBox", "exit"]);
  });
});

describe("main.js executes under a stubbed electron (load test)", () => {
  // `node --check` proves main.js parses; nothing short of RUNNING its
  // module-level code proves the boot sequence executes. The driver replaces
  // `electron` at the require boundary and requires main.js in a child node.

  function runDriver(mode) {
    const root = fs.mkdtempSync(path.join(os.tmpdir(), "kirocrew-early-boot-"));
    const logsDir = path.join(root, "logs");
    const userDataDir = path.join(root, "userData");
    const home = path.join(root, "home");
    fs.mkdirSync(logsDir, { recursive: true });
    fs.mkdirSync(userDataDir, { recursive: true });
    fs.mkdirSync(home, { recursive: true });
    try {
      const result = spawnSync(process.execPath, [DRIVER, logsDir, userDataDir, mode], {
        cwd: root,
        encoding: "utf8",
        timeout: 60_000,
        env: {
          ...process.env,
          // Keep every path main.js derives from the data home inside the sandbox.
          KIROCREW_HOME: home,
          KIROCREW_PORT: "5476",
        },
      });
      const logPath = path.join(logsDir, "gateway-launch.log");
      const log = fs.existsSync(logPath) ? fs.readFileSync(logPath, "utf8") : "";
      return { ...result, log };
    } finally {
      fs.rmSync(root, { recursive: true, force: true });
    }
  }

  it("loads main.js to completion: parse + module-level execution, exit 0", () => {
    const run = runDriver("ok");
    assert.equal(run.status, 0, `driver exited ${run.status}\n${run.stdout}\n${run.stderr}`);
    assert.match(run.stdout, /^MAIN_LOADED$/m);
    assert.doesNotMatch(run.stdout, /SHOW_ERROR_BOX/, "no error box on a clean boot");
    // The boot span actually ran: these log lines come from armCrashCollector
    // and initNativeLogging, which sit after the guard and before whenReady.
    assert.match(run.log, /crash collector armed/);
    assert.match(run.log, /native logging armed/);
  });

  it("an induced pre-ready throw reaches the log, the dialog, and exit 1 -- in that order", () => {
    const run = runDriver("throw-once");
    assert.equal(run.status, 1, `driver exited ${run.status}\n${run.stdout}\n${run.stderr}`);
    assert.doesNotMatch(run.stdout, /MAIN_LOADED/, "main.js must not report a completed load");

    // Log entry: the guard's line, carrying the induced error and its stack.
    assert.match(run.log, /early boot failed \(uncaughtException\): Error: induced: getPath\(userData\)/);
    assert.match(run.log, /main\.js:\d+/, "the stack names the main.js call site");

    // Dialog before exit: the stub prints both markers to stdout in call order.
    const box = run.stdout.indexOf("SHOW_ERROR_BOX:Kiro Crew failed to start");
    const exit = run.stdout.indexOf("APP_EXIT:1");
    assert.notEqual(box, -1, "showErrorBox was called");
    assert.notEqual(exit, -1, "app.exit(1) was called");
    assert.ok(box < exit, "showErrorBox precedes app.exit(1)");
    assert.match(run.stdout, /SHOW_ERROR_BOX_CONTENT:An error occurred before the app could open a window/);
  });
});
