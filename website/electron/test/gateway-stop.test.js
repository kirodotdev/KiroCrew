const { test } = require("node:test");
const assert = require("node:assert");
const http = require("http");
const os = require("os");
const fs = require("fs");
const path = require("path");
const { spawn } = require("child_process");
const { postShutdown, stopGatewayGracefully, forceStopPort, classifyPortOwner, KIROCREW_PROC_RE } = require("../gateway-stop");

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
  // Migration edge: a partially-copied canonical secret is stale, but the
  // gateway is authenticated by the legacy one. postShutdown must POST both.
  const { server, port } = await startServer({ secret: "live-legacy", status: 200 });
  try {
    const ok = await postShutdown({
      backendUrl: `http://127.0.0.1:${port}`,
      secrets: ["stale-canonical", "live-legacy"],
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

test("stopGatewayGracefully: SIGKILL fallback when SIGTERM ignored", async () => {
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
  assert.ok(KIROCREW_PROC_RE.test("python -m kiro_crew gateway"));
  assert.ok(KIROCREW_PROC_RE.test("/Applications/KiroCrew.app/.../kirocrew"));
  assert.ok(!KIROCREW_PROC_RE.test("ssh -NL 5476:localhost:5476 host"));
  // The matcher keys on the executable/module TOKEN, not a path substring:
  // an unrelated process merely living under a `kirocrew` home dir is foreign.
  assert.ok(!KIROCREW_PROC_RE.test("C:\\Users\\kirocrew\\OtherApp\\server.exe --port 5476"));
  assert.ok(!KIROCREW_PROC_RE.test("/home/kirocrew/some-other-server --port 5476"));
  assert.ok(!KIROCREW_PROC_RE.test("C:\\Users\\kirocrew\\python.exe -m http.server 5476"));
  // …but the real Windows executable / backend / module invocation still match.
  assert.ok(KIROCREW_PROC_RE.test("C:\\Program Files\\KiroCrew\\kirocrew.exe"));
  assert.ok(KIROCREW_PROC_RE.test("C:\\Program Files\\KiroCrew\\kirocrew-backend.exe --gateway"));
  assert.ok(KIROCREW_PROC_RE.test("C:\\Python\\python.exe -m kiro_crew gateway"));
});
