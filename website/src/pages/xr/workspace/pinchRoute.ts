/**
 * Whose a pinch is in a session, and what it is: the rules of the head's pointer (the dot at the picture's centre)
 * and of the hands' rays, as pure functions and one small book. `hands.tsx` and `HeadAim.tsx` act on what they say.
 *
 * The dot is first. A hand points only while it is raised; a hand at rest has no ray, lights nothing, and its pinch
 * is the dot's. One thing is lit at a time, and a pinch presses exactly that. A pinch is one press: per hand the
 * device's own select when it has sent one in this session, else the finger tips' distance, never both. The first
 * pinch owns the button until it ends; the other hand's pinch does nothing meanwhile. The target is fixed at the
 * down; a pinch is a click only when it ends within `PINCH.clickMs` and the hand moved less than `PINCH.stillM`.
 */
import { Quaternion, Vector3, type Object3D } from 'three'

export const PINCH = {
  /** A hand points while its ray's origin is less than this far below the eyes, in metres. */
  raisedM: 0.35,
  /** A pinch that ends sooner than this, with the hand all but still, is a click (ms). */
  clickMs: 500,
  /** The hand's travel under which it counts as still, in metres: a click, or a long press. */
  stillM: 0.015,
  /**
   * After the quiet time a raised hand's book is stale until its next frame (its ray was off): for that long the
   * light is nobody's. A raised hand that gets no frame (its tracking was lost) holds the light back no longer
   * than this (ms): three frames at 30 a second. The hand's book is written once a drawn frame (`hands.tsx`).
   */
  lightWaitMs: 100,
  /** How far the held dot's ray turns for a metre of the hand's travel past `stillM`, in radians. */
  turnPerM: 0.5,
  /** The finger tips' distance at which a pinch begins, and the larger one at which it ends (so it does not flicker). */
  tipsCloseM: 0.015,
  tipsOpenM: 0.03,
  /** With a desk (`desk=1`): a hand is raised only when its ray's origin stands this high over the desk's top. */
  deskRaisedM: 0.15,
  /**
   * With a desk, a pinch of the finger tips is a down only in the frame in which the tips close, and only when the
   * tips were more than `tipsOpenM` apart in every frame of some `deskApartMs`; the last such frame came at most
   * `deskBriskMs` before the close; the wrist moved less than `deskStillM` in the `deskStillMs` before it; and the
   * hand was tracked in every frame of the `deskTrackedMs` before it (`TipsPinch.deskDown`).
   */
  deskApartMs: 150,
  deskBriskMs: 250,
  deskStillM: 0.02,
  deskStillMs: 300,
  deskTrackedMs: 400,
  /** Over how long before the fingers open the pinching hand's speed is counted, in ms. */
  speedMs: 150,
} as const

export type Handed = 'left' | 'right'
export type V3 = readonly [number, number, number]

/**
 * Whether a hand points: its ray's origin is less than `raisedM` below the eyes; with a desk, at least
 * `deskRaisedM` over its top (a hand that lifts off the desk to reach a seat is not raised).
 */
export const raised = (eyeY: number, handY: number, deskY: number | null = null): boolean => (deskY === null ? eyeY - handY < PINCH.raisedM : handY - deskY >= PINCH.deskRaisedM)

/** What a pinch at its end was: a click, a press held long with the hand still, or a drag (a scroll on a page). */
export type PinchKind = 'click' | 'long' | 'drag'

export function pinchKind(heldMs: number, travelM: number): PinchKind {
  if (travelM >= PINCH.stillM) return 'drag'
  return heldMs < PINCH.clickMs ? 'click' : 'long'
}

/** How far his head's line may be from the point a long pinch went down on, at its release (degrees, M54, D96 4). */
export const LONG_LOOK_DEG = 3
/**
 * Where a pinch went down: the thing and the point on it, kept in the thing's own frame, so that a thing that moves
 * (a tile on his palm) carries the point with it.
 */
export interface DownAt { object: Object3D; local: Vector3 }
export function downAt(hit: { object: Object3D; point: Vector3 } | null | undefined): DownAt | null {
  if (!hit) return null
  hit.object.updateMatrixWorld()
  return { object: hit.object, local: hit.object.worldToLocal(hit.point.clone()) }
}
/**
 * Whether his head's own forward line (`eye`'s) passes within `LONG_LOOK_DEG` of the point a long pinch went down on
 * (M47, D91 1; M54). While a pinch is held the dot's ray is the ray of the down, so a long pinch whose eyes left the
 * thing would press what he no longer looks at: its release presses only when this holds, and is a cancel otherwise.
 * A head that drifts half a degree in a 2 s hold, on a name 1.4 degrees tall, still presses.
 */
const LOOK_AT = new Vector3()
const LOOK_DIR = new Vector3()
const LOOK_P = new Vector3()
const LOOK_TURN = new Quaternion()
export function headStillOn(eye: Object3D, down: DownAt | null): boolean {
  if (!down) return false
  eye.getWorldPosition(LOOK_AT)
  eye.getWorldQuaternion(LOOK_TURN)
  LOOK_DIR.set(0, 0, -1).applyQuaternion(LOOK_TURN)
  down.object.updateMatrixWorld()
  const to = down.object.localToWorld(LOOK_P.copy(down.local)).sub(LOOK_AT)
  return (to.angleTo(LOOK_DIR) * 180) / Math.PI <= LONG_LOOK_DEG
}

/**
 * Whether the thing a pinch went down on has its own use for a long hold (D84 2, D86 5). Only these, named here: the
 * stamp (his release of a long pinch presses it, `BoardStamp.tsx`), a Bot (the hold to speak, `Crew.tsx`) and the
 * inside of a page (its own long press, `domSource.ts`, `streamSource.ts`). The pointer lets a long pinch on them go
 * by a cancel. Everywhere else the release of a long pinch is an up and presses as a click does, however slow the
 * frames made it. The chat card's mark tells a long hold apart in its own up (`ChatCard.tsx`).
 */

/**
 * Whether the release of a pinch is let go by a cancel instead of an up. A long one on a thing with its own long hold
 * is; and a long one of the head's dot whose head left the point of the down is (M47, M54). A raised hand's ray is not
 * asked about the head (M53, D96 3): it is not held, it follows the hand, and its up goes where the hand points.
 */
export function releaseCancels(kind: PinchKind, ownLongUse: boolean, byHead: boolean, headOn: () => boolean): boolean {
  if (kind !== 'long') return false
  if (ownLongUse) return true
  return byHead && !headOn()
}

export function holdsLong(object: Object3D | null | undefined): boolean {
  for (let o = object ?? null; o; o = o.parent) {
    if (o.userData.xrPress === 'stamp' || o.userData.xrPress === 'bot') return true
    if (typeof o.userData.xrPage === 'string') return true
  }
  return false
}

/**
 * How far the held dot's ray turns for the hand's travel since the down: none while the hand is all but still, then
 * `turnPerM` a metre. `[yaw, pitch]` in radians: the hand to the right turns the ray right, the hand down turns it
 * down (a page under it is dragged down, as a finger drags it).
 */
export function heldTurn(travel: V3): [number, number] {
  const len = Math.hypot(travel[0], travel[1], travel[2])
  if (len < PINCH.stillM) return [0, 0]
  const k = (PINCH.turnPerM * (len - PINCH.stillM)) / len
  return [-travel[0] * k, travel[1] * k]
}

/** What the desk's rule reads of a hand in one frame: whether it lies flat, and where its wrist is (`null`: not tracked). */
export interface TipsDesk { flat: boolean; wrist: V3 | null }

/** The finger tips of one hand, read only when the device sends no select of its own for that hand. */
export class TipsPinch {
  held = false
  /** With a desk: the tips' distance in the frame before, since when every frame was tracked, and the wrist's places. */
  private before: number | null = null
  private trackedSince: number | null = null
  private wrists: { at: number; wrist: V3 }[] = []
  /** The stretch of frames that has the tips apart now (its first frame), and the last frame of one that lasted `deskApartMs`. */
  private apartFrom: number | null = null
  private apartLast = -Infinity

  /**
   * The tips' distance this frame (`null`: the hand's joints are not tracked). Says what changed. `desk`: the
   * desk's rule holds (`deskDown`). With no desk the tips pinch as they always did.
   */
  read(distanceM: number | null, now = 0, desk: TipsDesk | null = null): 'down' | 'up' | 'lost' | null {
    if (distanceM === null) { this.forget(); if (!this.held) return null; this.held = false; return 'lost' }
    const closes = desk ? this.see(distanceM, now, desk) : true
    if (!this.held && distanceM < PINCH.tipsCloseM) {
      if (!closes) return null
      this.held = true
      return 'down'
    }
    if (this.held && distanceM > PINCH.tipsOpenM) { this.held = false; return 'up' }
    return null
  }

  private forget(): void {
    this.before = this.trackedSince = this.apartFrom = null
    this.wrists = []
    this.apartLast = -Infinity
  }

  /** Takes in one frame with a desk; says whether tips that close in it are a down (`deskDown`). */
  private see(distanceM: number, now: number, desk: TipsDesk): boolean {
    if (desk.wrist === null) { this.forget(); this.before = distanceM; return false }
    if (this.trackedSince === null) this.trackedSince = now
    this.wrists.push({ at: now, wrist: desk.wrist })
    while (this.wrists.length > 1 && now - this.wrists[0].at > PINCH.deskStillMs) this.wrists.shift()
    if (distanceM > PINCH.tipsOpenM) {
      this.apartFrom ??= now
      if (now - this.apartFrom >= PINCH.deskApartMs) this.apartLast = now
    } else this.apartFrom = null
    const closeFrame = this.before !== null && this.before >= PINCH.tipsCloseM && distanceM < PINCH.tipsCloseM
    this.before = distanceM
    return closeFrame && this.deskDown(now, desk)
  }

  /**
   * Whether the tips that close in this frame are a down by the desk's rule: a flat hand never pinches; the tips
   * were apart in every frame of `deskApartMs`, the last of them at most `deskBriskMs` ago (a pinch is brisk, a curl
   * is slow); the wrist moved less than `deskStillM` in the last `deskStillMs` (a hand that comes to rest is not
   * pinching); and the hand was tracked in every frame of the last `deskTrackedMs`.
   */
  private deskDown(now: number, desk: TipsDesk): boolean {
    if (desk.flat || this.trackedSince === null || now - this.trackedSince < PINCH.deskTrackedMs) return false
    if (now - this.apartLast > PINCH.deskBriskMs) return false
    const w = desk.wrist as V3
    return this.wrists.every(({ wrist }) => Math.hypot(wrist[0] - w[0], wrist[1] - w[1], wrist[2] - w[2]) < PINCH.deskStillM)
  }
}

const far = (a: V3, b: V3) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])

/** Whose pointer a pinch presses: the dot's, or a raised hand's ray (whichever hand pinches). */
export type PressTo = 'dot' | Handed

/**
 * One pinch that owns the button: whose hand, whose pointer it presses, when and where it began. `open`: when and
 * where its fingers opened, once they have. A pinch that the device sends owns the button a little longer than that
 * (`pinchGate.ts`), and in that time the hand is already on its way: so from the opening on, the pinch's place, its
 * travel and its speed stand as they were then.
 */
/** `held`: the pinch began before the hand that it will press had its frame (`PinchRoute.awaited`): its down is still to come. */
export interface Owned { hand: Handed; to: PressTo; at: number; from: V3; open?: { at: number; place: V3 }; held?: boolean }

/** The pinch that ended last, as its hand was when its fingers opened: for what reads the hand at the release. */
export interface Released { hand: Handed; place: V3; travelM: number; velocity: V3; mps: number }

/** How a pointer is pressed by a pinch: the dot's (`HeadAim.tsx`) or a hand's ray (`hands.tsx`). */
export interface Press { down: () => void; up: (kind: PinchKind) => void; cancel: () => void }

/**
 * One hand: whether it points (raised), whether its ray lies on a thing of the room and on which, and whether the
 * device sent a select for it. `point`: where its ray meets that thing, in the room; not there with none.
 */
export interface HandBook { raised: boolean; onPanel: boolean; on: string; select: boolean; point?: V3 | null; /** When this hand's ray was last on for a frame, so that `onPanel` was read. */ readAt?: number }

/** What the probe (`?probe=1`) reads of the pinches in a session. */
export interface PinchProbe {
  /** The dot's pointer: its ray, what it lies on (a panel and the page's point, or a thing of the room), and whether it is lit there. */
  head: { origin: V3; direction: V3; on: { panel: string | null; x: number | null; y: number | null; what: string; lit?: boolean } | null; enabled: boolean; faded?: string[] } | null
  /** Whose the one light is now: the dot's, or a raised hand's. */
  light: 'dot' | Handed | null
  hands: Record<Handed, HandBook>
  /** The last pinch: whose, whose pointer it pressed, what took its down and its up, and what it was. */
  last: { hand: Handed; to: PressTo; down: string; up: string; kind: PinchKind | 'cancel'; ms: number } | null
  /** How long the quiet time after a close still runs (ms), read in the frame the dot is drawn; 0 when it does not. */
  quietMs?: number
  /** The last moves of a pointer on a page's face, oldest first: when, on which panel, whose pointer (its id and type: `head` is the dot's, `ray` a hand's, `screen-mouse` the flat preview's), and where its ray met the face, in the room's metres. */
  moves: { ms: number; panel: string; pointerId: number; pointerType: string; at: V3 | null }[]
}

/** The book of the pinches of one session. */
export class PinchRoute {
  owner: Owned | null = null
  /** The pages that would drop a press now, by panel id: the head's dot is not lit over them. That is a page that
   *  takes no input yet (D49, D50, D52) and any other hold of a press (an ask's, the quiet time after a close; D53 2).
   *  A panel keeps its own entry each frame; a render of the panel does not wipe it. */
  readonly notYet = new Set<string>()
  /** Whether a hand points now, and whether its ray lies on a panel, its frame or a handle. */
  readonly hands: Record<Handed, HandBook> = { left: { raised: false, onPanel: false, on: 'nothing', select: false }, right: { raised: false, onPanel: false, on: 'nothing', select: false } }
  readonly probe: PinchProbe = { head: null, light: null, hands: this.hands, last: null, moves: [] }
  /** A pointer moved on a page's face (`Panel.tsx`): kept for the probe, the last 12. */
  movedOn(panel: string, pointerId: number, pointerType: string, at: V3 | null = null, now = performance.now()): void {
    this.probe.moves.push({ ms: Math.round(now), panel, pointerId, pointerType, at: at && [+at[0].toFixed(3), +at[1].toFixed(3), +at[2].toFixed(3)] })
    if (this.probe.moves.length > 12) this.probe.moves.shift()
  }
  /**
   * What the pinch that ends right now was, while its pointer's release (or its cancel) is handed on; else `null`. A
   * page reads it at that release: a long press and a drag end with no click, on either kind of page.
   */
  ending: PinchKind | null = null

  /**
   * Hands a pinch's release on with `ending` set to its kind, for its handlers to read, and clears it on every way out,
   * a handler that throws too. So `ending` is set only inside his own let-go, never at a later cancel of a lost hand.
   */
  release(kind: PinchKind, up: () => void): void {
    this.ending = kind
    try { up() } finally { this.ending = null }
  }
  /**
   * How many pinches have gone down. A thing that keeps a press from its own down reads it there and at its up: the
   * same number means this up is of the pinch that went down on it (G5, J1).
   */
  downs = 0
  /** How each raised hand's ray is pressed, set by that hand while it is there. */
  readonly presses: Partial<Record<Handed, Press>> = {}
  /** How each hand ends its own pinch now, its gate let go at once (`hands.tsx`): for the other hand's start (D55 6). */
  readonly finishers: Partial<Record<Handed, () => void>> = {}
  /** Where each hand's ray begins this frame (the held dot turns by the owner's travel since the down). */
  readonly at: Record<Handed, V3> = { left: [0, 0, 0], right: [0, 0, 0] }
  /**
   * The pinch that ended last, as its hand was when its fingers opened; `null` from the next pinch's down, and for
   * one that was lost. What acts at a pinch's release reads the hand here: the owner is gone by then.
   */
  released: Released | null = null
  /** The owner's hand in the last `PINCH.speedMs` before now, or before its fingers opened. */
  private trail: { at: number; p: V3 }[] = []
  /** With `desk=1`: the height of the desk's top, for the raised line and the tips' rule; `null` with no desk. */
  deskY: number | null = null
  /**
   * With `desk=1` (`DeskTouch.tsx`): the hand whose finger is down on a pad or tile, which owns the press as a pinch
   * does; and whether a tip is near one, which turns the dot off and lets no pinch press. Never set with no desk.
   */
  touch: Handed | null = null
  tipNear = false
  /**
   * The session's quiet time (the flat preview's is `Workspace.tsx`'s): until `quietUntil` no pinch presses
   * anything. It starts at a close, and at a page that goes by itself; after a close each pinch that is held back
   * starts it anew, so a pinch right after a close never reaches the page that stood behind the mark.
   */
  quietUntil = 0
  private quietMs = 0
  /**
   * When the dot's pointer had its last two frames (`HeadAim.tsx` says so each frame, after it has turned the pointer
   * on or off): the newest, and the one before it.
   */
  private dotFrames: [number, number] = [-Infinity, -Infinity]
  /** The dot's pointer had a frame of its own at `now`. */
  dotFrame(now: number): void { this.dotFrames = [now, this.dotFrames[0]] }
  /**
   * Whether the dot's pointer has had two frames since `t`: in the first after a quiet time it is turned on again,
   * and by the end of the second what it lies on has been told so, whichever of the two runs first in a frame.
   */
  dotSeenSince(t: number): boolean { return this.dotFrames[1] > t }
  /**
   * Set by a thing that is drawn over everything and takes the presses that fall on it (the readout's plate,
   * `ReadoutPlate.tsx`), while it does: whether it hides this point of the room from the eye. A hand's ray starts at
   * the hand, so it reaches what such a thing hides without crossing it; a pinch whose ray lies on a hidden point
   * presses nothing. The dot's ray starts at the eye and is stopped by the thing itself.
   */
  veil: ((point: V3) => boolean) | null = null
  private quietClose = false

  /** Whether the quiet time runs at `now`. */
  quiet(now: number): boolean { return now < this.quietUntil }

  /** The quiet time starts now and lasts `ms`; `closed`: a close began it. */
  hush(now: number, ms: number, closed: boolean): void {
    this.quietUntil = now + ms
    this.quietMs = ms
    this.quietClose = closed
  }

  /**
   * The raised hand that the light waits for at `now`, or `null`: the quiet time has ended less than
   * `lightWaitMs` ago and this hand's ray has had no frame since, so its book still says "on no panel" from the
   * quiet time, whatever it points at. Without the wait the light fell to the dot for that one frame, and a pinch
   * in it went to what the dot lay on.
   */
  awaited(now = performance.now()): Handed | null {
    if (this.quietUntil <= 0 || now < this.quietUntil || now - this.quietUntil >= PINCH.lightWaitMs) return null
    for (const h of ['right', 'left'] as const) if (this.hands[h].raised && !((this.hands[h].readAt ?? -Infinity) >= this.quietUntil)) return h
    return null
  }

  /** Whose the one light is: a raised hand's whose ray lies on a panel, else the dot's; while a pinch is held, the one it presses. */
  light(now = performance.now()): 'dot' | Handed | null {
    // While a pinch is held, the light is the one that it presses, until its up or cancel.
    if (this.owner) return this.owner.to
    // In the quiet time nothing is lit, since a pinch presses nothing: the light comes back by itself after it.
    if (this.quiet(now)) return null
    // Right after it, nobody's until each raised hand had a frame with its ray on (`awaited`).
    if (this.awaited(now)) return null
    for (const h of ['right', 'left'] as const) if (this.hands[h].raised && this.hands[h].onPanel) return h
    return 'dot'
  }

  /** What the one lit thing is, for the probe: what the dot lies on, or what the lit hand's ray lies on. */
  lit(now = performance.now()): string {
    const l = this.light(now)
    if (!l) return 'nothing'
    return l === 'dot' ? this.probe.head?.on?.what ?? 'nothing' : this.hands[l].on
  }

  /**
   * A pinch of `hand` began. Returns whose pointer it presses: the one lit thing's, whichever hand pinches. `null`
   * when another pinch owns the button.
   */
  start(hand: Handed, now: number, at: V3, what: string): PressTo | null {
    // The other hand's pinch whose fingers have opened is over when this hand pinches, though its gate has not let it
    // go yet: a pinch of the other hand is its own pinch, never the first one going on (D55 6, small 2).
    const other = this.owner
    if (other && other.hand !== hand && other.open) this.finishers[other.hand]?.()
    if (this.owner || this.touch || this.tipNear) return null
    if (now < this.quietUntil) {
      if (this.quietClose) this.quietUntil = now + this.quietMs
      this.probe.last = { hand, to: 'dot', down: 'quiet', up: 'quiet', kind: 'cancel', ms: 0 }
      return null
    }
    // The light waits for a raised hand's frame: the pinch is that hand's ray's, and its down waits with it, one
    // frame (`taken`). It is never the dot's: the dot is not lit now.
    const wait = this.awaited(now)
    if (wait) {
      this.owner = { hand, to: wait, at: now, from: at, held: true }
      this.downs += 1
      this.released = null
      this.trail = [{ at: now, p: at }]
      this.probe.last = { hand, to: wait, down: 'held', up: '', kind: 'cancel', ms: 0 }
      return null
    }
    const to = this.light(now) ?? 'dot'
    // The lit ray is a hand's and lies on a point that the eye does not see: the pinch presses nothing, as the picture shows.
    const point = to === 'dot' ? null : this.hands[to].point
    if (point && this.veil?.(point)) {
      this.probe.last = { hand, to, down: 'hidden', up: 'hidden', kind: 'cancel', ms: 0 }
      return null
    }
    this.owner = { hand, to, at: now, from: at }
    this.downs += 1
    this.released = null
    this.trail = [{ at: now, p: at }]
    this.probe.last = { hand, to, down: what, up: '', kind: 'cancel', ms: 0 }
    return to
  }

  /**
   * `hand`'s ray has had its frame (its book is read). Whether a pinch that waited for it goes down on it now:
   * yes when the hand is still raised and its ray lies on a thing of the room that the eye sees. Else the pinch is over, as one in the quiet
   * time is: the dot was not lit when it began, so it is not handed to the dot.
   */
  taken(hand: Handed, what: string): boolean {
    const own = this.owner
    if (!own?.held || own.to !== hand) return false
    own.held = false
    const book = this.hands[hand]
    if (book.raised && book.onPanel && !(book.point && this.veil?.(book.point))) { if (this.probe.last) this.probe.last.down = what; return true }
    this.owner = null
    this.trail = []
    if (this.probe.last) { this.probe.last.down = 'gap'; this.probe.last.up = 'gap' }
    return false
  }

  /** Where `hand`'s ray begins at `now`, told once a frame. */
  move(hand: Handed, now: number, at: V3): void {
    this.at[hand] = at
    const own = this.owner
    if (!own || own.hand !== hand || own.open) return
    this.trail.push({ at: now, p: at })
    while (this.trail.length > 2 && now - this.trail[0].at > PINCH.speedMs) this.trail.shift()
  }

  /** The fingers of the pinch of `hand` opened at `now`, with the hand at `at`: the pinch's place, travel and speed stand from here. */
  opened(hand: Handed, now: number, at: V3): void {
    const own = this.owner
    if (own && own.hand === hand && !own.open) own.open = { at: now, place: at }
  }

  /** The fingers of `hand` are together again before the pinch ended (the tracker dropped it for a frame): it goes on as if it had never opened. */
  reclosed(hand: Handed): void {
    const own = this.owner
    if (own && own.hand === hand) delete own.open
  }

  /** Where the hand of the pinch that owns the button is, or was when its fingers opened; `null` with none. */
  place(): V3 | null {
    const own = this.owner
    return own ? own.open?.place ?? this.at[own.hand] : null
  }

  /**
   * The pinch of `hand` ended, at `now` with the hand at `at`; when its fingers were told open before (`opened`),
   * at that time and place instead. Returns what it was and whose pointer it pressed, or `null` when it owned
   * nothing.
   */
  end(hand: Handed, now: number, at: V3, what: string): { to: PressTo; kind: PinchKind } | null {
    const own = this.owner
    if (!own || own.hand !== hand) return null
    const when = own.open?.at ?? now
    const place = own.open?.place ?? at
    const velocity = this.velocity()
    this.owner = null
    this.trail = []
    const travelM = far(place, own.from)
    this.released = { hand, place, travelM, velocity, mps: Math.hypot(velocity[0], velocity[1], velocity[2]) }
    // Its down never came (the hand it waited for had no frame): nothing was pressed, so nothing is let go.
    if (own.held) { if (this.probe.last) { this.probe.last.down = 'gap'; this.probe.last.up = 'gap' } return null }
    const kind = pinchKind(when - own.at, travelM)
    if (this.probe.last) { this.probe.last.up = what; this.probe.last.kind = kind; this.probe.last.ms = Math.round(when - own.at) }
    return { to: own.to, kind }
  }

  /** How far the hand of the pinch that owns the button moved since its down, up to when its fingers opened, in metres; 0 with none. */
  travel(): number {
    const own = this.owner
    const at = this.place()
    return own && at ? far(at, own.from) : 0
  }

  /**
   * How fast the hand of the pinch that owns the button moves, in metres a second along each axis: over the last
   * `PINCH.speedMs`, up to when its fingers opened. Nothing with no pinch, or before its hand was told twice.
   */
  velocity(): V3 {
    const first = this.trail[0]
    const last = this.trail[this.trail.length - 1]
    if (!this.owner || !first || !last || last.at <= first.at) return [0, 0, 0]
    const k = 1000 / (last.at - first.at)
    return [(last.p[0] - first.p[0]) * k, (last.p[1] - first.p[1]) * k, (last.p[2] - first.p[2]) * k]
  }

  /** The hand of the pinch that owns the button is lost: the pinch ends with no click. */
  lost(hand: Handed): PressTo | null {
    const own = this.owner
    if (!own || own.hand !== hand) return null
    this.owner = null
    this.trail = []
    if (this.probe.last) this.probe.last.kind = 'cancel'
    return own.held ? null : own.to
  }
}

/** A hit nearer than this is one of the room's things (a panel, its frame, a handle, a Bot); the empty space's backdrop is farther. */
export const ROOM_THING_M = 40

/** What a pointer's hit is, for the probe: a panel's page or the panel by its id, or another thing of the room. */
export function whatIs(hit: { object: Object3D } | undefined): string {
  if (!hit) return 'nothing'
  for (let o: Object3D | null = hit.object; o; o = o.parent) {
    if (typeof o.userData.xrPage === 'string') return `page:${o.userData.xrPage}`
    if (typeof o.userData.xrPanel === 'string') return `panel:${o.userData.xrPanel}`
    // A dashboard by its mark, whatever piece of it was hit: its pieces have no names (M93).
    if (typeof o.userData.dash3d === 'string') return `dash:${o.userData.dash3d}`
  }
  return hit.object.name || 'room'
}

/** The one book of the XR page's session: one session runs at a time. */
export const pinchRoute = new PinchRoute()
