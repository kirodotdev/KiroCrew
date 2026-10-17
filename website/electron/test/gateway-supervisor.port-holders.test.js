"use strict";

// A gateway port something else already holds at launch: the other release
// family's app (the takeover prompt) and a draining same-version gateway (the
// lock probe). The stuck gateway for this data folder is tested in
// gateway-supervisor.test.js. Shared fakes live in gateway-supervisor-test-helpers.js.

const { EventEmitter } = require("node:events");
const { test, mock } = require("node:test");
const assert = require("node:assert");

const {
  switchableHttp,
  flush,
  harness,
  OWN_GATEWAY_COMMAND,
  FOREIGN_COMMAND,
} = require("./gateway-supervisor-test-helpers");

// ---------------------------------------------------------------------------
// Cross-family conflict: the takeover prompt on a platform that cannot quit the
// other app for the user (#12398). The owner probe runs for real here — only the
// two OS calls it makes (netstat -ano, Win32_Process) and the dialog are faked.
// ---------------------------------------------------------------------------

const PROD_VERSION = "0.7.0";
const NIGHTLY_VERSION = "0.7.0-nightly.20260919";

function windowsOwnerExecFile(state) {
  return (file, args, options, callback) => {
    const tool = String(file).toLowerCase();
    if (tool.includes("netstat")) {
      state.netstatCalls = (state.netstatCalls || 0) + 1;
      if (state.probeFails || state.failNetstatCall === state.netstatCalls) {
        callback(new Error("netstat unavailable"));
        return;
      }
      callback(null, state.held
        ? "  TCP    0.0.0.0:5476           0.0.0.0:0              LISTENING       4242\r\n"
        : "", "");
      return;
    }
    if (tool.includes("powershell") || tool.includes("wmic")) {
      callback(null, state.command ?? OWN_GATEWAY_COMMAND, "");
      return;
    }
    callback(new Error(`unexpected command: ${file}`));
  };
}

function posixOwnerExecFile(state) {
  return (file, args, options, callback) => {
    if (file === "osascript") {
      state.quitAttempts.push(args.join(" "));
      state.held = false;
      callback(null, "", "");
      return;
    }
    if (String(file).endsWith("lsof")) {
      state.lsofCalls = (state.lsofCalls || 0) + 1;
      if (state.failLsofCall === state.lsofCalls) {
        const error = new Error("lsof unavailable");
        error.code = "ENOENT";
        callback(error);
        return;
      }
      callback(null, state.held ? "4242\n" : "", "");
      return;
    }
    if (file === "/bin/ps") {
      callback(null, args.includes("ppid=") ? "500\n" : `${OWN_GATEWAY_COMMAND}\n`, "");
      return;
    }
    callback(new Error(`unexpected command: ${file}`));
  };
}

// A dialog that answers each prompt from a scripted queue (the last answer
// repeats) and records every message box it was asked to show.
function scriptedDialog(responses, onPrompt = () => {}) {
  const shown = [];
  return {
    shown,
    dialog: {
      showMessageBox: async (options) => {
        shown.push(options);
        onPrompt(shown.length);
        const index = Math.min(shown.length - 1, responses.length - 1);
        return { response: responses[index] };
      },
    },
  };
}

function conflictHarness({
  platform,
  ownVersion,
  otherVersion,
  responses,
  state,
  quits,
  onPrompt,
}) {
  const { shown, dialog } = scriptedDialog(responses, onPrompt);
  const httpState = {
    status: 200,
    body: JSON.stringify({ ok: true, app: "kirocrew", version: otherVersion }),
  };
  const harnessResult = harness({
    dialog,
    app: { getVersion: () => ownVersion },
    httpMod: switchableHttp(httpState),
    execFileFn: platform === "win32" ? windowsOwnerExecFile(state) : posixOwnerExecFile(state),
    requestQuit: () => quits.push("quit"),
    processRef: {
      platform,
      arch: "x64",
      env: { KIROCREW_HOME: "/virtual/kirocrew-home" },
      resourcesPath: "/virtual/resources",
      // Signal-0 liveness only. Returning normally says "still alive"; throwing
      // without EPERM says "gone", which is what pidAlive reads.
      kill() {
        if (!state.incumbentAlive) throw new Error("no such process");
      },
    },
  });
  return { ...harnessResult, shown };
}

// waitForPortFree polls the real clock through the global timers, so a test that
// needs it to TIME OUT drives both from node:test's timer mock.
async function settleWithTimeoutMock(promise) {
  let settled = false;
  const result = promise.then((value) => { settled = true; return value; });
  for (let step = 0; step < 40 && !settled; step += 1) {
    await flush();
    mock.timers.tick(31000);
  }
  return result;
}

// otherDisplay is FAMILY_META.displayName; otherApp is its appName, the
// technical Finder/AppleScript target, which is the joined identifier form.
for (const [label, ownVersion, otherVersion, otherDisplay, otherApp] of [
  ["production → Nightly", PROD_VERSION, NIGHTLY_VERSION, "Kiro Crew Nightly", "KiroCrew Nightly"],
  ["Nightly → production", NIGHTLY_VERSION, PROD_VERSION, "Kiro Crew", "KiroCrew"], // brand-ok
]) {
  test(`win32 ${label}: Retry after a manual quit completes the pending launch`, async () => {
    const state = { held: true };
    const quits = [];
    const { supervisor, logs, spawnCalls, shown } = conflictHarness({
      platform: "win32",
      ownVersion,
      otherVersion,
      responses: [0],
      state,
      quits,
      // The user quits the other app while the dialog is up. The freed LISTEN
      // socket is the only thing Retry waits for.
      onPrompt: () => { state.held = false; },
    });

    assert.strictEqual(await supervisor.start(), true);
    assert.strictEqual(shown.length, 1);
    assert.deepStrictEqual(shown[0].buttons, ["I quit it — Retry", "Cancel"]);
    assert.strictEqual(shown[0].cancelId, 1);
    assert.ok(shown[0].detail.endsWith(`Quit ${otherDisplay}, then choose “I quit it — Retry”.`));
    assert.ok(shown[0].detail.includes(shown[0].buttons[0]), "the detail names the button as written");
    assert.ok(shown[0].message.includes(otherVersion));
    assert.ok(logs.some((line) => line.includes("canTakeover=false on win32")));
    assert.ok(logs.some((line) => line === `takeover (manual): ${otherApp} released :5476 — proceeding to spawn`));
    assert.strictEqual(spawnCalls.length, 1);
    assert.deepStrictEqual(quits, []);
  });
}

test("win32 Retry while the port is still held re-prompts, then aborts", async () => {
  mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const quits = [];
  const { supervisor, logs, spawnCalls, shown } = conflictHarness({
    platform: "win32",
    ownVersion: PROD_VERSION,
    otherVersion: NIGHTLY_VERSION,
    responses: [0],
    state: { held: true },
    quits,
  });

  try {
    assert.strictEqual(await settleWithTimeoutMock(supervisor.start()), false);
  } finally {
    mock.timers.reset();
  }

  // Bounded: the prompt comes back, then the launch gives up with a notice
  // rather than asking forever or closing the app silently.
  assert.strictEqual(shown.length, 4);
  assert.ok(shown[1].buttons.includes("I quit it — Retry"));
  // A re-prompt that is byte-identical to the first reads as a glitch.
  assert.ok(shown[0].detail.endsWith(`Quit Kiro Crew Nightly, then choose “I quit it — Retry”.`));
  assert.ok(shown[1].detail.includes("was still running a moment ago"));
  assert.ok(shown[1].detail.includes(shown[1].buttons[0]), "the re-prompt names the button too");
  assert.ok(!shown[1].detail.includes("5476"), "port numbers are not the user's task");
  assert.notStrictEqual(shown[1].detail, shown[0].detail);
  assert.strictEqual(shown[2].detail, shown[1].detail);
  assert.strictEqual(shown[3].type, "error");
  assert.deepStrictEqual(shown[3].buttons, ["OK"]);
  assert.ok(shown[3].message.includes("Kiro Crew Nightly is still running."));
  assert.ok(shown[3].detail.includes("This launch was cancelled."));
  assert.strictEqual(
    logs.filter((line) => line.includes("still holds :5476 after retry")).length,
    3,
  );
  assert.ok(logs.some((line) => line.includes("never released :5476 — aborting this launch")));
  assert.strictEqual(spawnCalls.length, 0);
  assert.deepStrictEqual(quits, ["quit"]);
});

test("win32 Cancel aborts on the first prompt, exactly as before", async () => {
  const quits = [];
  const { supervisor, spawnCalls, shown } = conflictHarness({
    platform: "win32",
    ownVersion: PROD_VERSION,
    otherVersion: NIGHTLY_VERSION,
    responses: [1],
    state: { held: true },
    quits,
  });

  assert.strictEqual(await supervisor.start(), false);
  assert.strictEqual(shown.length, 1);
  assert.strictEqual(spawnCalls.length, 0);
  assert.deepStrictEqual(quits, ["quit"]);
});

for (const [label, state, adopted] of [
  ["an unprobeable listener", { held: true, probeFails: true }, true],
  ["a foreign listener", { held: true, command: FOREIGN_COMMAND }, false],
  ["no local listener", { held: false }, true],
]) {
  test(`win32 never prompts for ${label}`, async () => {
    const quits = [];
    const { supervisor, logs, shown } = conflictHarness({
      platform: "win32",
      ownVersion: PROD_VERSION,
      otherVersion: NIGHTLY_VERSION,
      responses: [0],
      state,
      quits,
    });

    // Not prompting and not quitting is the rule here, and it is unchanged in all
    // three: a tunnel is not ours to evict, and the AppleScript quit would target
    // a local app that is not running.
    //
    // Whether the holder is ADOPTED is a different question, and only the foreign
    // case answers it differently now. A positively foreign LISTEN owner with no
    // crew recorded on the port cannot be attributed to us or to a crew the user
    // named, and adopting it posts this machine's local secret to it -- the mint
    // only requires a literal loopback origin, which a tunnel's local end is. An
    // unreadable probe and an unseen socket are not that, so they keep the
    // historical reuse: this narrows the fail-open to the case that carries the
    // exposure rather than closing it.
    assert.strictEqual(await supervisor.start(), adopted, label);
    assert.strictEqual(shown.length, 0, `${label}: no takeover prompt`);
    assert.deepStrictEqual(quits, [], `${label}: nothing is quit`);
    assert.ok(!logs.some((line) => line.includes("prompting for takeover")), label);
    if (adopted) {
      assert.ok(logs.some((line) => line.includes("reusing existing gateway on :5476")), label);
    } else {
      assert.ok(
        logs.some((line) => line.includes("this app did not start and no remote crew is configured")),
        `${label}: the refusal names why it was not adopted`,
      );
      assert.ok(!logs.some((line) => line.includes("reusing existing gateway")), label);
    }
  });
}

test("darwin still offers the automatic quit and takes over itself", async () => {
  const state = { held: true, quitAttempts: [] };
  const quits = [];
  const { supervisor, logs, spawnCalls, shown } = conflictHarness({
    platform: "darwin",
    ownVersion: PROD_VERSION,
    otherVersion: NIGHTLY_VERSION,
    responses: [0],
    state,
    quits,
  });

  assert.strictEqual(await supervisor.start(), true);
  assert.strictEqual(shown.length, 1);
  assert.deepStrictEqual(shown[0].buttons, ["Quit Kiro Crew Nightly & Continue", "Cancel"]);
  assert.ok(shown[0].detail.endsWith("Quit Kiro Crew Nightly and continue here?"));
  assert.deepStrictEqual(state.quitAttempts, ['-e quit app "KiroCrew Nightly"']);
  assert.ok(logs.some((line) => line === "takeover: KiroCrew Nightly released :5476 — proceeding to spawn"));
  assert.ok(!logs.some((line) => line.includes("canTakeover=false")));
  assert.ok(!logs.some((line) => line.includes("takeover (manual)")));
  assert.strictEqual(spawnCalls.length, 1);
  assert.deepStrictEqual(quits, []);
});

test("win32 Retry waits for the incumbent process, not just for the port", async () => {
  mock.timers.enable({ apis: ["setTimeout", "Date"] });
  const state = { held: true, incumbentAlive: true };
  const quits = [];
  const { supervisor, logs, spawnCalls } = conflictHarness({
    platform: "win32",
    ownVersion: PROD_VERSION,
    otherVersion: NIGHTLY_VERSION,
    responses: [0],
    state,
    quits,
    // The socket closes on the quit, but the process lives on holding
    // gateway.lock — "port free is not lock free".
    onPrompt: () => { state.held = false; },
  });

  try {
    assert.strictEqual(await settleWithTimeoutMock(supervisor.start()), true);
  } finally {
    mock.timers.reset();
  }

  assert.ok(logs.some((line) => line.includes("takeover (manual): incumbent gateway process still alive after the exit grace")));
  assert.strictEqual(spawnCalls.length, 1);
  assert.deepStrictEqual(quits, []);
});

test("win32 refuses the respawn when the incumbent PID cannot be captured", async () => {
  const quits = [];
  const { supervisor, logs, spawnCalls, shown } = conflictHarness({
    platform: "win32",
    ownVersion: PROD_VERSION,
    otherVersion: NIGHTLY_VERSION,
    responses: [0],
    // Call 1 classifies the owner; call 2 is the PID snapshot. Failing only the
    // second is the transient-probe case: port free would then be read as lock
    // free, and the replacement would race the incumbent's gateway.lock.
    state: { held: true, incumbentAlive: true, failNetstatCall: 2 },
    quits,
  });

  assert.strictEqual(await supervisor.start(), false);
  assert.strictEqual(shown.length, 0, "no Retry is offered that could not be honoured");
  assert.strictEqual(spawnCalls.length, 0);
  assert.ok(logs.some((line) => line.includes("could not capture the incumbent PID on :5476")));
  assert.deepStrictEqual(quits, []);
});

test("linux refuses the respawn too, where unverifiedIncumbent is false by design", async () => {
  const quits = [];
  const { supervisor, logs, spawnCalls, shown } = conflictHarness({
    platform: "linux",
    ownVersion: PROD_VERSION,
    otherVersion: NIGHTLY_VERSION,
    responses: [0],
    // Call 1 classifies the owner, call 2 is the PID snapshot. A probe that
    // named a PID a moment ago and now names none is anomalous on every
    // platform, so the POSIX degrade-to-no-op rule must not apply here:
    // incumbentSnapshotBlocksRespawn is Windows-only, and relying on it would
    // let Linux spawn into the incumbent's still-held gateway.lock.
    state: { held: true, incumbentAlive: true, failLsofCall: 2, quitAttempts: [] },
    quits,
  });

  assert.strictEqual(await supervisor.start(), false);
  assert.strictEqual(shown.length, 0, "no Retry is offered that could not be honoured");
  assert.strictEqual(spawnCalls.length, 0);
  assert.ok(logs.some((line) => line.includes("could not capture the incumbent PID on :5476")));
  assert.deepStrictEqual(quits, []);
});

// A same-version gateway that answers /api/status but reports shutting-down on
// /api/ready is waited out, never adopted. On Windows a socket lookup that names
// no PID hands the decision to the `kirocrew gateway-pid` lock probe. The probe
// answer is scripted per test; `quits` records a backoff cancellation, because
// the harness reports the app quitting as soon as the probe has answered once
// (so a transient miss stops at the first backoff check instead of sleeping).
function drainingGatewayHarness({ isPackaged = false, gatewayPid }) {
  const state = { held: true, failNetstatCall: 2, gatewayPidCalls: 0 };
  const ownerExecFile = windowsOwnerExecFile(state);
  const quits = [];
  let built = null;
  built = harness({
    app: { getVersion: () => PROD_VERSION, isPackaged },
    httpMod: {
      get(url, _options, callback) {
        const request = new EventEmitter();
        request.destroy = () => {};
        queueMicrotask(() => {
          const ready = String(url).includes("/api/ready");
          const response = new EventEmitter();
          response.statusCode = ready ? 503 : 200;
          response.resume = () => {};
          callback(response);
          response.emit("data", ready
            ? JSON.stringify({ ready: false, shutting_down: true })
            : JSON.stringify({ ok: true, app: "kirocrew", version: PROD_VERSION }));
          response.emit("end");
        });
        return request;
      },
    },
    execFileFn: (file, args, options, callback) => {
      if (Array.isArray(args) && args.includes("gateway-pid")) {
        state.gatewayPidCalls += 1;
        gatewayPid(callback);
        return;
      }
      ownerExecFile(file, args, options, callback);
    },
    isQuitting: () => Boolean(built) && built.logs.some((line) => line.includes("lock probe") || line.includes("gateway-pid")),
    requestQuit: () => quits.push("quit"),
    processRef: {
      platform: "win32",
      arch: "x64",
      env: { KIROCREW_HOME: "/virtual/kirocrew-home" },
      resourcesPath: "/virtual/resources",
      kill() { /* the incumbent stays alive */ },
    },
  });
  return { ...built, state, quits };
}

test("win32 drain: an indeterminate lock ends the launch on the probe-failed error, without backing off", async () => {
  const { supervisor, logs, spawnCalls, state, quits } = drainingGatewayHarness({
    gatewayPid: (callback) => {
      const error = new Error("Command failed: kirocrew gateway-pid");
      error.code = 2;
      callback(error, `${JSON.stringify({ holder: "indeterminate", reason: "lock file unreadable" })}\n`, "");
    },
  });

  assert.strictEqual(await supervisor.start(), false);
  assert.strictEqual(spawnCalls.length, 0, "nothing is spawned over a lock that could not be read");
  assert.strictEqual(state.gatewayPidCalls, 1, "the refusal is not retried");
  assert.ok(logs.some((line) => line.includes("lock probe is indeterminate (lock file unreadable)")));
  assert.ok(logs.some((line) => line.includes("drain: could not verify the incumbent on :5476")));
  assert.ok(!logs.some((line) => line.includes("trying again in")), "no backoff for a refusal");
  assert.deepStrictEqual(quits, [], "the launch ends on its error, not on a cancelled backoff");
});

test("win32 drain: a packaged app with no bundled backend ends on the probe-failed error, without backing off", async () => {
  const { supervisor, logs, spawnCalls, state, quits } = drainingGatewayHarness({
    isPackaged: true,
    gatewayPid: () => { throw new Error("a packaged app must not run kirocrew from PATH"); },
  });

  assert.strictEqual(await supervisor.start(), false);
  assert.strictEqual(spawnCalls.length, 0);
  assert.strictEqual(state.gatewayPidCalls, 0, "the PATH fallback is refused before anything runs");
  assert.ok(logs.some((line) => line.includes("lock probe REFUSED: no bundled backend")));
  assert.ok(logs.some((line) => line.includes("drain: could not verify the incumbent on :5476")));
  assert.ok(!logs.some((line) => line.includes("trying again in")), "no backoff for a refusal");
  assert.deepStrictEqual(quits, []);
});

test("win32 drain: a lock probe that times out is backed off, not ended on the error", async () => {
  const { supervisor, logs, spawnCalls, state, quits } = drainingGatewayHarness({
    gatewayPid: (callback) => {
      const error = new Error("Command failed: kirocrew gateway-pid");
      error.killed = true;
      error.signal = "SIGTERM";
      callback(error, "", "");
    },
  });

  assert.strictEqual(await supervisor.start(), false);
  assert.strictEqual(spawnCalls.length, 0, "a transient miss never spawns blind");
  assert.strictEqual(state.gatewayPidCalls, 1);
  assert.ok(logs.some((line) => line.includes("gave no usable answer")));
  assert.ok(!logs.some((line) => line.includes("could not verify the incumbent")), "a timeout is not a refusal");
  assert.deepStrictEqual(quits, ["quit"], "the miss reached the backoff, which the quitting app cancelled");
});

test("win32 drain: a lock probe reporting ETIMEDOUT is backed off as a timeout", async () => {
  const { supervisor, logs, spawnCalls, state, quits } = drainingGatewayHarness({
    gatewayPid: (callback) => {
      const error = new Error("Command failed: kirocrew gateway-pid ETIMEDOUT");
      error.code = "ETIMEDOUT";
      callback(error, "", "");
    },
  });

  assert.strictEqual(await supervisor.start(), false);
  assert.strictEqual(spawnCalls.length, 0);
  assert.strictEqual(state.gatewayPidCalls, 1);
  assert.ok(!logs.some((line) => line.includes("could not verify the incumbent")), "a timeout is not a refusal");
  assert.deepStrictEqual(quits, ["quit"], "the miss reached the backoff, which the quitting app cancelled");
});

// A probe that could not be started, or that ran to completion without
// printing an answer, gets the same result on every later attempt: a missing
// or unexecutable interpreter, or a backend that fails on import. Backing off
// on it would keep the retry status up for good, so it ends on the
// probe-failed error like any other refusal, and nothing is spawned.
for (const [label, reply] of [
  ["the interpreter is missing (ENOENT)", { code: "ENOENT", errno: -2, syscall: "spawn python.exe" }],
  ["the interpreter is not executable (EACCES)", { code: "EACCES", errno: -13, syscall: "spawn python.exe" }],
  ["the backend exits non-zero without printing", { code: 1, stderr: "Traceback (most recent call last):\nImportError: no module\n" }],
  ["the backend exits 0 with no JSON on stdout", { ok: true, stdout: "not json\n" }],
]) {
  test(`win32 drain: a lock probe that gives no answer because ${label} ends on the probe-failed error, without backing off`, async () => {
    const { supervisor, logs, spawnCalls, state, quits } = drainingGatewayHarness({
      gatewayPid: (callback) => {
        if (reply.ok) {
          callback(null, reply.stdout, "");
          return;
        }
        const error = new Error(`spawn failed: ${reply.code}`);
        error.code = reply.code;
        if (reply.errno !== undefined) error.errno = reply.errno;
        if (reply.syscall) error.syscall = reply.syscall;
        error.killed = false;
        error.signal = null;
        callback(error, "", reply.stderr || "");
      },
    });

    assert.strictEqual(await supervisor.start(), false);
    assert.strictEqual(spawnCalls.length, 0, "nothing is spawned without a lock answer");
    assert.strictEqual(state.gatewayPidCalls, 1, "a probe that cannot answer is not retried");
    assert.ok(logs.some((line) => line.includes("drain: could not verify the incumbent on :5476")));
    assert.ok(!logs.some((line) => line.includes("trying again in")), "no backoff when the probe cannot answer");
    assert.deepStrictEqual(quits, [], "the launch ends on its error, not on a cancelled backoff");
  });
}
