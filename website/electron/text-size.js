// Content text size ladder — the single source of truth for the tiers the
// View menu offers and the px value each one means.
//
// "Content text size" means Chromium's default font size, which governs text
// with no explicit size in its CSS: chat messages, markdown bodies and dialog
// copy. It is not a general font-size control, because roughly 4,000
// `text-[NNpx]` declarations pin the sidebars, the tab strips and the buttons.
// The menu label says Content Text Size for that reason.
//
// The ladder mirrors Chromium's own preset stops (9, 12, 16, 20, 24) minus the
// 9px stop, which website/AGENTS.md forbids: no text below 10px. The chosen
// size renders into the dashboard, so that floor binds the result even though
// this module sits in the shell. No other stop is dropped.
//
// This module requires nothing: no Electron, no Node built-ins, no store. The
// menu reads the tiers to build its items, the window chrome reads the default
// and the validator, and the window construction reads the seed px. Keeping it
// pure is what lets a test load it without an Electron binary.

const DEFAULT_TEXT_SIZE_PX = 16;

/** The menu item id for a tier, derived from the tier's own name. Every name on
 *  the ladder is one word today; a two-word name would stay camelCase here, so
 *  "veryLarge" would give `font-size-veryLarge`. */
function textSizeItemId(name) {
  return `font-size-${name}`;
}

// The labels live here because code-style.md gives every user-facing label one
// owning module. They are raw English on purpose: web/docs/i18n-catalog.md puts
// the Electron main process outside the catalogues and outside the i18n gates.
//
// The ladder stops at Large, one stop short of Chromium's own 24. Chromium
// scales a whole web page; this scales the minority of the dashboard's text
// that is not pinned in px, so the gap between scaled text and the 6,466
// `text-[NNpx]` declarations widens with every stop. At 20px that is 17.5px
// body text beside 11px metadata; at 24px it is 21px beside the same 11px, and
// the nav rail's 236px track can no longer hold its own labels. A tier that
// makes the interface look more broken the harder a reader leans on it is not
// an accessibility feature, so it is left out rather than shipped and
// explained away.
const TEXT_SIZE_TIERS = [
  { name: "small", label: "Content Text Size: Small", px: 12 },
  { name: "medium", label: "Content Text Size: Medium", px: DEFAULT_TEXT_SIZE_PX },
  { name: "large", label: "Content Text Size: Large", px: 20 },
].map((tier) => ({ ...tier, id: textSizeItemId(tier.name) }));

/** Whether a value is one of the ladder's px sizes. A stored value that fails
 *  this is left alone rather than corrected: the caller falls back to the
 *  default and warns, so an operator debugging a bad config still sees it. */
function isSupportedTextSize(px) {
  if (typeof px !== "number") return false;
  return TEXT_SIZE_TIERS.some((tier) => tier.px === px);
}

// `textSizeItemId` stays private: the ladder above is its only caller, and
// the three ids it produces are pinned literally in the test.
module.exports = {
  TEXT_SIZE_TIERS,
  DEFAULT_TEXT_SIZE_PX,
  isSupportedTextSize,
};
