/**
 * The readout of `/xr?view=pages&fps=1`: a few lines of numbers over the canvas's corner, so that how fast the pages
 * come can be read on any machine, and copied back as text. The rules are here as pure functions; `Readout.tsx` keeps
 * the clock and reads the sources.
 */
import { i18nT } from '../../../i18n/t'
import { fmtNumber } from '../../../i18n/format'
import { PAGES } from './layout'
import type { PageReadout } from './source'

/** How often the readout reads, and over how long a rate is counted. */
export const READOUT = { everyMs: 500, windowMs: 2000, ringFrames: 240, lastPictures: 30 } as const

/** The plate of a session (`ReadoutPlate.tsx`) holds this many lines at the least, and never more than this: it grows with what it says, and no further. */
export const PLATE_ROWS = { least: 9, most: 20 } as const

/** How many lines high the plate is for so many lines. */
export const plateRows = (lines: number): number => Math.max(PLATE_ROWS.least, Math.min(PLATE_ROWS.most, lines))

/** The plate's canvas: its width, one line's height and its text's size, in pixels. */
export const PLATE_PX = { width: 1024, line: 30, text: 22 } as const

/**
 * How wide the plate is, in metres, where it stands `distanceM` from the eye: so wide that its text is as big as
 * the smallest text a page may show there (`PAGES.minTextDp`). A plate that is farther away is larger by as much.
 */
export const plateWidthM = (distanceM: number): number => (PLATE_PX.width * (PAGES.mmPerDpAt1M / 1000) * PAGES.minTextDp * distanceM) / PLATE_PX.text

/** One page as the readout sees it. */
export interface ReadoutPage { id: string; path: string; facts: PageReadout }

/** What the room's renderer says of itself. */
export interface ReadoutGl { dpr: number; canvas: [number, number]; samples: number; draws: number; textures: number; renderer: string }

/** One read of every page's count, for the rate a few reads later. */
export interface ReadoutSample { at: number; counts: ReadonlyMap<string, number> }

const n = (v: number, digits = 0) => fmtNumber(v, { maximumFractionDigits: digits, minimumFractionDigits: digits })
const ms = (v: number | null) => (v === null ? '-' : n(v))
/** A size in pixels, without grouping: `w` and `h` of a line's `{{w}}x{{h}}`. */
const px = ([w, h]: readonly [number, number]) => ({ w: fmtNumber(w, { useGrouping: false }), h: fmtNumber(h, { useGrouping: false }) })

/** The middle of the last few values, or `null`. */
export function middle(list: readonly number[], last: number = READOUT.lastPictures): number | null {
  const tail = list.slice(-last)
  if (!tail.length) return null
  const sorted = [...tail].sort((a, b) => a - b)
  return sorted[Math.floor(sorted.length / 2)]
}

/** The read to count a rate from: the newest one at least the window old, else the oldest. Keeps what is needed. */
export function sinceWindow(samples: ReadoutSample[], now: number): ReadoutSample | undefined {
  while (samples.length > 1 && now - samples[1].at >= READOUT.windowMs) samples.shift()
  return samples[0]
}

/** Pictures a second of a page since `before`. */
export function rateOf(id: string, count: number, now: number, before: ReadoutSample | undefined): number {
  if (!before || now <= before.at) return 0
  const was = before.counts.get(id)
  return was === undefined ? 0 : (Math.max(0, count - was) * 1000) / (now - before.at)
}

/** The room's frames a second over the window, and its longest frame in it, from the times of its last frames. */
export function roomRate(frames: readonly number[], now: number): { fps: number; longest: number } {
  const from = now - READOUT.windowMs
  let count = 0
  let longest = 0
  for (let i = 0; i < frames.length; i += 1) {
    if (frames[i] < from) continue
    count += 1
    if (i > 0 && frames[i - 1] >= from) longest = Math.max(longest, frames[i] - frames[i - 1])
  }
  return { fps: (count * 1000) / READOUT.windowMs, longest }
}

/**
 * The page that the person works on first: the one that the gateway does not hold back (a pointer is on it), then
 * the ones in view, then the rest, each in the room's order.
 */
export function inOrder(pages: readonly ReadoutPage[]): ReadoutPage[] {
  const rank = (p: ReadoutPage) => (p.facts.gateway?.held === false ? 0 : p.facts.visible ? 1 : 2)
  return pages.map((p, i) => ({ p, i })).sort((a, b) => rank(a.p) - rank(b.p) || a.i - b.i).map(x => x.p)
}

/**
 * The pace lines (D57 2): at most `pages` of the pages in his view, the one the gateway does not hold back first, and
 * the middle of the last `last` pictures. Each number keeps `width` places, so a line is as wide from read to read.
 */
export const PACE = { pages: 3, last: 20, width: 4, name: 20 } as const

/**
 * A page's address as the readout prints it, on the plate, the block and in the message that the tile sends: up to
 * its `?`, and at most `PACE.name` characters. An address can come from a link (`page=/members?member=<words>`, a
 * chat's key, a project's path), and the message goes into a Bot's turn: a link's words never ride along (D59 1).
 * The numbers keep their size on the plate too, which squeezes a line wider than itself.
 */
export function shownPath(path: string): string {
  const bare = path.split(/[?#]/)[0]
  return [...bare].length > PACE.name ? `${[...bare].slice(0, PACE.name - 1).join('')}…` : bare
}

/** A number in `PACE.width` places, filled on the left with figure spaces (as wide as a digit). */
const wide = (text: string) => text.padStart(PACE.width, '\u2007')

/**
 * One line for each page in his view, at most `PACE.pages`: where each number comes from is the page's own book
 * (`StreamSource.readout`). `aims`: the pace that the gateway's last picture header said (`pace`, the pictures a
 * second its ladder aims at); `round`: the middle of the headers' `round_ms` (the gateway's time from a picture's send
 * to its decoded ack); `decode` and `land`: the middle of this tab's own times (the decode; the socket to the texture);
 * `landed`: the pictures that reached the texture a second, over the last two seconds; `layers`: whether this session
 * was granted composition layers (`-` with no session). A page drawn by this tab itself has no gateway: `-`.
 */
export function paceLines(pages: readonly ReadoutPage[], rates: ReadonlyMap<string, number>, layers: boolean | null): string[] {
  const yesNo = (v: boolean | null) => (v === null ? '-' : i18nT(v ? 'xr.readout_yes' : 'xr.readout_no'))
  // No grouping: a round trip of 1,234 ms would take five places and widen the line (w98-plate, a loaded host).
  const ms = (v: number | null) => (v === null ? '-' : fmtNumber(v, { maximumFractionDigits: 0, useGrouping: false }))
  return inOrder(pages.filter(p => p.facts.visible)).slice(0, PACE.pages).map(p => {
    const f = p.facts
    return i18nT('xr.readout_pace', {
      name: shownPath(p.path),
      aims: wide(f.gateway?.pace == null ? '-' : n(f.gateway.pace)),
      round: wide(ms(middle(f.roundMs, PACE.last))),
      // R5 (D59 2): the slowest of the same 20, which decides when the middle is between about 40 and 90 ms.
      slowest: wide(ms(f.roundMs.length ? Math.max(...f.roundMs.slice(-PACE.last)) : null)),
      decode: wide(ms(middle(f.decodeMs, PACE.last))),
      land: wide(ms(middle(f.landMs, PACE.last))),
      landed: wide(n(rates.get(p.id) ?? 0, 1)),
      layers: yesNo(layers),
    })
  })
}

/** The `room` line: the room's frames a second and its longest frame. */
export const roomLine = (room: { fps: number; longest: number }): string => i18nT('xr.readout_room', { fps: n(room.fps), longest: n(room.longest) })

/** The readout's lines, first line first. */
export function readoutLines(pages: readonly ReadoutPage[], rates: ReadonlyMap<string, number>, room: { fps: number; longest: number }, gl: ReadoutGl | null): string[] {
  const lines: string[] = []
  const all = inOrder(pages)
  const inView = pages.filter(p => p.facts.visible).length
  all.forEach((p, i) => {
    const f = p.facts
    const line = i18nT('xr.readout_page', { rate: n(rates.get(p.id) ?? 0, 1), name: shownPath(p.path), ...px(f.picture), kb: f.count && f.bytes ? n(f.bytes / f.count / 1000) : '-', way: f.way })
    lines.push(i === 0 ? `${line} · ${i18nT('xr.readout_in_view', { count: inView, all: pages.length })}` : line)
  })
  const first = all[0]?.facts
  if (first) {
    lines.push(i18nT('xr.readout_picture', { land: ms(middle(first.landMs)), decode: ms(middle(first.decodeMs)), wait: ms(middle(first.readyWaitMs)), upload: ms(middle(first.writeMs)), press: ms(middle(first.inputMs, 5)) }))
    const g = first.gateway
    if (g) lines.push(i18nT('xr.readout_gateway', { round: g.roundMs === null ? '-' : n(g.roundMs), pace: g.pace === null ? '-' : n(g.pace), held: g.held === null ? '-' : i18nT(g.held ? 'xr.readout_yes' : 'xr.readout_no') }))
  }
  lines.push(roomLine(room))
  if (gl) lines.push(i18nT('xr.readout_gl', { dpr: n(gl.dpr, 2), ...px(gl.canvas), samples: n(gl.samples), draws: n(gl.draws), textures: n(gl.textures), renderer: gl.renderer }))
  return lines
}
