const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  chooseRecoveryStrategy,
  unrecoverableGatewayDialog,
} = require("../gateway-recovery");

describe("chooseRecoveryStrategy", () => {
  it("respawns when we own the spawned gateway", () => {
    assert.equal(chooseRecoveryStrategy({ weSpawnedGateway: true }), "respawn");
  });

  // Regression guard for the lid-close / network-switch crash: on the reuse
  // path (remote-tunnel setup) the port-holder is our SSH forward, not a
  // backend we spawned. Recovery must NOT kill the port or spawn a local
  // backend — it must wait for the tunnel to heal and reconnect. Returning
  // "respawn" here is exactly the bug that force-killed the tunnel and then quit
  // the app on Retry.
  it("reconnects (never respawns) for a gateway we did not spawn", () => {
    assert.equal(chooseRecoveryStrategy({ weSpawnedGateway: false }), "reconnect");
  });

  // Ownership defaults to "not ours" when unknown: the safe strategy is the
  // non-destructive reconnect, never a port-kill.
  it("defaults to reconnect when ownership is falsy/unknown", () => {
    assert.equal(chooseRecoveryStrategy({}), "reconnect");
    assert.equal(chooseRecoveryStrategy({ weSpawnedGateway: undefined }), "reconnect");
    assert.equal(chooseRecoveryStrategy({ weSpawnedGateway: null }), "reconnect");
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
});
