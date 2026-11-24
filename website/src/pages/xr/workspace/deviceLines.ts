/**
 * What the readout (`?fps=1`) says of the device in a session: a few more lines of numbers that the page already
 * keeps, so that a person who wears the glasses can read them off the plate (`ReadoutPlate.tsx`) and, after the
 * session, copy them from the block on the page (`Readout.tsx`). The rules are here as pure functions and one small
 * book; `DeviceRead.tsx` reads the session each frame and writes the book.
 *
 * Nothing here runs without `fps=1`, and nothing here changes what a hand or the head does.
 */
import { i18nT } from '../../../i18n/t'
import { fmtNumber } from '../../../i18n/format'
import type { Handed, PinchKind, PinchRoute, V3 } from './pinchRoute'

const n = (v: number, digits = 0) => fmtNumber(v, { maximumFractionDigits: digits, minimumFractionDigits: digits })
const plain = (v: number) => fmtNumber(Math.round(v), { useGrouping: false })

/**
 * The angle that one eye's view spans, across and up, in degrees, from its projection matrix (sixteen numbers,
 * column by column). The view need not be the same to both sides of straight ahead.
 */
export function viewDeg(m: ArrayLike<number>): [number, number] {
  const span = (scale: number, skew: number) => ((Math.atan((1 + skew) / scale) - Math.atan((skew - 1) / scale)) * 180) / Math.PI
  return [span(m[0], m[8]), span(m[5], m[9])]
}

/** How the pages stand on composition layers: how many do, how many may, and why none may. */
export interface LayerFacts { on: number; most: number; granted: boolean; off: boolean }

/** The last pinch, as the session's book has it (`pinchRoute.probe.last`), and how the device told of it. */
export interface PinchFacts {
  hand: Handed
  /** The device sent its own select for this hand in this session; else the finger tips' distance is read. */
  select: boolean
  /** The pinch is held now. */
  held: boolean
  kind: PinchKind | 'cancel'
  ms: number
  /** What took its down. */
  on: string
}

/** How still the hand of the last held pinch was. */
export interface StillFacts { hand: Handed; windowMm: number; fromStartMm: number; ms: number }

/** Over how long a hand's travel is counted for `still`: the time in which a held pinch must stay under the still bound. */
export const STILL_WINDOW_MS = 500

const far = (a: V3, b: V3) => Math.hypot(a[0] - b[0], a[1] - b[1], a[2] - b[2])

/**
 * Measures how still the hand of a held pinch is: the largest distance between any two places of the hand that lie
 * at most `STILL_WINDOW_MS` apart, and the largest distance from where the pinch began. It is told each frame of the
 * pinch that owns the button, and keeps the last pinch's numbers after it ends.
 *
 * It reads the hand where the page's own rule reads it (`pinchRoute.place()`): a pinch that the device sends owns
 * the button a little longer than the fingers are together, and from their opening on that place stands still.
 */
export class StillMeter {
  last: StillFacts | null = null
  private since: number | null = null
  private from: V3 = [0, 0, 0]
  private places: { at: number; p: V3 }[] = []

  /** One frame of the session's book of pinches: its owner, where that pinch's hand counts as being, and until when. */
  readRoute(now: number, route: Pick<PinchRoute, 'owner' | 'place'>): void {
    const owner = route.owner
    const at = route.place()
    this.read(owner?.open?.at ?? now, owner && at ? { hand: owner.hand, since: owner.at, at } : null)
  }

  /** One frame. `held`: the pinch that owns the button, when it began, and where its hand is now; `null` with none. */
  read(now: number, held: { hand: Handed; since: number; at: V3 } | null): void {
    if (!held) {
      this.since = null
      this.places = []
      return
    }
    if (this.since !== held.since) {
      this.since = held.since
      this.from = held.at
      this.places = []
      this.last = { hand: held.hand, windowMm: 0, fromStartMm: 0, ms: 0 }
    }
    const last = this.last
    if (!last) return
    while (this.places.length && now - this.places[0].at > STILL_WINDOW_MS) this.places.shift()
    for (const place of this.places) last.windowMm = Math.max(last.windowMm, far(place.p, held.at) * 1000)
    this.places.push({ at: now, p: held.at })
    last.fromStartMm = Math.max(last.fromStartMm, far(this.from, held.at) * 1000)
    last.ms = now - held.since
  }
}

/** One hand, as the device tracks it. */
export interface HandFacts {
  /** `seen`: tracked in this frame. `lost`: it was tracked in this session and is not now. `never`: not yet in this session. */
  state: 'seen' | 'lost' | 'never'
  /** The device gives the hand's joints; else only a ray. */
  joints: boolean
  /**
   * How far under the eyes the hand's ray begins, or began when it was last seen, in centimetres: the point that
   * the raised-hand rule compares (`pinchRoute.ts`, `raised`).
   */
  underCm: number | null
  /** How far under the eyes the wrist was in that same frame; `null` where the device gives no joints. */
  wristCm: number | null
  /** The share of the last frames in which it was tracked, 0 to 1, over `TRACKED_WINDOW_MS`. */
  share: number
}

/** Over how long the share of tracked frames is counted. */
export const TRACKED_WINDOW_MS = 2000

/** Watches one hand frame by frame: whether it is tracked, how low it is, and in what share of the last frames it was tracked. */
export class HandWatch {
  private frames: { at: number; seen: boolean }[] = []
  private underCm: number | null = null
  private wristCm: number | null = null
  private joints = false
  private ever = false

  /**
   * One frame. `rayY`: the height of where the hand's ray begins, or `null` when the device does not track the hand
   * now. `wristY`: the wrist's height, or `null` where the device gives no joints.
   */
  read(now: number, rayY: number | null, eyeY: number, wristY: number | null = null): void {
    while (this.frames.length && now - this.frames[0].at > TRACKED_WINDOW_MS) this.frames.shift()
    this.frames.push({ at: now, seen: rayY !== null })
    if (rayY === null) return
    this.ever = true
    this.joints = wristY !== null
    this.underCm = (eyeY - rayY) * 100
    this.wristCm = wristY === null ? null : (eyeY - wristY) * 100
  }

  facts(): HandFacts {
    const seen = this.frames.length > 0 && this.frames[this.frames.length - 1].seen
    const share = this.frames.length ? this.frames.filter(f => f.seen).length / this.frames.length : 0
    return { state: seen ? 'seen' : this.ever ? 'lost' : 'never', joints: this.joints, underCm: this.underCm, wristCm: this.wristCm, share }
  }
}

/** What a session says of itself and of the device, in one frame. */
export interface DeviceFacts {
  /** `immersive-ar` or `immersive-vr`. */
  mode: string | null
  /** `additive`, `alpha-blend` or `opaque`. */
  blend: string | null
  /** The features the session was granted; `null` when the browser does not say. */
  features: readonly string[] | null
  /** The frames a second the session says it runs at; `null` when it does not say. */
  frameRate: number | null
  /** One eye's view, across and up, in degrees. */
  view: [number, number] | null
  /**
   * The picture the page draws into, in pixels, and what was measured: `base`, the session's one layer of the old
   * kind, whole (every eye's part of it); `layer`, the first projection layer's texture. `eyes`: how many eyes the
   * session draws.
   */
  picture: { size: [number, number]; from: 'base' | 'layer'; eyes: number } | null
  layers: LayerFacts
  /** The last pinch of this session; `null` before the first. */
  pinch: PinchFacts | null
  /** How still the hand of the last held pinch was; `null` before the first. */
  still: StillFacts | null
  hands: Record<Handed, HandFacts>
  /** With a desk (`desk=1`): how high each index finger's tip is over the desk's top, skin to desk, in metres; `null` with no desk. */
  tip: Record<Handed, number | null> | null
}

/**
 * The `device` line: the session's kind, how its picture meets the room, its rate, one eye's view, and the
 * picture's size with what that size is of (which of the session's pictures was read, and for how many eyes).
 */
export function deviceLine(f: DeviceFacts): string {
  const of = f.picture ? ` ${i18nT(f.picture.from === 'base' ? 'xr.readout_drawn_base' : 'xr.readout_drawn_layer', { eyes: plain(f.picture.eyes) })}` : ''
  return i18nT('xr.readout_device', {
    mode: f.mode ?? '-',
    blend: f.blend ?? '-',
    rate: f.frameRate === null ? '-' : n(f.frameRate),
    vw: f.view ? n(f.view[0]) : '-',
    vh: f.view ? n(f.view[1]) : '-',
    pw: f.picture ? plain(f.picture.size[0]) : '-',
    ph: f.picture ? plain(f.picture.size[1]) : '-',
  }) + of
}

/** The `granted` line: the session's features, as the browser names them. */
export function grantedLine(f: DeviceFacts): string {
  return i18nT('xr.readout_granted', { features: f.features === null ? '-' : f.features.length ? [...f.features].sort().join(', ') : i18nT('xr.readout_none') })
}

/** The `layers` line: how many pages stand on a layer, of how many may; and why none may, when none may. */
export function layersLine(l: LayerFacts): string {
  const count = i18nT('xr.readout_layers', { on: n(l.on), most: n(l.most) })
  if (l.off) return `${count} · ${i18nT('xr.readout_layers_off')}`
  return l.granted ? count : `${count} · ${i18nT('xr.readout_layers_not_granted')}`
}

const handName = (hand: Handed) => i18nT(hand === 'left' ? 'xr.readout_left' : 'xr.readout_right')

/**
 * The `pinch` line: whose the last pinch was, where it came from (`select`: the device's own; `tips`: the finger
 * tips' distance), what it was (`held` while it lasts, then `click`, `long`, `drag` or `cancel`), how long it
 * lasted, and what took its down. The words in code type are the page's own names and stay as they are.
 */
export function pinchLine(p: PinchFacts | null): string {
  if (!p) return i18nT('xr.readout_pinch_none')
  return i18nT('xr.readout_pinch', { hand: handName(p.hand), source: p.select ? 'select' : 'tips', kind: p.held ? 'held' : p.kind, ms: n(p.ms), on: p.on || '-' })
}

/** The `still` line: the hand's largest travel in any half second of the last held pinch, its largest from the start, and how long it was held. */
export function stillLine(s: StillFacts | null): string {
  if (!s) return i18nT('xr.readout_still_none')
  return i18nT('xr.readout_still', { mm: n(s.windowMm, 1), total: n(s.fromStartMm, 1), ms: n(s.ms), hand: handName(s.hand) })
}

function handWords(h: HandFacts): string {
  if (h.state === 'never' || h.underCm === null) return i18nT('xr.readout_hand_never')
  const wrist = h.wristCm === null ? '' : `, ${i18nT('xr.readout_hand_wrist', { cm: n(h.wristCm) })}`
  if (h.state === 'lost') return i18nT('xr.readout_hand_lost', { cm: n(h.underCm) }) + wrist
  return i18nT(h.joints ? 'xr.readout_hand_seen' : 'xr.readout_hand_seen_ray', { cm: n(h.underCm), share: n(h.share * 100) }) + wrist
}

/**
 * The `hands` line: each hand seen or lost, how far under the eyes its ray begins (or began), which is the height
 * that decides whether the hand points; the share of the last two seconds it was tracked; and its wrist's height.
 */
export function handsLine(hands: Record<Handed, HandFacts>): string {
  return i18nT('xr.readout_hands', { left: handWords(hands.left), right: handWords(hands.right) })
}

/** The `tip` line (`desk=1`): how high each index finger's tip is over the desk, in millimetres; under the desk's top it is less than nothing. */
export function tipLine(tip: Record<Handed, number | null>): string {
  const mm = (v: number | null) => (v === null ? '-' : n(v * 1000))
  return i18nT('xr.readout_tip', { left: mm(tip.left), right: mm(tip.right) })
}

/** The device's lines, first line first. */
export function deviceLines(f: DeviceFacts): string[] {
  return [deviceLine(f), grantedLine(f), layersLine(f.layers), pinchLine(f.pinch), stillLine(f.still), handsLine(f.hands), ...(f.tip ? [tipLine(f.tip)] : [])]
}

/**
 * The book of the device's facts: written each frame of a session, and kept after it, so that the numbers of the
 * last session can be read and copied on the page.
 */
export class DeviceBook {
  /** The facts of the running session, or of the last one. */
  facts: DeviceFacts | null = null
  /** Whether a session writes the book now. */
  live = false

  /** The lines for the readout: the session's own while it runs; after it, the last session's under a line that says so. */
  lines(): string[] {
    if (!this.facts) return []
    const lines = deviceLines(this.facts)
    return this.live ? lines : [i18nT('xr.readout_last_session'), ...lines]
  }
}

/** The one book of the XR page: one session runs at a time. */
export const deviceBook = new DeviceBook()
