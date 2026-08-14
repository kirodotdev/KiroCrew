/**
 * instanceGate — "can that instance host the pet".
 *
 * Two rules worth pinning: the `/api/apps` payload shape variance, and what a
 * NON-ANSWER means. The second is the load-bearing one — reading a timeout as
 * "disabled" would move the pet (and therefore its appearance and chat history)
 * on a hiccup.
 */
const test = require("node:test");
const assert = require("node:assert");

const { parseMochiEnabled, enabledOrTrust, hostDisabledMeansTeardown } = require("../instanceGate");

test("both payload shapes are understood", () => {
  // {apps: [...]} — the documented shape.
  assert.strictEqual(parseMochiEnabled({ apps: [{ name: "mochi", enabled: true }] }), true);
  assert.strictEqual(parseMochiEnabled({ apps: [{ name: "mochi", enabled: false }] }), false);
  // A bare array — the shape that has caught this project out before.
  assert.strictEqual(parseMochiEnabled([{ name: "mochi", enabled: true }]), true);
  assert.strictEqual(parseMochiEnabled([{ name: "mochi", enabled: false }]), false);
});

test("answered-but-Mochi-absent is a real 'no', not a non-answer", () => {
  // The gateway replied; Mochi simply is not installed there. It cannot host the
  // pet, and that is a fact, not a failure to learn one.
  assert.strictEqual(parseMochiEnabled({ apps: [{ name: "issue-radar", enabled: true }] }), false);
  assert.strictEqual(parseMochiEnabled({ apps: [] }), false);
  assert.strictEqual(parseMochiEnabled([]), false);
});

test("an ununderstandable payload is a NON-answer (null), never false", () => {
  for (const bad of [null, undefined, {}, { apps: "nope" }, 42, "text", { apps: null }]) {
    assert.strictEqual(parseMochiEnabled(bad), null, `expected null for ${JSON.stringify(bad)}`);
  }
});

test("missing enabled flag reads as disabled, not as enabled", () => {
  // Deny the ambiguous case: an app row with no flag must not turn the pet loose
  // on an instance we have no positive confirmation for.
  assert.strictEqual(parseMochiEnabled({ apps: [{ name: "mochi" }] }), false);
});

test("a non-answer is TRUSTED, so one slow reply cannot move the pet", () => {
  assert.strictEqual(enabledOrTrust(null), true);
});

test("a real answer is passed through untouched", () => {
  assert.strictEqual(enabledOrTrust(true), true);
  assert.strictEqual(enabledOrTrust(false), false);
});

// ── hostDisabledMeansTeardown ──────────────────────────────────────────────────
//
// THE BUG THIS ENCODES: disabling Mochi on the crew serving this window removed a
// pet that a DIFFERENT crew was still serving, with its own Mochi still enabled.
// The host owns "should there be a pet" only for ITSELF: when the pet shows
// instance X, X serves the page, the token and every /api/apps/mochi/* call, so the
// host's switch says nothing about whether that pet can continue.

test("host disabled tears the pet down when the pet is showing the host", () => {
  // 'self' means the host IS the thing being shown, so there is nothing to keep.
  assert.strictEqual(hostDisabledMeansTeardown("self", true), true);
  assert.strictEqual(hostDisabledMeansTeardown("self", false), true);
});

test("host disabled KEEPS a pet that a live remote is still serving", () => {
  // The regression this whole change exists to prevent.
  assert.strictEqual(hostDisabledMeansTeardown("crew-remote", true), false);
});

test("host disabled tears down when the remote is no longer usable", () => {
  // A definite resolve away from the remote (tunnel down, or Mochi switched off
  // there too) falls back to self — which is disabled — so there is genuinely no
  // pet left to keep.
  assert.strictEqual(hostDisabledMeansTeardown("crew-remote", false), true);
});

test("an uninterpretable shown-id tears down rather than keeping an orphan", () => {
  // Fail CLOSED on a value we cannot read: keeping a pet whose origin is unknown
  // leaves an inert always-on-top window with no way to explain itself.
  for (const bad of [null, undefined, "", "   ", 42, {}]) {
    assert.strictEqual(hostDisabledMeansTeardown(bad, true), true, `${JSON.stringify(bad)}`);
  }
});
