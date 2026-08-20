const { test } = require("node:test");
const assert = require("node:assert");
const { waitForGateway, describeGatewayFailure, tailLines, isPortInUse } = require("../gateway-wait");

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
