/**
 * A Bot's 3D dashboard in the room of pages, as data (`Dash3d.tsx` stands it, `dashKit.tsx` draws its parts,
 * `useDash3d.ts` reads it). Plain values and pure functions.
 *
 * The Bot writes a small document, the layout, into a field of its own Dashboard (`DASH_FIELD`), with the tool it
 * writes every other value with. The layout names parts of a fixed kit and, for each, the field of the Bot's
 * Dashboard that it shows. It is DATA: a part's kind is picked from `DASH_KINDS`, a field is looked up by name, and
 * nothing in a layout is run, fetched or drawn as markup. Every size and every colour is the page's.
 *
 *   { "v": 1, "parts": [ { "kind": "ring", "name": "Checks", "field": "checks_done", "max": 20 }, ... ] }
 *
 * Parts stand in one row, in the list's order. `readLayout` holds a layout to its limits and never throws: a
 * document that is no layout gives no parts and says why; one part that is wrong becomes an empty socket that says
 * why, and the others stand. `partValue` reads what a part shows out of the Dashboard's values.
 */
import { plainNumber } from '../glance'
import { crewText } from '../text'

/** The field of a Bot's Dashboard that holds its layout. */
export const DASH_FIELD = 'xr_layout'
export const DASH_KINDS = ['number', 'bar', 'ring', 'lamps'] as const
export type DashKind = (typeof DASH_KINDS)[number]

/** The limits a layout is held to, whoever wrote it. */
export const DASH = {
  version: 1,
  mostParts: 6,
  /** A part's name, in letters; each letter keeps at most `marksMost` marks that are drawn over or under it. */
  nameMax: 16,
  marksMost: 2,
  /** The number over a part, in characters; a longer one is written with an exponent. */
  numberMax: 6,
  /** How many keys a path walks into a value, and how long a path's key or a lamp's key is, in characters. */
  pathKeys: 4,
  keyMax: 64,
  /**
   * The raw name and the raw path are cut by their length before any work, in signs (code points), in both readers
   * (`dashboard_xr_layout.py`, M74): a name of more signs is a bad name, a path of more a bad path. A good path is
   * at most 4 keys of 64 and 3 dots: 259.
   */
  nameRawMax: 256,
  pathRawMax: 259,
  /** How many lamps a part draws; the rest are only counted. */
  mostLamps: 12,
  /** A share is drawn up to this much over its limit. */
  overShare: 1.2,
} as const

/** A field's name, as the Dashboard's own model spells one. */
const FIELD = /^[a-z][a-z0-9_]{0,63}$/
const KEY = new RegExp(`^[A-Za-z0-9_]{1,${DASH.keyMax}}$`)
/** What a part shows for a value that is missing or that its kind does not take. */
export const NO_VALUE = '\u2014'
/** The units a number may carry. Any other is dropped: a unit is not a place for words. */
export const DASH_UNITS: readonly string[] = ['%', '$', 'ms', '/s']
/** Characters that are not drawn but change what is: controls, and the format characters (direction marks, joiners). */
const UNSEEN = /[\p{Cc}\p{Cf}]/gu
const MARK = /\p{M}/u

/** Why a layout, or one part of it, is not drawn. Each has its own short line (`xr.dash3d_why_*`). */
export type DashWhy = 'not_layout' | 'version' | 'no_parts' | 'too_many' | 'part' | 'kind' | 'name' | 'field' | 'path' | 'max' | 'key'

export interface DashPart {
  kind: DashKind
  name: string
  field: string
  /** Keys walked into the field's value, when it is a record. */
  path: readonly string[]
  /** A bar's or a ring's limit: a number, or the name of the field that holds it. Null for the other kinds. */
  max: number | string | null
  /** The key of a lamp's row that holds its state. */
  key: string
  unit: string
}
/** The place of a part that is wrong: drawn as an empty socket. */
export interface DashSocket { broken: DashWhy }
export type DashPlace = DashPart | DashSocket
export const isPart = (p: DashPlace): p is DashPart => !('broken' in p)

export interface DashLayout {
  parts: readonly DashPlace[]
  /** Why the whole document is no layout; '' when it is one. */
  why: DashWhy | ''
}

const record = (v: unknown): v is Record<string, unknown> => typeof v === 'object' && v !== null && !Array.isArray(v)
/** A key of a record that is its own: a name like `constructor` finds nothing. */
const own = (o: Record<string, unknown>, key: string): unknown => (Object.hasOwn(o, key) ? o[key] : undefined)

/**
 * A name of a layout as it is drawn: no control or format character, none of the room's own state glyphs, no mark
 * with no letter under it, and at most `DASH.marksMost` marks on one letter (a pile of them draws a smear over
 * the lines above).
 */
function clean(v: unknown): string | null {
  if (typeof v !== 'string') return null
  let out = ''
  let marks: number = DASH.marksMost
  // Counted with every mark apart from its letter, then written the short way again.
  for (const c of crewText(v.replace(UNSEEN, '')).normalize('NFD').trim()) {
    if (!MARK.test(c)) { out += c; marks = 0 } else if (marks < DASH.marksMost) { out += c; marks += 1 }
  }
  return out.normalize('NFC')
}
/** How many letters a name has: the marks on a letter are part of it. */
const letters = (s: string): number => [...s.normalize('NFD')].filter(c => !MARK.test(c)).length

/** Whether a text holds more than `n` signs, counted only when its code units could hold that many. */
const longer = (s: string, n: number): boolean => s.length > n && (s.length > 2 * n || [...s].length > n)

function readPart(raw: unknown): DashPlace {
  if (!record(raw)) return { broken: 'part' }
  const kind = own(raw, 'kind')
  if (typeof kind !== 'string' || !(DASH_KINDS as readonly string[]).includes(kind)) return { broken: 'kind' }
  const rawName = own(raw, 'name')
  if (typeof rawName === 'string' && longer(rawName, DASH.nameRawMax)) return { broken: 'name' }
  const name = clean(rawName)
  if (!name || letters(name) === 0 || letters(name) > DASH.nameMax) return { broken: 'name' }
  const field = own(raw, 'field')
  if (typeof field !== 'string' || !FIELD.test(field)) return { broken: 'field' }
  const rawPath = own(raw, 'path')
  if (typeof rawPath === 'string' && longer(rawPath, DASH.pathRawMax)) return { broken: 'path' }
  const path = rawPath === undefined ? [] : typeof rawPath === 'string' ? rawPath.split('.') : null
  if (!path || path.length > DASH.pathKeys || !path.every(k => KEY.test(k))) return { broken: 'path' }
  let max: number | string | null = null
  if (kind === 'bar' || kind === 'ring') {
    const m = own(raw, 'max')
    if (typeof m === 'number' ? !(Number.isFinite(m) && m > 0) : !(typeof m === 'string' && FIELD.test(m))) return { broken: 'max' }
    max = m as number | string
  }
  // A lamp's key only: on another kind the key is read by nothing, as a `max` on a number is, so it breaks nothing.
  const rawKey = kind === 'lamps' ? own(raw, 'key') : undefined
  if (rawKey !== undefined && !(typeof rawKey === 'string' && KEY.test(rawKey))) return { broken: 'key' }
  const rawUnit = own(raw, 'unit')
  const unit = typeof rawUnit === 'string' && DASH_UNITS.includes(rawUnit) ? rawUnit : ''
  return { kind: kind as DashKind, name, field, path, max, key: typeof rawKey === 'string' ? rawKey : 'state', unit }
}

/** A layout out of whatever the field holds. Never throws; what is wrong is dropped and named. */
export function readLayout(raw: unknown): DashLayout {
  const no = (why: DashWhy): DashLayout => ({ parts: [], why })
  if (!record(raw)) return no('not_layout')
  if (own(raw, 'v') !== DASH.version) return no('version')
  const parts = own(raw, 'parts')
  if (!Array.isArray(parts)) return no('not_layout')
  if (parts.length === 0) return no('no_parts')
  if (parts.length > DASH.mostParts) return no('too_many')
  return { parts: parts.map(readPart), why: '' }
}

/**
 * What a lamp says of the thing it stands for: `other` is any word outside the list, drawn grey. "Needs you" is not
 * in the list, and no lamp is drawn in a colour that the room uses for a Bot's own state (`dashKit.tsx`): that a Bot
 * needs the person, or failed, is said by the room at the Bot's feet and is never the Bot's to draw.
 */
export const LAMP_STATES = ['idle', 'working', 'done', 'failed'] as const
export type LampState = (typeof LAMP_STATES)[number] | 'other'

export interface DashValue {
  /** False when the field, the path or the limit is not there or is not what the part shows. */
  found: boolean
  /** The reading: a number's, a bar's, a ring's; for lamps, how many there are. */
  number: number | null
  /** The limit it is read against, when the part has one. */
  max: number | null
  /** The reading as a share of its limit, from 0 to `DASH.overShare`. */
  share: number | null
  /** The lamps that are drawn, at most `DASH.mostLamps`. */
  lamps: readonly LampState[]
}

const NOT_FOUND: DashValue = { found: false, number: null, max: null, share: null, lamps: [] }

/** A reading as a number: a finite number, or text that is a plain number. */
const numberOf = (v: unknown): number | null => (typeof v === 'number' ? (Number.isFinite(v) ? v : null) : typeof v === 'string' ? plainNumber(v) : null)

/** What a part shows, read from the Dashboard's values by name. */
export function partValue(part: DashPart, fields: Readonly<Record<string, unknown>> | null | undefined): DashValue {
  if (!fields || !Object.hasOwn(fields, part.field)) return NOT_FOUND
  let v: unknown = fields[part.field]
  for (const k of part.path) {
    if (!record(v) || !Object.hasOwn(v, k)) return NOT_FOUND
    v = v[k]
  }
  if (part.kind === 'lamps') {
    if (!Array.isArray(v)) return NOT_FOUND
    const lamps = v.slice(0, DASH.mostLamps).map((row): LampState => {
      const s = record(row) ? own(row, part.key) : row
      return typeof s === 'string' && (LAMP_STATES as readonly string[]).includes(s) ? (s as LampState) : 'other'
    })
    return { found: true, number: v.length, max: null, share: null, lamps }
  }
  const n = numberOf(v)
  if (n === null) return NOT_FOUND
  if (part.kind === 'number') return { found: true, number: n, max: null, share: null, lamps: [] }
  const max = typeof part.max === 'number' ? part.max : typeof part.max === 'string' && Object.hasOwn(fields, part.max) ? numberOf(fields[part.max]) : null
  if (max === null || !(max > 0)) return NOT_FOUND
  return { found: true, number: n, max, share: Math.max(0, Math.min(DASH.overShare, n / max)), lamps: [] }
}

/**
 * The number over a taken part, as text: the reading (a ring's with its limit), with the part's unit. `fmt` writes
 * a number in the reader's language. A part with nothing to show says a dash.
 */
export function partText(part: DashPart, value: DashValue, fmt: (n: number) => string): string {
  if (!value.found || value.number === null) return NO_VALUE
  const n = shortNumber(value.number, fmt)
  if (part.kind === 'ring' && value.max !== null) return `${n}/${shortNumber(value.max, fmt)}`
  return part.unit ? `${n} ${part.unit}` : n
}

/** A number in at most `DASH.numberMax` characters: as `fmt` writes it (compact), else with an exponent. */
export function shortNumber(n: number, fmt: (n: number) => string): string {
  const s = fmt(n)
  return [...s].length <= DASH.numberMax ? s : n.toExponential(0).replace('+', '')
}

/** The colours of the parts. None is a colour that the room uses for a Bot's own state. */
export interface DataHues {
  /** A bar's and a ring's filling, and a lamp that works. */
  fill: number
  /** A lamp that is done. */
  done: number
  /** A lamp that failed, and the part of a reading that is over its limit. */
  failed: number
}

/** Hues a part may take, in the order they are tried, in degrees round the colour wheel. */
const DATA_HUES = [190, 265, 150, 300, 225, 95, 170, 330] as const
/** A data hue stands at least this far from every state colour on the wheel. */
export const HUE_APART_DEG = 35

/** The hue (degrees) and the saturation (0 to 1) of a colour written as 0xRRGGBB. */
export function hueOf(gl: number): { hue: number; sat: number } {
  const r = ((gl >> 16) & 255) / 255
  const g = ((gl >> 8) & 255) / 255
  const b = (gl & 255) / 255
  const hi = Math.max(r, g, b)
  const lo = Math.min(r, g, b)
  const d = hi - lo
  if (d === 0) return { hue: 0, sat: 0 }
  const hue = hi === r ? ((g - b) / d) % 6 : hi === g ? (b - r) / d + 2 : (r - g) / d + 4
  const light = (hi + lo) / 2
  return { hue: (hue * 60 + 360) % 360, sat: d / (1 - Math.abs(2 * light - 1)) }
}

/** A colour of the given hue, as 0xRRGGBB: bright enough to read on the room's dark, one strength for all. */
function hueGl(hue: number): number {
  const s = 0.62
  const l = 0.64
  const c = (1 - Math.abs(2 * l - 1)) * s
  const x = c * (1 - Math.abs(((hue / 60) % 2) - 1))
  const m = l - c / 2
  const [r, g, b] = hue < 60 ? [c, x, 0] : hue < 120 ? [x, c, 0] : hue < 180 ? [0, c, x] : hue < 240 ? [0, x, c] : hue < 300 ? [x, 0, c] : [c, 0, x]
  const to = (v: number) => Math.round((v + m) * 255)
  return (to(r) << 16) | (to(g) << 8) | to(b)
}

const apart = (a: number, b: number): number => { const d = Math.abs(a - b) % 360; return Math.min(d, 360 - d) }

/**
 * The parts' colours for a room whose Bots' states are drawn in `stateColors` (the ring at a Bot's feet, its rim):
 * three hues of the data's own list, each at least `HUE_APART_DEG` from every state colour that has a hue, so that
 * nothing a Bot stands at its own feet reads as the room saying "this Bot works, needs you, failed".
 */
export function dataHues(stateColors: readonly number[]): DataHues {
  const taken = stateColors.map(hueOf).filter(c => c.sat > 0.15).map(c => c.hue)
  const near = (h: number): number => Math.min(360, ...taken.map(t => apart(h, t)))
  const picked: number[] = []
  // First the hues that stand apart from every state colour and from each other; with too few, the farthest left.
  for (const h of DATA_HUES) if (picked.length < 3 && near(h) >= HUE_APART_DEG && picked.every(p => apart(p, h) >= HUE_APART_DEG)) picked.push(h)
  for (const h of [...DATA_HUES].sort((a, b) => near(b) - near(a))) if (picked.length < 3 && !picked.includes(h)) picked.push(h)
  const [fill, done, failed] = picked
  return { fill: hueGl(fill), done: hueGl(done), failed: hueGl(failed) }
}

/**
 * The dashboard as an object, in metres: solid parts in a row on one tray. Taken, the tray has a sloped lip at its
 * front that carries the words. A part's own box is `part`: so wide, so tall, so deep.
 */
export const FORM = {
  part: [0.08, 0.16, 0.07],
  /** Taken: from one part to the next. */
  pitchM: 0.13,
  /** The tray: how thick and how deep it is, and how far it reaches past the row of parts at each end. */
  trayThickM: 0.012,
  trayDeepM: 0.1,
  trayEndM: 0.01,
  /** The lip at the tray's front: how long its slope is, and how far it leans back from upright. */
  lipM: 0.085,
  lipLeanDeg: 45,
  /** At rest the parts stand close: each as wide as its kind is, with this much air between two. */
  restGapM: 0.012,
  /** At rest: no wider than this (a Bot's body is 21.6 cm wide), never larger than this share of the taken form. */
  restWideM: 0.21,
  restScale: 0.62,
  /**
   * At rest its Bot holds it in front of its own body: the tray's far edge this far in front of the feet, the
   * tray's top this far over them.
   */
  restFrontM: 0.1,
  restLiftM: 0.045,
} as const

/** How wide each kind of part is; a socket (a part that is wrong) is the last. */
export const PART_WIDE: Record<DashKind | 'socket', number> = { number: 0.1, bar: 0.054, ring: 0.124, lamps: 0.12, socket: 0.06 }

/** How wide the taken tray of so many parts is. */
export const trayWideM = (parts: number): number => Math.max(1, Math.min(DASH.mostParts, parts)) * FORM.pitchM + 2 * FORM.trayEndM

/** Where each part stands across the taken tray: one pitch apart, about its middle. */
export const takenRow = (parts: number): number[] => Array.from({ length: Math.max(1, parts) }, (_v, i) => (i - (Math.max(1, parts) - 1) / 2) * FORM.pitchM)

/** At rest the parts stand close: where each stands across the tray, about its middle, and how wide the tray is. */
export function restRow(kinds: readonly (DashKind | 'socket')[]): { xs: number[]; wideM: number } {
  const list = kinds.length ? kinds : (['socket'] as const)
  const span = list.reduce((sum, k) => sum + PART_WIDE[k], 0) + (list.length - 1) * FORM.restGapM
  let x = -span / 2
  const xs = list.map(k => { const at = x + PART_WIDE[k] / 2; x += PART_WIDE[k] + FORM.restGapM; return at })
  return { xs, wideM: span + 2 * FORM.trayEndM }
}

/**
 * How the rest form stands, for a tray `wideM` wide before it is made small: the same tray with no lip, held by its
 * Bot in front of its own body. Its size against the taken form; how wide, deep and tall it is; how far in front of
 * the Bot's feet its middle stands (`centreM`); and how far over the feet its tray's top is (`liftM`). It is as large
 * as its Bot's body is wide, so it is in front of no neighbour.
 */
export function restBox(wideM: number): { scale: number; wideM: number; deepM: number; tallM: number; centreM: number; liftM: number } {
  const scale = Math.min(FORM.restScale, FORM.restWideM / wideM)
  const deepM = FORM.trayDeepM * scale
  return { scale, wideM: wideM * scale, deepM, tallM: FORM.part[1] * scale, centreM: FORM.restFrontM + deepM / 2, liftM: FORM.restLiftM }
}

/**
 * One step that brings the taken form whole into the lens: `corners` the eight corners of its own drawn box, turned
 * as it stands (never a box laid along the room's axes: a form turned 45 degrees would read a near corner that is
 * not there, M71), seen from `eye`, a lens `halfFovDeg` over and under `pitchDeg`. The lowest and highest of its corners as angles
 * at the eye: when that span is wider than the lens leaves (`TAKEN_MARGIN_DEG` at each end), the form stands `k`
 * times farther; else it moves up or down by `dy` metres so its lowest corner is the margin over the lens's foot.
 */
export function fitTaken(corners: readonly (readonly [number, number, number])[], eye: readonly [number, number, number], pitchDeg: number, halfFovDeg: number, sides?: { yawRad: number; halfWideDeg: number }): { k: number; dy: number } {
  const lowT = pitchDeg - halfFovDeg + TAKEN_MARGIN_DEG
  const highT = pitchDeg + halfFovDeg - TAKEN_MARGIN_DEG
  let low = Infinity, high = -Infinity, lowY = 0, lowH = 1
  // Across too (`sides`: the way he faced, and the lens's half width): a tray of six parts at 0.8 m is about as wide
  // as the Aura's 61 degrees and stood cut at both sides (w1112). Its corners' angles off his heading.
  let left = Infinity, right = -Infinity
  const fx = sides ? -Math.sin(sides.yawRad) : 0, fz = sides ? -Math.cos(sides.yawRad) : -1
  for (const [x, y, z] of corners) {
    const h = Math.max(0.05, Math.hypot(x - eye[0], z - eye[2]))
    const a = (Math.atan2(y - eye[1], h) * 180) / Math.PI
    if (a < low) { low = a; lowY = y; lowH = h }
    if (a > high) high = a
    if (sides) {
      const dx = x - eye[0], dz = z - eye[2]
      const s = (Math.atan2(dx * -fz + dz * fx, Math.max(0.05, dx * fx + dz * fz)) * 180) / Math.PI
      left = Math.min(left, s); right = Math.max(right, s)
    }
  }
  if (!Number.isFinite(low)) return { k: 1, dy: 0 }
  const tall = (high - low) / (highT - lowT)
  const wideRoom = sides ? 2 * (sides.halfWideDeg - TAKEN_MARGIN_DEG) : Infinity
  const wide = sides && Number.isFinite(left) ? (right - left) / wideRoom : 0
  if (tall > 1 || wide > 1) return { k: Math.min(2, Math.max(tall, wide) * 1.05), dy: 0 }
  return { k: 1, dy: eye[1] + Math.tan((lowT * Math.PI) / 180) * lowH - lowY }
}

/** The taken form's lowest point (the lip's lower edge) stands this far over the lens's foot (D99 2). */
export const TAKEN_MARGIN_DEG = 2
/** How far ahead of his eye the taken form's middle stands: at 0.7 m it was too tall for the room the lens leaves. */
export const TAKEN_DISTANCE_M = 0.8
/** The taken form stays whole in the lens while his head turns this far up or down from where he looked at the take (D99 2). */
export const TAKEN_LOOK_DEG = 9

/** What holds a taken form where it stands this frame (D103 2). */
export interface TakenHold {
  /** A pinch is down (`pinchRoute.owner`). */
  pinching: boolean
  /** A pointer (his dot, a hand's ray) lies on a drawn piece of it. */
  pointed: boolean
  /** His look has turned `restDeg` or more within the last `restMs`. */
  moving: boolean
}

/**
 * Whether a taken form that is cut in the lens is placed again where his head rests now (D103 2), by the held
 * board's own rule (`Board.tsx` `HELD_COME`): his look rested (turned less than `restDeg`) for `restMs`, counted
 * from the later of that rest and the take, and nothing holds it. A form that is whole never moves.
 */
export function takenComes(whole: boolean, hold: TakenHold, now: number, restSince: number, takenAt: number, restMs: number): boolean {
  return !whole && !hold.pinching && !hold.pointed && !hold.moving && now - Math.max(restSince, takenAt) >= restMs
}

/** How far along a glide stands, eased at both ends as the board's (`k * k * (3 - 2k)`). */
export function glideShare(done: number): number {
  const k = Math.min(1, Math.max(0, done))
  return k * k * (3 - 2 * k)
}

/** His eye moved this far since the form was last placed: a new look, though he faces the same way (R9). */
export const SAME_PLACE_M = 0.1

/** The fit runs again at most once after a placement, within this long of it: when the lip's words are laid out. */
export const REFIT = { ms: 2000, most: 1 } as const

/**
 * Whether the taken form's fit begins its looks again this frame (M82, D108 4): its looks are done (`n` 4), what is
 * drawn of it is not the box it was fitted for (the lip's words came a few frames late), it has not run again since
 * the placement, the placement was under `REFIT.ms` ago, and no pinch is down. A later change moves nothing.
 */
export function fitAgain(f: { n: number; box: string; at: number; again: number }, box: string, now: number, pinching: boolean): boolean {
  return f.n >= 4 && box !== f.box && f.again < REFIT.most && now - f.at < REFIT.ms && !pinching
}

/** A look as the taken form is placed for it: where his eye is, the way he faces (radians) and how far up (degrees). */
export interface LookAt { eye: readonly [number, number, number]; yawRad: number; pitchDeg: number }

/**
 * Whether `now` is the look the form was last placed for (`last`): his eye within `SAME_PLACE_M` of it (R9), and his
 * heading and pitch within `restDeg`. Then the form is not placed again, so a form the fit cannot bring whole does not
 * move again and again.
 */
export function sameLook(now: LookAt, last: LookAt, restDeg: number): boolean {
  if (Math.hypot(now.eye[0] - last.eye[0], now.eye[1] - last.eye[1], now.eye[2] - last.eye[2]) >= SAME_PLACE_M) return false
  const turn = Math.abs(Math.atan2(Math.sin(now.yawRad - last.yawRad), Math.cos(now.yawRad - last.yawRad))) * (180 / Math.PI)
  return turn < restDeg && Math.abs(now.pitchDeg - last.pitchDeg) < restDeg
}

/**
 * The glide goes on this frame unless a pinch is down (D108 3). His dot or his turning head holds back only the START
 * of a move (`takenComes`): the form glides to the middle of his look, where his dot is, so a glide held for the dot
 * stopped for good under it, cut at the side (M80).
 */
export const glideGoes = (hold: TakenHold): boolean => !hold.pinching

/**
 * Where the taken form's tray top stands, in the room's metres, for an eye at `eyeY` whose head is turned `pitchDeg`
 * up when the form is taken, a lens `halfFovDeg` tall over its middle, and the tray's middle `distM` ahead: its
 * lowest point `TAKEN_MARGIN_DEG` over the lens's foot, so the whole form stands in the lens, as low as it can
 * (a thing set on a table). `topM`: the tallest part over the tray's top.
 */
export function takenTrayTopY(eyeY: number, distM: number, pitchDeg: number, halfFovDeg: number, topM: number): number {
  const lean = (FORM.lipLeanDeg * Math.PI) / 180
  const dropM = FORM.trayThickM + FORM.lipM * Math.cos(lean)
  const nearM = Math.max(0.05, distM - FORM.trayDeepM / 2 - FORM.lipM * Math.sin(lean))
  const foot = ((pitchDeg - halfFovDeg + TAKEN_MARGIN_DEG) * Math.PI) / 180
  const y = eyeY + Math.tan(foot) * nearM + dropM
  // A head turned far up would put it over the eye line: then its top stands at the lens's top margin instead.
  const roof = ((pitchDeg + halfFovDeg - TAKEN_MARGIN_DEG) * Math.PI) / 180
  return Math.min(y, eyeY + Math.tan(roof) * distM - topM)
}

/** A Bot's body as the eye sees it in the room: a column from its feet to its top, `radiusM` round. */
export interface BodyAt { feet: readonly [number, number, number]; top: readonly [number, number, number]; radiusM: number }

/**
 * Whether a body stands between the eye and a point, nearer than the point: the line from the eye to it passes
 * through the body's column. A tray at rest so covered is lifted over its Bot's head (D99 2), where it is seen.
 */
export function hiddenBy(eye: readonly [number, number, number], at: readonly [number, number, number], bodies: readonly BodyAt[], slackM = 0): boolean {
  const dx = at[0] - eye[0], dz = at[2] - eye[2]
  const flat = dx * dx + dz * dz
  if (flat < 1e-9) return false
  return bodies.some(b => {
    const t = ((b.feet[0] - eye[0]) * dx + (b.feet[2] - eye[2]) * dz) / flat
    // Any body whose middle stands nearer than the point counts, one just in front of the tray too (Q7).
    if (t <= 0 || t >= 1) return false
    const px = eye[0] + dx * t - b.feet[0], pz = eye[2] + dz * t - b.feet[2]
    const y = eye[1] + (at[1] - eye[1]) * t
    const r = b.radiusM + slackM
    return px * px + pz * pz < r * r && y >= b.feet[1] && y <= b.top[1]
  })
}

/** A hidden tray turns clear only past its hider's radius plus this, and either way only once the answer has held for `holdMs` (M72). */
export const HIDDEN_SLACK = { slackM: 0.02, holdMs: 300 } as const

/**
 * Whether a tray at rest stands hidden (lifted over its Bot) this frame, from what it was (`was`), the answer `now`
 * with no slack and `nowSlack` with `HIDDEN_SLACK.slackM`, since when the other answer has stood (`since`, or null),
 * and whether a pointer is down on it or lies on it (`held`: then nothing changes). Returns the new state and since.
 * A hidden tray stays hidden while its hider is within the slack; a clear one turns hidden at the radius (M72).
 */
export function hiddenStep(was: boolean, now: boolean, nowSlack: boolean, since: number | null, t: number, held: boolean): { hidden: boolean; since: number | null } {
  const wants = was ? nowSlack : now
  if (held || wants === was) return { hidden: was, since: null }
  const from = since ?? t
  return t - from >= HIDDEN_SLACK.holdMs ? { hidden: wants, since: null } : { hidden: was, since: from }
}

/**
 * What stands where a taken tray would go, so that no tray is taken now: a board in his hand, or the new chat card's
 * keyboard. A board that a Bot holds up does not stand there; it only sends a taken tray back when it comes up.
 */
export type TrayBlock = 'board' | 'keys' | null
export const trayBlock = (boardInHand: boolean, keysOut: boolean): TrayBlock => (boardInHand ? 'board' : keysOut ? 'keys' : null)

/** What a press on a tray does: a taken one goes back; another is taken unless something stands in its way. */
export function trayPress(isTaken: boolean, block: TrayBlock): 'back' | 'take' | 'refused' {
  if (isTaken) return 'back'
  return block ? 'refused' : 'take'
}

/** The boards that came up at a Bot since `before` (by ask). Each sends a taken tray back if it stands in front of it. */
export function boardsCameUp(before: ReadonlySet<string>, now: ReadonlySet<string>): string[] {
  return [...now].filter(id => !before.has(id))
}

/** A box on the picture as the eye sees it: x0, y0, x1, y1 in the view's normalized coordinates. */
export type PictureBox = readonly [number, number, number, number]

/** How long after it came up a board is watched for its place (it is drawn and settles at its Bot meanwhile). */
export const CAME_UP_MS = 1500

/**
 * Whether a board's picture overlaps the taken tray's inside the view (-1 to 1 each way): only then does the tray
 * stand in front of that placard as the eye sees them. Two parts that meet outside the view do not count.
 */
export function picturesOverlap(a: PictureBox, b: PictureBox): boolean {
  const x0 = Math.max(a[0], b[0], -1), x1 = Math.min(a[2], b[2], 1)
  const y0 = Math.max(a[1], b[1], -1), y1 = Math.min(a[3], b[3], 1)
  return x0 < x1 && y0 < y1
}

/** A piece of a form as `raysOnDrawn` walks it (a three.js object). */
export interface RayPiece { visible: boolean; parent: RayPiece | null; material?: unknown; isMesh?: boolean; userData: Record<string, unknown>; raycast: unknown; children: RayPiece[] }

/**
 * Each walked mesh's own ray, by mesh. Not in `userData`: a mesh with a `userData` prop gets a new object at each
 * render of its form, and one made while the mesh was off would keep "no ray" as its own (D155: a gap on again took
 * no press).
 */
const OWN_RAY = new WeakMap<RayPiece, unknown>()

/**
 * Only a drawn piece of a form is hit by a ray (D140 D). A ray finds a hidden mesh too, and the pointer gives a form
 * one event, for its nearest piece: a hidden piece lying where the rest shape is drawn took the down, and the press
 * was dropped while the form lit (11 of 16 clicks, w1236). The gaps and the outline keep their own rule (their
 * material is never drawn). Each mesh's own ray is kept by `OWN_RAY`, the ray it had when the walk first met it.
 */
export function raysOnDrawn(root: RayPiece, noRay: unknown): void {
  const walk = (o: RayPiece) => {
    // The outline sets its own ray each frame (`Dash3d.tsx`, D114 2). A gap's press mesh is never drawn (its material
    // is hidden), so it is judged by whether it and the groups above it are shown: switched off (the form taken, or
    // the gap wide), it is not hit either (D147 4: five gap meshes kept a ray while off, w1242).
    if (o.isMesh && !o.userData.dashOutline) {
      let own = OWN_RAY.get(o)
      if (own === undefined) { own = o.raycast; OWN_RAY.set(o, own) }
      const shown = o.userData.dashGap ? drawnUnder({ visible: o.visible, parent: o.parent }, root) : drawnUnder(o, root)
      const want = shown ? own : noRay
      if (o.raycast !== want) o.raycast = want
    }
    for (const c of o.children) walk(c)
  }
  walk(root)
}

/** What of a dashboard a ray found: a thing with a parent, whether it is shown, and its material when it has one. */
export interface Found { visible: boolean; parent: Found | null; material?: unknown }

/**
 * Whether a ray's hit is a piece of the dashboard that is drawn: it and every group above it up to the dashboard's
 * own are shown, and its material is not hidden. Only such a piece takes a press, so a press where the Bot behind it
 * shows is the Bot's (D55 5). A ray also finds hidden meshes: the lip folded at rest, the tray drawn by the merged
 * shape at rest.
 */
export function drawnUnder(hit: Found | null, root: Found | null): boolean {
  for (let o = hit; o && o !== root; o = o.parent) {
    const m = o.material as { visible?: boolean } | undefined
    if (!o.visible || m?.visible === false) return false
  }
  // A dashboard that is not shown (its Bot sits at a desk, or before its first frame) takes no press (small 7).
  return root ? root.visible : true
}

/** A gap between two parts of a dashboard at rest narrower than this, as his eye sees it, is the dashboard's (D56 3). */
export const GAP_TAKES_DEG = 0.5

/** A socket's height over the tray, for the gap beside it (a socket is a low lid). */
const SOCKET_TOP_M = 0.02

/**
 * The gaps between neighbouring parts of the rest row, in the row's own metres before the rest scale: where each
 * begins and ends across the row, and how high it is (the lower of the two parts beside it). `tops`: each kind's
 * height over the tray (`dashKit.PART_TOP`).
 */
export function restGaps(kinds: readonly (DashKind | 'socket')[], tops: Partial<Record<DashKind, number>>): { x0: number; x1: number; top: number }[] {
  const { xs } = restRow(kinds)
  const top = (k: DashKind | 'socket') => (k === 'socket' ? SOCKET_TOP_M : tops[k] ?? SOCKET_TOP_M)
  return kinds.slice(1).map((k, i) => ({ x0: xs[i] + PART_WIDE[kinds[i]] / 2, x1: xs[i + 1] - PART_WIDE[k] / 2, top: Math.min(top(kinds[i]), top(k)) }))
}

/** The slack about `GAP_TAKES_DEG`: a gap turns the dashboard's under it less this, and the Bot's over it plus this (S4). */
export const GAP_SLACK_DEG = 0.05

/**
 * Whether a gap `gapM` wide at `distanceM` from his eye is the dashboard's: narrower than `GAP_TAKES_DEG`. `was`: what
 * it was the frame before; then it changes only past the slack (on under 0.45 degrees, off over 0.55), so a gap at
 * the edge does not hand its press to and fro each frame as he leans (REVIEW-19 S4).
 */
export const gapTakes = (gapM: number, distanceM: number, was?: boolean): boolean => {
  if (!(distanceM > 0)) return false
  const deg = (Math.atan2(gapM, distanceM) * 180) / Math.PI
  if (was === undefined) return deg < GAP_TAKES_DEG
  return was ? deg <= GAP_TAKES_DEG + GAP_SLACK_DEG : deg < GAP_TAKES_DEG - GAP_SLACK_DEG
}

/**
 * The fixed words that say what a part shows, for the Bot that wrote it (the reply to its write,
 * `dashboard_xr_layout.py` `part_says`). Both readers are held to one file of cases
 * (`test/fixtures/xr_layout_cases.json`, `value_cases`).
 */
export const DASH_SAYS = {
  /** The part's field is not among the values, or holds none. */
  noValue: 'a dash: its field holds no value',
  noPath: 'a dash: its path finds nothing',
  notList: 'a dash: not a list',
  notNumber: 'a dash: not a number',
  noLimit: 'a dash: its limit is no number above 0',
  /** A number this long is written with an exponent in the room; here it is only named. */
  big: 'a number of more than 12 digits',
  over: 'over its limit',
  under: 'under 0',
  drawn: 'drawn',
  grey: 'grey',
} as const

/**
 * A number in plain signs, the same in both readers: whole when it is whole, else two places after the point, a
 * half rounded away from 0, with no zeros at its end. The room itself writes it short in the reader's language
 * (`shortNumber`), which the gateway cannot repeat.
 */
export function sayNumber(n: number): string {
  if (Math.abs(n) >= 1e12) return DASH_SAYS.big
  const s = n.toFixed(2).replace(/(\.\d*?)0+$/, '$1').replace(/\.$/, '')
  return s === '-0' ? '0' : s
}

/**
 * What a part shows now, in fixed words: its kind, then the number it reads (a ring's and a bar's with the limit,
 * and whether it is past it; the lamps' count and how many are in each state), or a dash and why. Made of the
 * part's kind, its unit (one of `DASH_UNITS`), numbers, and words of `DASH_SAYS` and `LAMP_STATES`: never the
 * part's name, and never a text that a field holds. It says a dash exactly where `partValue` finds nothing.
 */
export function partSays(part: DashPart, fields: Readonly<Record<string, unknown>> | null | undefined): string {
  const dash = (why: string) => `${part.kind}, ${why}`
  if (!fields || !Object.hasOwn(fields, part.field) || fields[part.field] === null || fields[part.field] === undefined) return dash(DASH_SAYS.noValue)
  let v: unknown = fields[part.field]
  for (const k of part.path) {
    if (!record(v) || !Object.hasOwn(v, k)) return dash(DASH_SAYS.noPath)
    v = v[k]
  }
  const value = partValue(part, fields)
  if (part.kind === 'lamps') {
    if (!value.found) return dash(DASH_SAYS.notList)
    const count = value.number ?? 0
    const each = [...LAMP_STATES, 'other' as const].map(s => [s === 'other' ? DASH_SAYS.grey : s, value.lamps.filter(l => l === s).length] as const).filter(([, n]) => n > 0)
    return `lamps ${count}${count > value.lamps.length ? `, ${value.lamps.length} ${DASH_SAYS.drawn}` : ''}${each.length ? ` (${each.map(([s, n]) => `${n} ${s}`).join(', ')})` : ''}`
  }
  const n = numberOf(v)
  if (n === null) return dash(DASH_SAYS.notNumber)
  if (part.kind === 'number') return `number ${sayNumber(n)}${part.unit ? ` ${part.unit}` : ''}`
  if (!value.found || value.max === null) return dash(DASH_SAYS.noLimit)
  const past = n > value.max ? `, ${DASH_SAYS.over}` : n < 0 ? `, ${DASH_SAYS.under}` : ''
  if (part.kind === 'ring') return `ring ${sayNumber(n)}/${sayNumber(value.max)}${past}`
  return `bar ${sayNumber(n)}${part.unit ? ` ${part.unit}` : ''} of ${sayNumber(value.max)}${past}`
}
