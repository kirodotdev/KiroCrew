const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {
  chooseRecoveryStrategy,
  classifyAdoptedGateway,
  GATEWAY_OWNERSHIP_STATES,
  waitForServiceRebind,
  waitForProcessExit,
  snapshotPortPids,
  incumbentSnapshotBlocksRespawn,
  snapshotIncumbentForRespawn,
  recoverIncumbentWithBackoff,
  INCUMBENT_RECOVERY_BACKOFF_MS,
  INCUMBENT_RETRY_STATUS,
  unrecoverableGatewayDialog,
  shouldReresolveBackend,
  isStaleBundleSignal,
  STALE_ASSET_EXIT_CODE,
  gatewayCliInvocation,
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

  // Empty/invalid pid sets (for example, a failed listener probe) degrade to a
  // no-op rather than hanging recovery.
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

describe("snapshotPortPids", () => {
  it("uses the Windows listener probe so recovery can wait for gateway.lock", async () => {
    const calls = [];
    const pids = await snapshotPortPids({
      port: 5476,
      isWindows: true,
      getWindowsPids: async (port) => {
        calls.push(["windows", port]);
        return [4242];
      },
      getPosixPids: async (port) => {
        calls.push(["posix", port]);
        return [9999];
      },
    });
    assert.deepEqual(pids, [4242]);
    assert.deepEqual(calls, [["windows", 5476]]);
  });

  it("returns unknown when the selected probe fails or misses the listener", async () => {
    const failed = await snapshotPortPids({
      port: 5476,
      isWindows: true,
      getWindowsPids: async () => { throw new Error("netstat unavailable"); },
      getPosixPids: async () => [9999],
    });
    const missed = await snapshotPortPids({
      port: 5476,
      isWindows: true,
      getWindowsPids: async () => [],
      getPosixPids: async () => [9999],
    });
    assert.equal(failed, null);
    assert.equal(missed, null);
  });
});

describe("unrecoverableGatewayDialog", () => {
  it("offers a real quit action for an unkillable primary gateway", () => {
    const model = unrecoverableGatewayDialog({
      port: 5476,
      isPrimaryWindow: true,
    });
    assert.equal(model.title, "Kiro Crew: backend stuck on port 5476");
    assert.equal(model.primaryAction, "quit");
    assert.equal(model.primaryLabel, "Quit Kiro Crew");
    assert.equal(model.showQuitButton, false);
    assert.equal(model.portConflict, false);
    assert.match(model.message, /Restart your computer/);
  });

  it("tells a probe-failure user to reopen before restarting", () => {
    const model = unrecoverableGatewayDialog({
      port: 5476,
      probeFailed: true,
      isPrimaryWindow: false,
    });
    assert.equal(model.title, "Kiro Crew: can't verify what's using port 5476");
    assert.equal(model.primaryAction, "quit");
    assert.equal(model.primaryLabel, "Close");
    assert.equal(model.showQuitButton, false);
    assert.match(model.message, /Quit and reopen Kiro Crew to try again/);
    assert.match(model.message, /If the port is still blocked, restart your computer/);
  });

  it("tells a user to quit an unowned process that still holds the port", () => {
    const model = unrecoverableGatewayDialog({
      port: 5476,
      variant: "held",
      isPrimaryWindow: true,
    });
    assert.equal(model.title, "Kiro Crew: port 5476 is in use");
    assert.equal(model.primaryAction, "quit");
    assert.equal(model.primaryLabel, "Quit Kiro Crew");
    assert.equal(model.showQuitButton, false);
    assert.match(model.message, /Quit the process using port 5476/);
    assert.doesNotMatch(model.message, /Restart your computer/);
  });
});

describe("incumbentSnapshotBlocksRespawn", () => {
  it("refuses an automatic respawn when the Windows probe named nothing", () => {
    assert.equal(incumbentSnapshotBlocksRespawn({ pids: null, isWindows: true }), true);
  });

  it("still boots a POSIX host whose lsof is missing or blocked", () => {
    assert.equal(incumbentSnapshotBlocksRespawn({ pids: null, isWindows: false }), false);
  });

  it("never blocks when the incumbent was actually captured", () => {
    for (const isWindows of [true, false]) {
      assert.equal(incumbentSnapshotBlocksRespawn({ pids: [4242], isWindows }), false);
    }
  });
});

// A transient PID-capture failure during an adopted
// gateway's drain must NOT terminate recovery for good. The helper takes ONE
// socket lookup; when it names no PID (a draining gateway that has shed its
// socket but still holds gateway.lock), it falls back to the socket-independent
// lock-holder probe, which names the incumbent (so the caller can wait for it
// to exit and release gateway.lock), reports the lock already released, or waits
// for a held-but-unnameable lock to release. "Port free" is NOT a success
// signal — a draining gateway frees its socket while still holding the lock.
describe("snapshotIncumbentForRespawn", () => {
  // A controllable clock + instant sleep so the held-wait budget is tested
  // without real time.
  const clockHarness = () => {
    let now = 0;
    const realDateNow = Date.now;
    Date.now = () => now;
    const slept = [];
    const sleep = async (ms) => { slept.push(ms); now += ms; };
    return {
      sleep,
      slept,
      advance: (ms) => { now += ms; },
      restore: () => { Date.now = realDateNow; },
    };
  };

  // On Windows a null snapshot is unusable; a named PID is usable.
  const blocksRespawn = (pids) => incumbentSnapshotBlocksRespawn({ pids, isWindows: true });

  it("uses the socket lookup directly when it names the PID — no lock probe, no sleep", async () => {
    const h = clockHarness();
    try {
      let probed = 0;
      const result = await snapshotIncumbentForRespawn({
        snapshot: async () => [4242],
        blocksRespawn,
        sleep: h.sleep,
        lockHolderProbe: async () => { probed += 1; return { verdict: "captured", pid: 1 }; },
      });
      assert.deepEqual(result, { verdict: "captured", pids: [4242], via: "snapshot" });
      assert.deepEqual(h.slept, [], "no sleep when the socket lookup works");
      assert.equal(probed, 0, "the lock probe is a fallback, not consulted when the socket answers");
    } finally {
      h.restore();
    }
  });

  it("takes exactly ONE socket lookup, then goes straight to the lock (no retry loop)", async () => {
    const h = clockHarness();
    try {
      let snaps = 0;
      let probed = 0;
      const result = await snapshotIncumbentForRespawn({
        snapshot: async () => { snaps += 1; return null; },
        blocksRespawn,
        sleep: h.sleep,
        lockHolderProbe: async () => { probed += 1; return { verdict: "captured", pid: 9191 }; },
      });
      assert.equal(snaps, 1, "the socket is looked up once, not retried");
      assert.equal(probed, 1, "the lock probe is consulted immediately after the single lookup");
      assert.equal(result.verdict, "captured");
      assert.equal(result.via, "lock");
      assert.deepEqual(result.pids, [9191]);
    } finally {
      h.restore();
    }
  });

  it("without a lock probe, a socket lookup that names no one is unverified", async () => {
    const h = clockHarness();
    try {
      let snaps = 0;
      const result = await snapshotIncumbentForRespawn({
        snapshot: async () => { snaps += 1; return null; },
        blocksRespawn,
        sleep: h.sleep,
      });
      assert.equal(result.verdict, "unverified");
      assert.equal(result.pids, null);
      assert.equal(snaps, 1, "one lookup, no retry");
      assert.deepEqual(h.slept, [], "nothing to wait on without a lock probe");
    } finally {
      h.restore();
    }
  });

  // The lock probe, and with it the held wait, runs only where a lookup that
  // names no one blocks a respawn: Windows. On a POSIX host the replacement's
  // own lock refusal arbitrates instead.
  it("never consults the lock probe on a POSIX host, where an empty lookup does not block", async () => {
    let probed = 0;
    const result = await snapshotIncumbentForRespawn({
      snapshot: async () => null,
      blocksRespawn: (pids) => incumbentSnapshotBlocksRespawn({ pids, isWindows: false }),
      sleep: async () => { throw new Error("a POSIX host waits on no held lock"); },
      lockHolderProbe: async () => { probed += 1; return { verdict: "held", pid: null }; },
    });
    assert.deepEqual(result, { verdict: "captured", pids: null, via: "snapshot" });
    assert.equal(probed, 0, "the lock probe is not consulted off Windows");
  });

  // The hard case: the draining gateway released its LISTEN socket but still
  // holds gateway.lock, so the socket lookup NEVER names it. The lock-holder
  // probe (socket-independent) is what actually fixes the bug.
  it("falls back to the lock-holder probe when the socket lookup names no incumbent", async () => {
    const h = clockHarness();
    try {
      let probed = 0;
      const result = await snapshotIncumbentForRespawn({
        snapshot: async () => null, // socket released
        blocksRespawn,
        sleep: h.sleep,
        lockHolderProbe: async () => { probed += 1; return { verdict: "captured", pid: 9191 }; },
      });
      assert.equal(result.verdict, "captured");
      assert.equal(result.via, "lock", "the identity came from the lock, not the socket");
      assert.deepEqual(result.pids, [9191], "the lock-named pid is returned so the caller can outwait it");
      assert.equal(probed, 1, "the lock probe is consulted exactly once");
    } finally {
      h.restore();
    }
  });

  it("reports 'released' when the lock probe says the lock is released — safe to spawn now", async () => {
    const h = clockHarness();
    try {
      const result = await snapshotIncumbentForRespawn({
        snapshot: async () => null,
        blocksRespawn,
        sleep: h.sleep,
        lockHolderProbe: async () => ({ verdict: "released", pid: null }),
      });
      assert.equal(result.verdict, "released");
      assert.equal(result.via, "lock");
      assert.equal(result.pids, null, "lock-released means nothing to wait on");
    } finally {
      h.restore();
    }
  });

  it("stays 'unverified' when the lock probe gives no answer — the caller retries later", async () => {
    const h = clockHarness();
    try {
      const result = await snapshotIncumbentForRespawn({
        snapshot: async () => null,
        blocksRespawn,
        sleep: h.sleep,
        lockHolderProbe: async () => ({ verdict: "unverified", pid: null }),
      });
      assert.equal(result.verdict, "unverified");
      assert.equal(result.via, "lock");
      assert.equal(result.pids, null);
    } finally {
      h.restore();
    }
  });

  // No bundled backend, or an indeterminate lock: the next attempt gets the
  // same answer, so the pass hands back "refused" at once and waits on nothing.
  it("reports 'refused' at once when the lock probe refuses — nothing to wait for", async () => {
    const h = clockHarness();
    try {
      let probes = 0;
      const result = await snapshotIncumbentForRespawn({
        snapshot: async () => null,
        blocksRespawn,
        sleep: h.sleep,
        lockHolderProbe: async () => { probes += 1; return { verdict: "refused", pid: null }; },
      });
      assert.equal(result.verdict, "refused");
      assert.equal(result.via, "lock");
      assert.equal(result.pids, null);
      assert.equal(probes, 1, "a refusal is not polled");
      assert.deepEqual(h.slept, [], "a refusal waits on nothing");
    } finally {
      h.restore();
    }
  });

  // The Windows reality: the draining gateway holds the lock but its pid
  // is unreadable under a mandatory lock, so the probe can only say "held". The
  // helper must WAIT for the lock to release (poll until released), not give up —
  // this is what actually fixes the outage on the reported host.
  it("waits on a held-but-unnameable lock and spawns once it is released (the Windows path)", async () => {
    const h = clockHarness();
    try {
      // held, held, then released: the draining gateway finishes teardown.
      const answers = [
        { verdict: "held", pid: null },
        { verdict: "held", pid: null },
        { verdict: "released", pid: null },
      ];
      let i = 0;
      const result = await snapshotIncumbentForRespawn({
        snapshot: async () => null,
        blocksRespawn,
        sleep: h.sleep,
        lockHeldBudgetMs: 60_000,
        lockHeldPollMs: 2_000,
        lockHolderProbe: async () => answers[i++],
      });
      assert.equal(result.verdict, "released", "once the lock is released it is safe to spawn");
      assert.equal(result.via, "lock");
      assert.equal(i, 3, "polled the lock until it reported released");
    } finally {
      h.restore();
    }
  });

  it("ends one pass unverified when a held lock never releases within the held budget", async () => {
    const h = clockHarness();
    try {
      let probes = 0;
      const result = await snapshotIncumbentForRespawn({
        snapshot: async () => null,
        blocksRespawn,
        sleep: h.sleep,
        lockHeldBudgetMs: 10_000,
        lockHeldPollMs: 2_000,
        lockHolderProbe: async () => { probes += 1; return { verdict: "held", pid: null }; },
      });
      assert.equal(result.verdict, "unverified", "a pass that outlasts the budget hands back to the caller's backoff");
      assert.equal(result.via, "lock");
      assert.ok(probes > 1, "polled the held lock across the budget before ending the pass");
    } finally {
      h.restore();
    }
  });
});

describe("recoverIncumbentWithBackoff", () => {
  const recordingSleep = () => {
    const slept = [];
    return { slept, sleep: async (ms) => { slept.push(ms); } };
  };

  it("uses a capped 30s / 60s / 120s schedule that stays at 120s", () => {
    assert.deepEqual([...INCUMBENT_RECOVERY_BACKOFF_MS], [30_000, 60_000, 120_000]);
    assert.ok(Object.isFrozen(INCUMBENT_RECOVERY_BACKOFF_MS));
  });

  it("its splash status names no delay, so it never reads as a frozen countdown", () => {
    assert.match(INCUMBENT_RETRY_STATUS, /retrying automatically/);
    assert.doesNotMatch(INCUMBENT_RETRY_STATUS, /\d/);
  });

  it("returns the first pass as-is when the incumbent is named — no backoff", async () => {
    const s = recordingSleep();
    const result = await recoverIncumbentWithBackoff({
      capture: async () => ({ verdict: "captured", pids: [4242], via: "snapshot" }),
      sleep: s.sleep,
    });
    assert.deepEqual(result, { verdict: "captured", pids: [4242], via: "snapshot", attempts: 1 });
    assert.deepEqual(s.slept, []);
  });

  // A gateway stuck holding the lock past every bounded wait must not end
  // recovery: each pass runs the real held-lock wait, then the loop backs off
  // and tries again, and the lock freeing later is what recovers it.
  it("keeps retrying a lock stuck held past the budget, then recovers once the lock frees", async () => {
    let now = 0;
    const realDateNow = Date.now;
    Date.now = () => now;
    try {
      const passSleeps = [];
      const backoffSleeps = [];
      let probes = 0;
      const releaseAfterProbes = 40; // well past several held budgets
      const waiting = [];
      const result = await recoverIncumbentWithBackoff({
        capture: () => snapshotIncumbentForRespawn({
          snapshot: async () => null,
          blocksRespawn: (pids) => incumbentSnapshotBlocksRespawn({ pids, isWindows: true }),
          sleep: async (ms) => { passSleeps.push(ms); now += ms; },
          lockHeldBudgetMs: 10_000,
          lockHeldPollMs: 2_000,
          lockHolderProbe: async () => {
            probes += 1;
            return probes >= releaseAfterProbes
              ? { verdict: "released", pid: null }
              : { verdict: "held", pid: null };
          },
        }),
        sleep: async (ms) => { backoffSleeps.push(ms); now += ms; },
        onWaiting: (delayMs, attempt) => waiting.push([delayMs, attempt]),
      });
      assert.equal(result.verdict, "released", "the freed lock recovers instead of a terminal stop");
      assert.ok(result.attempts >= 5, `retried across several stuck passes (attempts=${result.attempts})`);
      assert.deepEqual(
        backoffSleeps,
        [30_000, 60_000, 120_000, ...Array(result.attempts - 4).fill(120_000)],
        "the backoff grows to 120s and stays there",
      );
      assert.deepEqual(waiting.map(([d]) => d), backoffSleeps, "the status line is told about every wait");
      assert.ok(passSleeps.length > 0, "each pass still ran the bounded held-lock wait");
    } finally {
      Date.now = realDateNow;
    }
  });

  it("keeps retrying a lock probe that gives no answer and recovers when it later names the holder", async () => {
    const s = recordingSleep();
    const answers = [
      { verdict: "unverified", pids: null, via: "lock" },
      { verdict: "unverified", pids: null, via: "lock" },
      { verdict: "captured", pids: [9191], via: "lock" },
    ];
    let i = 0;
    const result = await recoverIncumbentWithBackoff({
      capture: async () => answers[i++],
      sleep: s.sleep,
    });
    assert.deepEqual(result, { verdict: "captured", pids: [9191], via: "lock", attempts: 3 });
    assert.deepEqual(s.slept, [30_000, 60_000]);
  });

  // A refusal cannot heal by waiting, so it must reach the caller's terminal
  // error on the first pass instead of the "still exiting" status line.
  it("returns a refused pass at once, with no backoff and no retry status", async () => {
    const s = recordingSleep();
    const waiting = [];
    let captures = 0;
    const result = await recoverIncumbentWithBackoff({
      capture: async () => { captures += 1; return { verdict: "refused", pids: null, via: "lock" }; },
      sleep: s.sleep,
      onWaiting: (delayMs) => waiting.push(delayMs),
    });
    assert.deepEqual(result, { verdict: "refused", pids: null, via: "lock", attempts: 1 });
    assert.equal(captures, 1);
    assert.deepEqual(s.slept, []);
    assert.deepEqual(waiting, [], "the retry status line is never shown for a refusal");
  });

  it("ends on a refusal that follows transient misses", async () => {
    const s = recordingSleep();
    const answers = [
      { verdict: "unverified", pids: null, via: "lock" },
      { verdict: "refused", pids: null, via: "lock" },
    ];
    let i = 0;
    const result = await recoverIncumbentWithBackoff({
      capture: async () => answers[i++],
      sleep: s.sleep,
    });
    assert.equal(result.verdict, "refused");
    assert.equal(result.attempts, 2);
    assert.deepEqual(s.slept, [30_000], "only the transient miss was backed off");
  });

  it("stops quietly once the caller cancels (window closed or app quitting)", async () => {
    const s = recordingSleep();
    let captures = 0;
    const result = await recoverIncumbentWithBackoff({
      capture: async () => { captures += 1; return { verdict: "unverified", pids: null, via: "lock" }; },
      sleep: s.sleep,
      cancelled: () => s.slept.length >= 2,
    });
    assert.equal(result.verdict, "cancelled");
    assert.equal(result.pids, null);
    assert.equal(captures, 2, "no further capture after cancellation");
    assert.deepEqual(s.slept, [30_000, 60_000]);
  });

  it("does not start a capture when already cancelled", async () => {
    let captures = 0;
    const result = await recoverIncumbentWithBackoff({
      capture: async () => { captures += 1; return { verdict: "captured", pids: [1], via: "snapshot" }; },
      sleep: async () => {},
      cancelled: () => true,
    });
    assert.deepEqual(result, { verdict: "cancelled", pids: null, attempts: 0 });
    assert.equal(captures, 0);
  });
});

describe("shouldReresolveBackend", () => {
  const mac = { isMac: true, bundled: true };

  it("pins the watchdog's exit status so the two sides cannot drift apart", () => {
    const backend = fs.readFileSync(
      path.join(__dirname, "..", "..", "..", "src", "kiro_crew", "dashboard", "stale_asset_watchdog.py"),
      "utf8",
    );
    assert.match(backend, new RegExp(`^STALE_ASSET_EXIT_CODE = ${STALE_ASSET_EXIT_CODE}$`, "m"));
  });

  it("re-resolves once when the bundled gateway exits with the stale-asset status", () => {
    assert.equal(
      shouldReresolveBackend({ ...mac, exitCode: STALE_ASSET_EXIT_CODE, attempts: 0 }),
      "reresolve",
    );
  });

  it("re-resolves once when the bundled binary vanished between probe and spawn", () => {
    assert.equal(
      shouldReresolveBackend({ ...mac, spawnErrorCode: "ENOENT", attempts: 0 }),
      "reresolve",
    );
  });

  it("relaunches the app when the re-resolved child is stale again and the app can still be re-executed", () => {
    assert.equal(
      shouldReresolveBackend({ ...mac, exitCode: STALE_ASSET_EXIT_CODE, attempts: 1, relaunchTargetExists: true }),
      "relaunch",
    );
    assert.equal(
      shouldReresolveBackend({ ...mac, spawnErrorCode: "ENOENT", attempts: 1, relaunchTargetExists: true }),
      "relaunch",
    );
  });

  // app.relaunch() returns void and only schedules the re-exec for exit time,
  // so a pruned bundle can only be caught before the call: when the caller's
  // probe of its own executable came back empty, exiting would strand the
  // user with no app and no dialog.
  it("surfaces the failure instead of relaunching when this app's executable is gone", () => {
    for (const signal of [{ exitCode: STALE_ASSET_EXIT_CODE }, { spawnErrorCode: "ENOENT" }]) {
      assert.equal(
        shouldReresolveBackend({ ...mac, ...signal, attempts: 1, relaunchTargetExists: false }),
        "none",
      );
      assert.equal(
        shouldReresolveBackend({ isMac: true, bundled: false, ...signal, attempts: 1, relaunchTargetExists: false }),
        "none",
      );
    }
  });

  it("defaults to not relaunching when the caller never probed the relaunch target", () => {
    assert.equal(
      shouldReresolveBackend({ ...mac, exitCode: STALE_ASSET_EXIT_CODE, attempts: 1 }),
      "none",
    );
  });

  it("does not need the relaunch target for the first, in-place re-resolve", () => {
    assert.equal(
      shouldReresolveBackend({ ...mac, exitCode: STALE_ASSET_EXIT_CODE, attempts: 0, relaunchTargetExists: false }),
      "reresolve",
    );
  });

  // After a prune the re-probe falls through to a PATH lookup, so the child
  // under judgment on the second signal is no longer the bundled one. That is
  // still the same incident, and the only move left is a relaunch.
  it("relaunches on the second signal even when the re-probe found no bundled binary", () => {
    assert.equal(
      shouldReresolveBackend({
        isMac: true, bundled: false, spawnErrorCode: "ENOENT", attempts: 1, relaunchTargetExists: true,
      }),
      "relaunch",
    );
  });

  it("never spends more than the single re-resolve before relaunching", () => {
    for (const attempts of [2, 5]) {
      assert.equal(
        shouldReresolveBackend({ ...mac, exitCode: STALE_ASSET_EXIT_CODE, attempts, relaunchTargetExists: true }),
        "relaunch",
      );
    }
  });

  it("leaves Linux on its service manager's recovery path", () => {
    for (const signal of [{ exitCode: STALE_ASSET_EXIT_CODE }, { spawnErrorCode: "ENOENT" }]) {
      assert.equal(shouldReresolveBackend({ isMac: false, bundled: true, ...signal }), "none");
      assert.equal(
        shouldReresolveBackend({
          isMac: false, bundled: true, attempts: 1, relaunchTargetExists: true, ...signal,
        }),
        "none",
      );
    }
  });

  // An install manager can prune the running version's directory on Windows
  // without stopping the app, so the same stale signal must arm the same
  // recovery there.
  it("re-resolves, then relaunches, on Windows exactly as on macOS", () => {
    const win = { isMac: false, isWindows: true, bundled: true };
    for (const signal of [{ exitCode: STALE_ASSET_EXIT_CODE }, { spawnErrorCode: "ENOENT" }]) {
      assert.equal(shouldReresolveBackend({ ...win, ...signal, attempts: 0 }), "reresolve");
      assert.equal(
        shouldReresolveBackend({ ...win, ...signal, attempts: 1, relaunchTargetExists: true }),
        "relaunch",
      );
      assert.equal(
        shouldReresolveBackend({ ...win, ...signal, attempts: 1, relaunchTargetExists: false }),
        "none",
      );
    }
    assert.equal(shouldReresolveBackend({ ...win, exitCode: 0 }), "none");
    assert.equal(
      shouldReresolveBackend({ ...win, exitCode: STALE_ASSET_EXIT_CODE, installingUpdate: true }),
      "none",
    );
  });

  // A dev checkout with no kirocrew anywhere spawns the bare PATH name and gets
  // ENOENT on every boot; treating that as stale would relaunch the app forever.
  it("ignores a first signal from a PATH or source-checkout gateway", () => {
    assert.equal(
      shouldReresolveBackend({ isMac: true, bundled: false, spawnErrorCode: "ENOENT" }),
      "none",
    );
    assert.equal(
      shouldReresolveBackend({ isMac: true, bundled: false, exitCode: STALE_ASSET_EXIT_CODE }),
      "none",
    );
  });

  it("ignores every other exit status and spawn error", () => {
    for (const exitCode of [0, 1, 2, 74, 76, 127, 137, null]) {
      assert.equal(shouldReresolveBackend({ ...mac, exitCode }), "none", `exit ${exitCode}`);
    }
    for (const spawnErrorCode of ["EACCES", "EPERM", "EMFILE", ""]) {
      assert.equal(shouldReresolveBackend({ ...mac, spawnErrorCode }), "none", spawnErrorCode);
    }
  });

  it("stands down while the app quits or the updater owns the bundle", () => {
    assert.equal(
      shouldReresolveBackend({ ...mac, exitCode: STALE_ASSET_EXIT_CODE, quitting: true }),
      "none",
    );
    assert.equal(
      shouldReresolveBackend({ ...mac, exitCode: STALE_ASSET_EXIT_CODE, installingUpdate: true }),
      "none",
    );
    assert.equal(
      shouldReresolveBackend({
        ...mac, spawnErrorCode: "ENOENT", attempts: 1, installingUpdate: true, relaunchTargetExists: true,
      }),
      "none",
    );
  });
});

describe("isStaleBundleSignal", () => {
  it("recognises the watchdog status and a vanished binary, nothing else", () => {
    assert.equal(isStaleBundleSignal({ exitCode: STALE_ASSET_EXIT_CODE }), true);
    assert.equal(isStaleBundleSignal({ spawnErrorCode: "ENOENT" }), true);
    for (const exitCode of [0, 1, 74, 76, null]) {
      assert.equal(isStaleBundleSignal({ exitCode }), false, `exit ${exitCode}`);
    }
    for (const spawnErrorCode of ["EACCES", "EPERM", ""]) {
      assert.equal(isStaleBundleSignal({ spawnErrorCode }), false, spawnErrorCode);
    }
    assert.equal(isStaleBundleSignal({}), false);
  });
});

describe("gatewayCliInvocation", () => {
  it("unwraps a Windows kirocrew.cmd to the bundled python.exe (win32 path rules)", () => {
    const cmd = "C:\\App\\resources\\backend-dist\\kirocrew-backend\\bin\\kirocrew.cmd";
    assert.deepEqual(gatewayCliInvocation(cmd, ["gateway-pid"], path.win32), {
      bin: "C:\\App\\resources\\backend-dist\\kirocrew-backend\\python.exe",
      args: ["-s", "-P", "-m", "kiro_crew", "gateway-pid"],
    });
  });

  it("passes any other bin through unchanged", () => {
    assert.deepEqual(gatewayCliInvocation("/opt/kc/bin/kirocrew", ["gateway-pid"]), {
      bin: "/opt/kc/bin/kirocrew",
      args: ["gateway-pid"],
    });
  });
});
