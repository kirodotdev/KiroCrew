/**
 * The readout's numbers handed back in one act (P7 3): a press on the tile (`SendNumbersTile.tsx`) sends what the
 * readout says now, as text, to the Bot that the address word `sendto` names (`/xr?fps=1&sendto=<slug>`). It goes by
 * the same send as a note on a board (`cardSend`, `writes.ts`): the Bot's own thread, the dashboard's API, no route
 * of its own. The plate says whether it went (`sendLine`).
 */
import { fmtTimeNumeric } from '../../../i18n/format'
import { i18nT } from '../../../i18n/t'
import { CREW_STEER_MAX_CHARS } from '../../../lib/crewMode'

/**
 * The most characters the numbers' text may have: the gateway's own limit for a message (`CREW_STEER_MAX_CHARS`,
 * its `MAX_SEND_MESSAGE_CHARS`), less room for the line that `cardSend` adds after it (`xrMessage`).
 */
export const NUMBERS_MOST_CHARS = CREW_STEER_MAX_CHARS - 2_000

/** Where the last send stands: none yet, on its way, gone, or refused (with why, as the error said it). */
export type NumbersSend = { phase: 'none' } | { phase: 'sending'; at: number } | { phase: 'sent'; at: number; chars: number } | { phase: 'failed'; at: number; why: string }

/**
 * The text that goes: a first line that says what it is, when (UTC) and on what browser, then the readout's lines.
 * Past `most` characters the lines are cut, whole lines only, and a last line says so.
 */
export function numbersText(lines: readonly string[], at: Date, browser: string, most: number = NUMBERS_MOST_CHARS): string {
  const whole = [`XR readout ${at.toISOString()} ${browser}`, ...lines].join('\n')
  if (whole.length <= most) return whole
  // Cut: as many whole lines as leave room for the line that says how many were cut.
  const cutLine = (left: number) => `[${left} lines cut at ${most} characters]`
  const room = most - cutLine(lines.length).length - 1
  const out = [`XR readout ${at.toISOString()} ${browser}`]
  let size = out[0].length
  for (const line of lines) {
    if (size + 1 + line.length > room) break
    out.push(line)
    size += 1 + line.length
  }
  return [...out, cutLine(lines.length - (out.length - 1))].join('\n').slice(0, most)
}

/**
 * The Bot that the numbers go to (D59 1): the address's `sendto` only when it is the slug of a Bot on his crew, in any
 * letter case, given as that Bot's own slug; `null` for any other word, for two different words, and while the crew
 * is not read yet. A link never chooses another Bot than one he has.
 */
export function sendToOf(asked: readonly string[], bots: readonly { slug: string }[]): string | null {
  const words = [...new Set(asked.map(w => w.trim().toLowerCase()).filter(Boolean))]
  if (words.length !== 1) return null
  return bots.find(b => b.slug.toLowerCase() === words[0])?.slug ?? null
}

/** The plate's line for the last send, or none before the first press. */
export function sendLine(send: NumbersSend, to: string): string | null {
  if (send.phase === 'none') return null
  if (send.phase === 'sending') return i18nT('xr.numbers_sending', { name: to })
  const time = fmtTimeNumeric(send.at)
  return send.phase === 'sent' ? i18nT('xr.numbers_sent', { name: to, time }) : i18nT('xr.numbers_failed', { name: to, time, why: send.why })
}

/**
 * How the log keeps the readout's lines between two sends, so that one press at the end carries what each check read
 * on the way (P8): a line of a kind (its first word: `speech`, `layers`, `device`, ...) when it changes, at most once a
 * `everyMs`, the last `perKind`; the `pinch` and `still` lines at every pinch that ended, the last `perPinch`; the
 * `hands`, `tip` and `room` lines, which change all the time, at most
 * once a `slowMs` of their own; the pace lines and the room's only while a pinch is held (a page dragged, check 6),
 * once a second, the last `held`.
 */
export const NUMBERS_LOG = { everyMs: 1000, perKind: 40, perPinch: 120, held: 240, slowMs: { hands: 10_000, tip: 2_000, room: 30_000 } } as const

/** The first word of a line of the readout, which names its kind; `null` for a page's own line (it starts with a number). */
const kindOf = (line: string): string | null => {
  const word = line.split(' ')[0]
  return word && !/^[\d-]/.test(word) ? word : null
}

/** The first word of the readout's line for `key`, in the dashboard's language. */
const wordOf = (key: string): string => i18nT(key, { left: '', right: '', fps: '', longest: '' }).split(' ')[0]

/** One line the log took: when (the wall clock's ms), the line, and its place in the order it was taken. */
type Entry = { at: number; line: string; n?: number }
/** Where the log stood at a press (`NumbersLog.mark`). */
export type LogMark = { at: number; n: number }

/** The log of the readout's lines since the first one it was given (see `NUMBERS_LOG`). */
export class NumbersLog {
  private since: number | null = null
  private readonly kinds = new Map<string, Entry[]>()
  private readonly heldPace: Entry[] = []
  /** How many entries it has taken: each entry's `n` is its place in that order (V7). */
  private taken = 0
  private lastHeld = 0

  /** The readout's lines at `now` (the wall clock, ms); the first `pace` are its pace lines. */
  take(lines: readonly string[], pace: number, now: number, heldMore: readonly string[] = []): void {
    this.since ??= now
    const held = lines.some(l => / · held · /.test(l))
    const slow: Record<string, number> = { [wordOf('xr.readout_hands')]: NUMBERS_LOG.slowMs.hands, [wordOf('xr.readout_tip')]: NUMBERS_LOG.slowMs.tip, [wordOf('xr.readout_room')]: NUMBERS_LOG.slowMs.room }
    if (held && now - this.lastHeld >= NUMBERS_LOG.everyMs) {
      this.lastHeld = now
      const room = lines.find(l => kindOf(l) === wordOf('xr.readout_room'))
      // `heldMore`: what the message alone carries beside them, each page's last 20 round trips (N3).
      for (const line of [...lines.slice(0, pace), ...heldMore, ...(room ? [room] : [])]) this.heldPace.push({ at: now, line, n: ++this.taken })
      while (this.heldPace.length > NUMBERS_LOG.held) this.heldPace.shift()
    }
    // Each pinch and each held pinch's stillness counts (checks 10 to 15, 19): their lines are kept at every change,
    // the last `perPinch`, but not while a pinch is held, when they say only how long it has been held so far.
    const each = new Set([wordOf('xr.readout_pinch'), wordOf('xr.readout_still')])
    for (const line of lines.slice(pace)) {
      const kind = kindOf(line)
      if (!kind || (held && each.has(kind))) continue
      const list = this.kinds.get(kind) ?? []
      const last = list[list.length - 1]
      const every = each.has(kind) ? 0 : slow[kind] ?? NUMBERS_LOG.everyMs
      if (last && (last.line === line || now - last.at < every)) continue
      list.push({ at: now, line, n: ++this.taken })
      while (list.length > (each.has(kind) ? NUMBERS_LOG.perPinch : NUMBERS_LOG.perKind)) list.shift()
      this.kinds.set(kind, list)
    }
  }

  /** What it holds, to be kept over a load of the tab (a swap loads it again mid-check). */
  save(): string { return JSON.stringify({ since: this.since, kinds: [...this.kinds], heldPace: this.heldPace }) }

  /** Takes back what `save` gave, when there is nothing in it yet; a text that is not a saved log changes nothing. */
  load(text: string | null): void {
    if (!text || this.since !== null) return
    try {
      const v = JSON.parse(text) as { since: number | null; kinds: [string, Entry[]][]; heldPace: Entry[] }
      if (typeof v.since !== 'number' || !Array.isArray(v.kinds) || !Array.isArray(v.heldPace)) return
      // The times are the wall clock's (`Date.now()`), so they hold over the load.
      this.since = v.since
      // An entry kept over a load has no place in this tab's order (its count starts again): taken before any mark,
      // so the next good send takes it out (M18, REVIEW-25).
      const placeless = (list: Entry[]): Entry[] => list.map(e => ({ at: e.at, line: e.line }))
      for (const [k, list] of v.kinds) this.kinds.set(k, placeless(list))
      this.heldPace.push(...placeless(v.heldPace))
    } catch { /* not a saved log */ }
  }

  /**
   * The log's lines: a head, then each kind's lines in time, `+m:ss` from the log's first read, the pace while held last.
   * Within `most` characters: past it the oldest lines of the kinds go first (N2 b), and the held lines stay (check 6
   * reads them); a line says how many went.
   */
  lines(most: number = Infinity): string[] {
    if (this.since === null) return []
    const since = this.since
    const t = (at: number) => { const s = Math.round((at - since) / 1000); return `+${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}` }
    const held = this.heldPace.length ? ['--- while a pinch was held: the pace lines and the room, once a second ---', ...this.heldPace.map(e => `${t(e.at)} ${e.line}`)] : []
    const head = '--- since the last send, each kind as it changed ---'
    const size = (list: readonly string[]) => list.reduce((n, l) => n + l.length + 1, 0)
    // Every kind's entries, newest first: as many as fit beside the head and the held lines are kept.
    const all = [...this.kinds.values()].flat().sort((a, b) => b.at - a.at)
    let room = most - size([head, ...held]) - 60
    const kept = new Set<Entry>()
    for (const e of all) { const n = t(e.at).length + 1 + e.line.length + 1; if (room - n < 0) break; room -= n; kept.add(e) }
    const cut = all.length - kept.size
    const body: string[] = []
    for (const list of this.kinds.values()) for (const e of list) if (kept.has(e)) body.push(`${t(e.at)} ${e.line}`)
    return [head, ...(cut ? [`[${cut} older lines cut to stay under ${most} characters]`] : []), ...body, ...held]
  }

  /** Where the log stands at a press: the wall clock's ms, and how many entries it had taken. */
  mark(now: number = Date.now()): LogMark { return { at: now, n: this.taken } }

  /**
   * A send went with what the log held at `upTo` (`mark()` at the press): those entries go, so the next send carries
   * only what came after (N2 a). The lines taken while the send was on its way stay for the next press (R2), and so
   * does one taken in the press's own millisecond after it, told by its place in order, not by its clock (V7). An
   * entry kept over a load of the tab has no place in order and counts as taken before any mark.
   */
  clear(upTo: LogMark = { at: Infinity, n: Infinity }): void {
    const after = (e: Entry) => (e.n ?? 0) > upTo.n
    for (const [kind, list] of this.kinds) {
      const rest = list.filter(after)
      if (rest.length) this.kinds.set(kind, rest)
      else this.kinds.delete(kind)
    }
    const held = this.heldPace.filter(after)
    this.heldPace.length = 0
    this.heldPace.push(...held)
    if (this.kinds.size || this.heldPace.length) this.since = upTo.at
    else { this.since = null; this.lastHeld = 0 }
  }
}

/** The one book of the last send: the tile writes it, the readout's notes read it. One room runs at a time. */
export const numbersBook: { send: NumbersSend; log: NumbersLog } = { send: { phase: 'none' }, log: new NumbersLog() }
