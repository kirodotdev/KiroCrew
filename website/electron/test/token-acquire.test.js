const { describe, it } = require("node:test");
const assert = require("node:assert/strict");
const {
  TOKEN_MINT_MAX_RETRIES,
  shouldRetryLocalTokenMint,
  tokenMintRetryDelayMs,
} = require("../token-acquire");

describe("shouldRetryLocalTokenMint", () => {
  // Regression guard for the spurious token prompt: a local gateway we just
  // restarted 403s briefly while its regenerated .local_secret settles. That is
  // self-healing, so our own gateway must be retried, not prompted.
  it("retries our own gateway while attempts remain", () => {
    for (let attempt = 0; attempt < TOKEN_MINT_MAX_RETRIES; attempt++) {
      assert.equal(shouldRetryLocalTokenMint({ kind: "local", attempt }), true);
    }
  });

  it("stops retrying once the attempt budget is spent (then prompts)", () => {
    assert.equal(shouldRetryLocalTokenMint({ kind: "local", attempt: TOKEN_MINT_MAX_RETRIES }), false);
  });

  // A foreign gateway (SSH forward / external) signs with a secret on the remote
  // host that the local CLI cannot read — retrying the local mint can never
  // succeed, so it must prompt immediately, never spin.
  it("never retries a foreign gateway, even on the first attempt", () => {
    assert.equal(shouldRetryLocalTokenMint({ kind: "foreign", attempt: 0 }), false);
  });

  // Unknown ownership (probe couldn't determine an owner) is treated as
  // same-host / ours — retry rather than prompt, matching the token page's
  // hedged default.
  it("retries an unknown/undetermined kind (treated as ours)", () => {
    assert.equal(shouldRetryLocalTokenMint({ kind: "unknown", attempt: 0 }), true);
    assert.equal(shouldRetryLocalTokenMint({ kind: "", attempt: 0 }), true);
  });
});

describe("tokenMintRetryDelayMs", () => {
  it("backs off exponentially and caps at 2s", () => {
    assert.equal(tokenMintRetryDelayMs(0), 500);
    assert.equal(tokenMintRetryDelayMs(1), 1000);
    assert.equal(tokenMintRetryDelayMs(2), 2000);
    assert.equal(tokenMintRetryDelayMs(3), 2000); // capped
    assert.equal(tokenMintRetryDelayMs(10), 2000); // still capped
  });
});
