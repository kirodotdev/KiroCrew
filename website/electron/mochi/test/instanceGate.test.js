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

const { parseMochiEnabled, enabledOrTrust } = require("../instanceGate");

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
