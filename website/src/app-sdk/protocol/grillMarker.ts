// The `[GRILL: {json}]` discussion-card marker: the conductor proposes a quick
// worker discussion, and the reply draws a card whose three direction lines the
// user can edit before starting it. Same placement rule as `[OPTIONS:]`: one
// line of its own. Contract: `worker-grill/plan.md`, data contract 1.
//
// Anything outside the contract -- JSON that does not parse, a field over its
// cap, a second marker in the same reply -- draws no card: the line stays in the
// prose as written, so the user still sees what the agent sent.

import { GRILL_KEY_PREFIX, GRILL_OPEN as OPEN } from './grillWireValues'

/** One participant and the side it argues. */
export interface GrillParticipant {
  name: string
  stance: string
}

/** A validated `[GRILL:]` payload. */
export interface GrillProposal {
  /** What the discussion must answer. */
  q: string
  /** What the answer must achieve. */
  must: string
  /** What the discussion leaves out. */
  skip: string
  who: GrillParticipant[]
  minutes: number
}

export interface ParsedGrill {
  /** `content` with the marker line removed (unchanged when there is no card). */
  text: string
  /** The card to draw, or null. */
  grill: GrillProposal | null
}

/** Field caps, in characters (code points). `text` caps what the marker
 *  proposes; `edit` caps what the reader may write into a card field. */
export const GRILL_LIMITS = Object.freeze({
  text: 200,
  edit: 1000,
  name: 20,
  stance: 80,
  whoMin: 2,
  whoMax: 3,
  minutesMin: 1,
  minutesMax: 60,
})

/** The line holds a marker candidate: `[GRILL:` ... `]`, nothing else but blanks. */
function isMarkerLine(line: string): boolean {
  const s = line.trim()
  return s.startsWith(OPEN) && s.endsWith(']')
}

const len = (s: string) => [...s].length

function str(v: unknown, max: number, required: boolean): string | null {
  if (typeof v !== 'string') return null
  const s = v.trim()
  if (required && !s) return null
  // A newline would break the line-per-field text the Start button sends.
  if (/[\r\n]/.test(s) || len(s) > max) return null
  return s
}

/** Validate a decoded payload against the contract; null when any field fails. */
export function validateGrill(raw: unknown): GrillProposal | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const o = raw as Record<string, unknown>
  const q = str(o.q, GRILL_LIMITS.text, true)
  const must = str(o.must, GRILL_LIMITS.text, false)
  const skip = str(o.skip, GRILL_LIMITS.text, false)
  if (q === null || must === null || skip === null) return null
  if (!Array.isArray(o.who) || o.who.length < GRILL_LIMITS.whoMin || o.who.length > GRILL_LIMITS.whoMax) return null
  const who: GrillParticipant[] = []
  for (const p of o.who) {
    if (!p || typeof p !== 'object') return null
    const name = str((p as Record<string, unknown>).name, GRILL_LIMITS.name, true)
    const stance = str((p as Record<string, unknown>).stance, GRILL_LIMITS.stance, true)
    if (name === null || stance === null) return null
    who.push({ name, stance })
  }
  const minutes = o.minutes
  if (typeof minutes !== 'number' || !Number.isInteger(minutes) || minutes < GRILL_LIMITS.minutesMin || minutes > GRILL_LIMITS.minutesMax) return null
  return { q, must, skip, who, minutes }
}

/**
 * Find the reply's `[GRILL:]` card. Exactly one marker line, valid JSON within
 * the caps, draws a card and leaves the prose; zero or several draw none.
 */
export function parseGrill(content: string): ParsedGrill {
  if (!content.includes(OPEN)) return { text: content, grill: null }
  const lines = content.split('\n')
  const at = lines.flatMap((l, i) => (isMarkerLine(l) ? [i] : []))
  if (at.length !== 1) return { text: content, grill: null }
  const s = lines[at[0]].trim()
  let decoded: unknown
  try {
    decoded = JSON.parse(s.slice(OPEN.length, -1))
  } catch {
    return { text: content, grill: null }
  }
  const grill = validateGrill(decoded)
  if (!grill) return { text: content, grill: null }
  lines.splice(at[0], 1)
  return { text: lines.join('\n').trim(), grill }
}

/** True when the reply draws a discussion card (so its `[OPTIONS:]` chips do not). */
export function hasGrillCard(content: string): boolean {
  return parseGrill(content).grill !== null
}

/**
 * While a reply streams, its marker line has not closed yet: hide a trailing
 * `[GRILL:` line so the raw JSON does not type itself out before the card.
 */
export function stripPartialGrillMarker(content: string): string {
  const nl = content.lastIndexOf('\n')
  const tail = content.slice(nl + 1)
  return tail.trimStart().startsWith(OPEN) ? content.slice(0, nl + 1) : content
}

/**
 * The key naming one card: its reply row's server `mid` plus the reply version.
 * Only a reply with one version gets one (`AssistantMessage`): a regenerated
 * row's versions share its `mid`, and a Regenerate settles which version is
 * which only at its turn's end. No `mid` (a row the server has not
 * named yet, or an old one it never will): no key, and the card sends without
 * one. Never a client stamp or `ts`: those change on refresh, so a lock keyed on
 * them would not hold.
 */
export function grillKey(mid: string | undefined, version: number): string | undefined {
  return mid ? `${mid}:${version}` : undefined
}

/** The continuation lines a card field adds after its labelled line. */
export const GRILL_CONTINUATION = '  '

/**
 * A card field's text for its labelled line: each further line the reader
 * wrote starts with two spaces, so the conductor still reads one labelled line
 * per field, and no reader line can pass for a whole `key: <value>` line.
 */
export function grillFieldText(text: string): string {
  return text.trim().split(/\r?\n/).map(l => l.trimEnd()).join('\n' + GRILL_CONTINUATION)
}

/** The line a Start / No thanks carries so a reload can tell this card was answered. */
export function grillKeyLine(key: string): string {
  return GRILL_KEY_PREFIX + key
}

/** The keys a sent message names: each line that is exactly `key: <value>`. */
export function grillKeysIn(text: string): string[] {
  return text.split(/\r?\n/).flatMap(l => (l.startsWith(GRILL_KEY_PREFIX) && l.length > GRILL_KEY_PREFIX.length ? [l.slice(GRILL_KEY_PREFIX.length)] : []))
}

interface KeyedRow { role?: string; content?: unknown; meta?: Record<string, unknown> }

/** A row the server holds: a stored user message (it carries a `mid`), or an
 *  entry still waiting in the session's queue (it carries the `queueId` the
 *  server named it by). An optimistic bubble the server refused has neither. */
function serverHeld(r: KeyedRow): boolean {
  const id = r.role === 'user' ? r.meta?.mid : r.role === 'queued' ? r.meta?.queueId : undefined
  return typeof id === 'string' && !!id && typeof r.content === 'string'
}

/**
 * The keys of this row's cards that a later message the server holds already
 * sent, newline-joined (a string, so a memoized row only redraws when it
 * changes). A refused optimistic bubble does not count, so it cannot lock a
 * card again after the card reopened. `queued`: the session's queued entries,
 * for a host that keeps them out of `rows`. Undefined for a row with no `mid`
 * or no card marker.
 */
export function grillKeysSentAfter(rows: readonly KeyedRow[], index: number, queued: readonly KeyedRow[] = []): string | undefined {
  const row = rows[index] as (KeyedRow & { variants?: { content?: unknown }[] }) | undefined
  const mid = row?.meta?.mid
  if (typeof mid !== 'string' || !mid) return undefined
  const texts = [row?.content, ...(row?.variants ?? []).map(v => v?.content)]
  if (!texts.some(c => typeof c === 'string' && c.includes(OPEN))) return undefined
  const mine = `${mid}:`
  const found: string[] = []
  for (const r of [...rows.slice(index + 1), ...queued]) {
    if (!serverHeld(r)) continue
    for (const k of grillKeysIn(r.content as string)) if (k.startsWith(mine) && !found.includes(k)) found.push(k)
  }
  return found.length ? found.join('\n') : undefined
}
