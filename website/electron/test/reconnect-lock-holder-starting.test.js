// Liveness recovery on a POSIX host when the socket names no one but a live
// gateway of this home holds gateway.lock: a gateway that took the lock and
// has not bound the port yet (a slow version restart). Recovery must ask the
// lock, wait for that gateway to start serving, and reconnect to it, instead
// of spawning a replacement that the held lock refuses.

const { describe, it, beforeEach, afterEach } = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");
const { createReconnect } = require("../runtime/gateway/reconnect");
const {
  incumbentSnapshotBlocksRespawn,
  waitForProcessExit,
  LOCK_HOLDER_START_GRACE_MS,
} = require("../gateway-recovery");

const HOLDER_PID = 31862;

// A fake clock: every timer resolves at once and moves the clock forward, so
// the 30 s adopted wait and the 120 s start grace run in microseconds.
let clock;
let realSetTimeout;
let realDateNow;

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

function harness({
  lockProbe,
  servesAfterMs = null,
  snapshot = null,
  isWindows = false,
  portFree = true,
  holderExitsAfterMs = null,
  readiness = () => "ready",
}) {
  const events = [];
  let startAt = null;
  const window = { webContents: { loadFile() {} }, isDestroyed: () => false };
  const reconnect = createReconnect({
    app: { getVersion: () => "0.11.1.5" },
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
    getGatewayOwnership: () => "reused-local",
    setGatewayOwnership: (o) => events.push(`ownership ${o}`),
    setGatewayStartFailure: () => {},
    checkBackend: async () => {
      // Down through the adopted wait; up once the holder has bound.
      if (startAt !== null && servesAfterMs !== null && clock - startAt >= servesAfterMs) return;
      throw new Error("ECONNREFUSED");
    },
    respawnGateway: async () => { events.push("spawn"); },
    showLoadingThenConnect: async () => { events.push("connect"); },
    showUnrecoverableGatewayError: async (_w, _p, kind) => { events.push(`error ${JSON.stringify(kind)}`); },
    waitForPortFree: async () => { events.push("port-free-check"); return portFree; },
    probeGatewayPortBinding: async () => "free",
    probeGatewayPortOwner: async () => "kirocrew",
    fetchHealthInfo: async () => ({ app: "kirocrew", version: "0.11.1.5" }),
    fetchGatewayReadiness: async () => readiness(clock - (startAt ?? clock)),
    snapshotGatewayPortPids: async () => snapshot,
    unverifiedIncumbent: (pids) => incumbentSnapshotBlocksRespawn({ pids, isWindows }),
    waitForIncumbentExit: (pids, _label, options = {}) => waitForProcessExit({
      ...options,
      pids,
      isAlive: (pid) => pid === HOLDER_PID
        && (holderExitsAfterMs === null || clock - startAt < holderExitsAfterMs),
      sleep: (ms) => new Promise((resolve) => global.setTimeout(resolve, ms)),
    }),
    probeLockHolderPid: async () => {
      events.push("lock-probe");
      if (startAt === null) startAt = clock;
      return lockProbe();
    },
  });
  return { events, run: () => reconnect.reconnectOrRespawnAdoptedGateway(window) };
}

const actions = (events) => events.filter((e) => !e.startsWith("log ") && e !== "port-free-check");

describe("liveness recovery into a held gateway.lock (POSIX)", () => {
  // The reported timeline: the holder binds ~80 s after recovery gives up on
  // the adopted gateway. Before the fix the probe was never asked and the app
  // spawned into the held lock.
  it("asks the lock and reconnects to a holder that starts serving, without spawning", async () => {
    const h = harness({
      lockProbe: () => ({ verdict: "captured", pid: HOLDER_PID }),
      servesAfterMs: 80_000,
    });
    await h.run();
    assert.deepEqual(actions(h.events), ["lock-probe", "ownership reused-local", "connect"]);
  });

  // The holder binds while the lock probe runs: the port is held by the time
  // recovery checks it, which is a gateway to reconnect to, not a conflict.
  it("reconnects to a holder that bound the port before the port-free check", async () => {
    const h = harness({
      lockProbe: () => ({ verdict: "captured", pid: HOLDER_PID }),
      servesAfterMs: 0,
      portFree: false,
    });
    await h.run();
    assert.deepEqual(actions(h.events), ["lock-probe", "ownership reused-local", "connect"]);
    assert.ok(!h.events.includes("port-free-check"));
  });

  // The holder exits and a successor answers on the same poll.
  it("reconnects to a successor that answers as the holder exits", async () => {
    const h = harness({
      lockProbe: () => ({ verdict: "captured", pid: HOLDER_PID }),
      servesAfterMs: 5_000,
      holderExitsAfterMs: 5_000,
    });
    await h.run();
    assert.deepEqual(actions(h.events), ["lock-probe", "ownership reused-local", "connect"]);
  });

  // A holder that answers while it shuts down is draining, not starting:
  // recovery waits for it to exit and then spawns, never reconnects to it.
  it("waits out a holder that answers while shutting down, then spawns", async () => {
    const h = harness({
      lockProbe: () => ({ verdict: "captured", pid: HOLDER_PID }),
      servesAfterMs: 0,
      holderExitsAfterMs: 10_000,
      readiness: () => "shutting-down",
    });
    await h.run();
    assert.deepEqual(actions(h.events), ["lock-probe", "spawn", "connect"]);
  });

  it("spawns after the start grace when the holder never serves and never exits", async () => {
    const h = harness({ lockProbe: () => ({ verdict: "captured", pid: HOLDER_PID }) });
    await h.run();
    assert.deepEqual(actions(h.events), ["lock-probe", "spawn", "connect"]);
    assert.ok(h.events.some((e) => e.includes(`${LOCK_HOLDER_START_GRACE_MS}ms`)));
  });

  it("spawns at once when the lock holder exits", async () => {
    const h = harness({ lockProbe: () => ({ verdict: "captured", pid: 4242 }) });
    await h.run();
    assert.deepEqual(actions(h.events), ["lock-probe", "spawn", "connect"]);
  });

  it("spawns at once when the lock is released", async () => {
    const h = harness({ lockProbe: () => ({ verdict: "released", pid: null }) });
    await h.run();
    assert.deepEqual(actions(h.events), ["lock-probe", "spawn", "connect"]);
  });

  // A host whose probe cannot answer (no bundled backend, no lsof) still boots.
  for (const verdict of ["refused", "unverified"]) {
    it(`still spawns when the probe answers ${verdict}`, async () => {
      const h = harness({ lockProbe: () => ({ verdict, pid: null }) });
      await h.run();
      assert.deepEqual(actions(h.events), ["lock-probe", "spawn", "connect"]);
    });
  }

  it("does not ask the lock when the socket names the incumbent", async () => {
    const h = harness({
      lockProbe: () => { throw new Error("must not be asked"); },
      snapshot: [4242],
    });
    await h.run();
    assert.deepEqual(actions(h.events), ["spawn", "connect"]);
  });
});
