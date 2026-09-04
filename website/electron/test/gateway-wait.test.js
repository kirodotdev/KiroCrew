const { test } = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const path = require("node:path");
const {
  DEFAULT_GATEWAY_WAIT_MS,
  WINDOWS_LOCAL_GATEWAY_WAIT_MS,
  gatewayWaitTimeoutMs,
  waitForGateway,
  describeGatewayFailure,
  tailLines,
  isPortInUse,
} = require("../gateway-wait");

// Synchronous fake clock + timer so poll loops resolve instantly and
// deterministically (no real waiting). setTimeoutFn advances the clock by the
// requested delay, so maxWaitMs is reached after a bounded number of polls.
function harness({
  checkBackend,
  getFailure = () => null,
  isWindowAlive = () => true,
  maxWaitMs = 30_000,
  pollIntervalMs = 500,
} = {}) {
  let t = 0;
  const statuses = [];
  const p = waitForGateway({
    checkBackend,
    getFailure,
    isWindowAlive,
    onStatus: (m) => statuses.push(m),
    now: () => t,
    setTimeoutFn: (fn, ms) => { t += ms; queueMicrotask(fn); },
    maxWaitMs,
    pollIntervalMs,
  });
  return { p, statuses };
}

// ── waitForGateway ──

test("waitForGateway resolves when the backend is healthy", async () => {
  const { p, statuses } = harness({ checkBackend: () => Promise.resolve() });
  await p;
  assert.ok(statuses.includes("Connected ✓"));
});

test("waitForGateway resolves after a few unhealthy polls", async () => {
  let n = 0;
  const { p } = harness({
    checkBackend: () => (++n < 3 ? Promise.reject(new Error("not yet")) : Promise.resolve()),
  });
  await p;
  assert.strictEqual(n, 3);
});

test("Windows local cold start stays on the splash past the ordinary deadline", async () => {
  let polls = 0;
  const maxWaitMs = gatewayWaitTimeoutMs({ platform: "win32", watchSpawn: true });
  const { p } = harness({
    // 110 failed 500ms polls model a 55-second packaged cold start.
    checkBackend: () => (++polls <= 110
      ? Promise.reject(new Error("not yet"))
      : Promise.resolve()),
    maxWaitMs,
  });

  await p;
  assert.strictEqual(polls, 111);
  assert.ok(maxWaitMs > DEFAULT_GATEWAY_WAIT_MS);
});

test("gateway wait policy extends only the primary Windows gateway", () => {
  assert.strictEqual(
    gatewayWaitTimeoutMs({ platform: "win32", watchSpawn: true }),
    WINDOWS_LOCAL_GATEWAY_WAIT_MS,
  );
  assert.strictEqual(
    gatewayWaitTimeoutMs({ platform: "win32", watchSpawn: false }),
    DEFAULT_GATEWAY_WAIT_MS,
  );
  assert.strictEqual(
    gatewayWaitTimeoutMs({ platform: "darwin", watchSpawn: true }),
    DEFAULT_GATEWAY_WAIT_MS,
  );
});

test("the supervisor extends the Windows deadline only for a gateway it spawned", () => {
  const supervisor = fs.readFileSync(
    path.join(__dirname, "..", "gateway-supervisor.js"),
    "utf8",
  );
  assert.match(
    supervisor,
    /watchSpawn: watchSpawn && gatewayOwnership === "spawned"/,
  );
});

test("waitForGateway fails fast when the spawned gateway exited (no health polling)", async () => {
  const failure = { code: 1, signal: null };
  let polls = 0;
  const { p } = harness({
    checkBackend: () => { polls++; return Promise.reject(new Error("dead port")); },
    getFailure: () => failure,
    maxWaitMs: 30_000,
  });
  await assert.rejects(p, (e) => {
    assert.strictEqual(e.kind, "failed");
    assert.deepStrictEqual(e.failure, failure);
    return true;
  });
  // The failure short-circuits before any health probe — we did NOT poll a dead
  // port toward the 30s timeout.
  assert.strictEqual(polls, 0);
});

test("waitForGateway checks the failure flag before the timeout", async () => {
  const { p } = harness({
    checkBackend: () => Promise.reject(new Error("no")),
    getFailure: () => ({ signal: "SIGKILL" }),
    maxWaitMs: -1, // already past the deadline; failure must still win
  });
  await assert.rejects(p, (e) => e.kind === "failed");
});

test("waitForGateway times out when never healthy and no failure", async () => {
  const { p } = harness({
    checkBackend: () => Promise.reject(new Error("no")),
    getFailure: () => null,
    maxWaitMs: 2000,
    pollIntervalMs: 500,
  });
  await assert.rejects(p, (e) => e.kind === "timeout");
});

test("waitForGateway aborts when the window is gone", async () => {
  const { p } = harness({
    checkBackend: () => Promise.resolve(),
    isWindowAlive: () => false,
  });
  await assert.rejects(p, (e) => e.kind === "window-closed");
});

// ── describeGatewayFailure ──

// An incomplete bundle is not a launch failure: the installer is still writing
// the backend. Prefixing "could not be launched" would open with failure
// vocabulary under a title that says the install is still finishing.
test("describeGatewayFailure: an incomplete bundle passes its message through bare", () => {
  const msg = "Kiro Crew's bundled Python runtime is still being installed — retry.";
  assert.strictEqual(describeGatewayFailure({ error: msg, incompleteBundle: true }), msg);
});

test("describeGatewayFailure: an ordinary spawn error keeps the launch-failure prefix", () => {
  assert.match(describeGatewayFailure({ error: "EACCES" }), /could not be launched/);
});

test("describeGatewayFailure: exit code", () => {
  assert.match(describeGatewayFailure({ code: 1, signal: null }), /code 1/);
});

test("describeGatewayFailure: the disabled case names the port and both ways out", () => {
  const s = describeGatewayFailure({ disabled: true, port: 5476 });
  assert.match(s, /5476/);
  assert.match(s, /set not to start one on this machine/);
  assert.match(s, /start one here/);
  // Must NOT send the user to Settings: that page is served by the gateway that
  // is not running, so the instruction would be unreachable exactly when shown.
  assert.doesNotMatch(s, /Settings/);
  // Nothing was launched, so wording that sends the user hunting a crash or a
  // launch log is wrong for this case.
  assert.doesNotMatch(s, /could not be launched|exited on launch|failed to start/);
});

// #6138: with the launch aimed at a configured remote crew, this text must name
// that target and must NOT offer to start a gateway here -- the spawn binds the
// crew's own port, so the offer the dialog hides cannot be promised in words.
test("describeGatewayFailure: a remote target is named and no local start offered", () => {
  const s = describeGatewayFailure({ disabled: true, port: 7778, remoteHost: "a.example.com" });
  assert.match(s, /a\.example\.com/);
  assert.match(s, /7778/);
  assert.match(s, /set not to start a gateway on this machine/);
  assert.doesNotMatch(s, /start one here/);
  assert.doesNotMatch(s, /Settings/);
  assert.doesNotMatch(s, /could not be launched|exited on launch|failed to start/);
});

// The dialog withholds its start-a-gateway button on a crew's port, and the page
// that owns the choice is served by a gateway, so this sentence is the only exit
// the state has. Without it the user is left with a Retry that cannot succeed.
test("describeGatewayFailure: a remote target names a way to run one here anyway", () => {
  const s = describeGatewayFailure({ disabled: true, port: 7778, remoteHost: "a.example.com" });
  assert.match(s, /KIROCREW_PORT/);
  assert.match(s, /no remote host configured/);
  // BOTH steps, or the instruction under-promises: an explicit port re-aims the
  // launch, but the opt-out is still in force, so that launch stops at this same
  // state -- on a port where the withheld button is offered again. A user told
  // only the first step reads the second dialog as "it did not work".
  assert.match(s, /then choose Start Local Gateway when prompted/);
});

test("describeGatewayFailure: the remote-side instruction names the tunnel", () => {
  // "the connection that reaches it" is vague at the moment of failure.
  const s = describeGatewayFailure({ disabled: true, port: 7778, remoteHost: "a.example.com" });
  assert.match(s, /tunnel or port-forward/);
});

test("describeGatewayFailure: title and body agree on 'answering at'", () => {
  // The dialog title reads "nothing answering at host:port"; the body said
  // "for", which reads as two different facts about the same target.
  const s = describeGatewayFailure({ disabled: true, port: 7778, remoteHost: "a.example.com" });
  assert.match(s, /answering at a\.example\.com/);
  assert.doesNotMatch(s, /answering for/);
});

test("describeGatewayFailure: an empty remoteHost keeps the local-start wording", () => {
  // The field is absent on every pre-existing caller, and a cleared host entry
  // stores "", so neither may change which of the two texts is chosen.
  for (const failure of [
    { disabled: true, port: 5476 },
    { disabled: true, port: 5476, remoteHost: "" },
  ]) {
    assert.match(describeGatewayFailure(failure), /start one here/);
  }
});

// #6138: the crew binds its OWN port on its own machine (effectivePort =
// remotePort || tokenPort || PORT), so naming the local end would send the user
// to check a port nothing over there was ever expected to serve.
test("describeGatewayFailure: a distinct remote port is the one to go and check", () => {
  const s = describeGatewayFailure({
    disabled: true,
    port: 5477,
    remoteHost: "a.example.com",
    remotePort: "9000",
  });
  assert.match(s, /a\.example\.com:9000/);
  // The local end stays visible, because that is the port the tunnel must land on.
  assert.match(s, /local port 5477/);
  assert.doesNotMatch(s, /a\.example\.com:5477/);
  assert.doesNotMatch(s, /start one here/);
});

test("describeGatewayFailure: no remotePort falls back to the shared port form", () => {
  // The field is optional and a cleared entry stores "", so both must read as
  // "the crew is on this same port" rather than rendering an empty target.
  for (const remotePort of [undefined, ""]) {
    const s = describeGatewayFailure({
      disabled: true, port: 7778, remoteHost: "a.example.com", remotePort,
    });
    assert.match(s, /a\.example\.com on port 7778/);
    assert.doesNotMatch(s, /local port/);
    assert.doesNotMatch(s, /:undefined|: ,|::/);
  }
});

test("describeGatewayFailure: disabled wins over a stale error field", () => {
  // waitForGateway hands over whatever record it was given; the deliberate
  // no-spawn reason must not be reported as a launch failure.
  const s = describeGatewayFailure({ disabled: true, port: 7000, error: "spawn ENOENT" });
  assert.match(s, /7000/);
  assert.doesNotMatch(s, /ENOENT/);
});

test("describeGatewayFailure: SIGKILL carries the Gatekeeper + xattr hint", () => {
  const s = describeGatewayFailure({ signal: "SIGKILL" });
  assert.match(s, /Gatekeeper/);
  assert.match(s, /xattr -cr/);
});

test("describeGatewayFailure: spawn error", () => {
  const s = describeGatewayFailure({ error: "spawn ENOENT" });
  assert.match(s, /could not be launched/);
  assert.match(s, /spawn ENOENT/);
});

test("describeGatewayFailure: other signal", () => {
  assert.match(describeGatewayFailure({ signal: "SIGSEGV" }), /signal SIGSEGV/);
});

test("describeGatewayFailure: null", () => {
  assert.match(describeGatewayFailure(null), /failed to start/);
});

// ── tailLines ──

test("tailLines returns the last n lines", () => {
  assert.strictEqual(tailLines("a\nb\nc\nd\ne", 2), "d\ne");
});

test("tailLines returns all lines when there are fewer than n", () => {
  assert.strictEqual(tailLines("x\ny", 10), "x\ny");
});

test("tailLines trims trailing blank lines", () => {
  assert.strictEqual(tailLines("a\nb\n\n\n", 2), "a\nb");
});

test("tailLines on empty/null input", () => {
  assert.strictEqual(tailLines("", 5), "");
  assert.strictEqual(tailLines(null, 5), "");
});

// ── isPortInUse ──

test("isPortInUse detects a port-bind failure", () => {
  assert.strictEqual(
    isPortInUse("17:57:53 ERROR kiro_crew.dashboard.server: Port 7788 already in use -- is another KiroCrew gateway running?"),
    true,
  );
  assert.strictEqual(isPortInUse("OSError: [Errno 48] Address already in use"), true);
  assert.strictEqual(isPortInUse("Error: listen EADDRINUSE: address already in use :::7788"), true);
});

test("isPortInUse is false for unrelated logs / empty input", () => {
  assert.strictEqual(isPortInUse("ModuleNotFoundError: No module named 'yaml'"), false);
  assert.strictEqual(isPortInUse("gateway child exited code=1 signal=null"), false);
  assert.strictEqual(isPortInUse(""), false);
  assert.strictEqual(isPortInUse(null), false);
});
