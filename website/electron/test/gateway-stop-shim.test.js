// The quit/update stop path when the gateway child is a launcher shim that
// FORKED the real gateway instead of exec'ing it. SIGTERM reaches only the
// shim; without the tree listing the gateway lives on re-parented to init,
// holding the port and the lock.
const { test } = require("node:test");
const assert = require("node:assert");
const { stopGatewayGracefully, isKirocrewGatewayCommand } = require("../gateway-stop");

const GATEWAY_CMD = "/opt/py/bin/python3 -m kirocrew_edition gateway --port 5476";

// A fake process table. `procs[pid] = { cmd, onSignal(signal) -> "die" | "live" }`.
function world(procs) {
  const sent = [];
  const alive = new Set(Object.keys(procs).map(Number));
  const signalPidFn = (pid, signal) => {
    if (!alive.has(pid)) { const e = new Error("ESRCH"); e.code = "ESRCH"; throw e; }
    if (signal === 0) return;
    sent.push([pid, signal]);
    if (signal === "SIGKILL" || procs[pid].onSignal(signal) === "die") alive.delete(pid);
  };
  return {
    sent,
    alive,
    signalPidFn,
    getCommandFn: async (pid) => (alive.has(pid) ? procs[pid].cmd : ""),
  };
}

// The shim: the direct child. It dies on any signal.
function shimProc(pid = 100) {
  return {
    pid,
    exitCode: null,
    _onExit: [],
    signals: [],
    kill(sig) {
      this.signals.push(sig);
      this.exitCode = 0;
      for (const fn of this._onExit) fn();
    },
    once(event, fn) { if (event === "exit") this._onExit.push(fn); },
  };
}

const base = {
  backendUrl: "http://127.0.0.1:1",
  kirocrewHome: "/nope",
  postShutdownFn: async () => false, // the endpoint failed: the signal path
  platform: "darwin",
  survivorPollMs: 5,
};

test("shim: the gateway it forked gets SIGTERM once the shim is gone", async () => {
  const w = world({ 101: { cmd: GATEWAY_CMD, onSignal: () => "die" } });
  const proc = shimProc();
  await stopGatewayGracefully(proc, {
    ...base,
    timeoutMs: 2000,
    listDescendantsFn: async () => [101],
    getCommandFn: w.getCommandFn,
    signalPidFn: w.signalPidFn,
  });
  assert.deepStrictEqual(proc.signals, ["SIGTERM"], "the shim itself still gets one SIGTERM");
  assert.deepStrictEqual(w.sent, [[101, "SIGTERM"]], "graceful first, and no SIGKILL once it exits");
  assert.ok(!w.alive.has(101));
});

test("shim: a forked gateway that ignores SIGTERM is SIGKILLed at the deadline", async () => {
  const w = world({ 101: { cmd: GATEWAY_CMD, onSignal: () => "live" } });
  const started = Date.now();
  await stopGatewayGracefully(shimProc(), {
    ...base,
    timeoutMs: 150,
    survivorGraceMs: 150,
    listDescendantsFn: async () => [101],
    getCommandFn: w.getCommandFn,
    signalPidFn: w.signalPidFn,
  });
  assert.deepStrictEqual(w.sent, [[101, "SIGTERM"], [101, "SIGKILL"]]);
  assert.ok(Date.now() - started >= 140, "SIGKILL waits for the deadline, not sooner");
});

test("shim: a leftover that is not a gateway is left for the backend's orphan sweep", async () => {
  const w = world({ 102: { cmd: "/usr/local/bin/kiro-cli acp", onSignal: () => "die" } });
  await stopGatewayGracefully(shimProc(), {
    ...base,
    timeoutMs: 100,
    listDescendantsFn: async () => [102],
    getCommandFn: w.getCommandFn,
    signalPidFn: w.signalPidFn,
  });
  assert.deepStrictEqual(w.sent, []);
});

test("shim: a survivor whose pid stops reading as a gateway is never SIGKILLed", async () => {
  // The gateway ignores SIGTERM, then (the pid reused) reads as another program.
  let cmd = GATEWAY_CMD;
  const w = world({ 101: { get cmd() { return cmd; }, onSignal: () => { cmd = "/bin/bash"; return "live"; } } });
  await stopGatewayGracefully(shimProc(), {
    ...base,
    timeoutMs: 100,
    survivorGraceMs: 100,
    listDescendantsFn: async () => [101],
    getCommandFn: w.getCommandFn,
    signalPidFn: w.signalPidFn,
  });
  assert.deepStrictEqual(w.sent, [[101, "SIGTERM"]], "identity is re-read before SIGKILL");
});

test("a gateway child that outlives the deadline is SIGKILLed alone, its tree untouched", async () => {
  const w = world({ 201: { cmd: "kiro-cli acp", onSignal: () => "live" } });
  const listed = [];
  const proc = {
    pid: 200,
    exitCode: null,
    _onExit: [],
    signals: [],
    kill(sig) {
      this.signals.push(sig);
      if (sig !== "SIGKILL") return; // ignores SIGTERM: wedged
      this.exitCode = 1;
      for (const fn of this._onExit) fn();
    },
    once(event, fn) { if (event === "exit") this._onExit.push(fn); },
  };
  await stopGatewayGracefully(proc, {
    ...base,
    timeoutMs: 60,
    survivorGraceMs: 60,
    listDescendantsFn: async (pid) => { listed.push(pid); return [201]; },
    getCommandFn: w.getCommandFn,
    signalPidFn: w.signalPidFn,
  });
  assert.deepStrictEqual(proc.signals, ["SIGTERM", "SIGKILL"]);
  assert.deepStrictEqual(listed, [200], "listed once, before SIGTERM");
  assert.deepStrictEqual(w.sent, [], "no listed pid is signalled without an identity check");
});

test("a slow tree listing does not eat the child's grace window before SIGKILL", async () => {
  let termAt = 0;
  let killAt = 0;
  const proc = {
    pid: 300,
    exitCode: null,
    _onExit: [],
    kill(sig) {
      if (sig === "SIGTERM") { termAt = Date.now(); return; } // flushing, slow to exit
      killAt = Date.now();
      this.exitCode = 1;
      for (const fn of this._onExit) fn();
    },
    once(event, fn) { if (event === "exit") this._onExit.push(fn); },
  };
  await stopGatewayGracefully(proc, {
    ...base,
    timeoutMs: 100,
    termGraceMs: 150,
    // The listing takes longer than the whole entry-anchored budget.
    listDescendantsFn: () => new Promise((r) => setTimeout(() => r([]), 120)),
    getCommandFn: async () => "",
    signalPidFn: () => {},
  });
  assert.ok(termAt > 0 && killAt > 0, "SIGTERM then SIGKILL");
  assert.ok(killAt - termAt >= 140, `SIGKILL ${killAt - termAt}ms after SIGTERM, wanted the full window`);
});

test("a listing that fails still signals the child", async () => {
  const proc = shimProc();
  await stopGatewayGracefully(proc, {
    ...base,
    timeoutMs: 100,
    listDescendantsFn: async () => { throw new Error("ps missing"); },
    getCommandFn: async () => "",
    signalPidFn: () => {},
  });
  assert.deepStrictEqual(proc.signals, ["SIGTERM"]);
});

test("shim: each pid's SIGTERM follows its own identity check, with no other check between", async () => {
  // Two listed gateways. The trace must read check(101), TERM(101), check(102), TERM(102):
  // a pid checked first and signalled after other ps reads could be reused in that gap.
  const trace = [];
  const w = world({
    101: { cmd: GATEWAY_CMD, onSignal: () => "die" },
    102: { cmd: GATEWAY_CMD, onSignal: () => "die" },
  });
  await stopGatewayGracefully(shimProc(), {
    ...base,
    timeoutMs: 500,
    listDescendantsFn: async () => [101, 102],
    getCommandFn: async (pid) => { trace.push(`check:${pid}`); return w.getCommandFn(pid); },
    signalPidFn: (pid, sig) => { if (sig !== 0) trace.push(`${sig}:${pid}`); return w.signalPidFn(pid, sig); },
  });
  assert.deepStrictEqual(trace, ["check:101", "SIGTERM:101", "check:102", "SIGTERM:102"]);
});

// The shim shape main.js actually spawns on POSIX: `<root>/bin/kirocrew gateway
// --no-open --port N`, where bin/kirocrew is a console script (so ps shows the
// interpreter first) or the executable itself. The gateway's own built-in MCP
// servers are the SAME executable with a non-server subcommand.
const CONSOLE_GATEWAY_CMD = "/app/backend-dist/kirocrew-backend/bin/kirocrew gateway --no-open --port 5476";
const SCRIPT_GATEWAY_CMD = "/app/backend-dist/kirocrew-backend/bin/python3 /app/backend-dist/kirocrew-backend/bin/kirocrew gateway --no-open --port 5476";
const MCP_CORE_CMD = "/app/backend-dist/kirocrew-backend/bin/kirocrew mcp-core";
const MCP_CRON_SCRIPT_CMD = "/app/backend-dist/kirocrew-backend/bin/python3 /app/backend-dist/kirocrew-backend/bin/kirocrew mcp-cron";

// A shim that IGNORES SIGTERM and dies only to the SIGKILL the child's own
// deadline sends. By the time its 'exit' fires, that deadline is already spent.
function stubbornShimProc(pid = 100) {
  return {
    pid,
    exitCode: null,
    _onExit: [],
    signals: [],
    kill(sig) {
      this.signals.push(sig);
      if (sig !== "SIGKILL") return;
      this.exitCode = 1;
      for (const fn of this._onExit) fn();
    },
    once(event, fn) { if (event === "exit") this._onExit.push(fn); },
  };
}

test("shim ignoring SIGTERM: its forked gateway still gets a grace window before SIGKILL", async () => {
  // The child's deadline (timeoutMs) is spent killing the shim. Without a floor
  // of its own, the sweep would SIGTERM the gateway and SIGKILL it in the same
  // tick -- no cooperative shutdown at all.
  const stamps = {};
  const w = world({ 101: { cmd: GATEWAY_CMD, onSignal: () => "live" } });
  const proc = stubbornShimProc();
  await stopGatewayGracefully(proc, {
    ...base,
    timeoutMs: 100,
    survivorGraceMs: 200,
    listDescendantsFn: async () => [101],
    getCommandFn: w.getCommandFn,
    signalPidFn: (pid, sig) => {
      if (sig !== 0) stamps[sig] = Date.now();
      return w.signalPidFn(pid, sig);
    },
  });
  assert.deepStrictEqual(proc.signals, ["SIGTERM", "SIGKILL"], "the shim outlived its deadline");
  assert.deepStrictEqual(w.sent, [[101, "SIGTERM"], [101, "SIGKILL"]]);
  assert.ok(
    stamps.SIGKILL - stamps.SIGTERM >= 180,
    `the gateway's SIGKILL waits for its own grace window (got ${stamps.SIGKILL - stamps.SIGTERM}ms)`,
  );
});

test("shim ignoring SIGTERM: a slow identity read before SIGTERM does not eat the grace window", async () => {
  // The identity read (a /bin/ps spawn) that precedes each SIGTERM can take up
  // to SURVIVOR_COMMAND_READ_MS. If the grace deadline were fixed BEFORE that
  // read, the read's duration would be subtracted from the gateway's own
  // cooperative shutdown and SIGKILL could land almost on top of SIGTERM. The
  // window must be measured from the SIGTERM itself.
  const stamps = {};
  const w = world({ 101: { cmd: GATEWAY_CMD, onSignal: () => "live" } });
  let reads = 0;
  await stopGatewayGracefully(stubbornShimProc(), {
    ...base,
    timeoutMs: 50,
    survivorGraceMs: 200,
    listDescendantsFn: async () => [101],
    // First read (before SIGTERM) is slow; the one before SIGKILL is instant,
    // so nothing but the deadline itself separates the two signals.
    getCommandFn: async (pid) => {
      reads += 1;
      if (reads === 1) await new Promise((r) => { setTimeout(r, 150); });
      return w.getCommandFn(pid);
    },
    signalPidFn: (pid, sig) => {
      if (sig !== 0) stamps[sig] = Date.now();
      return w.signalPidFn(pid, sig);
    },
  });
  assert.deepStrictEqual(w.sent, [[101, "SIGTERM"], [101, "SIGKILL"]]);
  assert.ok(
    stamps.SIGKILL - stamps.SIGTERM >= 180,
    `the grace window starts at SIGTERM, after the identity read (got ${stamps.SIGKILL - stamps.SIGTERM}ms)`,
  );
});

test("shim: a gateway that exits on SIGTERM is not held for the grace window", async () => {
  const w = world({ 101: { cmd: GATEWAY_CMD, onSignal: () => "die" } });
  const started = Date.now();
  await stopGatewayGracefully(stubbornShimProc(), {
    ...base,
    timeoutMs: 50,
    survivorGraceMs: 5000,
    termGraceMs: 50,
    listDescendantsFn: async () => [101],
    getCommandFn: w.getCommandFn,
    signalPidFn: w.signalPidFn,
  });
  assert.deepStrictEqual(w.sent, [[101, "SIGTERM"]]);
  assert.ok(Date.now() - started < 2000, "the floor is a ceiling on patience, not a fixed wait");
});

test("shim: a hung identity read settles the stop and signals nothing", async () => {
  // `ps` stuck in an uninterruptible read: execFile's timeout never fires its
  // callback. The sweep must still resolve, and a pid whose identity could not
  // be read is not a gateway -- no signal.
  const w = world({ 101: { cmd: GATEWAY_CMD, onSignal: () => "live" } });
  const started = Date.now();
  await stopGatewayGracefully(shimProc(), {
    ...base,
    timeoutMs: 100,
    survivorGraceMs: 100,
    listDescendantsFn: async () => [101],
    getCommandFn: () => new Promise(() => {}), // never settles
    signalPidFn: w.signalPidFn,
  });
  assert.deepStrictEqual(w.sent, [], "fail closed: an unreadable pid is never signalled");
  assert.ok(Date.now() - started < 4000, "the hung read is bounded on the JS side");
});

test("shim: a `kirocrew mcp-core` descendant is left alone while the `kirocrew gateway` one is stopped", async () => {
  const w = world({
    101: { cmd: CONSOLE_GATEWAY_CMD, onSignal: () => "die" },
    102: { cmd: MCP_CORE_CMD, onSignal: () => "die" },
    103: { cmd: SCRIPT_GATEWAY_CMD, onSignal: () => "die" },
    104: { cmd: MCP_CRON_SCRIPT_CMD, onSignal: () => "die" },
  });
  await stopGatewayGracefully(shimProc(), {
    ...base,
    timeoutMs: 500,
    listDescendantsFn: async () => [104, 103, 102, 101],
    getCommandFn: w.getCommandFn,
    signalPidFn: w.signalPidFn,
  });
  assert.deepStrictEqual(w.sent, [[103, "SIGTERM"], [101, "SIGTERM"]],
    "only the server subcommand shapes are the forked gateway; the MCP servers stay for the backend's sweep");
  assert.ok(w.alive.has(102) && w.alive.has(104));
});

test("isKirocrewGatewayCommand: server subcommand required in argparse's first positional slot", () => {
  assert.strictEqual(isKirocrewGatewayCommand(CONSOLE_GATEWAY_CMD), true);
  assert.strictEqual(isKirocrewGatewayCommand(SCRIPT_GATEWAY_CMD), true);
  assert.strictEqual(isKirocrewGatewayCommand("/opt/py/bin/python3 -m kiro_crew gateway"), true);
  assert.strictEqual(isKirocrewGatewayCommand("kirocrew-backend start"), true);
  assert.strictEqual(isKirocrewGatewayCommand(MCP_CORE_CMD), false);
  assert.strictEqual(isKirocrewGatewayCommand(MCP_CRON_SCRIPT_CMD), false);
  assert.strictEqual(isKirocrewGatewayCommand("/opt/bin/kirocrew"), false, "no subcommand at all");
  assert.strictEqual(isKirocrewGatewayCommand("/opt/bin/kirocrew run gateway"), false, "later slot never qualifies");
  assert.strictEqual(isKirocrewGatewayCommand("/usr/local/bin/kiro-cli acp"), false);
});
