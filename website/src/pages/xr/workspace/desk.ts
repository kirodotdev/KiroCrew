/**
 * The desk (`/xr?...&desk=1`): a flat rectangle in front of the person where each Bot has its own seat and the
 * palette's tiles lie. Plain numbers and pure functions; `Desk.tsx` draws it and `Crew.tsx` stands the Bots on it.
 *
 * The desk is level. Its place is its near edge's middle (`at`, a point of the room's own frame, whose middle is
 * where the person stood at the start), how high its top is (`at[1]`), and which way it faces (`yawDeg`, to the
 * right of straight ahead, the way the person looks over it). Across it runs `u` (to the right), into it `v` (away
 * from the person, from the near edge). The near 20 cm are the hands' rest and hold nothing; the seats and the
 * tiles lie in the band after it, where a finger reaches with no lean; the far 15 cm stay empty.
 *
 * Without the word `desk=1` in the address none of this is read: the eighth's crew row under the first page and the
 * palm's palette stand as they did.
 */
import type { Zone } from './layout'

const RAD = Math.PI / 180

export const DESK = {
  /** How wide and deep the desk is. */
  widthM: 1.0,
  depthM: 0.5,
  /**
   * With no hands seen, the desk stands here at once: its top so far under the eyes, its near edge so far ahead,
   * level and straight ahead (its far edge `depthM` farther).
   */
  defaultUnderEyesM: 0.4,
  defaultNearM: 0.35,
  /** The near band that holds nothing: the hands rest there. */
  restM: 0.2,
  /** The band where the seats and the tiles lie: from `restM` into the desk, this deep. */
  bandM: 0.15,
  /** A seat: its round pad, its Bot's height on it, and from one seat to the next in one row. */
  padM: 0.09,
  botM: 0.08,
  seatStepM: 0.1,
  /**
   * So many seats stand in one row in the band's middle. With up to `seatsMost` they stand as a zigzag of two rows:
   * from one seat to the next `zigStepM` across (13.6 cm along one row: at 13.4 two neighbours' pads would touch),
   * the far row at `zigFarV`, the near one at `zigNearV`, every pad clear of the hands' rest and of its neighbours. Past that, `seatsMost - 1` Bots are seated and the last place
   * is a pad with the number of the others, which opens the Bots page.
   */
  rowMost: 6,
  seatsMost: 8,
  zigStepM: 0.068,
  zigFarV: 0.305,
  zigNearV: 0.245,
  /** A tile of the palette: its side and the air between two; they stand in one row at the band's left. */
  tileM: 0.085,
  tileGapM: 0.008,
  /** The tiles' places: as many as the most tiles, so that the seats stand where they stand in either case. */
  tileSlots: 4,
  /** The air from the tiles' block to the first seat. */
  blockGapM: 0.02,
  /** A page is stood clear of the Bots' heads by this much, as the eye sees them (`deskZone`). */
  clearM: 0.03,
} as const

type V3 = readonly [number, number, number]

/** Where the desk is: its near edge's middle, how high its top is, which way it faces, and how it was set. */
export interface DeskPlace {
  at: [number, number, number]
  yawDeg: number
  /** `default`: no hands set it; `hands`: flat hands did; `kept`: read from the room's record. */
  from: 'default' | 'hands' | 'kept'
  /** A desk set in a session, or in the flat preview (which has no real height): a preview's desk is never used in a session. */
  inSession: boolean
  /** Flat hands confirmed it, or a press landed on it. A kept desk is shown dim until then. */
  confirmed: boolean
  /** Which Bot sits at which seat (`seatOrder`): kept with the desk, so that a Bot keeps its own seat. */
  seats?: readonly string[]
}

/** The desk at its default place for an eye at `eye` that looks `yawDeg` to the right of straight ahead. */
export function defaultDesk(eye: V3, yawDeg = 0, inSession = false): DeskPlace {
  const yaw = yawDeg * RAD
  return {
    at: [eye[0] + Math.sin(yaw) * DESK.defaultNearM, eye[1] - DESK.defaultUnderEyesM, eye[2] - Math.cos(yaw) * DESK.defaultNearM],
    yawDeg, from: 'default', inSession, confirmed: !inSession,
  }
}

/** A point of the desk's top, `u` to the right of its middle and `v` in from its near edge, as a point of the room. */
export function deskPoint(d: DeskPlace, u: number, v: number, up = 0): [number, number, number] {
  const yaw = d.yawDeg * RAD
  // Right is (cos, -sin) turned with the facing; "in" is the facing itself, which is -z at yaw 0.
  return [d.at[0] + Math.cos(yaw) * u + Math.sin(yaw) * v, d.at[1] + up, d.at[2] + Math.sin(yaw) * u - Math.cos(yaw) * v]
}

/** Where one seat is on the desk: across, in, and in which row. */
export interface SeatPlace { u: number; v: number; row: 0 | 1 }

/** The left edge of the seats: right of the tiles' row. */
const seatsFromU = () => -DESK.widthM / 2 + DESK.tileSlots * (DESK.tileM + DESK.tileGapM) - DESK.tileGapM + DESK.blockGapM
/** The band's middle, into the desk. */
const bandMiddleV = () => DESK.restM + DESK.bandM / 2

/**
 * The places of `spots` seats (at most `seatsMost`): up to `rowMost` in one row in the band's middle, else a zigzag
 * of two rows. No two share a spot, and every pad is clear of the hands' rest.
 */
export function seatPlaces(spots: number): SeatPlace[] {
  const n = Math.min(DESK.seatsMost, Math.max(0, Math.floor(spots)))
  const u0 = seatsFromU() + DESK.padM / 2
  const out: SeatPlace[] = []
  for (let i = 0; i < n; i += 1) {
    if (n <= DESK.rowMost) out.push({ u: u0 + i * DESK.seatStepM, v: bandMiddleV(), row: 0 })
    else out.push({ u: u0 + i * DESK.zigStepM, v: i % 2 === 0 ? DESK.zigFarV : DESK.zigNearV, row: (i % 2) as 0 | 1 })
  }
  return out
}

/**
 * The seats after the Bot `slug` was put down on seat `to`: it sits there, and the Bot that sat there takes the seat
 * it left. A Bot with no seat, or a seat that does not stand, changes nothing.
 */
export function swapSeat(seats: readonly string[], slug: string, to: number): string[] {
  const out = [...seats]
  const from = out.indexOf(slug)
  if (from < 0 || to < 0 || to >= out.length || from === to) return out
  out[from] = out[to]
  out[to] = slug
  return out
}

/**
 * Which Bot sits where: each Bot keeps the seat of `kept` (the slugs by seat, `''` for an empty one), a Bot that
 * left leaves its seat empty, and a new one takes the first empty seat, else the next one. At most `seatsMost`
 * places; past that the last place is the others' pad (`more`), and its Bots have no seat.
 */
export function seatOrder(kept: readonly string[], slugs: readonly string[]): { seats: string[]; more: string[] } {
  const here = new Set(slugs)
  const seats = kept.slice(0, DESK.seatsMost).map(s => (here.has(s) ? s : ''))
  const seated = new Set(seats.filter(Boolean))
  const more: string[] = []
  for (const slug of slugs) {
    if (seated.has(slug)) continue
    const empty = seats.indexOf('')
    if (empty >= 0) seats[empty] = slug
    else if (seats.length < DESK.seatsMost) seats.push(slug)
    else { more.push(slug); continue }
    seated.add(slug)
  }
  while (seats.length && seats[seats.length - 1] === '') seats.pop()
  // A full desk with Bots left over: the last seat is their pad, and its Bot joins them.
  if (more.length && seats.length === DESK.seatsMost) { const last = seats.pop() ?? ''; if (last) more.unshift(last); seats.push('') }
  return { seats, more }
}

/** The palette's tiles on the desk, in the order they are given: one row at the band's left. */
export function tilePlaces(count: number): { u: number; v: number }[] {
  const out: { u: number; v: number }[] = []
  const u0 = -DESK.widthM / 2 + DESK.tileM / 2
  for (let i = 0; i < count; i += 1) out.push({ u: u0 + i * (DESK.tileM + DESK.tileGapM), v: bandMiddleV() })
  return out
}

/**
 * The flat preview's start pitch with a desk, for a head at `eye` that looks straight ahead: level, or lower until
 * the near edge of the band (where every pad and tile begins) is whole inside the picture, over its lower edge by
 * `marginDeg`. `lowerTan` is how far the lower edge lies under the picture's middle, as a tangent (the lens and the
 * frame's shift together). Never lower than `mostDownDeg`.
 */
export function deskStartPitch(d: DeskPlace, eye: V3, lowerTan: number, marginDeg = 1, mostDownDeg = 60): number {
  const edge = Math.tan(Math.atan(lowerTan) - marginDeg * RAD)
  const points = Array.from({ length: 11 }, (_, i) => deskPoint(d, -DESK.widthM / 2 + (i * DESK.widthM) / 10, DESK.restM)).map(p => [p[0] - eye[0], p[1] - eye[1], p[2] - eye[2]])
  for (let pitch = 0; pitch >= -mostDownDeg; pitch -= 0.25) {
    const s = Math.sin(pitch * RAD)
    const c = Math.cos(pitch * RAD)
    // In the head's frame: how far ahead (along its line) and how far up each point is.
    if (points.every(([, y, z]) => { const ahead = y * s - z * c; return ahead > 0 && (y * c + z * s) / ahead >= -edge })) return pitch
  }
  return -mostDownDeg
}

/**
 * The part of the view, seen from `eye`, that a page may not reach into while the desk stands: over the desk's
 * width, from its near edge up to the Bots' heads at the seats' far row and `clearM` more (`layout.ts`,
 * `placeNew`). A page that would reach into it is stood higher or elsewhere when it opens.
 */
export function deskZone(d: DeskPlace, eye: V3): Zone {
  const corners = [deskPoint(d, -DESK.widthM / 2, 0), deskPoint(d, DESK.widthM / 2, 0), deskPoint(d, -DESK.widthM / 2, DESK.depthM), deskPoint(d, DESK.widthM / 2, DESK.depthM)]
  const yawOf = (p: V3) => Math.atan2(p[0] - eye[0], -(p[2] - eye[2])) / RAD
  const pitchOf = (p: V3) => Math.atan2(p[1] - eye[1], Math.hypot(p[0] - eye[0], p[2] - eye[2])) / RAD
  const yaws = corners.map(yawOf)
  // The heads of the farthest seats, and the margin over them, at their own distance: the highest that a Bot reaches in the view.
  const head = deskPoint(d, 0, Math.max(bandMiddleV(), DESK.zigFarV), DESK.botM + DESK.clearM)
  return {
    yawMinDeg: Math.min(...yaws), yawMaxDeg: Math.max(...yaws),
    pitchMinDeg: Math.min(...corners.map(pitchOf)) - 5, pitchMaxDeg: pitchOf(head),
  }
}

/** The desk to keep in the room's record (rounded), or what a record keeps: `null` when it is not a desk. */
export function keptDesk(raw: unknown): DeskPlace | null {
  if (!raw || typeof raw !== 'object') return null
  const r = raw as Record<string, unknown>
  const at = Array.isArray(r.at) ? r.at : []
  const ok = (v: unknown, lo: number, hi: number) => typeof v === 'number' && Number.isFinite(v) && v >= lo && v <= hi
  if (!ok(at[0], -4, 4) || !ok(at[1], 0.2, 2.2) || !ok(at[2], -4, 4) || !ok(r.yawDeg, -180, 180)) return null
  const place: DeskPlace = { at: [at[0] as number, at[1] as number, at[2] as number], yawDeg: r.yawDeg as number, from: 'kept', inSession: r.inSession === true, confirmed: false }
  // The seats' order: slugs (or '' for an empty seat), as many as the desk has seats; anything else keeps no order.
  const seats = r.seats
  if (Array.isArray(seats) && seats.length <= DESK.seatsMost && seats.every(s => typeof s === 'string' && s.length <= 200)) place.seats = seats as string[]
  return place
}

export function deskToKeep(d: DeskPlace): { at: [number, number, number]; yawDeg: number; inSession: boolean; seats?: string[] } {
  const r = (v: number) => Math.round(v * 1000) / 1000
  return { at: [r(d.at[0]), r(d.at[1]), r(d.at[2])], yawDeg: Math.round(d.yawDeg * 10) / 10, inSession: d.inSession, ...(d.seats?.length ? { seats: [...d.seats] } : {}) }
}

/** The desk to stand in a session from what the record keeps: a desk kept in the flat preview is not one. */
export function deskForSession(kept: DeskPlace | null): DeskPlace | null {
  return kept && kept.inSession ? kept : null
}

/** Whether `desk=1` stands in the XR page's address. */
export const deskAsked = (search: string): boolean => new URLSearchParams(search).get('desk') === '1'
