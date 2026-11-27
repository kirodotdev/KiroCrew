// Liveness recovery of an adopted gateway that stays on the port and answers
// again just after the 30 s bounded wait (a service-managed gateway whose event
// loop stalled for ~30 s). Recovery must reconnect to it, not wait for a port a
// supervised gateway never frees and then stop on "port held".

const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { createReconnect } = require("../runtime/gateway/reconnect");
const { incumbentSnapshotBlocksRespawn } = require("../gateway-recovery");

const HOLDER_PID = 40869;
const VERSION = "0.12.2.4";

let clock;
let realSetTimeout;
let realDateNow;

function harness({
  servesAfterMs = null,
  freesAfterMs = null,
  readiness = () => "ready",
  edition = undefined,
  destroyAfterMs = null,
  installAfterMs = null,
  probeMs = 0,
}) {
  const events = [];
  let endedAtMs = null;
  const startAt = clock;
  const elapsed = () => clock - startAt;
  const after = (ms) => ms !== null && elapsed() >= ms;
  const portFree = () => after(freesAfterMs);
  const window = { webContents: { loadFile() {} }, isDestroyed: () => after(destroyAfterMs) };
  const reconnect = createReconnect({
    app: { getVersion: () => VERSION },
    path,
    dirname: __dirname,
    port: 5476,
    backendUrl: "http://127.0.0.1:5476",
    healthUrl: "http://127.0.0.1:5476/health",
    log: (line) => events.push(`log ${line}`),
    quitting: () => false,
    sendStatus: () => {},
    splashQuery: () => ({}),
    setTimeoutFn: (fn, ms) => global.setTimeout(fn, ms),
    getGatewayOwnership: () => "reused-service",
    setGatewayOwnership: (o) => events.push(`ownership ${o}`),
    setGatewayStartFailure: () => {},
    getInstallEpoch: () => (after(installAfterMs) ? 1 : 0),
    // probeMs stands in for a health probe that runs to its timeout.
    checkBackend: async () => {
      if (after(servesAfterMs)) return;
      clock += probeMs;
      throw new Error("timeout");
    },
    respawnGateway: async () => { events.push("spawn"); },
    showLoadingThenConnect: async () => { events.push("connect"); },
    showUnrecoverableGatewayError: async (_w, _p, kind) => {
      endedAtMs = elapsed();
      events.push(`error ${JSON.stringify(kind)}`);
    },
    // Mirrors the supervisor's wait: a socket probe every 500 ms until maxWaitMs.
    waitForPortFree: async (maxWaitMs = 30_000) => {
      const start = clock;
      for (;;) {
        if (portFree()) return true;
        if (clock - start > maxWaitMs) return false;
        await new Promise((resolve) => global.setTimeout(resolve, 500));
      }
    },
    probeGatewayPortBinding: async () => (portFree() ? "free" : "bound"),
    probeGatewayPortOwner: async () => "service",
    fetchHealthInfo: async () => ({ app: "kirocrew", version: VERSION, edition }),
    fetchGatewayReadiness: async () => readiness(elapsed()),
    snapshotGatewayPortPids: async () => (portFree() ? null : [HOLDER_PID]),
    unverifiedIncumbent: (pids) => incumbentSnapshotBlocksRespawn({ pids, isWindows: false }),
    waitForIncumbentExit: async () => "exited",
    probeLockHolderPid: async () => { throw new Error("socket names the holder; lock must not be asked"); },
  });
  return { events, endedAtMs: () => endedAtMs, run: () => reconnect.reconnectOrRespawnAdoptedGateway(window) };
}

const actions = (events) => events.filter((e) => !e.startsWith("log "));
const logged = (events, text) => events.some((e) => e.startsWith("log ") && e.includes(text));

describe("liveness recovery of an adopted gateway that keeps the port", () => {
  beforeEach(() => {
    clock = 1_000_000;
    realSetTimeout = global.setTimeout;
    realDateNow = Date.now;
    Date.now = () => clock;
    global.setTimeout = (fn, ms = 0) => { clock += ms; return realSetTimeout(fn, 0); };
  });

  afterEach(() => {
    global.setTimeout = realSetTimeout;
    Date.now = realDateNow;
  });

  // The reported timeline: the bounded wait ends at 30 s, the gateway answers
  // at 32 s and never releases :5476.
  for (const servesAfterMs of [32_000, 55_000]) {
    it(`reconnects to the holder when it answers at ${servesAfterMs / 1000} s`, async () => {
      const h = harness({ servesAfterMs });
      await h.run();
      assert.deepEqual(actions(h.events), ["ownership reused-service", "connect"]);
      assert.ok(logged(h.events, "adopted gateway answering again on :5476"));
    });
  }

  it("surfaces port-held when the holder never answers and never frees the port", async () => {
    const h = harness({});
    await h.run();
    assert.deepEqual(actions(h.events), ['error "held"']);
  });

  it("does not reconnect to a holder that answers while shutting down", async () => {
    const h = harness({ servesAfterMs: 32_000, readiness: () => "shutting-down" });
    await h.run();
    assert.deepEqual(actions(h.events), ['error "held"']);
  });

  it("spawns when a draining holder frees the port", async () => {
    const h = harness({ servesAfterMs: 32_000, freesAfterMs: 40_000, readiness: () => "shutting-down" });
    await h.run();
    assert.deepEqual(actions(h.events), ["spawn", "connect"]);
  });

  it("does not read an unknown readiness as serving", async () => {
    const h = harness({ servesAfterMs: 32_000, readiness: () => "unknown" });
    await h.run();
    assert.deepEqual(actions(h.events), ['error "held"']);
  });

  it("reconnects once an unknown readiness turns ready", async () => {
    const h = harness({ servesAfterMs: 32_000, readiness: (ms) => (ms < 45_000 ? "unknown" : "ready") });
    await h.run();
    assert.deepEqual(actions(h.events), ["ownership reused-service", "connect"]);
  });

  // The boot decision table still judges the holder: an lts service is not adopted.
  it("refuses a holder the decision table does not reuse", async () => {
    const h = harness({ servesAfterMs: 32_000, edition: "lts" });
    await h.run();
    assert.deepEqual(actions(h.events), ['error "held"']);
    assert.ok(logged(h.events, "unusable holder"));
  });

  for (const servesAfterMs of [null, 40_000]) {
    it(`stops when the window closes during the port wait (holder serves: ${servesAfterMs !== null})`, async () => {
      const h = harness({ servesAfterMs, destroyAfterMs: 35_000 });
      await h.run();
      assert.deepEqual(actions(h.events), []);
    });
  }

  it("stops when an update install is dispatched during the port wait", async () => {
    const h = harness({ servesAfterMs: 40_000, installAfterMs: 35_000 });
    await h.run();
    assert.deepEqual(actions(h.events), []);
  });

  it("does not spawn when an update install is dispatched and then the port frees", async () => {
    const h = harness({ installAfterMs: 35_000, freesAfterMs: 36_000 });
    await h.run();
    assert.deepEqual(actions(h.events), []);
  });

  // Each poll's health probe runs to its 2 s timeout: the port wait still ends
  // near its 30 s budget rather than adding the probe time to every poll.
  it("keeps the port wait near its budget when every health probe times out", async () => {
    const h = harness({ probeMs: 2_000 });
    await h.run();
    assert.deepEqual(actions(h.events), ['error "held"']);
    assert.ok(h.endedAtMs() <= 75_000, `ended at ${h.endedAtMs()} ms`);
  });
});
