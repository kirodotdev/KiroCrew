/**
 * The plain maths of looking round in the flat preview with a finger or the mouse: which way a point of the canvas
 * looks into the room, how the head must turn so that what was under the finger stays under it, how wide the
 * preview's lens must be for the first page to be whole in view on a narrow screen, and how far two fingers that
 * move apart bring the eye nearer, so that a page's text can be read on a phone.
 *
 * Nothing here draws or listens. Angles are degrees, the canvas is measured in CSS pixels from its top left corner,
 * and a direction is a unit vector of the room (x to the right, y up, -z ahead at a yaw of zero).
 */
import { M_PER_PX, PAGES } from './layout'

/** Where the preview's head looks: `yawDeg` to the right, `pitchDeg` up. */
export interface Turn { yawDeg: number; pitchDeg: number }
/**
 * The canvas and its lens: its size in CSS pixels, how tall the lens is, and how far the picture's frame is shifted
 * down, as a share of half its height (0 when left out). A shifted frame shows more of what is under the level line
 * and less of what is over it, and upright edges stay upright: the head does not tip.
 */
export interface View { width: number; height: number; fovDeg: number; shift?: number }
export type Direction = [number, number, number]

export const LOOK = {
  /** The head of the preview looks no further up or down than this. */
  maxPitchDeg: 60,
  /** The lens that shows the whole first page is no narrower than the Aura's view. */
  fovDeg: 40.7,
  /** On a narrow screen that lens widens until the first page fits, and no further than this. */
  maxFovDeg: 110,
  /**
   * The start picture, on every screen: a lens this tall, a level head, and the frame shifted down by this share of
   * half its height, as a camera with a shift lens does. The first page (34.7 degrees tall) and the crew's row under
   * it are both in it, the page is a true rectangle (a tipped head would make its sides lean), and a page's text is
   * big enough to read on a phone held upright, where the page's sides are then out of the picture.
   */
  startFovDeg: 50,
  startShift: 0.317,
  /**
   * The shift is for the start's lens. With the eye nearer a page is larger than the picture's upper part, and a
   * shifted frame cuts its upper edge off while room stays under it. So the shift goes to nothing as the eye comes
   * nearer: all of it up to the start's lens, none of it from this many times nearer on (`shiftShare`).
   */
  noShiftNear: 1.3,
  /** The air over a page's close mark once the picture has turned to that page, in pixels of the screen. */
  markAirPx: 4,
  /** The air kept round the first page, so the edge that moves it, the corner that resizes it and the one line the panel says under it are in view too. */
  marginM: 0.08,
  /** A direction this close to straight up or down has no heading to hold: the head keeps its yaw. */
  poleEps: 0.02,
  /** Two fingers that turn the head never keep quite the same distance. A change of less than this share is no pinch. */
  spreadSlack: 0.06,
  /** One notch of a mouse wheel (100) on empty space comes this much nearer or goes this much back. */
  wheelNear: 1.25,
  /** A page shown smaller than this many pixels of the screen for each of its own cannot be read: its 14 px text is under 10 px. */
  readable: 0.7,
} as const

const RAD = Math.PI / 180
const clamp = (v: number, lo: number, hi: number) => Math.max(lo, Math.min(hi, v))
/** The same angle within half a turn of zero. */
const wrap = (deg: number) => deg - 360 * Math.round(deg / 360)

/** The direction under a point of the canvas as the lens sees it, before the head turns. Not a unit vector: z is -1. */
function lens(x: number, y: number, view: View): Direction {
  const t = Math.tan((view.fovDeg * RAD) / 2)
  return [((2 * x) / view.width - 1) * t * (view.width / view.height), (1 - (2 * y) / view.height - (view.shift ?? 0)) * t, -1]
}

/** A direction of the lens, tipped up by the head's pitch. */
function tip([x, y, z]: Direction, pitchDeg: number): Direction {
  const p = pitchDeg * RAD
  return [x, y * Math.cos(p) - z * Math.sin(p), y * Math.sin(p) + z * Math.cos(p)]
}

/** Which way a direction points on the floor, in degrees to the right of straight ahead. */
const heading = ([x, , z]: Direction) => Math.atan2(x, -z) / RAD

/** The direction into the room under the point `(x, y)` of the canvas, with the head turned as `turn` says. */
export function under(turn: Turn, x: number, y: number, view: View): Direction {
  const [vx, vy, vz] = tip(lens(x, y, view), turn.pitchDeg)
  const yaw = turn.yawDeg * RAD
  // The head's yaw is to the right: it carries straight ahead (-z) toward +x.
  const d: Direction = [vx * Math.cos(yaw) - vz * Math.sin(yaw), vy, vx * Math.sin(yaw) + vz * Math.cos(yaw)]
  const n = Math.hypot(...d)
  return [d[0] / n, d[1] / n, d[2] / n]
}

/**
 * How the head must turn so that the direction `grab` is under the point `(x, y)` of the canvas: what a finger took
 * hold of stays under the finger. The pitch stays within its bound, and past the bound the finger slides. `from` is
 * the turn so far: the yaw that is returned is the one nearest to it, so the head never spins the long way round.
 */
export function hold(grab: Direction, x: number, y: number, view: View, from: Turn): Turn {
  const c = lens(x, y, view)
  const n = Math.hypot(...c)
  // Tipped by the pitch p, the lens direction's height is c.y cos p + sin p (c.z is -1). It must be the grab's.
  const reach = Math.hypot(c[1], 1)
  const pitchDeg = clamp(Math.asin(clamp((grab[1] * n) / reach, -1, 1)) / RAD - Math.atan2(c[1], 1) / RAD, -LOOK.maxPitchDeg, LOOK.maxPitchDeg)
  const tipped = tip(c, pitchDeg)
  if (Math.hypot(grab[0], grab[2]) < LOOK.poleEps || Math.hypot(tipped[0], tipped[2]) < LOOK.poleEps * n) return { yawDeg: from.yawDeg, pitchDeg }
  return { yawDeg: from.yawDeg + wrap(heading(grab) - heading(tipped) - from.yawDeg), pitchDeg }
}

/**
 * How tall the preview's lens must be for the page that the address opens to be whole in view on a canvas of this
 * shape, with the air of `LOOK.marginM` round it. A wide canvas keeps the lens of the glasses; a narrow one, as a
 * phone held upright, gets a wider lens. The head stays where it is, in the middle of the pages.
 */
export function fovFor(aspect: number): number {
  const halfW = (PAGES.cssWidth * M_PER_PX) / 2 + LOOK.marginM
  const halfH = (PAGES.cssHeight * M_PER_PX) / 2 + LOOK.marginM
  if (!(aspect > 0)) return LOOK.fovDeg
  const needs = 2 * Math.atan(Math.max(halfH / PAGES.distanceM, halfW / PAGES.distanceM / aspect)) / RAD
  return clamp(needs, LOOK.fovDeg, LOOK.maxFovDeg)
}

/** How many CSS pixels of the screen one CSS pixel of a page covers, for a page at the distance it opens at. */
export const shownAt = (view: View): number => (view.height / 2 / Math.tan((view.fovDeg * RAD) / 2)) * (M_PER_PX / PAGES.distanceM)

/**
 * Whether the preview must say a panel's one line over the canvas as well. The panel's own line is under its page,
 * at the page's size: on a small screen it is too small to read, and with the eye near it may be out of view.
 */
export const lineOverCanvas = (view: View, near: number): boolean => near > 1 || shownAt(view) < LOOK.readable

/** Where the head looks at the start, and where the home button takes it back to. */
export const START_TURN: Turn = { yawDeg: 0, pitchDeg: 0 }

const START_TAN = Math.tan((LOOK.startFovDeg * RAD) / 2)

/**
 * How near the eye can come on a canvas of this size, as a number of times the start view: until one CSS pixel of a
 * page at the distance it opens at covers one CSS pixel of the screen, so that its 14 px text is 14 px. Never less
 * than 1: on a canvas whose start view is already that big, there is no nearer.
 */
export function mostNear(width: number, height: number): number {
  if (!(width > 0 && height > 0)) return 1
  // One pixel of the page is one pixel of the canvas when the lens is as long, in pixels, as the page is far in its own pixels.
  const wanted = PAGES.distanceM / M_PER_PX
  const start = height / 2 / START_TAN
  return Math.max(1, wanted / start)
}

/**
 * How far back the eye can step on a canvas of this size, as a number of times the start view (1 or less): until
 * the whole first page is in view (`fovFor`). On a canvas whose start view shows it whole, there is no farther.
 */
export function leastNear(width: number, height: number): number {
  if (!(width > 0 && height > 0)) return 1
  return Math.min(1, START_TAN / Math.tan((fovFor(width / height) * RAD) / 2))
}

/**
 * The preview's lens when the eye is `near` times nearer than the start view (less than 1: a step back). The head
 * stays where it is, in the middle of the pages: the lens narrows, which shows a page that faces the eye as a step
 * toward it would.
 */
export function lensFor(width: number, height: number, near: number): number {
  const n = clamp(near, leastNear(width, height), mostNear(width, height))
  return (2 * Math.atan(START_TAN / n)) / RAD
}

/**
 * How near the eye is after two fingers that were `spread0` apart when the eye was `near0` near are `spread` apart:
 * apart brings it nearer, together takes it back, between `least` (a step back from the start view) and `most`. Fingers that keep about their
 * distance (`LOOK.spreadSlack`) are turning the head, and the eye stays as near as it was.
 */
export function pinchNear(near0: number, spread0: number, spread: number, most: number, least = 1): number {
  const ratio = spread / Math.max(1, spread0)
  const by = ratio > 1 + LOOK.spreadSlack ? ratio / (1 + LOOK.spreadSlack) : ratio < 1 - LOOK.spreadSlack ? ratio / (1 - LOOK.spreadSlack) : 1
  return clamp(near0 * by, Math.min(1, least), Math.max(1, most))
}

/** How near the eye is after a mouse wheel turned by `deltaY` on empty space: away from the person comes nearer. */
export const wheelNear = (near: number, deltaY: number, most: number, least = 1): number => clamp(near * LOOK.wheelNear ** (-deltaY / 100), Math.min(1, least), Math.max(1, most))

/* ---------------- the picture turns to a page ---------------- */

/** How long the picture takes to turn to a page that the plus opened, or to the nearest page left after a close. */
export const TURN_MS = 400

/**
 * The quiet time. From the moment the picture begins to move by itself, or a page closes, the pages are not where
 * the finger was aimed: the next page's close mark arrives where the closed page's was. Until this long after it,
 * and while the picture still turns, a touch or a click on the canvas does nothing at all: nothing is sent to a
 * page, no edge and no mark lights, and what was lit went dark when the time began. With reduced motion set the
 * turn is a jump, and the same time runs from the jump.
 */
export const TAP_AGAIN_MS = 500

/**
 * A notch of the wheel on empty space latches the wheel to the eye's nearness: the next notch at the same place
 * (within this many CSS px, `WHEEL_LATCH_PX`, and this soon, `WHEEL_LATCH_MS`) goes nearer or back again, also when
 * the notch before brought a page under the pointer. A pointer that moved goes to what is under it.
 */
export const WHEEL_LATCH_MS = 1500
export const WHEEL_LATCH_PX = 8
/** Whether a notch at `(x, y)` (client px) at `now` is latched to the nearness by the empty-space notch `last`. */
export const wheelLatched = (last: { x: number; y: number; at: number } | null, x: number, y: number, now: number): boolean =>
  last !== null && now - last.at < WHEEL_LATCH_MS && Math.hypot(x - last.x, y - last.y) <= WHEEL_LATCH_PX

/** A touch that was held back and then moves this far on the screen, in its pixels, is a drag: it ends the turn, and the picture follows the finger. A tap does not end the turn. */
export const TURN_DRAG_PX = 8

/** Whether a touch that begins at `now` is held back: the picture turns by itself, or the quiet time runs until `quietUntil`. */
export const notAPress = (turning: boolean, now: number, quietUntil: number): boolean => turning || now < quietUntil

/**
 * Until when the quiet time runs once a touch at `now` was held back. After a close each such touch starts the
 * half second anew: a person who goes on tapping at the close mark closes one page, and the next page's mark
 * lights only by a tap that comes after a pause. After the plus opened a page the time stays as it is: a quick
 * second try on the new page is not held back again and again.
 */
export const quietAfter = (quietUntil: number, now: number, afterClose: boolean): number => (afterClose ? Math.max(quietUntil, now + TAP_AGAIN_MS) : quietUntil)

/** A place round the head, as the picture needs it: so far to the right of straight ahead, and so far up. */
export interface Aim { yawDeg: number; pitchDeg: number }

/** The shorter way round from one yaw to another, in degrees: more than -180 and no more than 180. */
export const shorterWay = (fromDeg: number, toDeg: number): number => {
  const d = (((toDeg - fromDeg) % 360) + 360) % 360
  return d > 180 ? d - 360 : d
}

/**
 * Where the head looks once the middle of a page at `aim` is in the middle of the picture. The yaw is the one
 * nearest to where the head looks now, so the picture turns the shorter way round, and never by more than half a
 * turn. The pitch stays within its bound.
 */
export function turnTo(from: Turn, aim: Aim): Turn {
  return { yawDeg: from.yawDeg + shorterWay(from.yawDeg, aim.yawDeg), pitchDeg: clamp(aim.pitchDeg, -LOOK.maxPitchDeg, LOOK.maxPitchDeg) }
}

/** How much of a turn is done with `k` of its time gone (0 to 1): quick at first, and slow into its place. */
const eased = (k: number): number => 1 - (1 - clamp(k, 0, 1)) ** 3

/** The head on its way from `from` to `to`, with `k` of the time gone (0 to 1): quick at first, and slow into its place. */
export function turnAt(from: Turn, to: Turn, k: number): Turn {
  const e = eased(k)
  return { yawDeg: from.yawDeg + (to.yawDeg - from.yawDeg) * e, pitchDeg: from.pitchDeg + (to.pitchDeg - from.pitchDeg) * e }
}

/** The frame's shift on its way from `from` to `to`, with the turn that it goes with. */
export const shiftAt = (from: number, to: number, k: number): number => from + (to - from) * eased(k)

/** How much of the frame's shift is in force with the eye `near` times nearer than the start view: 1 down to 0. */
export const shiftShare = (near: number): number => clamp((LOOK.noShiftNear - near) / (LOOK.noShiftNear - 1), 0, 1)

/**
 * The largest shift of the frame that keeps the upper edge of a page in the picture, with `overM` metres over it
 * (its close mark) and `LOOK.markAirPx` of air over that. The head looks at the page's middle, level with it, and
 * the page faces the head, `distanceM` away and `heightM` tall. 0 when the page is taller than that leaves room for.
 */
export function shiftToShow(view: View, page: { distanceM: number; heightM: number }, overM: number): number {
  const t = Math.tan((view.fovDeg * RAD) / 2)
  if (!(view.height > 0 && page.distanceM > 0)) return 0
  return clamp(1 - (page.heightM / 2 + overM) / (page.distanceM * t) - (2 * LOOK.markAirPx) / view.height, 0, 1)
}

/**
 * The frame's shift once the picture has turned to a page by itself: `base` or less, and small enough that the
 * page's upper edge and its close mark are in the picture (`shiftToShow`). The start picture's shift puts a page
 * flush under the picture's upper edge, with its close mark over that edge; a page that a person just opened needs
 * its mark. That holds with the lens as it is now (`view`, `overM`), and at the start's lens too (`startOverM`, the
 * mark's square there), where the whole shift is in force again once the eye steps back: also when the eye is so
 * near now that no shift is in force.
 */
export function shiftAfterTurn(base: number, near: number, view: View, page: { distanceM: number; heightM: number }, overM: number, startOverM = overM): number {
  const share = shiftShare(near)
  const atStart = shiftToShow({ ...view, fovDeg: lensFor(view.width, view.height, 1) }, page, startOverM)
  return Math.min(base, atStart, share > 0 ? shiftToShow(view, page, overM) / share : Infinity)
}

/** How far under and over the head's own line the picture reaches, in degrees: the same both ways unless its frame is shifted. */
function upAndDown(view: View): [number, number] {
  const t = Math.tan((view.fovDeg / 2) * RAD)
  const shift = view.shift ?? 0
  return [Math.atan(t * (1 + shift)) / RAD, Math.atan(t * (1 - shift)) / RAD]
}

/** Whether the middle of a page at `aim` is inside the picture, with the head turned as `turn` says. */
export function inPicture(turn: Turn, view: View, aim: Aim): boolean {
  const t = Math.tan((view.fovDeg / 2) * RAD)
  const halfAcross = Math.atan(t * (view.width / view.height)) / RAD
  const [down, up] = upAndDown(view)
  const above = aim.pitchDeg - turn.pitchDeg
  return Math.abs(shorterWay(turn.yawDeg, aim.yawDeg)) <= halfAcross && above <= up && above >= -down
}

/**
 * Whether a page keeps the keys: text and keys go only to a page that the person can see. That is a page whose
 * middle is in the picture, or one that the middle of the picture is on: with the eye near, a person types into
 * the lower part of a page while its middle is above the picture's top. `page` is where the page's middle is and
 * how far, `sizeM` how wide and high it is.
 */
export function keysStay(turn: Turn, view: View, page: Aim & { distanceM: number }, sizeM: readonly [number, number]): boolean {
  if (inPicture(turn, view, page)) return true
  const halfAcross = Math.atan(sizeM[0] / 2 / page.distanceM) / RAD
  const halfUp = Math.atan(sizeM[1] / 2 / page.distanceM) / RAD
  // The middle of the picture is under the head's own line when the frame is shifted down.
  const middle = turn.pitchDeg - Math.atan(Math.tan((view.fovDeg / 2) * RAD) * (view.shift ?? 0)) / RAD
  return Math.abs(shorterWay(turn.yawDeg, page.yawDeg)) <= halfAcross && Math.abs(page.pitchDeg - middle) <= halfUp
}

/** The page to turn to when a close has left the picture with no page in it: the nearest one, or none when a page is in the picture or none is left. */
export function nearestAim<T extends Aim>(turn: Turn, view: View, pages: readonly T[]): T | null {
  if (pages.some(p => inPicture(turn, view, p))) return null
  let best: T | null = null
  let least = Infinity
  for (const p of pages) {
    const away = Math.hypot(shorterWay(turn.yawDeg, p.yawDeg), p.pitchDeg - turn.pitchDeg)
    if (away < least) { least = away; best = p }
  }
  return best
}

/**
 * The page in front: of the pages whose middle is in the picture, the one nearest the picture's middle (under the
 * head's own line when the frame is shifted down); none when no page is in the picture. Its close mark shows at rest.
 */
export function frontAim<T extends Aim>(turn: Turn, view: View, pages: readonly T[]): T | null {
  const middle = turn.pitchDeg - Math.atan(Math.tan((view.fovDeg / 2) * RAD) * (view.shift ?? 0)) / RAD
  let best: T | null = null
  let least = Infinity
  for (const p of pages) {
    if (!inPicture(turn, view, p)) continue
    const away = Math.hypot(shorterWay(turn.yawDeg, p.yawDeg), p.pitchDeg - middle)
    if (away < least) { least = away; best = p }
  }
  return best
}
