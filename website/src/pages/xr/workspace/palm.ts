/**
 * The palm that holds the palette of pages, as plain rules: when a hand shows it, which hand holds it, and where
 * its tiles are. Nothing here draws or reads a device, so the rules are tested without one.
 *
 * An open hand with its fingers up, turned to the face and held so for about a third of a second, shows the
 * palette above the palm. It goes when the palm turns away. A fist, a hand that points and a palm turned to the
 * room never show it: a hand that types, holds a cup or rests on the desk is none of those (`gestures.ts`,
 * `palmToward`, which the land uses for its own palms).
 */
import { PALM, palmToward, type OpenHand } from '../gestures'
import type { V3 } from '../hand'
import { PAGES } from './layout'

export const PALM_MENU = {
  /** The palm faces the head this squarely to show the palette, and at least this squarely to keep it: no flicker at the edge. */
  showFacing: PALM.FACING,
  keepFacing: 0.4,
  /** How long the palm must be held to show it. */
  holdMs: PALM.HOLD_MS,
  /** Two rows of four tiles. Sizes are in dp at the eye: a label of 14 dp is as big as the body text of a page. */
  columns: 4,
  tileDp: [132, 76] as readonly [number, number],
  gapDp: 8,
  labelDp: PAGES.minTextDp,
  iconDp: 24,
  /** The palette's lower edge stands this far above the fingertips, so the hand does not cover it. */
  liftDp: 24,
} as const

export type Side = 'left' | 'right'

/** One hand's palm, from frame to frame: whether it shows the palette now. */
export class PalmHold {
  private since: number | null = null
  private on = false

  /** Feed the hand as it is now (null when it is not tracked) and where the head is. True while it shows the palette. */
  feed(hand: OpenHand | null, head: V3, now: number): boolean {
    const facing = hand ? palmToward(hand, head) : 0
    if (this.on) {
      if (facing < PALM_MENU.keepFacing) { this.on = false; this.since = null }
      return this.on
    }
    if (facing < PALM_MENU.showFacing) { this.since = null; return false }
    this.since ??= now
    if (now - this.since >= PALM_MENU.holdMs) this.on = true
    return this.on
  }

  get shown(): boolean { return this.on }
}

/** Which hand holds the palette: the one that held it a frame ago while its palm still shows it, else the first that does. */
export function holder(left: boolean, right: boolean, was: Side | null): Side | null {
  if (was === 'left' && left) return 'left'
  if (was === 'right' && right) return 'right'
  return left ? 'left' : right ? 'right' : null
}

/** A tile of the palette: its middle and its size in dp, from the palette's own middle, x to the right and y up. */
export interface Tile { x: number; y: number; w: number; h: number }

/** Where `count` tiles are: rows of four, the first row on top. */
export function tiles(count: number): Tile[] {
  const [w, h] = PALM_MENU.tileDp
  const gap = PALM_MENU.gapDp
  const rows = Math.ceil(count / PALM_MENU.columns)
  const out: Tile[] = []
  for (let i = 0; i < count; i += 1) {
    const col = i % PALM_MENU.columns
    const rowIndex = Math.floor(i / PALM_MENU.columns)
    out.push({ x: (col - (PALM_MENU.columns - 1) / 2) * (w + gap), y: ((rows - 1) / 2 - rowIndex) * (h + gap), w, h })
  }
  return out
}

/** The whole palette's size in dp. */
export function paletteSize(count: number): [number, number] {
  const [w, h] = PALM_MENU.tileDp
  const rows = Math.ceil(count / PALM_MENU.columns)
  return [PALM_MENU.columns * w + (PALM_MENU.columns - 1) * PALM_MENU.gapDp, rows * h + (rows - 1) * PALM_MENU.gapDp]
}

/** How many metres one dp is on a thing `distanceM` from the eye: Android XR's own measure. */
export const mPerDp = (distanceM: number): number => (PAGES.mmPerDpAt1M / 1000) * distanceM
