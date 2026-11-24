/**
 * Where the chat card's standing keys come out (D101 5): never over a Bot that asks or failed, with its board. Over a
 * Bot at rest they may.
 */
import { CARD } from './chatCard'
import { KEYS, KEYS_LINE, boardSize } from './deskKeys'
import { PAGES } from './layout'
import { PLACES, WITH_BOARD, onBox, seenAt, seenBox } from './places'

const RAD = Math.PI / 180

/**
 * The places tried, in order, as degrees from his look (to the right, and up), all at the card's own height (M84): the
 * card's own side, the other side, then twice as far to each side. The card and its keys are one thing, a sheet with
 * its keyboard: the card stands over the keys on the same side, when it comes out and after a glide. A place that the lens
 * cuts while he looks at the card is no place (D109 3): a lower place (26 degrees down) is cut there, see `cardKeysSeen`.
 */
export const CARD_KEYS_TRIES: readonly (readonly [number, number])[] = [
  [CARD.outYawDeg, KEYS.standPitchDeg],
  [-CARD.outYawDeg, KEYS.standPitchDeg],
  [2 * CARD.outYawDeg, KEYS.standPitchDeg],
  [-2 * CARD.outYawDeg, KEYS.standPitchDeg],
]

const DEG = 180 / Math.PI

/** The chat card's lower edge at its place, in degrees from his eyes' level (`CARD.outPitchDeg`, its height at `outM`). */
export const cardLowDeg = (): number => CARD.outPitchDeg - Math.atan(CARD.heightM / 2 / CARD.outM) * DEG

/**
 * The top of the line over the standing keys, in degrees from his eyes' level, their board's middle `pitchDeg` under
 * it at `KEYS.standM`. The board faces his eye (`facing`), so its up leans back by that pitch.
 */
export function keysLineTopDeg(pitchDeg: number): number {
  const [, h] = boardSize(KEYS.standStepM)
  const top = h / 2 + KEYS.standStepM * KEYS_LINE.gapShare + KEYS.standStepM * (1 - KEYS.gapShare)
  const p = pitchDeg / DEG
  return Math.atan2(KEYS.standM * Math.sin(p) + top * Math.cos(p), KEYS.standM * Math.cos(p) - top * Math.sin(p)) * DEG
}

/**
 * The keys and the line over them at a place, as he sees them with his look on the card's middle (where he looks when
 * he types on it): their edges in degrees from that look, and whether the glasses' view holds all of it.
 */
export function cardKeysSeen(sideDeg: number, downDeg: number): { left: number; right: number; top: number; bottom: number; whole: boolean } {
  const [w, h] = boardSize(KEYS.standStepM)
  const line = KEYS.standStepM * (1 + KEYS_LINE.gapShare)
  const [halfW, halfH] = [w / 2 + KEYS.marginM, (h + line) / 2 + KEYS.marginM]
  const [sy, cy, sp, cp] = [Math.sin(sideDeg * RAD), Math.cos(sideDeg * RAD), Math.sin(downDeg * RAD), Math.cos(downDeg * RAD)]
  const mid = [sy * cp * KEYS.standM, PAGES.eyeM + sp * KEYS.standM + line / 2, -cy * cp * KEYS.standM]
  const look = { yaw: sideDeg, pitch: CARD.outPitchDeg }
  const seen = [-1, 1].flatMap(a => [-1, 1].map(b => seenAt([mid[0] + cy * a * halfW, mid[1] + b * halfH, mid[2] + sy * a * halfW], look)))
  const xs = seen.map(s => (s ? Math.atan(s[0]) / RAD : NaN))
  const ys = seen.map(s => (s ? Math.atan(s[1]) / RAD : NaN))
  const [left, right, bottom, top] = [Math.min(...xs), Math.max(...xs), Math.min(...ys), Math.max(...ys)]
  const whole = seen.every(Boolean) && left >= -PLACES.viewHalfYaw && right <= PLACES.viewHalfYaw && bottom >= -PLACES.viewHalfPitch && top <= PLACES.viewHalfPitch
  return { left, right, top, bottom, whole }
}

type P3 = { x: number; y: number; z: number }

/** What the keys and the line over them take in his view, their board's middle at `at` (a point as his head sees it, see `fromHead`). */
export function cardKeysBox(at: P3) {
  const [w, h] = boardSize(KEYS.standStepM)
  const line = KEYS.standStepM * (1 + KEYS_LINE.gapShare)
  return seenBox({ x: at.x, y: at.y + line / 2, z: at.z }, w + 2 * KEYS.marginM, h + line + 2 * KEYS.marginM)
}

/** A point of the room as his head sees it: `places.ts` reckons a view from the room's middle at eye height, so the point is moved by where his head is from there (T9). */
const fromHead = (head: P3, p: readonly number[]): [number, number, number] => [p[0] - head.x, p[1] - (head.y - PAGES.eyeM), p[2] - head.z]

/** A point so far to the right of straight ahead, so far up and so far away, as his head sees it. */
const pointAt = (yawDeg: number, pitchDeg: number, distanceM: number): P3 => {
  const [yaw, pitch] = [yawDeg * RAD, pitchDeg * RAD]
  return { x: Math.sin(yaw) * Math.cos(pitch) * distanceM, y: PAGES.eyeM + Math.sin(pitch) * distanceM, z: -Math.cos(yaw) * Math.cos(pitch) * distanceM }
}

/**
 * The Bots of `seen` (feet, as his head sees them) that lie under the keys and their line at a place of his look at
 * `lookYawDeg`, each with its board; with `withCard` also those under the card that stands over the keys there.
 */
function under(lookYawDeg: number, [side, down]: readonly [number, number], seen: readonly (readonly number[])[], withCard: boolean): readonly (readonly number[])[] {
  const keys = cardKeysBox(pointAt(lookYawDeg + side, down, KEYS.standM))
  const card = withCard ? seenBox(pointAt(lookYawDeg + side, CARD.outPitchDeg, CARD.outM), CARD.widthM, CARD.heightM) : null
  return seen.filter(feet => onBox(feet, keys, PLACES.airDeg, WITH_BOARD) || (!!card && onBox(feet, card, PLACES.airDeg, WITH_BOARD)))
}

/** Whether the card or its keys, at `place` of his look at `lookYawDeg`, lie over a Bot of `busy` (feet), with its board. */
export function cardKeysOver(head: P3, lookYawDeg: number, place: readonly [number, number], busy: readonly (readonly number[])[]): boolean {
  return under(lookYawDeg, place, busy.map(feet => fromHead(head, feet)), true).length > 0
}

/**
 * The place of `CARD_KEYS_TRIES` for the keys, his head at `head` and his look at `lookYawDeg`: the first whose keys
 * and line lie over none of `busy` (the feet of each Bot that asks or failed, with its board); with `withCard` the
 * card over them lies over none either. With none clear (M84): the place whose nearest such Bot is the farthest from
 * his look, so the keys cover the one he looks at least; Home's line still names who asks. It is read as the card
 * comes out, and at a room switch when the card or the keys lie over such a Bot of the new room (`useChatCard`): the
 * keys never move while he types, nor inside one room.
 */
export function cardKeysSpot(head: P3, lookYawDeg: number, busy: readonly (readonly number[])[], withCard = false): readonly [number, number] {
  const seen = busy.map(feet => fromHead(head, feet))
  const away = (p: readonly number[]) => Math.abs(((Math.atan2(p[0], -p[2]) / RAD - lookYawDeg + 540) % 360) - 180)
  const covered = CARD_KEYS_TRIES.map(place => under(lookYawDeg, place, seen, withCard))
  const clear = covered.findIndex(c => c.length === 0)
  if (clear >= 0) return CARD_KEYS_TRIES[clear]
  const nearest = covered.map(c => Math.min(...c.map(away)))
  return CARD_KEYS_TRIES[nearest.indexOf(Math.max(...nearest))]
}
