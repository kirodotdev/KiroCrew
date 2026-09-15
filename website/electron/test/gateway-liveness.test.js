const { test } = require("node:test");
const assert = require("node:assert");
const { createLivenessMonitor } = require("../gateway-liveness");

// Drive tick() directly with a stubbed probe — no timers, fully deterministic.
function harness({ results, failureThreshold = 3, isWindowAlive = () => true } = {}) {
  let i = 0;
  const events = [];
  const m = createLivenessMonitor({
    // results[i] === true => probe resolves (alive); false => rejects (unresponsive)
    probe: () => (results[Math.min(i++, results.length - 1)]
      ? Promise.resolve()
      : Promise.reject(new Error("unresponsive"))),
    onUnresponsive: () => events.push("unresponsive"),
    onRecovered: () => events.push("recovered"),
    isWindowAlive,
    failureThreshold,
    log: () => {},
  });
  return { m, events };
}

test("healthy backend never fires recovery", async () => {
  const { m, events } = harness({ results: [true, true, true, true, true] });
  for (let k = 0; k < 5; k++) await m.tick();
  assert.deepStrictEqual(events, []);
  assert.strictEqual(m.getState().consecutiveFailures, 0);
});

test("fires onUnresponsive after threshold consecutive failures", async () => {
  const { m, events } = harness({ results: [false, false, false, false], failureThreshold: 3 });
  await m.tick(); // 1
  await m.tick(); // 2
  assert.deepStrictEqual(events, []);
  await m.tick(); // 3 -> trips
  assert.deepStrictEqual(events, ["unresponsive"]);
});

test("onUnresponsive fires exactly once per episode (debounced)", async () => {
  const { m, events } = harness({ results: [false, false, false, false, false, false], failureThreshold: 3 });
  for (let k = 0; k < 6; k++) await m.tick();
  assert.deepStrictEqual(events, ["unresponsive"]);
  // Once fired, further ticks are no-ops until stop()/start().
  assert.strictEqual(m.getState().fired, true);
});

test("a single failure then recovery does not fire, and emits recovered", async () => {
  const { m, events } = harness({ results: [false, false, true], failureThreshold: 3 });
  await m.tick(); // fail 1
  await m.tick(); // fail 2
  await m.tick(); // recover before threshold
  assert.deepStrictEqual(events, ["recovered"]);
  assert.strictEqual(m.getState().consecutiveFailures, 0);
});

test("failure counter resets after an intermittent success", async () => {
  // fail, fail, ok (reset), fail, fail -> still only 2 in a row, no fire
  const { m, events } = harness({ results: [false, false, true, false, false], failureThreshold: 3 });
  for (let k = 0; k < 5; k++) await m.tick();
  assert.deepStrictEqual(events, ["recovered"]);
});

test("stops itself when the window is gone", async () => {
  let alive = true;
  const m = createLivenessMonitor({
    probe: () => Promise.reject(new Error("x")),
    onUnresponsive: () => { throw new Error("should not fire after window close"); },
    isWindowAlive: () => alive,
    failureThreshold: 1,
    log: () => {},
  });
  m.start();
  alive = false;
  await m.tick(); // observes dead window -> stop(), no fire
  assert.strictEqual(m.getState().running, false);
});

test("start/stop lifecycle is idempotent and uses injected timers", () => {
  let id = 0;
  const cleared = [];
  const m = createLivenessMonitor({
    probe: () => Promise.resolve(),
    onUnresponsive: () => {},
    setIntervalFn: () => ++id,
    clearIntervalFn: (h) => cleared.push(h),
    intervalMs: 5,
  });
  m.start();
  assert.strictEqual(m.getState().running, true);
  const firstId = id;
  m.start(); // idempotent — no second timer
  assert.strictEqual(id, firstId);
  m.stop();
  assert.deepStrictEqual(cleared, [firstId]);
  assert.strictEqual(m.getState().running, false);
});

test("restart after firing re-arms for a new episode", async () => {
  const { m, events } = harness({ results: [false, false, false], failureThreshold: 3 });
  await m.tick();
  await m.tick();
  await m.tick();
  assert.deepStrictEqual(events, ["unresponsive"]);
  m.start(); // fresh episode
  assert.strictEqual(m.getState().fired, false);
  assert.strictEqual(m.getState().consecutiveFailures, 0);
  m.stop();
});

// ---------------------------------------------------------------------------
// The probe the monitor polls WITH. Field report (Windows desktop, 0.6.x): a
// gateway whose event loop lagged 1-9s under a full test run answered every
// request, and was still force-killed — three 2s probes in a row timed out
// (log: `backend probe failed (1/3) … (3/3) … force-killing wedged gateway`),
// orphaning six subagents. The probe gets its own budget; the boot poll keeps
// its 2s because a slow answer there only means "poll again".
// ---------------------------------------------------------------------------

const { EventEmitter } = require("node:events");
const { createBackendProbe, LIVENESS_PROBE_TIMEOUT_MS } = require("../gateway-liveness");

// An http fake that records the options each GET is made with and lets the
// test decide how the request ends: answer(status), error(), or timeout().
function recordingHttp() {
  const calls = [];
  return {
    calls,
    get(url, options, callback) {
      const req = new EventEmitter();
      req.destroyed = false;
      req.destroy = () => { req.destroyed = true; };
      const call = {
        url,
        options,
        req,
        answer(statusCode) {
          const res = new EventEmitter();
          res.statusCode = statusCode;
          res.resumed = false;
          res.resume = () => { res.resumed = true; };
          callback(res);
          return res;
        },
        error() { req.emit("error", new Error("connection refused")); },
        timeout() { req.emit("timeout"); },
      };
      calls.push(call);
      return req;
    },
  };
}

test("the post-handoff probe waits longer than the boot poll before counting a miss", () => {
  // The boot poll's 2s is the number this must NOT be: a loop blocked 2-9s by
  // disk I/O still answers, and killing it orphans every subagent for nothing.
  assert.ok(LIVENESS_PROBE_TIMEOUT_MS > 2000, `budget ${LIVENESS_PROBE_TIMEOUT_MS}ms must exceed the 2s boot poll`);
  // …and short enough that three misses at the 10s poll cadence still land
  // near the in-process watchdog's 25s budget rather than long after it.
  assert.ok(LIVENESS_PROBE_TIMEOUT_MS <= 10_000, "one probe must not outlast the poll interval");
  const httpMod = recordingHttp();
  const probe = createBackendProbe({ httpMod, url: "http://localhost:5476/api/status" });
  void probe();
  assert.strictEqual(httpMod.calls.length, 1);
  assert.strictEqual(httpMod.calls[0].url, "http://localhost:5476/api/status");
  assert.deepStrictEqual(httpMod.calls[0].options, { timeout: LIVENESS_PROBE_TIMEOUT_MS });
});

test("the probe resolves on any non-5xx answer and drains the body", async () => {
  const httpMod = recordingHttp();
  const probe = createBackendProbe({ httpMod, url: "http://localhost:5476/api/status" });
  const pending = probe();
  const res = httpMod.calls[0].answer(200);
  await pending;
  assert.strictEqual(res.resumed, true, "the body is drained so the socket is released");
  const auth = probe();
  httpMod.calls[1].answer(401); // an auth wall still proves the loop is turning
  await auth;
});

test("the probe rejects on a 5xx, a connection error, and a timeout — destroying the timed-out request", async () => {
  const httpMod = recordingHttp();
  const probe = createBackendProbe({ httpMod, url: "http://localhost:5476/api/status", timeoutMs: 1234 });

  const fiveHundred = probe();
  httpMod.calls[0].answer(503);
  await assert.rejects(fiveHundred, /status 503/);

  const refused = probe();
  httpMod.calls[1].error();
  await assert.rejects(refused, /connection refused/);

  const silent = probe();
  assert.deepStrictEqual(httpMod.calls[2].options, { timeout: 1234 }, "the override reaches the request");
  httpMod.calls[2].timeout();
  await assert.rejects(silent, /no answer in 1234ms/);
  assert.strictEqual(httpMod.calls[2].req.destroyed, true, "a timed-out request must not linger");
});

test("the supervisor polls liveness with the wider probe, not the boot poll's checkBackend", () => {
  // A source-shape contract, like shell-contract.test.js: the fix is one line
  // in startLivenessMonitor, and a refactor that reverts it to checkBackend()
  // would silently reintroduce the 2s budget.
  const fs = require("node:fs");
  const path = require("node:path");
  const source = fs.readFileSync(path.join(__dirname, "..", "gateway-supervisor.js"), "utf8");
  const start = source.indexOf("function startLivenessMonitor(");
  assert.ok(start >= 0, "startLivenessMonitor exists");
  const body = source.slice(start, source.indexOf("livenessMonitor.start();", start));
  assert.match(body, /probe:\s*createBackendProbe\(\{\s*httpMod:\s*http,\s*url:\s*HEALTH_URL\s*\}\)/);
  assert.doesNotMatch(body, /probe:\s*\(\)\s*=>\s*checkBackend\(/);
});
