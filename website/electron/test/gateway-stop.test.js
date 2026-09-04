const { test } = require("node:test");
const assert = require("node:assert");
const http = require("http");
const os = require("os");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const {
  postShutdown,
  stopGatewayGracefully,
  forceStopPort,
  classifyPortOwner,
  isKirocrewCommand,
} = require("../gateway-stop");

// Helper: temp KIROCREW_HOME containing a .local_secret file.
function tmpHomeWithSecret(secret) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "gw-stop-"));
  if (secret !== null) fs.writeFileSync(path.join(dir, ".local_secret"), secret);
  return dir;
}

// Helper: a long-lived child. ignoreSigterm=true => only SIGKILL can stop it.
// Prints "ready" once its signal handler is registered so tests don't send
// signals during the child's startup window (before the handler exists).
function spawnDummy({ ignoreSigterm = false } = {}) {
  const code = ignoreSigterm
    ? "process.on('SIGTERM',()=>{}); console.log('ready'); setInterval(()=>{}, 1e9);"
    : "console.log('ready'); setInterval(()=>{}, 1e9);"; // default: SIGTERM terminates
  return spawn(process.execPath, ["-e", code]);
}

// Resolve once the child has printed "ready" (handler registered, loop running).
function waitReady(proc) {
  return new Promise((resolve) => {
    let buf = "";
    const onData = (d) => {
      buf += d.toString();
      if (buf.includes("ready")) { proc.stdout.off("data", onData); resolve(); }
    };
    proc.stdout.on("data", onData);
  });
}

// Helper: local server implementing the /api/shutdown contract.
// onShutdown(req) lets a test simulate the gateway exiting itself on 200.
function startServer({ secret, status = 200, onShutdown }) {
  const server = http.createServer((req, res) => {
    if (req.method === "POST" && req.url === "/api/shutdown") {
      const ok = req.headers["x-local-secret"] === secret;
      if (!ok) { res.writeHead(403); return res.end('{"error":"invalid secret"}'); }
      res.writeHead(status); res.end(status === 200 ? '{"ok":true}' : "{}");
      if (status === 200 && onShutdown) onShutdown(req);
      return;
    }
    res.writeHead(404); res.end();
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      resolve({ server, port: server.address().port });
    });
  });
}

test("postShutdown returns true on 200 with correct secret", async () => {
  const home = tmpHomeWithSecret("s3cr3t");
  const { server, port } = await startServer({ secret: "s3cr3t", status: 200 });
  try {
    const ok = await postShutdown({ backendUrl: `http://127.0.0.1:${port}`, kirocrewHome: home });
    assert.strictEqual(ok, true);
  } finally { server.close(); fs.rmSync(home, { recursive: true, force: true }); }
});

test("postShutdown returns false on 403 (wrong secret)", async () => {
  const home = tmpHomeWithSecret("wrong");
  const { server, port } = await startServer({ secret: "right", status: 200 });
  try {
    const ok = await postShutdown({ backendUrl: `http://127.0.0.1:${port}`, kirocrewHome: home });
    assert.strictEqual(ok, false);
  } finally { server.close(); fs.rmSync(home, { recursive: true, force: true }); }
});

test("postShutdown returns false when no secret file exists", async () => {
  const home = tmpHomeWithSecret(null);
  const ok = await postShutdown({ backendUrl: "http://127.0.0.1:1", kirocrewHome: home });
  assert.strictEqual(ok, false);
  fs.rmSync(home, { recursive: true, force: true });
});

test("postShutdown tries each candidate secret — a stale first secret does not block the live one", async () => {
  // The gateway is authenticated by whichever secret it actually loaded, so a
  // stale first candidate must not stop postShutdown from POSTing the live one.
  const { server, port } = await startServer({ secret: "live-secret", status: 200 });
  try {
    const ok = await postShutdown({
      backendUrl: `http://127.0.0.1:${port}`,
      secrets: ["stale-secret", "live-secret"],
    });
    assert.strictEqual(ok, true);
  } finally { server.close(); }
});

test("postShutdown returns false only after every candidate secret is rejected", async () => {
  const { server, port } = await startServer({ secret: "the-real-one", status: 200 });
  try {
    const ok = await postShutdown({
      backendUrl: `http://127.0.0.1:${port}`,
      secrets: ["nope-1", "nope-2"],
    });
    assert.strictEqual(ok, false);
  } finally { server.close(); }
});

test("stopGatewayGracefully: happy path — endpoint exits process, no signal needed", async () => {
  const home = tmpHomeWithSecret("s3cr3t");
  const proc = spawnDummy({ ignoreSigterm: true }); // proves SIGTERM was NOT used
  await waitReady(proc);
  // Server kills the child on 200, simulating the gateway exiting itself.
  const { server, port } = await startServer({
    secret: "s3cr3t", status: 200, onShutdown: () => proc.kill("SIGKILL"),
  });
  try {
    await stopGatewayGracefully(proc, {
      backendUrl: `http://127.0.0.1:${port}`, kirocrewHome: home, timeoutMs: 10000,
    });
    assert.notStrictEqual(proc.exitCode === null && proc.signalCode === null, true, "process should be gone");
  } finally { server.close(); fs.rmSync(home, { recursive: true, force: true }); }
});

test("stopGatewayGracefully: SIGTERM fallback when endpoint fails", async () => {
  const home = tmpHomeWithSecret("s3cr3t");
  const proc = spawnDummy({ ignoreSigterm: false }); // exits on SIGTERM
  await waitReady(proc);
  const { server, port } = await startServer({ secret: "s3cr3t", status: 500 }); // endpoint fails
  try {
    await stopGatewayGracefully(proc, {
      backendUrl: `http://127.0.0.1:${port}`, kirocrewHome: home, timeoutMs: 10000,
    });
    assert.strictEqual(proc.signalCode, "SIGTERM");
  } finally { server.close(); fs.rmSync(home, { recursive: true, force: true }); }
});

// The SIGKILL escalation exists for a child that IGNORES SIGTERM, which is a
// POSIX-only state: on Windows there are no real signals, and
// ChildProcess.kill("SIGTERM") maps onto TerminateProcess -- an unconditional
// kill the target cannot install a handler for (verified: the child's SIGTERM
// handler never runs and it dies with signalCode "SIGTERM"). So the premise
// "SIGTERM was ignored" is unreachable there, and the escalation cannot be
// exercised rather than merely being unnecessary.
//
// Split per platform instead of relaxed to `signalCode != null`, which would
// pass on POSIX even if the SIGKILL fallback regressed into a plain SIGTERM --
// the exact bug this test exists to catch. Each platform asserts the strongest
// true statement about its own semantics.
test("stopGatewayGracefully: SIGKILL fallback when SIGTERM ignored", { skip: process.platform === "win32" ? "no real signals on Windows: kill(SIGTERM) is TerminateProcess, which cannot be ignored" : false }, async () => {
  const home = tmpHomeWithSecret("s3cr3t");
  const proc = spawnDummy({ ignoreSigterm: true }); // ignores SIGTERM
  await waitReady(proc);
  const { server, port } = await startServer({ secret: "s3cr3t", status: 500 });
  try {
    await stopGatewayGracefully(proc, {
      backendUrl: `http://127.0.0.1:${port}`, kirocrewHome: home, timeoutMs: 800,
    });
    assert.strictEqual(proc.signalCode, "SIGKILL");
  } finally { server.close(); fs.rmSync(home, { recursive: true, force: true }); }
});

test("stopGatewayGracefully: a SIGTERM-ignoring child still dies on Windows", { skip: process.platform === "win32" ? false : "covered by the SIGKILL-escalation test on POSIX" }, async () => {
  // The guarantee callers actually depend on -- stopGatewayGracefully resolves
  // only once the process is GONE, so the auto-update bundle swap never races a
  // live gateway child. Windows reaches that end state through the first kill
  // rather than through the escalation, so assert the end state, not the route.
  const home = tmpHomeWithSecret("s3cr3t");
  const proc = spawnDummy({ ignoreSigterm: true });
  await waitReady(proc);
  const { server, port } = await startServer({ secret: "s3cr3t", status: 500 });
  try {
    await stopGatewayGracefully(proc, {
      backendUrl: `http://127.0.0.1:${port}`, kirocrewHome: home, timeoutMs: 800,
    });
    assert.notStrictEqual(
      proc.exitCode === null && proc.signalCode === null,
      true,
      "process should be gone once stopGatewayGracefully resolves",
    );
  } finally { server.close(); fs.rmSync(home, { recursive: true, force: true }); }
});

test("stopGatewayGracefully: the Windows fallback reaps the TREE, not just the gateway pid", async () => {
  // THE ORPHAN BUG. The happy path is fine on every platform: POST
  // /api/shutdown sets shutdown_event, and the gateway's own shutdown reaps its
  // kiro-cli / MCP / app-server children before exiting. This test is about the
  // FALLBACK -- an unreadable .local_secret, a 403, a timeout, or a wedged loop
  // -- where the endpoint never takes and the shell resorts to killing.
  //
  // On POSIX, kill(SIGTERM) still lets the gateway run its handler and clean up.
  // On Windows there is no such thing: Node maps both SIGTERM and SIGKILL onto
  // TerminateProcess, so no handler runs, nothing is flushed, and every
  // descendant is orphaned and reparented -- still holding the data home's locks
  // and the same .local_secret. Worse, the port frees, so the caller's
  // verification reports success while the orphans race the replacement gateway.
  //
  // So on Windows the kill must be TREE-scoped. Injected rather than spawning a
  // real tree: the point is WHICH call the fallback makes, and a test cannot
  // portably assert on grandchildren it did not create.
  const treeKills = [];
  // Collect EVERY 'exit' listener: stopGatewayGracefully registers more than one
  // (the resolver plus the timer-clearing cleanup), so a mock that keeps only the
  // last one never resolves and the test hangs instead of failing.
  const proc = {
    pid: 4242,
    exitCode: null,
    signalCode: null,
    _onExit: [],
    kill() { /* single-pid kill: must NOT be the Windows fallback */ },
    once(event, fn) { if (event === "exit") this._onExit.push(fn); },
    _finish() { this.exitCode = 1; for (const fn of this._onExit) fn(); },
  };
  await stopGatewayGracefully(proc, {
    backendUrl: "http://127.0.0.1:1",
    kirocrewHome: "/nope",
    timeoutMs: 50,
    // The endpoint fails -- this is the fallback path, by construction.
    postShutdownFn: async () => false,
    platform: "win32",
    killTreeFn: async (pid) => { treeKills.push(pid); proc._finish(); },
  });
  assert.deepStrictEqual(
    treeKills,
    [4242],
    "on Windows a failed /api/shutdown must escalate to a TREE kill; a single-pid " +
      "kill frees the port but orphans every kiro-cli and MCP child"
  );
});

test("stopGatewayGracefully: a REJECTED tree kill still falls back to killing the pid", async () => {
  // A tree kill can legitimately refuse: windowsTaskkill fails closed when the
  // identity probe cannot run or reports a recycled pid. Swallowing that leaves
  // NO kill attempted at all -- strictly worse than the single-pid kill it
  // replaced -- and the hard timer then resolves anyway, so the auto-update
  // caller proceeds to swap the app's files while the gateway is still running.
  // Losing the tree is bad; losing the parent too is data loss.
  const signals = [];
  const proc = {
    pid: 4242,
    exitCode: null,
    signalCode: null,
    _onExit: [],
    kill(sig) { signals.push(sig); this.exitCode = 1; for (const fn of this._onExit) fn(); },
    once(event, fn) { if (event === "exit") this._onExit.push(fn); },
  };
  await stopGatewayGracefully(proc, {
    backendUrl: "http://127.0.0.1:1",
    kirocrewHome: "/nope",
    timeoutMs: 50,
    postShutdownFn: async () => false,
    platform: "win32",
    killTreeFn: async () => { throw new Error("process identity changed"); },
  });
  assert.deepStrictEqual(
    signals,
    ["SIGTERM"],
    "a refused tree kill must degrade to the single-pid kill, not to no kill at all"
  );
});

test("stopGatewayGracefully: does not resolve while the tree kill is still reaping", async () => {
  // The parent dies FIRST -- taskkill /T terminates it and then walks the rest of
  // the tree -- so the process 'exit' event fires while descendants are still being
  // reaped. Resolving on that event alone hands control back to the auto-update
  // caller mid-sweep, which then swaps the app's files with kiro-cli / MCP children
  // still live and holding the data home's locks: exactly the state the tree kill
  // was added to prevent, reintroduced by resolving too early.
  let releaseTree;
  const treeDone = new Promise((r) => { releaseTree = r; });
  let resolvedBeforeTreeSettled = false;
  let treeSettled = false;
  const proc = {
    pid: 4242,
    exitCode: null,
    signalCode: null,
    _onExit: [],
    kill() {},
    once(event, fn) { if (event === "exit") this._onExit.push(fn); },
  };
  const stopping = stopGatewayGracefully(proc, {
    backendUrl: "http://127.0.0.1:1",
    kirocrewHome: "/nope",
    timeoutMs: 5000,
    postShutdownFn: async () => false,
    platform: "win32",
    killTreeFn: async () => {
      // The parent goes down immediately; the rest of the tree lags.
      proc.exitCode = 1;
      for (const fn of proc._onExit) fn();
      await treeDone;
      treeSettled = true;
    },
  }).then(() => { resolvedBeforeTreeSettled = !treeSettled; });

  // Give the parent-exit path every chance to resolve early.
  await new Promise((r) => setTimeout(r, 150));
  releaseTree();
  await stopping;
  assert.strictEqual(
    resolvedBeforeTreeSettled,
    false,
    "resolved on the parent's exit while the tree kill was still reaping descendants"
  );
});

test("stopGatewayGracefully: a HUNG tree kill still resolves, and never pre-empts itself", async () => {
  // A tree kill is AWAITED, not raced against a timer. Pre-empting it would kill
  // the parent alone and orphan exactly the descendants it exists to reap -- the
  // bug, dressed up as a mitigation. So the contract is: the caller sizes the tree
  // kill's own timeouts to fit this deadline (pinned by the
  // gateway-supervisor.js source test below),
  // and this function must not resolve a moment later than it otherwise would.
  //
  // Asserted with a kill that NEVER settles, the pathological case: the hard timer
  // must still resolve, and no single-pid kill may have fired behind the tree
  // kill's back.
  const signals = [];
  const proc = {
    pid: 4242,
    exitCode: null,
    signalCode: null,
    _onExit: [],
    kill(sig) { signals.push(sig); this.exitCode = 1; for (const fn of this._onExit) fn(); },
    once(event, fn) { if (event === "exit") this._onExit.push(fn); },
  };
  const started = Date.now();
  await stopGatewayGracefully(proc, {
    backendUrl: "http://127.0.0.1:1",
    kirocrewHome: "/nope",
    timeoutMs: 60,
    postShutdownFn: async () => false,
    platform: "win32",
    killTreeFn: () => new Promise(() => {}),
  });
  assert.deepStrictEqual(
    signals,
    [],
    "the tree kill must not be pre-empted by a single-pid kill; that would orphan " +
      "the descendants it exists to reap"
  );
  // Bounded by the hard safety net (timeoutMs + 3000), not by the hung kill.
  assert.ok(
    Date.now() - started < 60 + 3000 + 2000,
    "a hung tree kill must not delay the caller past the hard deadline"
  );
});

test("wedge recovery kills the gateway TREE, not just the wedged pid", () => {
  // The wedge path deliberately SKIPS /api/shutdown -- that endpoint runs on the
  // very loop that is frozen -- so it is the one trigger where Windows always
  // reached a bare single-pid kill. And the port sweep that follows cannot cover
  // for it: the bare kill frees the LISTEN port, so forceStopGatewayPort then
  // finds no owner and the /T taskkill inside it is never reached. The detached
  // kiro-cli / MCP children survive holding the data home's locks and race the
  // gateway that is about to be respawned.
  //
  // Source-asserted at the supervisor ownership boundary. The requirement is
  // that the wedge kill go through a tree-scoped path on win32, not that it use
  // any particular helper name.
  const supervisor = fs.readFileSync(
    path.join(__dirname, "..", "gateway-supervisor.js"),
    "utf8",
  );
  const marker = "liveness: backend unresponsive — force-killing wedged gateway";
  const at = supervisor.indexOf(marker);
  assert.notStrictEqual(
    at,
    -1,
    "expected the wedge-recovery force-kill path in gateway-supervisor.js",
  );
  // The kill happens within the next few dozen lines of that log line.
  const region = supervisor.slice(at, at + 2000);
  assert.match(
    region,
    /killGatewayProcessTree|windowsTaskkill|IS_WIN/,
    "wedge recovery must tree-kill on Windows; a bare pid kill frees the port, so " +
      "the port sweep that follows finds no owner and never reaps the descendants"
  );
  // ...and the helper it goes through must NOT borrow the shutdown path's
  // shortened timeouts. Those exist only because stopGatewayGracefully has a hard
  // deadline to fit inside. Wedge recovery has none, so inheriting them would time
  // the identity probe out early on a slow box, fall back to the parent-only kill,
  // and respawn beside the very orphans this path exists to reap.
  const helper = supervisor.indexOf("async function killGatewayProcessTree");
  assert.notStrictEqual(
    helper,
    -1,
    "expected the shared tree-kill helper in gateway-supervisor.js",
  );
  assert.doesNotMatch(
    supervisor.slice(helper, helper + 1200),
    /killGatewayTreeOnWindowsBounded/,
    "the wedge-recovery tree kill must not reuse the shutdown-BOUNDED helper: it " +
      "has no deadline to fit, so the shortened probe timeouts only make an early " +
      "fallback to the parent-only kill more likely"
  );
});

test("the gateway supervisor bounds the shutdown tree kill to the hard deadline", () => {
  // windowsTaskkill's DEFAULTS are sized for the interactive port sweep: identity
  // revalidation via PowerShell (8s) then WMIC (5s), then taskkill itself (10s) --
  // 23s worst case, which is longer than stopGatewayGracefully's whole deadline of
  // timeoutMs + 3000. On the shutdown path that ordering is what matters: the hard
  // timer resolves on schedule regardless, so an unbounded kill would still be in
  // flight when the auto-update caller starts swapping the app's files.
  //
  // Rather than pre-empting the kill (which would defeat its purpose by killing
  // the parent alone and orphaning the tree it exists to reap), the call site
  // passes TIGHTER timeouts so the whole tree kill provably completes first.
  const supervisor = fs.readFileSync(
    path.join(__dirname, "..", "gateway-supervisor.js"),
    "utf8",
  );
  const start = supervisor.indexOf("function killGatewayTreeOnWindowsBounded");
  assert.notStrictEqual(
    start,
    -1,
    "gateway-supervisor.js must define the bounded Windows tree kill",
  );
  // Brace-match to the end of the function rather than regex-scanning to the first
  // "})," -- the call nests one options object inside another, so a lazy pattern
  // stops early and silently misses the outer timeout.
  let depth = 0;
  let end = start;
  for (; end < supervisor.length; end += 1) {
    if (supervisor[end] === "{") depth += 1;
    else if (supervisor[end] === "}") { depth -= 1; if (depth === 0) break; }
  }
  const call = supervisor.slice(start, end + 1);
  const budget = ["powershellTimeoutMs", "wmicTimeoutMs", "timeoutMs"]
    .map((k) => Number((new RegExp(`${k}:\\s*(\\d+)`).exec(call) || [])[1]));
  assert.ok(
    budget.every((n) => Number.isFinite(n) && n > 0),
    "the shutdown tree kill must pin all three of windowsTaskkill's timeouts " +
      "(powershell, wmic, taskkill); an unpinned one inherits a default too long " +
      "for the shutdown deadline"
  );
  const worstCase = budget.reduce((a, b) => a + b, 0);
  assert.ok(
    worstCase < 15000 + 3000,
    `the tree kill's worst case (${worstCase}ms) must fit inside the hard deadline, `
      + "or the caller is told the gateway is gone while the kill is still running"
  );
});

test("stopGatewayGracefully: POSIX keeps signalling the child, not a tree kill", async () => {
  // The Windows branch must not change POSIX behaviour: there, SIGTERM genuinely
  // reaches the gateway's handler, which is strictly better than a tree kill
  // because it still flushes sessions/memory/cron before exiting.
  const signals = [];
  const treeKills = [];
  const proc = {
    pid: 4242,
    exitCode: null,
    signalCode: null,
    _onExit: [],
    kill(sig) { signals.push(sig); this.exitCode = 1; for (const fn of this._onExit) fn(); },
    once(event, fn) { if (event === "exit") this._onExit.push(fn); },
  };
  await stopGatewayGracefully(proc, {
    backendUrl: "http://127.0.0.1:1",
    kirocrewHome: "/nope",
    timeoutMs: 50,
    postShutdownFn: async () => false,
    platform: "linux",
    killTreeFn: async (pid) => { treeKills.push(pid); },
  });
  assert.deepStrictEqual(signals, ["SIGTERM"]);
  assert.deepStrictEqual(treeKills, [], "POSIX must keep the graceful SIGTERM path");
});

test("stopGatewayGracefully: no-op on already-dead process", async () => {
  const proc = spawnDummy({ ignoreSigterm: false });
  await new Promise((r) => { proc.once("exit", r); proc.kill("SIGKILL"); });
  // Should resolve immediately without throwing.
  await stopGatewayGracefully(proc, { backendUrl: "http://127.0.0.1:1", kirocrewHome: "/nope", timeoutMs: 500 });
  assert.ok(true);
});

// ── forceStopPort ──
// Injectable deps so we exercise the verify-the-kill-worked logic without a
// real OS process. `listenSeq` is a queue of lsof results returned on each
// successive call, letting a test model "killed then gone" vs "never gone".
function fakeDeps({ listenSeq, command = "python -m kiro_crew gateway", onKill = () => {} }) {
  let i = 0;
  const killed = [];
  return {
    killed,
    getListenPids: async () => listenSeq[Math.min(i++, listenSeq.length - 1)],
    getCommand: async () => command,
    kill: (pid, sig) => { killed.push([pid, sig]); onKill(pid, sig); },
    sleep: async () => {}, // instant — no real waiting in tests
    verifyTimeoutMs: 1000,
    pollIntervalMs: 250,
  };
}

test("forceStopPort: no owner on the port reports freed, kills nothing", async () => {
  const deps = fakeDeps({ listenSeq: [[]] });
  const r = await forceStopPort(7788, deps);
  assert.deepStrictEqual(r, {
    killed: 0, freed: true, survivors: [], foreignHolder: false, serviceHolder: false,
  });
  assert.strictEqual(deps.killed.length, 0);
});

test("forceStopPort: killable owner frees the port (freed=true)", async () => {
  // First lsof: pid 4242 holds it. After the kill, the verify poll sees it gone.
  const deps = fakeDeps({ listenSeq: [[4242], []] });
  const r = await forceStopPort(7788, deps);
  assert.strictEqual(r.killed, 1);
  assert.strictEqual(r.freed, true);
  assert.deepStrictEqual(r.survivors, []);
  assert.deepStrictEqual(deps.killed, [[4242, "SIGKILL"]]);
});

test("forceStopPort: UNKILLABLE owner still holds port -> freed=false, survivors listed", async () => {
  // The regression case: SIGKILL is accepted but the process is in
  // uninterruptible sleep, so every subsequent lsof still shows it. We must
  // report freed=false so the caller does NOT respawn into a doomed bind.
  const deps = fakeDeps({ listenSeq: [[4242]] }); // always [4242]
  const r = await forceStopPort(7788, deps);
  assert.strictEqual(r.killed, 1);
  assert.strictEqual(r.freed, false);
  assert.deepStrictEqual(r.survivors, [4242]);
});

test("forceStopPort: never signals a non-KiroCrew owner", async () => {
  const deps = fakeDeps({ listenSeq: [[999], [999]], command: "nginx: worker process" });
  const r = await forceStopPort(7788, deps);
  assert.strictEqual(r.killed, 0);
  assert.strictEqual(deps.killed.length, 0);
  // We won't kill a foreign process, but it STILL holds the port: freed must be
  // false (a respawn would fail to bind) and foreignHolder true so the caller
  // routes to a restart/port-conflict path instead of a doomed respawn.
  assert.strictEqual(r.freed, false);
  assert.strictEqual(r.foreignHolder, true);
  assert.deepStrictEqual(r.survivors, []);
});

test("forceStopPort: foreign owner that vanishes during verify reports freed", async () => {
  // A non-KiroCrew owner we skip, but the port frees on its own before we finish
  // (the other app exited). freed must reflect the real port state, not our kills.
  const deps = fakeDeps({ listenSeq: [[999], []], command: "nginx: worker process" });
  const r = await forceStopPort(7788, deps);
  assert.strictEqual(r.killed, 0);
  assert.strictEqual(r.freed, true);
  assert.strictEqual(r.foreignHolder, false);
});

test("forceStopPort: freed reflects real port state even after killing our target", async () => {
  // We kill our target, but a DIFFERENT (foreign) pid is now listening — the
  // port is not actually free, so don't claim freed just because our target died.
  let i = 0;
  const seq = [[4242], [777]]; // ours dies, foreign 777 appears
  const deps = {
    getListenPids: async () => seq[Math.min(i++, seq.length - 1)],
    getCommand: async () => "python -m kiro_crew gateway",
    kill: () => {},
    sleep: async () => {},
    verifyTimeoutMs: 1000,
    pollIntervalMs: 250,
  };
  const r = await forceStopPort(7788, deps);
  assert.strictEqual(r.killed, 1);
  assert.deepStrictEqual(r.survivors, []); // OUR pid is gone
  assert.strictEqual(r.freed, false); // but the port is still held by 777
});

test("forceStopPort: awaits an asynchronous Windows kill before verifying", async () => {
  let killFinished = false;
  let probes = 0;
  const r = await forceStopPort(7788, {
    getListenPids: async () => {
      probes += 1;
      if (probes === 1) return [4242];
      assert.strictEqual(killFinished, true, "verification raced taskkill completion");
      return [];
    },
    getCommand: async () => "C:\\bundle\\kirocrew.exe gateway",
    kill: async () => { killFinished = true; },
    sleep: async () => {},
    isKirocrew: (command) => command.includes("kirocrew.exe"),
  });
  assert.strictEqual(r.killed, 1);
  assert.strictEqual(r.freed, true);
});

test("forceStopPort: a failed initial Windows probe is not a free port", async () => {
  const probeError = new Error("netstat timed out");
  const r = await forceStopPort(7788, {
    getListenPids: async () => { throw probeError; },
    getCommand: async () => "",
    kill: async () => {},
    sleep: async () => {},
    failClosedOnProbeError: true,
  });
  assert.deepStrictEqual(r, {
    killed: 0, freed: false, survivors: [], foreignHolder: false,
    serviceHolder: false, probeFailed: true,
  });
});

test("forceStopPort: a failed Windows verification never claims recovery", async () => {
  let probes = 0;
  const r = await forceStopPort(7788, {
    getListenPids: async () => {
      if (probes++ === 0) return [4242];
      throw new Error("netstat verify timed out");
    },
    getCommand: async () => "C:\\bundle\\kirocrew.exe gateway",
    kill: async () => {},
    sleep: async () => {},
    isKirocrew: (command) => command.includes("kirocrew.exe"),
    failClosedOnProbeError: true,
  });
  assert.deepStrictEqual(r, {
    killed: 1, freed: false, survivors: [], foreignHolder: false,
    serviceHolder: false, probeFailed: true,
  });
});

// ── classifyPortOwner ───────────────────────────────────────────────────────
// Ground truth for "is the thing on our port local, or a tunnel?". Every
// outcome except a positively identified local KiroCrew process must be
// treated as "not ours" by callers.

test("classifyPortOwner: local KiroCrew gateway is ours", async () => {
  const owner = await classifyPortOwner(5476, {
    getListenPids: async () => [4242],
    getCommand: async () => "python -m kiro_crew gateway --port 5476",
  });
  assert.strictEqual(owner, "kirocrew");
});

// A gateway the OS service manager owns (launchd LaunchAgent / systemd unit) is
// reparented to init. Evicting it cannot free the port — KeepAlive/Restart=
// respawns it in milliseconds, so a "successful" force-stop only makes our own
// retry race the respawn. These four tests pin the reuse-not-evict direction.
test("classifyPortOwner: a service-managed gateway (ppid 1) is 'service'", async () => {
  const owner = await classifyPortOwner(5476, {
    getListenPids: async () => [4242],
    getCommand: async () => "python -m kiro_crew gateway --port 5476",
    getPpid: async () => "1",
  });
  assert.strictEqual(owner, "service");
});

test("classifyPortOwner: an app-spawned gateway (real ppid) stays 'kirocrew'", async () => {
  const owner = await classifyPortOwner(5476, {
    getListenPids: async () => [4242],
    getCommand: async () => "python -m kiro_crew gateway --port 5476",
    getPpid: async () => "  3310\n",
  });
  assert.strictEqual(owner, "kirocrew");
});

test("classifyPortOwner: an unreadable ppid fails closed to 'service'", async () => {
  // Mistaking a service for a wedge kills a gateway the OS instantly respawns;
  // mistaking a wedge for a service only costs an eviction we can explain.
  const owner = await classifyPortOwner(5476, {
    getListenPids: async () => [4242],
    getCommand: async () => "python -m kiro_crew gateway --port 5476",
    getPpid: async () => { throw new Error("ps failed"); },
  });
  assert.strictEqual(owner, "service");
});

test("classifyPortOwner: without a ppid probe the old classification stands", async () => {
  // Windows has no /bin/ps; omitting the probe must not silently reclassify
  // every local gateway as a service.
  const owner = await classifyPortOwner(5476, {
    getListenPids: async () => [4242],
    getCommand: async () => "python -m kiro_crew gateway --port 5476",
  });
  assert.strictEqual(owner, "kirocrew");
});

test("forceStopPort: never SIGKILLs a service-managed gateway", async () => {
  const killed = [];
  const res = await forceStopPort(5476, {
    getListenPids: async () => [4242],
    getCommand: async () => "python -m kiro_crew gateway --port 5476",
    getPpid: async () => "1",
    kill: (pid, sig) => killed.push([pid, sig]),
    sleep: async () => {},
  });
  assert.deepStrictEqual(killed, [], "a service-managed gateway must not be signalled");
  assert.strictEqual(res.killed, 0);
  assert.strictEqual(res.serviceHolder, true);
  assert.strictEqual(res.freed, false, "the port is still held, so a respawn would fail to bind");
});

test("forceStopPort: still evicts a gateway this app spawned", async () => {
  const killed = [];
  let probes = 0;
  const res = await forceStopPort(5476, {
    // Second probe reports the port free, i.e. the kill took.
    getListenPids: async () => (probes++ === 0 ? [4242] : []),
    getCommand: async () => "python -m kiro_crew gateway --port 5476",
    getPpid: async () => "3310",
    kill: (pid, sig) => killed.push([pid, sig]),
    sleep: async () => {},
  });
  assert.deepStrictEqual(killed, [[4242, "SIGKILL"]]);
  assert.strictEqual(res.serviceHolder, false);
  assert.strictEqual(res.freed, true);
});

test("classifyPortOwner: an ssh -L forward is foreign, not ours", async () => {
  // The exact shape of the reported bug: the tunnel's local socket belongs to
  // ssh, while the gateway answering /api/health lives on another machine.
  const owner = await classifyPortOwner(5476, {
    getListenPids: async () => [909],
    getCommand: async () => "ssh -NL 5476:localhost:5476 dev-dsk-example.amazon.com",
  });
  assert.strictEqual(owner, "foreign");
});

test("classifyPortOwner: no listener is 'none'", async () => {
  const owner = await classifyPortOwner(5476, {
    getListenPids: async () => [],
    getCommand: async () => "",
  });
  assert.strictEqual(owner, "none");
});

test("classifyPortOwner: an unrunnable probe is 'unknown', never 'none'", async () => {
  // A swallowed ENOENT previously looked like "nothing is listening", which is
  // the dangerous direction: it would authorise an eviction.
  const enoent = Object.assign(new Error("spawn lsof ENOENT"), { code: "ENOENT" });
  const owner = await classifyPortOwner(5476, {
    getListenPids: async () => { throw enoent; },
    getCommand: async () => "",
  });
  assert.strictEqual(owner, "unknown");
});

test("classifyPortOwner: ours wins when a mixed set holds the port", async () => {
  const owner = await classifyPortOwner(5476, {
    getListenPids: async () => [909, 4242],
    getCommand: async (pid) => (pid === 4242 ? "kirocrew gateway" : "ssh -NL 5476:localhost:5476 host"),
  });
  assert.strictEqual(owner, "kirocrew");
});

test("classifyPortOwner and forceStopPort share one KiroCrew matcher", async () => {
  // Drift between the two would let one mis-target a stranger's process.
  assert.ok(isKirocrewCommand("python -m kiro_crew gateway"));
  assert.ok(isKirocrewCommand("/Applications/KiroCrew.app/.../kirocrew"));
  assert.ok(!isKirocrewCommand("ssh -NL 5476:localhost:5476 host"));
  assert.ok(!isKirocrewCommand("ssh -NL 5476:localhost:5476 kirocrew"));
  // The matcher keys on the executable/module TOKEN, not a path substring:
  // an unrelated process merely living under a `kirocrew` home dir is foreign.
  assert.ok(!isKirocrewCommand("C:\\Users\\kirocrew\\OtherApp\\server.exe --port 5476"));
  assert.ok(!isKirocrewCommand("/home/kirocrew/some-other-server --port 5476"));
  assert.ok(!isKirocrewCommand("C:\\Users\\kirocrew\\python.exe -m http.server 5476"));
  assert.ok(!isKirocrewCommand("python app.py C:\\tmp\\kirocrew"));
  assert.ok(!isKirocrewCommand("python app.py -m kiro_crew"));
  // A POSIX `ps` line is unquoted, so an install path with a space arrives
  // split across tokens. Our own gateway must still be identified there, and a
  // later argument must still never pose as the executable.
  assert.ok(isKirocrewCommand("/Users/Jane Doe/Apps/KiroCrew.app/Contents/Resources/bin/kirocrew gateway --port 5476"));
  assert.ok(isKirocrewCommand("/Users/Jane Doe/venv/bin/python -m kiro_crew gateway"));
  assert.ok(!isKirocrewCommand("/tmp/evil --spoof /usr/local/bin/kirocrew"));
  assert.ok(!isKirocrewCommand("/tmp/evil /usr/local/bin/kirocrew"));
  assert.ok(!isKirocrewCommand("/tmp/dir with space/evil kirocrew --port 5476"));
  // Windows identity is path-bound: a matching basename at any other location
  // remains foreign and can never authorize taskkill.
  const trustedCli = "C:\\Program Files\\KiroCrew\\kirocrew.exe";
  const trustedBackend = "C:\\Program Files\\KiroCrew\\kirocrew-backend.exe";
  const trustedPython = "C:\\Program Files\\KiroCrew\\python.exe";
  assert.ok(isKirocrewCommand(`"${trustedCli}"`, {
    trustedExecutablePaths: [trustedCli],
  }));
  assert.ok(isKirocrewCommand(`"${trustedBackend}" --gateway`, {
    trustedExecutablePaths: [trustedBackend],
  }));
  assert.ok(isKirocrewCommand(`"${trustedPython}" -m kiro_crew gateway`, {
    trustedExecutablePaths: [trustedPython],
  }));
  assert.ok(!isKirocrewCommand("C:\\Temp\\kirocrew.exe gateway", {
    trustedExecutablePaths: [trustedCli],
  }));
});
