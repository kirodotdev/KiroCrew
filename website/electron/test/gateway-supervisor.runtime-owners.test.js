"use strict";

// The runtime owners gateway-supervisor.js composes, each pinned through the
// supervisor's public surface. Shared fakes live in gateway-supervisor-test-helpers.js.

const { EventEmitter } = require("node:events");
const { test } = require("node:test");
const assert = require("node:assert");

const {
  fakeStore,
  switchableHttp,
  fakeTimers,
  flush,
  harness,
} = require("./gateway-supervisor-test-helpers");

// ── Characterization of the supervisor's runtime owners ──────────────────
//
// The facade composes cohesive owners (token sources, port holders, family
// takeover, launch preflight, the remote-crew prompt). These tests pin what each
// one does through the public supervisor surface, so an ownership move that
// changes an argv, an environment entry or a refusal fails here by name.

function remoteCrewStore(port, crew, extra = {}) {
  return fakeStore({ remoteHosts: { [String(port)]: crew }, ...extra });
}

test("fetchRemoteToken runs one bounded ssh against the port's crew and parses its token", async (t) => {
  t.mock.method(console, "error", () => {});
  const calls = [];
  const { supervisor, logs } = harness({
    store: remoteCrewStore(5476, {
      host: "myhost.example.com",
      binPath: "/opt/kc/bin/kirocrew",
      remotePort: "7000",
      remotePath: "",
    }, { sshTimeoutMs: 1000 }),
    execFileFn: (file, args, options, callback) => {
      calls.push({ file, args, options });
      callback(null, "open http://localhost:7000/?token=abc123\n", "");
    },
  });

  assert.deepStrictEqual(await supervisor.fetchRemoteToken(), { token: "abc123", error: null });
  assert.strictEqual(calls.length, 1);
  assert.strictEqual(calls[0].file, "/usr/bin/ssh");
  assert.deepStrictEqual(
    calls[0].args.slice(0, 6),
    ["-n", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", "myhost.example.com"],
    "stdin closed, no prompt, and ssh's connect deadline inside the kill budget",
  );
  assert.strictEqual(calls[0].args.length, 7);
  assert.match(calls[0].args[6], /KIROCREW_PORT=7000/, "the crew's own port, not the local end");
  assert.deepStrictEqual(calls[0].options, { timeout: 5000 }, "the SSH budget never drops below 5s");
  assert.ok(logs.includes("SSH token fetch: /usr/bin/ssh myhost.example.com for port 7000"));
});

test("fetchRemoteToken defaults the SSH budget and the remote port to the tab's port", async (t) => {
  t.mock.method(console, "error", () => {});
  const calls = [];
  const { supervisor } = harness({
    store: remoteCrewStore(7778, {
      host: "clouddesk",
      binPath: "/opt/kc/bin/kirocrew",
      remotePort: "",
      remotePath: "",
    }),
    execFileFn: (file, args, options, callback) => {
      calls.push({ file, args, options });
      callback(null, "no token here", "");
    },
  });

  assert.deepStrictEqual(await supervisor.fetchRemoteToken(7778), { token: "", error: null });
  assert.deepStrictEqual(calls[0].options, { timeout: 20000 });
  assert.ok(calls[0].args.includes("ConnectTimeout=18"));
  assert.match(calls[0].args[6], /KIROCREW_PORT=7778/);
});

function windowsCrewHarness(kernelRoot, execFileFn) {
  return harness({
    processRef: {
      platform: "win32",
      arch: "x64",
      env: { KIROCREW_HOME: "C:\\virtual\\kirocrew-home", SystemRoot: "C:\\planted" },
      resourcesPath: "C:\\virtual\\resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
    fsMod: {
      constants: { X_OK: 1 },
      mkdirSync() {},
      accessSync() { throw Object.assign(new Error("not found"), { code: "ENOENT" }); },
      existsSync() { return false; },
      realpathSync: {
        native: () => {
          if (kernelRoot instanceof Error) throw kernelRoot;
          return kernelRoot;
        },
      },
      readFileSync() { throw new Error("unexpected filesystem read"); },
    },
    store: remoteCrewStore(5476, {
      host: "devbox",
      binPath: "/opt/kc/bin/kirocrew",
      remotePort: "",
      remotePath: "",
    }),
    execFileFn,
  });
}

test("fetchRemoteToken runs the in-box OpenSSH client under the kernel's Windows directory", async (t) => {
  t.mock.method(console, "error", () => {});
  const calls = [];
  const { supervisor } = windowsCrewHarness("D:\\Windows", (file, args, options, callback) => {
    calls.push({ file, args, options });
    callback(null, "http://localhost:5476?token=abc\n", "");
  });

  assert.deepStrictEqual(await supervisor.fetchRemoteToken(), { token: "abc", error: null });
  assert.strictEqual(calls[0].file, "D:\\Windows\\System32\\OpenSSH\\ssh.exe");
});

test("fetchRemoteToken refuses on Windows when the system directory does not resolve", async (t) => {
  t.mock.method(console, "error", () => {});
  let runs = 0;
  const { supervisor } = windowsCrewHarness(new Error("EINVAL"), () => { runs += 1; });

  assert.deepStrictEqual(
    await supervisor.fetchRemoteToken(),
    { token: "", error: "ssh client not found: the Windows system directory did not resolve." },
  );
  assert.strictEqual(runs, 0, "no guessed ssh path is ever run");
});

test("fetchRemoteToken reports ssh's stderr, else its error message", async (t) => {
  t.mock.method(console, "error", () => {});
  let stderr = "  Permission denied (publickey).  \n";
  const { supervisor } = harness({
    store: remoteCrewStore(5476, {
      host: "myhost.example.com",
      binPath: "/opt/kc/bin/kirocrew",
      remotePort: "",
      remotePath: "",
    }),
    execFileFn: (_file, _args, _options, callback) => {
      callback(new Error("Command failed: ssh"), "", stderr);
    },
  });

  assert.deepStrictEqual(
    await supervisor.fetchRemoteToken(),
    { token: "", error: "Permission denied (publickey)." },
  );
  stderr = "";
  assert.deepStrictEqual(
    await supervisor.fetchRemoteToken(),
    { token: "", error: "Command failed: ssh" },
  );
});

test("fetchRemoteToken names a missing ssh client and a timeout kill", async (t) => {
  t.mock.method(console, "error", () => {});
  let failure;
  const { supervisor } = harness({
    store: remoteCrewStore(5476, {
      host: "devbox",
      binPath: "/opt/kc/bin/kirocrew",
      remotePort: "",
      remotePath: "",
    }),
    execFileFn: (_file, _args, _options, callback) => callback(failure, "", ""),
  });

  failure = Object.assign(new Error("spawn /usr/bin/ssh ENOENT"), { code: "ENOENT" });
  assert.deepStrictEqual(
    await supervisor.fetchRemoteToken(),
    { token: "", error: "ssh client not found: /usr/bin/ssh. Install the OpenSSH client and retry." },
  );
  failure = Object.assign(new Error("Command failed: ssh"), { killed: true, signal: "SIGTERM" });
  assert.deepStrictEqual(
    await supervisor.fetchRemoteToken(),
    { token: "", error: "ssh devbox timed out after 20 s" },
  );
});

test("fetchRemoteToken refuses invalid settings and runs nothing without a crew", async (t) => {
  t.mock.method(console, "error", () => {});
  let runs = 0;
  const execFileFn = (_file, _args, _options, callback) => {
    runs += 1;
    callback(null, "", "");
  };
  const invalid = harness({
    store: remoteCrewStore(5476, {
      host: "bad host name",
      binPath: "/opt/kc/bin/kirocrew",
      remotePort: "",
      remotePath: "",
    }),
    execFileFn,
  });
  const refused = await invalid.supervisor.fetchRemoteToken();
  assert.strictEqual(refused.token, "");
  assert.match(refused.error, /^Invalid hostname/);

  const none = harness({ execFileFn });
  assert.deepStrictEqual(await none.supervisor.fetchRemoteToken(), { token: "", error: null });
  assert.strictEqual(runs, 0, "neither a refused nor an absent crew reaches ssh");
});

test("the spawned gateway's project dir is the Electron app's parent off Windows", async () => {
  const { supervisor, spawnCalls } = harness();
  assert.strictEqual(await supervisor.start(), true);
  const [bin, args, options] = spawnCalls[0];
  assert.strictEqual(bin, "kirocrew");
  assert.deepStrictEqual(args, ["gateway", "--no-open", "--port", "5476"]);
  assert.strictEqual(options.env.KIROCREW_PROJECT_DIR, "/virtual");
  assert.deepStrictEqual(options.stdio, ["ignore", 41, 41]);
  assert.strictEqual(options.detached, false);
  assert.strictEqual(options.windowsHide, true);
});

test("Windows takes the first tree above the Electron sources carrying agents and skills", async () => {
  const baseFs = harness().fsMod;
  const probed = [];
  const windowsProcess = (existing) => ({
    processRef: {
      platform: "win32",
      arch: "x64",
      env: { KIROCREW_HOME: "/virtual/kirocrew-home" },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
    fsMod: {
      ...baseFs,
      existsSync(candidate) {
        probed.push(candidate);
        return existing.has(candidate);
      },
    },
  });

  const twoUp = harness(windowsProcess(new Set(["/agents", "/skills"])));
  assert.strictEqual(await twoUp.supervisor.start(), true);
  assert.strictEqual(twoUp.spawnCalls[0][2].env.KIROCREW_PROJECT_DIR, "/");
  assert.notStrictEqual(probed.indexOf("/virtual/agents"), -1);
  assert.ok(probed.indexOf("/virtual/agents") < probed.indexOf("/agents"), "one level up is probed first");

  const both = harness(windowsProcess(new Set(["/virtual/agents", "/virtual/skills", "/agents", "/skills"])));
  assert.strictEqual(await both.supervisor.start(), true);
  assert.strictEqual(
    both.spawnCalls[0][2].env.KIROCREW_PROJECT_DIR,
    "/virtual",
    "when both levels carry the trees, the nearer one wins",
  );

  const neither = harness(windowsProcess(new Set(["/agents"])));
  assert.strictEqual(await neither.supervisor.start(), true);
  assert.strictEqual(
    neither.spawnCalls[0][2].env.KIROCREW_PROJECT_DIR,
    "/virtual",
    "a tree missing either directory falls back to the app's parent",
  );
});

test("a GUI-launched macOS gateway appends only the launchd domain's new PATH entries", async () => {
  const launchctlCalls = [];
  const { supervisor, spawnCalls, logs } = harness({
    processRef: {
      platform: "darwin",
      arch: "arm64",
      env: { KIROCREW_HOME: "/virtual/kirocrew-home", PATH: "/usr/bin:/bin" },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
    execFileSyncFn: (file, args) => {
      launchctlCalls.push([file, ...args]);
      return "/opt/homebrew/bin:/usr/bin\n";
    },
  });

  assert.strictEqual(await supervisor.start(), true);
  // One launchd PATH read and one system-proxy read: the proxy discovery runs
  // beside the PATH recovery (issue #19821) and this environment carries no
  // proxy variable, so gateway-env.js asks the system store. A store without
  // a manual proxy must leave the spawn env untouched — asserted below.
  assert.deepStrictEqual(launchctlCalls, [
    ["/bin/launchctl", "getenv", "PATH"],
    ["/usr/sbin/scutil", "--proxy"],
  ]);
  assert.strictEqual(spawnCalls[0][2].env.PATH, "/usr/bin:/bin:/opt/homebrew/bin");
  assert.strictEqual(
    spawnCalls[0][2].env.HTTP_PROXY,
    undefined,
    "a proxy-less scutil answer discovers nothing",
  );
  assert.ok(logs.includes("PATH recovered from launchd domain: +1 dir(s) appended"));
  assert.strictEqual(spawnCalls[0][2].env.KIROCREW_PORT, undefined, "the explicit --port wins");
});

test("a proxy-only macOS host gets the system store's proxy into the gateway env", async () => {
  // A GUI launch inherits launchd's environment, not the shell's: with the
  // proxy only in System Settings, the spawn env must come from scutil —
  // this is the whole fix for issue #19821's "every turn fails as a
  // transient 5xx".
  const { supervisor, spawnCalls } = harness({
    processRef: {
      platform: "darwin",
      arch: "arm64",
      env: { KIROCREW_HOME: "/virtual/kirocrew-home", PATH: "/usr/bin:/bin" },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
    execFileSyncFn: (file) => {
      if (file === "/usr/sbin/scutil") {
        return [
          "<dictionary> {",
          "  HTTPEnable : 1",
          "  HTTPPort : 7890",
          "  HTTPProxy : proxy.corp.example",
          "  HTTPSEnable : 1",
          "  HTTPSPort : 7890",
          "  HTTPSProxy : proxy.corp.example",
          "  ExceptionsList : <array> {",
          "    0 : *.local",
          "  }",
          "}",
          "",
        ].join("\n");
      }
      return "/opt/homebrew/bin:/usr/bin\n";
    },
  });

  assert.strictEqual(await supervisor.start(), true);
  const env = spawnCalls[0][2].env;
  assert.strictEqual(env.HTTP_PROXY, "http://proxy.corp.example:7890");
  assert.strictEqual(env.HTTPS_PROXY, "http://proxy.corp.example:7890");
  assert.strictEqual(env.NO_PROXY, ".local,localhost,127.0.0.1,::1");
  assert.strictEqual(env.PATH, "/usr/bin:/bin:/opt/homebrew/bin");
});

test("an inherited proxy variable suppresses the system-proxy read at spawn", async () => {
  let scutilReads = 0;
  const { supervisor, spawnCalls } = harness({
    processRef: {
      platform: "darwin",
      arch: "arm64",
      env: {
        KIROCREW_HOME: "/virtual/kirocrew-home",
        PATH: "/usr/bin:/bin",
        HTTPS_PROXY: "http://operator.example:3128",
      },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
    execFileSyncFn: (file) => {
      if (file === "/usr/sbin/scutil") scutilReads += 1;
      return "/opt/homebrew/bin:/usr/bin\n";
    },
  });

  assert.strictEqual(await supervisor.start(), true);
  assert.strictEqual(scutilReads, 0, "an explicit env must not pay for discovery");
  assert.strictEqual(
    spawnCalls[0][2].env.HTTPS_PROXY,
    "http://operator.example:3128",
    "the operator's own proxy variable wins verbatim",
  );
});

test("PATH is left exactly as inherited off macOS", async () => {
  let reads = 0;
  const { supervisor, spawnCalls, logs } = harness({
    processRef: {
      platform: "linux",
      arch: "x64",
      env: { KIROCREW_HOME: "/virtual/kirocrew-home", PATH: "/usr/bin:/bin", KIROCREW_PORT: "9999" },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
    execFileSyncFn: () => { reads += 1; return "/opt/extra/bin"; },
  });

  assert.strictEqual(await supervisor.start(), true);
  assert.strictEqual(reads, 0);
  assert.strictEqual(spawnCalls[0][2].env.PATH, "/usr/bin:/bin");
  assert.strictEqual(spawnCalls[0][2].env.KIROCREW_PORT, undefined);
  assert.ok(!logs.some((line) => line.startsWith("PATH recovered")));
});

test("the conflict prompt quits the other family's app through AppleScript by NAME", async () => {
  const osascript = [];
  const probes = { released: false };
  const { supervisor, spawnCalls } = harness({
    processRef: {
      platform: "darwin",
      arch: "arm64",
      env: { KIROCREW_HOME: "/virtual/kirocrew-home" },
      resourcesPath: "/virtual/resources",
      kill() { throw new Error("process kill must not run in this harness"); },
    },
    app: { getVersion: () => "0.6.0" },
    httpMod: switchableHttp({
      status: 200,
      body: JSON.stringify({ app: "kirocrew", version: "0.6.0-nightly.20260101t000000" }),
    }),
    dialog: { showMessageBox: async () => ({ response: 0 }) },
    execFileFn: (file, args, options, callback) => {
      if (file === "osascript") {
        osascript.push({ args, options });
        probes.released = true;
        callback(null, "", "");
        return;
      }
      if (String(file).endsWith("lsof")) {
        callback(null, probes.released ? "" : "4321\n", "");
        return;
      }
      if (file === "/bin/ps") {
        callback(null, args.includes("ppid=") ? "999\n" : "/opt/kc/bin/kirocrew gateway\n", "");
        return;
      }
      throw new Error(`unexpected ${file}`);
    },
  });

  assert.strictEqual(await supervisor.start(), true);
  assert.strictEqual(osascript.length, 1, "the nightly-owned port prompts one takeover");
  assert.deepStrictEqual(osascript[0].args, ["-e", 'quit app "KiroCrew Nightly"']);
  assert.deepStrictEqual(osascript[0].options, { timeout: 10000 });
  assert.strictEqual(spawnCalls.length, 1, "the released port is spawned into");
});


function staleWarningHarness({ response = 0, parentPid = 99 } = {}) {
  const requests = [];
  const dialogs = [];
  const instance = harness({
    app: { isPackaged: true, getVersion: () => "0.7.1" },
    processRef: {
      platform: "darwin", arch: "arm64", env: {}, resourcesPath: "/virtual/resources",
      kill() { throw new Error("stale warning must not signal the gateway"); },
    },
    httpMod: {
      get(url, _options, callback) {
        requests.push(String(url));
        const req = new EventEmitter();
        req.destroy = () => {};
        queueMicrotask(() => {
          const res = new EventEmitter();
          res.statusCode = 200;
          res.resume = () => {};
          callback(res);
          res.emit("data", JSON.stringify(url.endsWith("/api/ready")
            ? { ready: true }
            : { app: "kirocrew", version: "0.7.0" }));
          res.emit("end");
        });
        return req;
      },
    },
    execFileFn(file, args, _options, callback) {
      if (file.endsWith("lsof")) callback(null, "123", "");
      else if (args.includes("ppid=")) callback(null, String(parentPid), "");
      else callback(null, "/virtual/resources/backend-dist/bin/python -m kiro_crew gateway", "");
    },
    dialog: { async showMessageBox(options) { dialogs.push(options); return { response }; } },
  });
  return { ...instance, requests, dialogs };
}

test("a stale bundled gateway warns and continues without requesting a restart", async () => {
  const state = staleWarningHarness();
  assert.equal(await state.supervisor.start(), true);
  assert.equal(state.spawnCalls.length, 0);
  assert.equal(state.dialogs.length, 1);
  assert.equal(state.dialogs[0].message, "The gateway is still running an older version.");
  assert.match(state.dialogs[0].detail, /app is version 0\.7\.1/);
  assert.match(state.dialogs[0].detail, /gateway is still running version 0\.7\.0/);
  assert.match(state.dialogs[0].detail, /Run this command in Terminal:\nkirocrew stop --port 5476/);
  assert.doesNotMatch(state.dialogs[0].detail, /[“”]/);
  assert.deepEqual(state.dialogs[0].buttons, ["Continue with existing gateway", "Quit"]);
  assert.deepEqual(state.requests, [
    "http://localhost:5476/api/status",
    "http://localhost:5476/api/health",
    "http://localhost:5476/api/ready",
  ]);
});

test("a stale bundled gateway warning honors Quit", async () => {
  const state = staleWarningHarness({ response: 1 });
  assert.equal(await state.supervisor.start(), false);
  assert.equal(state.spawnCalls.length, 0);
  assert.equal(state.dialogs.length, 1);
});

test("a service-owned stale bundle adds conditional service recovery guidance", async () => {
  const state = staleWarningHarness({ parentPid: 1 });
  assert.equal(await state.supervisor.start(), true);
  assert.match(state.dialogs[0].detail, /If the gateway starts again automatically/);
  assert.match(state.dialogs[0].detail, /stop or update the service that restarts it/);
});

// A gateway this app found already serving and adopted, as it is when a desktop
// update relaunches over a gateway it did not spawn. `command` is what ps reports
// for the listener; under /virtual/resources/backend-dist it runs from this
// app's own bundle. `shutdownStatus` is what POST /api/shutdown answers. When
// the gateway accepts the stop it releases the port and exits; `rebindPid`
// models a service manager starting it again from the same files.
function adoptedGatewayHarness({
  command = "/virtual/resources/backend-dist/bin/python -m kiro_crew gateway",
  gatewayVersion = "0.7.0",
  shutdownStatus = 200,
  parentPid = 99,
  platform = "darwin",
  rebindPid = 0,
  cliStop = null,
  exitsOnStop = true,
} = {}) {
  const state = {
    listenPids: [123],
    alive: new Set([123]),
    rebindPending: 0,
    shutdowns: [],
    signals: [],
    cliStops: [],
    dialogs: [],
  };
  const releasePort = () => {
    if (exitsOnStop) state.alive.delete(123);
    state.listenPids = [];
    state.rebindPending = rebindPid;
  };
  // The port reads free once, then the manager's replacement holds it.
  const listenerProbe = () => {
    const answer = state.listenPids.join("\n");
    if (!state.listenPids.length && state.rebindPending) {
      state.listenPids = [state.rebindPending];
      state.alive.add(state.rebindPending);
      state.rebindPending = 0;
    }
    return answer;
  };
  const baseFs = harness().fsMod;
  const instance = harness({
    app: { isPackaged: true, getVersion: () => "0.7.1" },
    processRef: {
      platform,
      arch: "arm64",
      env: {},
      resourcesPath: "/virtual/resources",
      kill(pid, signal) {
        if (signal === 0) {
          if (state.alive.has(pid)) return true;
          const error = new Error("no such process");
          error.code = "ESRCH";
          throw error;
        }
        state.signals.push([pid, signal]);
        return true;
      },
    },
    fsMod: {
      ...baseFs,
      readFileSync(file) {
        // The gateway publishes one secret under every address it bound; the
        // home-wide slot holds a different value so a test proves it is never sent.
        if (/[\\/]run[\\/]gateway-5476-[^\\/]+\.secret$/.test(String(file))) return "boot-secret\n";
        if (String(file).endsWith(".local_secret")) return "home-wide-secret\n";
        if (String(file).endsWith("gateway.lock")) return "123\n";
        throw new Error(`unexpected filesystem read: ${file}`);
      },
    },
    httpMod: {
      get(url, _options, callback) {
        const req = new EventEmitter();
        req.destroy = () => {};
        queueMicrotask(() => {
          const res = new EventEmitter();
          res.statusCode = 200;
          res.resume = () => {};
          callback(res);
          res.emit("data", JSON.stringify(String(url).endsWith("/api/ready")
            ? { ready: true }
            : { app: "kirocrew", version: gatewayVersion }));
          res.emit("end");
        });
        return req;
      },
      request(options, callback) {
        const req = new EventEmitter();
        req.destroy = () => {};
        req.end = () => {
          state.shutdowns.push({
            method: options.method,
            path: options.path,
            secret: options.headers["X-Local-Secret"],
          });
          queueMicrotask(() => {
            if (shutdownStatus === 200) releasePort();
            callback({ statusCode: shutdownStatus, resume() {} });
          });
        };
        return req;
      },
    },
    execFileFn(file, args, _options, callback) {
      if (args[0] === "stop") {
        state.cliStops.push(args);
        if (cliStop === "accept") releasePort();
        callback(cliStop === "accept" ? null : new Error("refused"), "", "");
      } else if (file.endsWith("lsof") && args.includes("-Fn")) {
        callback(null, "n/virtual/kirocrew-home/gateway.lock\n", "");
      } else if (file.endsWith("lsof")) {
        callback(null, listenerProbe(), "");
      } else if (args.includes("ppid=")) {
        callback(null, String(parentPid), "");
      } else {
        callback(null, command, "");
      }
    },
    dialog: {
      async showMessageBox(options) {
        state.dialogs.push(options);
        return { response: 0 };
      },
    },
  });
  return { ...instance, state };
}

test("the update stop shuts down an adopted gateway that runs from this app's bundle", async () => {
  const { supervisor, spawnCalls, state } = adoptedGatewayHarness();
  assert.equal(await supervisor.start(), true);
  assert.equal(spawnCalls.length, 0, "the existing gateway is adopted, not replaced");
  assert.equal(state.dialogs.length, 1, "the older bundled gateway is warned about, then continued");

  await supervisor.stopForUpdate();

  assert.deepStrictEqual(state.shutdowns, [
    { method: "POST", path: "/api/shutdown", secret: "boot-secret" },
  ], "the adopted gateway is asked to flush and exit before its files are replaced");
  assert.deepStrictEqual(state.listenPids, [], "the stop resolves only once the port is free");
  assert.deepStrictEqual(state.signals, [], "a gateway that accepted the stop is never signalled");
});

test("the same-version adopted bundled gateway is stopped for the update too", async () => {
  const { supervisor, state } = adoptedGatewayHarness({ gatewayVersion: "0.7.1" });
  assert.equal(await supervisor.start(), true);
  assert.equal(state.dialogs.length, 0);

  await supervisor.stopForUpdate();

  assert.equal(state.shutdowns.length, 1);
  assert.deepStrictEqual(state.listenPids, []);
});

test("an adopted bundled gateway that refuses the stop fails the update stop and is never signalled", async () => {
  const { supervisor, state } = adoptedGatewayHarness({ shutdownStatus: 403 });
  assert.equal(await supervisor.start(), true);

  await assert.rejects(supervisor.stopForUpdate(), {
    code: "gateway-still-running",
    stopCommand: "kirocrew stop --port 5476",
  });

  assert.equal(state.shutdowns.length, 1);
  assert.deepStrictEqual(state.signals, [], "macOS has no pidfd to pin a signal to the checked process");
  assert.deepStrictEqual(state.cliStops, [], "the pid-pinned CLI stop is Linux-only");
  assert.deepStrictEqual(state.listenPids, [123], "the gateway is left running");
});

test("on Linux a refused shutdown falls back to the pid-pinned CLI stop for the proven pid", async () => {
  const { supervisor, state } = adoptedGatewayHarness({
    platform: "linux", shutdownStatus: 403, cliStop: "accept",
  });
  assert.equal(await supervisor.start(), true);

  await supervisor.stopForUpdate();

  assert.equal(state.cliStops.length, 1);
  assert.deepStrictEqual(state.cliStops[0], ["stop", "--port", "5476", "--expect-pid", "123"]);
  assert.deepStrictEqual(state.signals, [], "the app itself signals nothing");
  assert.deepStrictEqual(state.listenPids, []);
});

test("an adopted bundled gateway that released the port but has not exited fails the update stop", async () => {
  const { supervisor, state } = adoptedGatewayHarness({ exitsOnStop: false });
  assert.equal(await supervisor.start(), true);

  await assert.rejects(
    supervisor.stopForUpdate({ adoptedTimeoutMs: 600 }),
    { code: "gateway-still-running" },
  );

  assert.deepStrictEqual(state.listenPids, [], "the port is free, yet the process still runs");
  assert.deepStrictEqual(state.signals, [], "a draining gateway is waited for, never signalled");
});

test("an adopted bundled gateway its service manager starts again fails the update stop", async () => {
  const { supervisor, state } = adoptedGatewayHarness({ parentPid: 1, rebindPid: 456 });
  assert.equal(await supervisor.start(), true);

  // The service would start a signalled process again, so the refusal names
  // the CLI stop that stops the service itself.
  await assert.rejects(supervisor.stopForUpdate(), {
    code: "gateway-still-running",
    stopCommand: "kirocrew stop",
  });

  assert.equal(state.shutdowns.length, 1);
  assert.deepStrictEqual(state.listenPids, [456], "the restarted gateway runs from the same files");
});

test("an adopted gateway from another install is left alone by the update stop", async () => {
  const { supervisor, state } = adoptedGatewayHarness({
    command: "/usr/local/bin/kirocrew gateway",
    gatewayVersion: "0.7.1",
  });
  assert.equal(await supervisor.start(), true);

  await supervisor.stopForUpdate();

  assert.deepStrictEqual(state.shutdowns, [], "a gateway that does not run from this bundle is not stopped");
  assert.deepStrictEqual(state.signals, []);
  assert.deepStrictEqual(state.listenPids, [123]);
});

test("a client-only launch whose crew opted in opens the managed tunnel before asking the port", async () => {
  const timers = fakeTimers();
  const { supervisor, spawnCalls } = harness({
    timers,
    store: remoteCrewStore(5477, {
      host: "devbox.example.com",
      binPath: "kirocrew",
      remotePort: "5476",
      remotePath: "",
      manageTunnel: true,
    }, { runLocalGateway: false }),
    port: 5477,
  });

  const started = supervisor.start();
  await flush();
  assert.strictEqual(spawnCalls.length, 1, "the keeper is spawned first");
  const [, args] = spawnCalls[0];
  assert.deepStrictEqual(args.slice(0, 2), ["desktop", "tunnel"]);
  assert.deepStrictEqual(
    args.slice(2),
    ["--host", "devbox.example.com", "--local-port", "5477", "--remote-port", "5476"],
  );

  // The forward never answers: after one connect budget the launch falls through
  // to the ordinary client-only failure, and no gateway is started here.
  while (timers.pending.some((timer) => timer.ms === 500)) {
    timers.fire(500);
    await flush();
  }
  assert.strictEqual(await started, false);
  assert.strictEqual(spawnCalls.length, 1, "client-only still starts no local gateway");

  supervisor.stopOnQuit();
  assert.ok(spawnCalls[0].child.killed, "quitting stops the keeper");
});

test("without the opt-in a client-only launch spawns nothing and does not wait", async () => {
  const { supervisor, spawnCalls } = harness({
    store: remoteCrewStore(5477, {
      host: "devbox.example.com",
      binPath: "kirocrew",
      remotePort: "5476",
      remotePath: "",
    }, { runLocalGateway: false }),
    port: 5477,
  });
  assert.strictEqual(await supervisor.start(), false);
  assert.strictEqual(spawnCalls.length, 0);
});
