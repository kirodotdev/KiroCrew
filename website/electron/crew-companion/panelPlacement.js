/**
 * panelPlacement.js — where the panel goes, given where the companion is.
 *
 * Pure geometry, no Electron, so it is unit-testable on its own. The maths is the
 * desktop window host's `app-window-placement.js`, which any app's panel opened
 * beside an anchor uses; this module keeps the companion's names for it.
 *
 * The rule it encodes: the panel opens BESIDE the companion, not in a screen corner.
 *
 *   side 'right' — panel to the RIGHT of the companion, so the companion reads as
 *                  being on the left of the composition. This is the
 *                  "mirrored" arrangement from the v1.0 spec, used whenever the
 *                  companion's centre is on the left half of the work area or
 *                  exactly on its midline.
 *   side 'left'  — panel to the LEFT, used whenever the companion's centre is on
 *                  the right half, whether or not the panel would fit to its right.
 *
 * Vertically the card's top aligns with the companion's top, so the panel's hero row
 * lines up with it rather than floating above or below. Everything is clamped to the
 * work area, so the panel never lands off-screen or under the menu bar — including on
 * displays arranged left of or above the primary, which have negative origins.
 */

const { ANCHOR_GAP, WORK_AREA_MARGIN, clamp, idealLeft, placeBesideAnchor } = require("../app-window-placement");

const PANEL_PLACEMENT_DEFAULTS = {
  gap: ANCHOR_GAP,
  margin: WORK_AREA_MARGIN,
  // The card's top aligns with the companion's top: no lift. Kept as an option so
  // the inverse stays symmetrical.
  slotOffsetY: 0,
};

/**
 * Transparent gutter around the card inside the window, giving room for the card's
 * own shadow.
 *
 * Modest on purpose: the padding is invisible but still part of the window, so it
 * swallows clicks that would otherwise reach the desktop.
 */
const PANEL_PAD = 32;

/** The desktop window host's placement beside an anchor; one copy of the maths. */
const placePanelNearPet = placeBesideAnchor;

module.exports = { PANEL_PAD, PANEL_PLACEMENT_DEFAULTS, clamp, idealLeft, placePanelNearPet };
