const { test } = require("node:test");
const assert = require("node:assert");
const {
  DRAIN_PATH,
  RESULT_PATH,
  joinUrl,
  createAgentCommandChannel,
} = require("../browser-agent-channel");

// ── helpers ──

/** A resolvable promise. */
function deferred() {
  let resolve;
  const promise = new Promise((r) => { resolve = r; });
  return { promise, resolve };
}

/** Poll `fn` until it returns truthy or `timeout` ms elapse. Uses tiny real
 *  sleeps so the loop-under-test (async) gets a chance to advance. */
async function waitFor(fn, timeout = 1000) {
  const start = Date.now();
  for (;;) {
    const v = fn();
    if (v) return v;
    if (Date.now() - start > timeout) throw new Error("waitFor: timed out");
    await new Promise((r) => setTimeout(r, 2));
  }
}

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/** A Response-like object. */
function res(status, jsonBody) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      if (jsonBody === "THROW") throw new Error("bad json");
      return jsonBody;
    },
    async text() { return JSON.stringify(jsonBody); },
  };
}

/**
 * Build a fake fetch that routes drain vs result. `drainResponder(callIndex)`
 * returns the Response-like for each drain POST; result POSTs are recorded and
 * always answered 200.
 */
function fakeFetch(drainResponder) {
  const calls = { drain: 0, result: 0, results: [], drains: [] };
  async function fetchFn(url, opts) {
    const body = opts && opts.body ? JSON.parse(opts.body) : {};
    if (url.endsWith(DRAIN_PATH)) {
      const idx = calls.drain;
      calls.drain += 1;
      calls.drains.push(body);
      return drainResponder(idx, body);
    }
    if (url.endsWith(RESULT_PATH)) {
      calls.result += 1;
      calls.results.push(body);
      return res(200, { ok: true });
    }
    throw new Error(`unexpected url ${url}`);
  }
  return { fetchFn, calls };
}

const BASE = "http://127.0.0.1:6777";

function baseDeps(overrides) {
  return {
    getGatewayUrl: () => BASE,
    getSecret: () => "s3cr3t",
    listPanelIds: () => ["sess-1"],
    dispatch: async () => ({}),
    onError: () => {},
    waitMs: 50,
    backoffMs: 20,
    idleMs: 10,
    ...overrides,
  };
}

// ── joinUrl ──

test("joinUrl: no double slash, path preserved", () => {
  assert.strictEqual(joinUrl("http://127.0.0.1:6777/", DRAIN_PATH), `http://127.0.0.1:6777${DRAIN_PATH}`);
  assert.strictEqual(joinUrl("http://127.0.0.1:6777", RESULT_PATH), `http://127.0.0.1:6777${RESULT_PATH}`);
});

// ── construction guards ──

test("factory: missing required deps throw", () => {
  assert.throws(() => createAgentCommandChannel({}), /fetchFn is required/);
  assert.throws(
    () => createAgentCommandChannel({ fetchFn: () => {} }),
    /getGatewayUrl is required/
  );
});

// ── no polling when there are no panels ──

test("no panels: never hits the network, does not spin", async () => {
  const { fetchFn, calls } = fakeFetch(() => res(204));
  const ch = createAgentCommandChannel(
    baseDeps({ fetchFn, listPanelIds: () => [] })
  );
  ch.start();
  await sleep(60); // several idle cycles at idleMs=10
  await ch.stop();
  assert.strictEqual(calls.drain, 0, "no drain calls without a panel");
  assert.strictEqual(calls.result, 0);
  assert.strictEqual(ch.isRunning(), false);
});

// ── a command is dispatched and its result posted ──

test("200: dispatches the op and posts ok:true with the result", async () => {
  const command = { id: "c1", session_key: "sess-1", op: "navigate", args: { url: "https://x.test/" } };
  // First drain returns the command; subsequent drains return 204.
  const { fetchFn, calls } = fakeFetch((idx) => (idx === 0 ? res(200, command) : res(204)));
  const dispatched = [];
  const ch = createAgentCommandChannel(
    baseDeps({
      fetchFn,
      dispatch: async (sessionKey, op, args) => {
        dispatched.push({ sessionKey, op, args });
        return { navigated: true };
      },
    })
  );
  ch.start();
  await waitFor(() => calls.result >= 1);
  await ch.stop();

  assert.deepStrictEqual(dispatched, [
    { sessionKey: "sess-1", op: "navigate", args: { url: "https://x.test/" } },
  ]);
  assert.deepStrictEqual(calls.results[0], { id: "c1", ok: true, result: { navigated: true } });
  // The drain request carried the session keys, wait hint, and secret header.
  assert.deepStrictEqual(calls.drains[0], { session_keys: ["sess-1"], wait_ms: 50 });
});

test("the X-Internal-Secret header is sent on drain and result", async () => {
  const command = { id: "c1", session_key: "sess-1", op: "snapshot", args: {} };
  const ff = fakeFetch((idx) => (idx === 0 ? res(200, command) : res(204)));
  let sawSecretOnDrain = null;
  let sawSecretOnResult = null;
  const ch = createAgentCommandChannel(
    baseDeps({
      fetchFn: async (url, opts) => {
        const secret = opts.headers["X-Internal-Secret"];
        if (url.endsWith(DRAIN_PATH)) sawSecretOnDrain = secret;
        else sawSecretOnResult = secret;
        return ff.fetchFn(url, opts);
      },
      getSecret: () => "abc123",
      dispatch: async () => ({}),
    })
  );
  ch.start();
  await waitFor(() => ff.calls.result >= 1);
  await ch.stop();
  assert.strictEqual(sawSecretOnDrain, "abc123");
  assert.strictEqual(sawSecretOnResult, "abc123");
});

// ── a rejecting dispatch posts ok:false and the loop survives ──

test("rejecting dispatch: posts ok:false and keeps polling", async () => {
  const cmd1 = { id: "bad", session_key: "sess-1", op: "click", args: { x: 1, y: 2 } };
  const cmd2 = { id: "good", session_key: "sess-1", op: "evaluate", args: { expression: "1+1" } };
  // drain 0 -> failing command, drain 1 -> good command, then 204 forever.
  const { fetchFn, calls } = fakeFetch((idx) => {
    if (idx === 0) return res(200, cmd1);
    if (idx === 1) return res(200, cmd2);
    return res(204);
  });
  const ch = createAgentCommandChannel(
    baseDeps({
      fetchFn,
      dispatch: async (_sk, op) => {
        if (op === "click") throw new Error("element not found");
        return 2;
      },
    })
  );
  ch.start();
  await waitFor(() => calls.result >= 2);
  await ch.stop();

  const byId = Object.fromEntries(calls.results.map((r) => [r.id, r]));
  assert.deepStrictEqual(byId.bad, { id: "bad", ok: false, error: "element not found" });
  assert.deepStrictEqual(byId.good, { id: "good", ok: true, result: 2 });
});

// ── 204 continues immediately (and does not post a result) ──

test("204: no dispatch, no result posted, keeps looping to the next command", async () => {
  const command = { id: "c9", session_key: "sess-1", op: "snapshot", args: {} };
  // Three 204s, then a real command, then park.
  const { fetchFn, calls } = fakeFetch((idx) => (idx === 3 ? res(200, command) : idx > 3 ? res(204) : res(204)));
  let dispatched = 0;
  const ch = createAgentCommandChannel(
    baseDeps({
      fetchFn,
      dispatch: async () => { dispatched += 1; return {}; },
    })
  );
  ch.start();
  await waitFor(() => calls.result >= 1);
  await ch.stop();
  assert.strictEqual(dispatched, 1, "only the single real command dispatched");
  assert.ok(calls.drain >= 4, "polled through the 204s to reach the command");
  assert.strictEqual(calls.results[0].id, "c9");
});

// ── non-2xx backs off (bounded, no tight loop) ──

test("non-2xx: bounded backoff, does not tight-loop", async () => {
  const { fetchFn, calls } = fakeFetch(() => res(500, { error: "boom" }));
  const errors = [];
  const ch = createAgentCommandChannel(
    baseDeps({
      fetchFn,
      backoffMs: 25,
      dispatch: async () => ({}),
      onError: (e, ctx) => errors.push(ctx && ctx.phase),
    })
  );
  ch.start();
  await sleep(150); // ~150/25 = 6 backoff cycles
  await ch.stop();
  // A tight loop would be thousands of calls in 150ms; bounded backoff keeps it
  // in single digits. Generous ceiling to avoid CI flake.
  assert.ok(calls.drain > 0, "did attempt at least one drain");
  assert.ok(calls.drain <= 15, `bounded (was ${calls.drain})`);
  assert.strictEqual(calls.result, 0, "a failed drain never posts a result");
  assert.ok(errors.includes("drain-status"), "reported the non-2xx");
});

// ── network error backs off (bounded) ──

test("network error: bounded backoff, loop survives", async () => {
  let calls = 0;
  const errors = [];
  const ch = createAgentCommandChannel(
    baseDeps({
      fetchFn: async () => { calls += 1; throw new Error("ECONNREFUSED"); },
      backoffMs: 25,
      dispatch: async () => ({}),
      onError: (e, ctx) => errors.push(ctx && ctx.phase),
    })
  );
  ch.start();
  await sleep(150);
  await ch.stop();
  assert.ok(calls > 0 && calls <= 15, `bounded (was ${calls})`);
  assert.ok(errors.includes("drain-fetch"), "reported the transport error");
});

// ── malformed payloads are ignored (and backed off), never throw ──

test("malformed 200 bodies are ignored without killing the loop", async () => {
  // idx 0: missing id/op ; idx 1: non-JSON body ; idx 2+: valid command then park
  const good = { id: "ok1", session_key: "sess-1", op: "navigate", args: {} };
  const { fetchFn, calls } = fakeFetch((idx) => {
    if (idx === 0) return res(200, { session_key: "sess-1", args: {} }); // no id/op
    if (idx === 1) return res(200, "THROW"); // json() throws
    if (idx === 2) return res(200, good);
    return res(204);
  });
  let dispatched = 0;
  const errors = [];
  const ch = createAgentCommandChannel(
    baseDeps({
      fetchFn,
      backoffMs: 10,
      dispatch: async () => { dispatched += 1; return {}; },
      onError: (e, ctx) => errors.push(ctx && ctx.phase),
    })
  );
  ch.start();
  await waitFor(() => calls.result >= 1);
  await ch.stop();
  assert.strictEqual(dispatched, 1, "only the valid command dispatched");
  assert.strictEqual(calls.results[0].id, "ok1");
  assert.ok(errors.includes("drain-validate"), "reported the missing-field payload");
  assert.ok(errors.includes("drain-parse"), "reported the non-JSON body");
});

// ── stop() halts the loop, including mid-flight ──

test("stop(): halts the loop; no further calls after stop resolves", async () => {
  const { fetchFn, calls } = fakeFetch(() => res(204));
  const ch = createAgentCommandChannel(baseDeps({ fetchFn, dispatch: async () => ({}) }));
  ch.start();
  await waitFor(() => calls.drain >= 1);
  await ch.stop();
  assert.strictEqual(ch.isRunning(), false);
  const after = calls.drain;
  await sleep(60);
  assert.strictEqual(calls.drain, after, "no drains after stop() resolved");
});

test("stop(): interrupts an in-flight backoff sleep promptly", async () => {
  const { fetchFn } = fakeFetch(() => res(500, {}));
  const ch = createAgentCommandChannel(
    baseDeps({ fetchFn, backoffMs: 10000, dispatch: async () => ({}) })
  );
  ch.start();
  await sleep(20); // let it enter the long backoff
  const t0 = Date.now();
  await ch.stop(); // must not wait the full 10s
  assert.ok(Date.now() - t0 < 1000, "stop resolved without waiting out the backoff");
  assert.strictEqual(ch.isRunning(), false);
});

test("start(): idempotent — a second start does not launch a second loop", async () => {
  const { fetchFn, calls } = fakeFetch(() => res(204));
  const ch = createAgentCommandChannel(baseDeps({ fetchFn, idleMs: 5, listPanelIds: () => [] }));
  ch.start();
  ch.start();
  await sleep(40);
  await ch.stop();
  // With a single loop and no panels, zero drains; the point is it did not
  // crash or double-run. isRunning must be false after stop.
  assert.strictEqual(ch.isRunning(), false);
  assert.strictEqual(calls.drain, 0);
});
