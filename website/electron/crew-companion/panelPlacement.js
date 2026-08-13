/**
 * panelPlacement.js — where the panel goes, given where the companion is.
 *
 * Pure geometry, no Electron, so it is unit-testable on its own. Ported from the
 * desktop app's `shared/panelPlacement.ts`; the maths and the constants are
 * unchanged, only the module format (CommonJS, for the main process).
 *
 * The rule it encodes: the panel opens BESIDE the companion, not in a screen corner.
 *
 *   side 'right' — panel to the RIGHT of the companion, so the companion reads as
 *                  being on the left of the composition. This is the default,
 *                  "mirrored" arrangement from the v1.0 spec.
 *   side 'left'  — the edge fallback, used when the companion is on the right half
 *                  and there is no room to its right.
 *
 * Vertically the card's top aligns with the companion's top, so the panel's hero row
 * lines up with it rather than floating above or below. Everything is clamped to the
 * work area, so the panel never lands off-screen or under the menu bar — including on
 * displays arranged left of or above the primary, which have negative origins.
 */

const PANEL_PLACEMENT_DEFAULTS = {
  gap: 12,
  margin: 8,
  // The card's top aligns with the companion's top — no lift. Kept as an option so
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

/** Clamp `value` into [min, max]. Returns `min` when the range is inverted. */
function clamp(value, min, max) {
  if (max < min) return min;
  return Math.min(max, Math.max(min, value));
}

/** The panel's ideal left edge for a given side. */
function idealLeft(pet, panelWidth, side, gap) {
  return side === 'right' ? pet.x + pet.width + gap : pet.x - gap - panelWidth;
}

/**
 * @param {{x:number,y:number,width:number,height:number}} pet companion rect, screen coords
 * @param {{width:number,height:number}} panel the card's size
 * @param {{x:number,y:number,width:number,height:number}} work the display's work area
 * @param {{gap?:number,margin?:number,slotOffsetY?:number}} [options]
 * @returns {{rect:{x:number,y:number,width:number,height:number},side:'left'|'right',clamped:boolean}}
 */
function placePanelNearPet(pet, panel, work, options = {}) {
  const gap = options.gap ?? PANEL_PLACEMENT_DEFAULTS.gap;
  const margin = options.margin ?? PANEL_PLACEMENT_DEFAULTS.margin;
  const slotOffsetY = options.slotOffsetY ?? PANEL_PLACEMENT_DEFAULTS.slotOffsetY;

  // Which half of the display is the companion's CENTRE on? Using the centre rather
  // than the left edge means a companion straddling the midline does not flip sides
  // on a one-pixel move.
  const petCentre = pet.x + pet.width / 2;
  const workCentre = work.x + work.width / 2;
  const side = petCentre > workCentre ? 'left' : 'right';

  const wantX = idealLeft(pet, panel.width, side, gap);
  const wantY = pet.y - slotOffsetY;

  const maxX = work.x + work.width - panel.width - margin;
  const maxY = work.y + work.height - panel.height - margin;
  const x = Math.round(clamp(wantX, work.x + margin, maxX));
  const y = Math.round(clamp(wantY, work.y + margin, maxY));

  return {
    rect: { x, y, width: panel.width, height: panel.height },
    side,
    clamped: x !== Math.round(wantX) || y !== Math.round(wantY),
  };
}

module.exports = { PANEL_PAD, PANEL_PLACEMENT_DEFAULTS, clamp, idealLeft, placePanelNearPet };
