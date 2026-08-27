import type { ChatMessage } from '../../types'
import { isSystemNoticeKind } from '../../lib/systemNotice'
import { isNoteRow } from '../../lib/noteContract'
import { OPTION_MARKER_RE } from './optionMarker'

// A plan is recognised by BOTH its header and at least one stage line, so ordinary
// prose that happens to mention a plan is not mistaken for one.
const PLAN_HEADER_RE = /📋\s*Plan for:/i
const STAGE_RE = /^Stage\s+\d+\s*:/m

/** A message split into the prose the user reads and the choices offered alongside it. */
export interface ParsedOptions {
  /** `content` with every marker removed, trimmed — what a transcript should render. */
  text: string
  /** Choices from the LAST marker, in the order the agent listed them. */
  options: string[]
  /** `[OPTIONS:]` allows several picks; `[OPTION:]` is a single choice. */
  multi: boolean
  /** The message is a plan (header plus at least one stage line), not a plain question. */
  isPlan: boolean
}

export function parseOptions(content: string): ParsedOptions {
  let last: RegExpMatchArray | null = null
  // `matchAll` seeds its internal clone from this regex's `lastIndex`, so a stray `.test()` or
  // `.exec()` anywhere would make the scan start mid-string and miss the marker. Clone per call:
  // the cost is one regex construction, the alternative is a silent parse failure.
  for (const m of content.matchAll(new RegExp(OPTION_MARKER_RE))) last = m
  if (!last || last.index === undefined) return { text: content, options: [], multi: true, isPlan: false }
  const multi = !!last[1] // [OPTIONS:] is the multi-select syntax; [OPTION:] is single
  const sep = last[2].includes('|') ? '|' : ','
  const options = last[2].split(sep).map(o => o.trim()).filter(Boolean)
  const isPlan = PLAN_HEADER_RE.test(content) && STAGE_RE.test(content)
  // Strip ALL markers from the displayed text (not just the last) so a stray earlier
  // marker can't leak as raw "[OPTION: …]" syntax to the user; options still come from
  // the LAST marker (computed above). OPTION_MARKER_RE is global, so replace removes
  // every occurrence while preserving the prose around them.
  const text = content.replace(OPTION_MARKER_RE, '').trim()
  return { text, options, multi, isPlan }
}

export interface FollowUpDerivation {
  followUpOptions: string[]
  followUpIsPlan: boolean
  /**
   * Identity of the row the options were derived from — `meta.mid` when
   * present, else the row's `ts`, else an index fallback. `null` when no
   * options are on offer (streaming, question pending, user boundary, none).
   *
   * Consumers that must know whether the CHIPS THEMSELVES changed — not just
   * their labels — compare this instead of the option labels: consecutive
   * plan footers are byte-identical (`[OPTION: Go | Go All | Cancel]`), so a
   * label key cannot distinguish stage 2's fresh offer from stage 1's stale
   * one after a single-write transcript hydration. The plan-dispatch latch
   * (usePlanActionMutation) is acknowledgement-gated on exactly this value.
   */
  followUpSourceKey: string | null
}

/**
 * Identity of the transcript row *m* sits at index *i* of, stable across
 * pagination AND across a hydration that enriches the row.
 *
 * The order matters and is NOT arbitrary: `meta.clientTs` is checked FIRST
 * because it is the only component guaranteed stable for the whole life of a
 * row. The store stamps it on any row lacking a server `ts` and then
 * deliberately CARRIES it onto the reloaded server copy (see
 * `chatSlice.ts` — "the renderer keys virtual rows by `clientTs ?? ts`, so
 * without this the row's key flips bornKey -> serverTs"), so this helper
 * matches the store's own keying convention rather than inventing a second,
 * conflicting one.
 *
 * Checking `mid` first would break that: a reconnect refresh preserves
 * `clientTs` but ADDS a server `mid`, so the same row would re-key mid-flight,
 * the acknowledgement effect would read it as a different row and free the
 * duplicate-action latch, and a stale second click could queue an unintended
 * extra `Go`. `mid` and `ts` remain as fallbacks for rows that never carried a
 * client stamp; the index fallback is a last resort for fixture-grade rows, and
 * a history prepend cannot re-key a real row.
 */
const rowIdentity = (m: ChatMessage, i: number): string =>
  (m.meta?.clientTs as string | undefined)
  ?? (m.meta?.mid as string | undefined)
  ?? m.ts
  ?? `idx:${i}`

/**
 * Derive the follow-up `[OPTIONS:]` buttons for the current chat by scanning
 * backward for the most recent real assistant turn.
 *
 * Three messages short-circuit the scan:
 *  - a `user` message ends the previous turn, so its options no longer apply →
 *    return none. UNLESS the turn it began failed: see `sawError` below.
 *  - a `queued` message means the user already acted (Quick Send while the
 *    slot was busy). The optimistic user bubble was suppressed, but the intent
 *    is identical — hide options immediately so they don't linger until the
 *    queue drains. Same failed-turn exception applies.
 *  - a `compaction` notice is skipped. Auto-compaction appends a
 *    "✅ Conversation compacted" message with the `assistant` role but tagged
 *    `kind="compaction"` (see `chat_utils._broadcast_compaction_result`). It
 *    carries no `[OPTIONS:]` marker, so without this skip it would shadow the
 *    real options-bearing turn it follows and the buttons would vanish after a
 *    compaction. The marker is read from `kind` (live websocket path) or
 *    `meta.kind` (history-reload path).
 *
 * A `user`/`queued` row is only a valid stop because it means "the user has
 * answered, so the question is closed". A row whose turn FAILED answered
 * nothing — the question is still open and the choices still apply — but the
 * row stays in the feed forever, so an unconditional stop hid the pills
 * permanently and the user had to retype the choice by hand. `sawError` tracks
 * an error row seen while scanning backward and lets exactly ONE such row be
 * crossed, re-arming per error so repeated failed attempts each get crossed.
 * `error` is the single role to key on: the backend routes every terminal turn
 * error through one `slot.append("error", …)` call site, so matching the role
 * covers every cause — timeout, transport, refusal — with no message parsing.
 *
 * `questionPending` suppresses the pills while an `ask_question` card is on
 * screen for the same slot, so the user is never offered the same choice twice
 * in two different widgets. The card wins because it is the one holding the
 * agent: it blocks a tool call, whereas the pills only compose a next message.
 * Clicking a pill against a blocked turn queues text that turn can never
 * consume, leaving the user waiting on an answer the agent never receives.
 * Callers that never render a card pass nothing — suppressing pills there would
 * leave that surface with no way to answer at all.
 */
export function deriveFollowUpOptions(
  messages: ChatMessage[],
  isStreaming: boolean,
  questionPending = false,
): FollowUpDerivation {
  if (isStreaming || questionPending) return { followUpOptions: [], followUpIsPlan: false, followUpSourceKey: null }
  // Errors were already transparent here (no branch matched them); the flag is
  // what makes that transparency mean something.
  let sawError = false
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m.role === 'error') { sawError = true; continue }
    if (m.role === 'user' || m.role === 'queued') {
      if (!sawError) return { followUpOptions: [], followUpIsPlan: false, followUpSourceKey: null }
      // Cross this failed turn and keep looking. Re-armed only by another error,
      // so a SUCCESSFUL turn further back still stops the scan.
      sawError = false
      continue
    }
    if (isSystemNoticeKind(m.kind ?? (m.meta?.kind as string | undefined))) continue
    // A note may carry options, so a zero-token cron can offer an action without an LLM turn.
    // `isNoteRow` also matches a rehydrated note, whose class the history format drops.
    if (m.role === 'inject' && isNoteRow(m) && m.content) {
      const parsed = parseOptions(m.content)
      if (parsed.options.length) {
        // NEVER isPlan: a note is not the orchestrator's plan turn, and `followUpIsPlan` is read
        // only to dispatch /plan-action — so plan-shaped note text would let `Cancel` kill a plan.
        // A note row still gets an identity: the bar keys its render off it, and a note whose
        // options never re-key would let a later identical note reuse the earlier row's key.
        return { followUpOptions: parsed.options, followUpIsPlan: false, followUpSourceKey: rowIdentity(m, i) }
      }
      continue
    }
    if (m.role === 'assistant' && m.content) {
      const { options, isPlan } = parseOptions(m.content)
      const followUpSourceKey = options.length > 0 ? rowIdentity(m, i) : null
      return { followUpOptions: options, followUpIsPlan: isPlan, followUpSourceKey }
    }
  }
  return { followUpOptions: [], followUpIsPlan: false, followUpSourceKey: null }
}
