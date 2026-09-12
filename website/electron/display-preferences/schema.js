"use strict";

// Chrome's font-size preset ladder, matching chrome://settings/appearance
// exactly. Values are px; names align with Chrome's dropdown labels so a user
// familiar with Chrome sees the same choices with the same effect.
//
// Frozen top-to-bottom (array + every entry) because the schema is the fixed
// contract that every other module in this feature reads — the IPC handlers,
// the menu builder, the window-creation sites and the validator all take
// this as-is. A mutation from an unrelated code path would silently reshape
// the menu or admit an unsupported px into the persisted config.
const TIERS = Object.freeze([
  Object.freeze({ name: "verySmall", label: "Very Small", px: 9 }),
  Object.freeze({ name: "small", label: "Small", px: 12 }),
  Object.freeze({ name: "medium", label: "Medium", px: 16 }),
  Object.freeze({ name: "large", label: "Large", px: 20 }),
  Object.freeze({ name: "veryLarge", label: "Very Large", px: 24 }),
]);

// Chromium's own default — a user who never touches the setting sees no
// change from vanilla Chromium behaviour. Also the fallback when a persisted
// value is missing or corrupt, so "unset" and "invalid" both land on the
// same visible outcome instead of on a weird intermediate size.
const DEFAULT_TIER = TIERS[2];

// Fast membership check for validators. Rebuilt from TIERS so adding a tier
// automatically extends the accepted-px set — no second list to keep in sync.
const VALID_PX = new Set(TIERS.map((tier) => tier.px));

// Accept only finite numbers whose value lands on a ladder stop. Strings that
// happen to parse to a number are rejected: this validator runs against IPC
// payloads and persisted config, both of which must be authored with the
// numeric contract in mind. A silent coerce would let "16 " (trailing space)
// or "16" (JSON-quoted) both pass while behaving differently downstream.
function isValidPx(px) {
  return typeof px === "number" && Number.isFinite(px) && VALID_PX.has(px);
}

// Map a px back to its tier. Never throws — a persisted value that no longer
// matches the schema (older ladder, hand-edited config, a value pinned before
// a schema change) resolves to DEFAULT_TIER so the menu still renders and
// startup still succeeds. The caller separately decides whether to REPLACE
// the stored value or leave it and just render the default.
function resolveTier(px) {
  if (!isValidPx(px)) return DEFAULT_TIER;
  return TIERS.find((tier) => tier.px === px) || DEFAULT_TIER;
}

module.exports = { TIERS, DEFAULT_TIER, isValidPx, resolveTier };
