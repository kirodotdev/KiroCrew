"use strict";

// Kiro Crew's Content Text Size tier ladder. Values are px; names align with
// Chrome's `chrome://settings/appearance` dropdown so a user familiar with
// Chrome sees the same choices with the same effect.
//
// FOUR tiers, not five: Chrome ships a fifth "Very Small" (9px) at the bottom
// of its ladder, but Kiro Crew's chrome — sidebar, tab strips, chips, menu
// items — is px-pinned via ~4,000 `text-[NNpx]` declarations in
// `website/src/`, and the pinned components don't reflow cleanly below 10px
// baseline. `website/AGENTS.md` codifies that floor. Dropping the 9px tier
// keeps every rendered surface honest — no user picks a size that would clip
// on their own chrome. The remaining four still cover the accessibility span
// (a 2x range from 12→24px, which is what OpenDyslexic readers actually
// need). If a follow-up ever removes the px-pinning, this list can grow back
// to five without a schema migration — the ladder is loaded fresh each boot
// and a persisted value that no longer matches falls back to DEFAULT_TIER.
//
// Frozen top-to-bottom (array + every entry) because the schema is the fixed
// contract that every other module in this feature reads — the IPC handlers,
// the menu builder, the window-creation sites and the validator all take
// this as-is. A mutation from an unrelated code path would silently reshape
// the menu or admit an unsupported px into the persisted config.
const TIERS = Object.freeze([
  Object.freeze({ name: "small", label: "Small", px: 12 }),
  Object.freeze({ name: "medium", label: "Medium", px: 16 }),
  Object.freeze({ name: "large", label: "Large", px: 20 }),
  Object.freeze({ name: "veryLarge", label: "Very Large", px: 24 }),
]);

// Chromium's own default — a user who never touches the setting sees no
// change from vanilla Chromium behaviour. Also the fallback when a persisted
// value is missing or corrupt, so "unset" and "invalid" both land on the
// same visible outcome instead of on a weird intermediate size.
const DEFAULT_TIER = TIERS[1];

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

// `resolveTier(px) → tier` used to live here but had zero consumers after
// the display-prefs:* IPC handlers were deleted (Fable First-Principles
// review, 16d827598). The menu code that would have called it does its
// own `.find()` via `reconcileFontSizeChecks`. Removed in Fable First-
// Principles round 4 on acefffa4b along with the unused import in
// display-preferences/index.js.

module.exports = { TIERS, DEFAULT_TIER, isValidPx };
