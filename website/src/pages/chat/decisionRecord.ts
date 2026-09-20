/**
 * The read model behind the transcript's decision strip.
 *
 * The gateway stamps one record on the assistant row that ends a turn whose
 * skill set was chosen by asking Jev (`decisions/points/skills_select.py`). The
 * strip renders that record and nothing else: it makes no request of its own to
 * learn what happened, so a transcript reloaded from disk and one that arrived
 * live say the same thing.
 *
 * Every field is validated here rather than at the render site, for the reason
 * `decisionsPreview.ts` gives about consent: this payload names what left the
 * machine and what came back, and a strip that prints a shape it did not check
 * would describe a decision nobody made. A record that fails validation renders
 * nothing at all — the absent-field path — because a half-drawn receipt is worse
 * than no receipt.
 *
 * The gateway stamps the record under `meta` on the assistant row, and `meta`
 * is what both doors carry: the live websocket frame and a row reloaded from
 * history. The row's own top level is read too, the same split
 * `CompactionCard.noticeKindOf` reads `kind` through, so a producer that stamps
 * the key there is understood rather than silently ignored.
 *
 * The stem is `decisionRecord`, not `decisionStrip`, so it cannot differ from
 * `DecisionStrip.tsx` beside it in case alone. A case-only pair resolves to ONE
 * module on a case-insensitive filesystem, so every import of both breaks on
 * macOS and Windows while a Linux checkout compiles — see the guard in
 * `test/fileNameCasing.test.ts`.
 */
import type { DecisionFeedbackSide, DecisionVerdictValue } from '../../api/client'
import type { ChatMessage } from '../../types'
import { DECISIONS_LIVE_POINT } from '../settings/decisionsPreview'

/** A skill the answer named that the gate then refused, with its own score. */
export interface DecisionStripDropped {
  key: string
  p: number
}

/**
 * One decision, as the strip prints it.
 *
 * `agree` is not read straight from the wire: see `readDecisionStrip`. The two
 * name lists are the whole claim the strip makes, so the flag that decides
 * whether to print one list or two is derived from them.
 */
export interface DecisionStripRecord {
  /** Identifies the turn this decision belongs to; the feedback POST's subject. */
  turnId: string
  /** The decision point, e.g. `skills.select`. */
  point: string
  /** What the shipped word-matching rule would have loaded. */
  baseline: string[]
  /** What Jev answered. */
  jev: string[]
  /** The two lists hold the same skills, in any order. */
  agree: boolean
  /** Jev's own confidence, or `null` when the answer carried none. */
  p: number | null
  /** Prompt tokens the narrower set saved, `0` when it saved none. */
  tokensSaved: number
  /** Skills the gate offered Jev to choose from. */
  candidates: number
  /**
   * Characters of the message excerpt the question sent, or `null` when the
   * record does not state it.
   *
   * Nullable, unlike every other count here, because 0 is not a credible
   * measurement of this one: a turn that reached the selector had text. So a
   * missing field read as 0 would print "message 0 chars" over a question that
   * did send an excerpt -- the same always-zero row this strip exists to remove,
   * reintroduced for every record stamped before the field existed.
   */
  messageChars: number | null
  /** Characters of conversation history the question carried. */
  historyChars: number
  /** Milliseconds between asking Jev and its answer. */
  latencyMs: number
  /** Answers the gate refused, each with the score it came with. */
  dropped: DecisionStripDropped[]
  /** Why the decision failed, or `null` when it did not. */
  error: string | null
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

/** A whole, non-negative, finite number, or `0` for anything else. */
function asCount(value: unknown): number {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : 0
}

/**
 * The same count, but `null` rather than `0` for a value that is not one.
 *
 * For a field whose 0 a reader would take as a measurement. Folding "the record
 * did not say" into "the record said zero" is the one shape a receipt must not
 * take: the absent case is every record a producer stamped before the field
 * existed, and it is silent rather than rare.
 */
function asCountOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? Math.floor(value)
    : null
}

/**
 * Skill keys off the wire, or `null` when the value is not a list of them.
 *
 * `null`, not a filtered list. Dropping unreadable entries looks forgiving and is
 * the opposite: two lists that were each unreadable in a DIFFERENT way both
 * filter down to `[]`, and `[]` equals `[]`, so the strip would print "same pick"
 * over a record whose two sides it never actually read. That is the one claim
 * this surface must not invent, and the module's own rule already says so --
 * a record that fails validation renders nothing.
 *
 * An EMPTY array is valid and returns `[]`: "no skill applies" is a real answer
 * (`/no skill applies` in the gate), and it is the answer the whole no-skill path
 * produces.
 */
function asNames(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null
  if (!value.every((n): n is string => typeof n === 'string' && n.length > 0)) return null
  return value
}

/** Same members, ignoring order — what "the two sides agreed" means. */
function sameSet(a: string[], b: string[]): boolean {
  const left = new Set(a)
  const right = new Set(b)
  if (left.size !== right.size) return false
  for (const name of right) if (!left.has(name)) return false
  return true
}

/**
 * The raw field off an assistant row, or `undefined`.
 *
 * Returned as it sits on the message so the reference is stable across renders:
 * `AssistantMessage` is memoised, and handing it a freshly built object every
 * render would defeat that for the row carrying the strip.
 */
export function decisionStripFieldOf(msg: Pick<ChatMessage, 'meta' | 'decisions_strip'>): unknown {
  return msg.decisions_strip ?? msg.meta?.decisions_strip
}

/**
 * Validate one raw record. `null` means "draw nothing".
 *
 * `agree` is recomputed from the two name lists instead of being read from the
 * wire. The strip prints ONE list when the sides agreed and BOTH when they did
 * not, so a flag that disagreed with the lists beside it would hide a real
 * divergence behind a check mark. Recomputing makes that impossible.
 */
export function readDecisionStrip(raw: unknown): DecisionStripRecord | null {
  const root = asRecord(raw)
  if (!root) return null
  const turnId = typeof root.turn_id === 'string' ? root.turn_id : ''
  if (!turnId) return null
  const point = typeof root.point === 'string' && root.point ? root.point : DECISIONS_LIVE_POINT
  // Both lists must be readable before anything is drawn: the strip's whole
  // claim is who picked what, and it cannot make that claim about a list it
  // could not read.
  const baseline = asNames(root.baseline)
  const jev = asNames(root.jev)
  if (baseline === null || jev === null) return null
  // A probability outside 0–1 is not a probability; the strip prints no number
  // rather than one the reader would take at face value.
  const rawP = root.p
  const p = typeof rawP === 'number' && Number.isFinite(rawP) && rawP >= 0 && rawP <= 1 ? rawP : null
  const dropped = Array.isArray(root.dropped)
    ? root.dropped.flatMap((entry): DecisionStripDropped[] => {
      const d = asRecord(entry)
      const key = d && typeof d.key === 'string' ? d.key : ''
      if (!key) return []
      const score = typeof d?.p === 'number' && Number.isFinite(d.p) ? d.p : 0
      return [{ key, p: score }]
    })
    : []
  const error = typeof root.error === 'string' && root.error.trim() ? root.error : null
  return {
    turnId,
    point,
    baseline,
    jev,
    agree: sameSet(baseline, jev),
    p,
    tokensSaved: asCount(root.tokens_saved),
    candidates: asCount(root.candidates),
    messageChars: asCountOrNull(root.message_chars),
    historyChars: asCount(root.history_chars),
    latencyMs: asCount(root.latency_ms),
    dropped,
    error,
  }
}

/**
 * Thumbs already pressed in this page session, keyed by turn and side.
 *
 * The transcript is virtualised: a row leaving the window is unmounted and its
 * component state destroyed, which is the problem `rowDisclosure.ts` exists for.
 * That store holds booleans and a verdict is three-valued, so the answers live
 * here instead. Bounded by how many thumbs one reader presses before a reload,
 * and keyed by a turn id, so two sessions cannot collide.
 */
const verdicts = new Map<string, DecisionVerdictValue>()

const verdictKey = (turnId: string, side: DecisionFeedbackSide) => `${turnId}:${side}`

/** The answer this reader gave, or `null` when they have not answered. */
export function recordedVerdict(turnId: string, side: DecisionFeedbackSide): DecisionVerdictValue {
  return verdicts.get(verdictKey(turnId, side)) ?? null
}

/** Remember an answer the server accepted. */
export function rememberVerdict(turnId: string, side: DecisionFeedbackSide, verdict: DecisionVerdictValue): void {
  verdicts.set(verdictKey(turnId, side), verdict)
}

/** Drop every remembered answer, so one test cannot inherit another's. */
export function __resetVerdicts(): void {
  verdicts.clear()
}

/**
 * Pressing the thumb that is already lit takes the answer back, which is what
 * the `null` verdict is for. Pressing the other one replaces it.
 */
export function nextVerdict(current: DecisionVerdictValue, pressed: 'right' | 'wrong'): DecisionVerdictValue {
  return current === pressed ? null : pressed
}
