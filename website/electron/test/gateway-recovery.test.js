const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  chooseRecoveryStrategy,
  classifyAdoptedGateway,
  GATEWAY_OWNERSHIP_STATES,
  waitForServiceRebind,
  waitForProcessExit,
} = require("../gateway-recovery");

describe("chooseRecoveryStrategy", () => {
  it("respawns when we own the spawned gateway", () => {
    assert.equal(chooseRecoveryStrategy({ gatewayOwnership: "spawned" }), "respawn");
  });

  // Regression guard for the lid-close / network-switch crash: on the reuse
  // path (remote-tunnel setup) the port-holder is our SSH forward, not a
  // backend we spawned. Recovery must NOT kill the port or spawn a local
  // backend — it must wait for the tunnel to heal and reconnect. Returning
  // "respawn" here is exactly the bug that force-killed the tunnel and then quit
  // the app on Retry.
  it("reconnects (never respawns) for a gateway we did not spawn", () => {
    assert.equal(chooseRecoveryStrategy({ gatewayOwnership: "none" }), "reconnect");
  });

  // Ownership defaults to "not ours" when unknown: the safe strategy is the
  // non-destructive reconnect, never a port-kill.
  it("defaults to reconnect when ownership is falsy/unknown", () => {
    assert.equal(chooseRecoveryStrategy({}), "reconnect");
    assert.equal(chooseRecoveryStrategy({ gatewayOwnership: undefined }), "reconnect");
    assert.equal(chooseRecoveryStrategy({ gatewayOwnership: null }), "reconnect");
    assert.equal(chooseRecoveryStrategy({ gatewayOwnership: "garbage" }), "reconnect");
  });

  // Regression guard for the adopted-gateway dead window: a relaunch adopted
  // a same-family local gateway mid-drain; when it died, recovery classified
  // it as "a gateway we did not spawn (remote tunnel)" and waited FOREVER for
  // a comeback that a local process can never make on its own. An adopted
  // LOCAL gateway must get the bounded wait-then-respawn strategy instead.
  it("bounded reconnect-then-respawn for an adopted local same-family gateway", () => {
    assert.equal(chooseRecoveryStrategy({ gatewayOwnership: "reused-local" }), "reconnect-bounded");
  });

  // A service-classified adoption is still an adopted LOCAL gateway for the
  // wedged-recovery fork (the rebind grace lives further down the respawn
  // path); it must never fall into the indefinite external wait.
  it("bounded reconnect-then-respawn for an adopted service-managed gateway", () => {
    assert.equal(chooseRecoveryStrategy({ gatewayOwnership: "reused-service" }), "reconnect-bounded");
  });

  it("covers every declared ownership state (vocabulary is closed)", () => {
    for (const state of GATEWAY_OWNERSHIP_STATES) {
      const strategy = chooseRecoveryStrategy({ gatewayOwnership: state });
      assert.ok(
        ["respawn", "reconnect-bounded", "reconnect"].includes(strategy),
        `state ${state} produced unknown strategy ${strategy}`,
      );
    }
  });
});

describe("classifyAdoptedGateway", () => {
  // Positive identification requires BOTH same-family health AND a local
  // LISTEN owner — anything less stays "none" (never-kill/never-respawn).
  it("classifies a same-family kirocrew-owned holder as reused-local", () => {
    assert.equal(classifyAdoptedGateway({ reason: "same-family", localOwner: "kirocrew" }), "reused-local");
  });

  it("classifies a same-family service-owned holder as reused-service", () => {
    assert.equal(classifyAdoptedGateway({ reason: "same-family", localOwner: "service" }), "reused-service");
  });

  it("stays none for a tunnel / unidentified holder (no positive owner)", () => {
    assert.equal(classifyAdoptedGateway({ reason: "same-family", localOwner: "none" }), "none");
    assert.equal(classifyAdoptedGateway({ reason: "same-family", localOwner: "other" }), "none");
    assert.equal(classifyAdoptedGateway({ reason: "same-family", localOwner: undefined }), "none");
  });

  it("stays none without the same-family health identification", () => {
    assert.equal(classifyAdoptedGateway({ reason: "healthy", localOwner: "kirocrew" }), "none");
    assert.equal(classifyAdoptedGateway({ reason: undefined, localOwner: "service" }), "none");
  });

  // The classifier's output must feed chooseRecoveryStrategy losslessly: a
  // positively-identified local adoption gets the bounded strategy, an
  // unidentified one keeps the indefinite external reconnect.
  it("composes with chooseRecoveryStrategy end to end", () => {
    const local = classifyAdoptedGateway({ reason: "same-family", localOwner: "kirocrew" });
    assert.equal(chooseRecoveryStrategy({ gatewayOwnership: local }), "reconnect-bounded");
    const external = classifyAdoptedGateway({ reason: "same-family", localOwner: "none" });
    assert.equal(chooseRecoveryStrategy({ gatewayOwnership: external }), "reconnect");
  });
});

describe("waitForServiceRebind", () => {
  const instantSleep = () => Promise.resolve();

  // A service-managed holder that released its port mid-restart is respawned
  // by its manager (launchd KeepAlive / systemd Restart=). Spawning locally in
  // that window races the manager for the bind — one side exits EADDRINUSE —
  // so a rebind within the grace must be adopted, never raced.
  it("reports rebound as soon as the port is bound again", async () => {
    let probes = 0;
    const verdict = await waitForServiceRebind({
      isPortBound: async () => ++probes >= 3, // rebinds on the third probe
      sleep: instantSleep,
      graceMs: 10_000,
    });
    assert.equal(verdict, "rebound");
    assert.equal(probes, 3);
  });

  // The service classification also matches orphans (a gateway reparented to
  // init has PPID 1 but no manager), so the grace must EXPIRE into a local
  // spawn — a blanket "never respawn after a service holder" would recreate
  // the adopted-gateway dead window for orphan exits.
  it("reports spawn when the grace expires with the port still free", async () => {
    const t0 = Date.now();
    let now = t0;
    const realNow = Date.now;
    Date.now = () => now;
    try {
      const verdict = await waitForServiceRebind({
        isPortBound: async () => false,
        sleep: async () => { now += 1_000; },
        graceMs: 5_000,
      });
      assert.equal(verdict, "spawn");
    } finally {
      Date.now = realNow;
    }
  });

  // An immediate rebind (manager beat our first probe) short-circuits without
  // sleeping at all.
  it("adopts an already-rebound port without waiting", async () => {
    let slept = false;
    const verdict = await waitForServiceRebind({
      isPortBound: async () => true,
      sleep: async () => { slept = true; },
      graceMs: 10_000,
    });
    assert.equal(verdict, "rebound");
    assert.equal(slept, false);
  });
});

describe("waitForProcessExit", () => {
  // A graceful stop releases the LISTEN socket before the process exits, and
  // the gateway.lock flock is held for the process lifetime. Spawning on
  // port-free alone gets the replacement refused by the singleton lock; these
  // tests lock in the wait-for-exit gate that closes that window.
  it("returns exited once every watched pid is dead", async () => {
    const alive = new Set([111, 222]);
    let polls = 0;
    const verdict = await waitForProcessExit({
      pids: [111, 222],
      isAlive: (p) => alive.has(p),
      sleep: async () => { polls += 1; if (polls === 1) alive.delete(111); if (polls === 2) alive.delete(222); },
      timeoutMs: 60_000,
    });
    assert.equal(verdict, "exited");
  });

  it("returns timeout when a pid outlives the grace (spawn proceeds, lock refusal surfaces honestly)", async () => {
    let now = Date.now();
    const realNow = Date.now;
    Date.now = () => now;
    try {
      const verdict = await waitForProcessExit({
        pids: [111],
        isAlive: () => true,
        sleep: async () => { now += 1_000; },
        timeoutMs: 5_000,
      });
      assert.equal(verdict, "timeout");
    } finally {
      Date.now = realNow;
    }
  });

  // Empty/invalid pid sets (probe failed, Windows without lsof) degrade to a
  // no-op — same behavior as before this gate existed, never a hang.
  it("degrades to exited immediately with no watchable pids", async () => {
    let slept = false;
    for (const pids of [[], null, undefined, [0, -3, NaN]]) {
      const verdict = await waitForProcessExit({
        pids,
        isAlive: () => { throw new Error("must not be called"); },
        sleep: async () => { slept = true; },
      });
      assert.equal(verdict, "exited");
    }
    assert.equal(slept, false);
  });
});
