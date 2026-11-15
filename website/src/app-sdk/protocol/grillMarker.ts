// The `[GRILL: {json}]` discussion-card marker: the conductor proposes a quick
// worker discussion, and the reply draws a card whose three direction lines the
// user can edit before starting it. Same placement rule as `[OPTIONS:]`: one
// line of its own. Contract: `worker-grill/plan.md`, data contract 1.
//
// Anything outside the contract -- JSON that does not parse, a field over its
// cap, a second marker in the same reply -- draws no card: the line stays in the
// prose as written, so the user still sees what the agent sent.

import { GRILL_OPEN as OPEN } from './grillWireValues'

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

/** Field caps, in characters (code points). */
export const GRILL_LIMITS = Object.freeze({
  text: 200,
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
