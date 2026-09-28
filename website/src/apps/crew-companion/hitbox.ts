/**
 * Cursor-hitbox geometry — the rects the overlay declares interactive, and the
 * point-in-rect test that classifies the cursor against them.
 *
 * The companion's overlay covers the whole display and is click-through by
 * default; the ONLY regions that accept input are the companion, its speech
 * bubble, and (while it is open) the context menu. The renderer reports those
 * rects to the main process, which polls the cursor at ~60fps and toggles
 * ignore-mouse itself — reporting the rects instead of toggling input on pointer
 * enter/leave is what removes the IPC round-trip that let a click on the
 * companion body fall through to the window behind it.
 *
 * Pure and free of React/Electron so both sides can share the same maths and it
 * can be unit-tested on its own.
 */
import { PET_W, PET_H } from './constants'
import type { Rect } from './bubbleLayout'

/** A hitbox in overlay-local pixels: origin plus width/height. */
export interface HitRect {
  x: number
  y: number
  w: number
  h: number
}

/**
 * Extra pixels added below the bubble's measured box so the downward arrow and
 * tail — which sit outside the box, between it and the companion — stay inside
 * the clickable region rather than being a dead strip.
 */
export const BUBBLE_HIT_PAD = 12

/** The companion's own rect, from its rendered position and shared size. */
export function petHitbox(pos: { x: number; y: number }): HitRect {
  return { x: pos.x, y: pos.y, w: PET_W, h: PET_H }
}

/**
 * The bubble's rect, from the measured placement rectangle, or null when no
 * bubble is showing. The tail padding is folded into the height.
 */
export function bubbleHitbox(rect: Rect | null | undefined): HitRect | null {
  if (!rect) return null
  return {
    x: rect.left,
    y: rect.top,
    w: rect.right - rect.left,
    h: rect.bottom - rect.top + BUBBLE_HIT_PAD,
  }
}

/** True when the point falls within the rect (inclusive of its edges). */
export function pointInRect(rect: HitRect | null | undefined, x: number, y: number): boolean {
  return (
    !!rect &&
    x >= rect.x &&
    x <= rect.x + rect.w &&
    y >= rect.y &&
    y <= rect.y + rect.h
  )
}

/** The set of hitboxes the overlay reports as interactive at this moment. */
export interface ReportedHitboxes {
  pet: HitRect
  bubble: HitRect | null
  menu: HitRect | null
}

/**
 * Collect the rects the overlay should report. The companion is always present;
 * the bubble and the menu are present only when shown.
 */
export function reportedHitboxes(input: {
  pos: { x: number; y: number }
  bubbleRect: Rect | null
  menuRect: HitRect | null
}): ReportedHitboxes {
  return {
    pet: petHitbox(input.pos),
    bubble: bubbleHitbox(input.bubbleRect),
    menu: input.menuRect ?? null,
  }
}

/**
 * True when a point falls inside ANY reported rect — the classification the main
 * process makes each poll tick to decide whether this overlay should keep
 * accepting input or return to click-through.
 */
export function hitsAny(
  boxes: { pet?: HitRect | null; bubble?: HitRect | null; menu?: HitRect | null },
  x: number,
  y: number,
): boolean {
  return (
    pointInRect(boxes.pet, x, y) ||
    pointInRect(boxes.bubble, x, y) ||
    pointInRect(boxes.menu, x, y)
  )
}
