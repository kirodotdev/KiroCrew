const { test } = require("node:test");
const assert = require("node:assert");
const {
  TEXT_SIZE_TIERS,
  DEFAULT_TEXT_SIZE_PX,
  isSupportedTextSize,
} = require("../text-size");

// ── ladder shape ──

test("ladder holds the three tiers in ascending order", () => {
  assert.strictEqual(TEXT_SIZE_TIERS.length, 3);
  for (let i = 1; i < TEXT_SIZE_TIERS.length; i++) {
    assert.ok(
      TEXT_SIZE_TIERS[i].px > TEXT_SIZE_TIERS[i - 1].px,
      `tier ${i} is larger than the one before it`,
    );
  }
  assert.deepStrictEqual(
    TEXT_SIZE_TIERS.map((tier) => tier.px),
    [12, 16, 20],
  );
});

// Chromium offers 9, 12, 16, 20 and 24. The 9px stop is dropped because
// website/AGENTS.md forbids text below 10px, and the chosen size renders into
// the dashboard. No other stop may be dropped.
test("no tier sits below the 10px floor", () => {
  for (const tier of TEXT_SIZE_TIERS) {
    assert.ok(tier.px >= 10, `${tier.name} is at or above 10px`);
  }
});

test("the default is 16px and is a tier on the ladder", () => {
  assert.strictEqual(DEFAULT_TEXT_SIZE_PX, 16);
  assert.ok(isSupportedTextSize(DEFAULT_TEXT_SIZE_PX));
});

// Hardcoded on both sides on purpose. Comparing `tier.id` against
// `textSizeItemId(tier.name)` would compare the module with itself: the ladder
// is built by that same call, so the assertion would hold even if the builder
// returned the empty string for everything.
test("the three menu ids are exactly these", () => {
  assert.deepStrictEqual(
    TEXT_SIZE_TIERS.map((tier) => tier.id),
    ["font-size-small", "font-size-medium", "font-size-large"],
  );
});

// Properties, NOT a literal inventory. An exact label list is a change
// detector under `a-ratchet-may-only-tighten`: rewording a tier's display text
// is a valid presentation change, and the menu consumes labels opaquely while
// dispatch goes by id, so the strings pin no behavioural contract. What DOES
// matter is that every tier carries a label a user can read, and that no two
// tiers read the same. The ids are pinned separately above, where they are a
// real contract: `windows-menu-model.js` fences those exact strings.
test("every tier carries a usable label", () => {
  for (const tier of TEXT_SIZE_TIERS) {
    assert.strictEqual(typeof tier.label, "string", `${tier.name} label is a string`);
    assert.ok(tier.label.trim().length > 0, `${tier.name} label is not blank`);
    assert.notStrictEqual(tier.label, tier.name, `${tier.name} label is not the bare key`);
  }
});

test("no two tiers read the same label", () => {
  const labels = TEXT_SIZE_TIERS.map((tier) => tier.label);
  assert.strictEqual(
    new Set(labels).size,
    labels.length,
    "two tiers would be indistinguishable in the menu",
  );
});

// ── isSupportedTextSize ──

test("every tier px is supported", () => {
  for (const tier of TEXT_SIZE_TIERS) {
    assert.strictEqual(isSupportedTextSize(tier.px), true, `${tier.px} is supported`);
  }
});

test("an off-ladder number is not supported", () => {
  // 24 is Chromium's own next stop and was on this ladder until the rail could
  // not hold its labels, so it is listed here deliberately: a reinstated Very
  // Large tier must come back through the ladder, not through the validator.
  for (const px of [9, 11, 13, 18, 24, 25, 0, -16, 1000]) {
    assert.strictEqual(isSupportedTextSize(px), false, `${px} is rejected`);
  }
});

test("a non-number is not supported", () => {
  for (const value of [NaN, Infinity, -Infinity, "16", null, undefined, {}, [], true]) {
    assert.strictEqual(
      isSupportedTextSize(value),
      false,
      `${String(value)} is rejected`,
    );
  }
});
