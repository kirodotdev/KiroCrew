"use strict";

/**
 * app-window-placement.js: where a hosted panel goes when another of the app's
 * windows opens it beside a box on screen (an avatar, a button).
 *
 * Pure geometry, no Electron. The panel opens BESIDE the anchor, not in a screen
 * corner: to its right when the anchor's centre is on the left half of the work
 * area or exactly on its midline, to its left otherwise. The card's top lines up
 * with the anchor's top, and everything is clamped to the work area, so the card
 * never lands off-screen or under the menu bar, including on displays with a
 * negative origin.
 */

/** Space between the anchor and the card. */
const ANCHOR_GAP = 12;
/** Space kept between the card and the work area's edge. */
const WORK_AREA_MARGIN = 8;

/** Clamp `value` into [min, max]; `min` when the range is inverted. */
function clamp(value, min, max) {
  if (max < min) return min;
  return Math.min(max, Math.max(min, value));
}

/** The card's ideal left edge on one side of the anchor. */
function idealLeft(anchor, cardWidth, side, gap) {
  return side === "right" ? anchor.x + anchor.width + gap : anchor.x - gap - cardWidth;
}

/**
 * @param {{x:number,y:number,width:number,height:number}} anchor screen coordinates
 * @param {{width:number,height:number}} card the card's size, without any gutter
 * @param {{x:number,y:number,width:number,height:number}} work the display's work area
 * @param {{gap?:number,margin?:number,slotOffsetY?:number}} [options] the space beside
 *   the anchor, the space kept inside the work area, and how far above the anchor's
 *   top the card sits
 * @returns {{rect:{x:number,y:number,width:number,height:number}, side:"left"|"right", clamped:boolean}}
 */
function placeBesideAnchor(anchor, card, work, options = {}) {
  const gap = options.gap ?? ANCHOR_GAP;
  const margin = options.margin ?? WORK_AREA_MARGIN;
  const slotOffsetY = options.slotOffsetY ?? 0;
  // The centre, not the left edge, so an anchor straddling the midline does not
  // flip sides on a one-pixel move.
  const anchorCentre = anchor.x + anchor.width / 2;
  const workCentre = work.x + work.width / 2;
  const side = anchorCentre > workCentre ? "left" : "right";
  const wantX = idealLeft(anchor, card.width, side, gap);
  const wantY = anchor.y - slotOffsetY;
  const maxX = work.x + work.width - card.width - margin;
  const maxY = work.y + work.height - card.height - margin;
  const x = Math.round(clamp(wantX, work.x + margin, maxX));
  const y = Math.round(clamp(wantY, work.y + margin, maxY));
  return {
    rect: { x, y, width: card.width, height: card.height },
    side,
    clamped: x !== Math.round(wantX) || y !== Math.round(wantY),
  };
}

module.exports = { ANCHOR_GAP, WORK_AREA_MARGIN, clamp, idealLeft, placeBesideAnchor };
