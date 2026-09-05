import type { ChatMessage } from '../../types'
import { isSystemNoticeKind } from '../../lib/systemNotice'
import { isStopEvent } from '../../lib/stopEvent'
import { isRetryNotice } from '../../lib/retryNotice'
import { isNoteRow } from '../../lib/noteContract'
import { OPTION_MARKER_RE } from './optionMarker'
import { splitRecommendation } from './recommendation'

// A plan is recognised by BOTH its header and at least one stage line, so ordinary
// prose that happens to mention a plan is not mistaken for one.
const PLAN_HEADER_RE = /📋\s*Plan for:/i
const STAGE_RE = /^Stage\s+\d+\s*:/m

/** A message split into the prose the user reads and the choices offered alongside it. */
export interface ParsedOptions {
  /** `content` with every marker removed, trimmed — what a transcript should render. */
  text: string
  /**
   * Choices from the LAST marker, in the order the agent listed them, each with
   * its `(recommended)` marker removed — see `./recommendation`. These strings
   * are canonical: a click sends one verbatim and every `picked` set is keyed on
   * them, so the marker must be gone HERE rather than at render time, or the
   * label a chip displays and the label its host tracks would disagree.
   */
  options: string[]
  /**
   * The one label from `options` that carried a recommendation, or `null`.
   *
   * A single label rather than a set, because the only sanctioned producer says
   * "Mark at most one option": a set would hedge a multi-recommendation producer
   * that does not exist, and every host consumer only ever asks whether ONE chip
   * is the recommended one. Narrowing later would be a breaking change for app
   * authors, so it happens here, before this field is published SDK surface.
   *
   * A label, not the marker word: the grammar admits exactly one word, so a
   * label-to-word map's value could only ever be that constant.
   *
   * When a producer breaks the contract and marks several, the FIRST wins and the
   * rest render unmarked — a chip menu with two "recommended" badges recommends
   * nothing, so collapsing to one is the reading that keeps the badge meaningful.
   */
  recommended: string | null
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
  if (!last || last.index === undefined) return { text: content, options: [], recommended: null, multi: true, isPlan: false }
  const multi = !!last[1] // [OPTIONS:] is the multi-select syntax; [OPTION:] is single
  const sep = last[2].includes('|') ? '|' : ','
  // Split the recommendation marker off each label BEFORE anything downstream sees it, so the
  // strings the rest of the app keys on are the instruction alone. `filter(Boolean)` runs on the
  // raw split (an empty label is not a choice); the marker cannot empty a label, because
  // `splitRecommendation` declines to strip one that would.
  let recommended: string | null = null
  const options = last[2].split(sep).map(o => o.trim()).filter(Boolean).map(raw => {
    const { label, hasMarker } = splitRecommendation(raw)
    if (hasMarker && recommended === null) recommended = label
    return label
  })
  const isPlan = PLAN_HEADER_RE.test(content) && STAGE_RE.test(content)
  // Strip ALL markers from the displayed text (not just the last) so a stray earlier
  // marker can't leak as raw "[OPTION: …]" syntax to the user; options still come from
  // the LAST marker (computed above). OPTION_MARKER_RE is global, so replace removes
  // every occurrence while preserving the prose around them.
  const text = content.replace(OPTION_MARKER_RE, '').trim()
  return { text, options, recommended, multi, isPlan }
}

export interface FollowUpDerivation {
  followUpOptions: string[]
  /** `ParsedOptions.recommended` for the row the options came from. */
  followUpRecommended: string | null
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
 *    queue drains. This stop is UNCONDITIONAL: no failed-turn exception.
 *  - a `stop_event` card is an UNCONDITIONAL stop too. A deliberate Stop ends
 *    the turn rather than interrupting it, so the question is closed by the
 *    user's own cancellation and no error may license reaching back past it.
 *  - a `compaction` notice is skipped. Auto-compaction appends a
 *    "✅ Conversation compacted" message with the `assistant` role but tagged
 *    `kind="compaction"` (see `chat_utils._broadcast_compaction_result`). It
 *    carries no `[OPTIONS:]` marker, so without this skip it would shadow the
 *    real options-bearing turn it follows and the buttons would vanish after a
 *    compaction. The marker is read from `kind` (live websocket path) or
 *    `meta.kind` (history-reload path).
 *
 * A `user`/`queued` row is only a valid stop because it means "the user has
 * answered, so the question is closed". A `user` row whose turn FAILED answered
 * nothing — the question is still open and the choices still apply — but the
 * row stays in the feed forever, so an unconditional stop hid the pills
 * permanently and the user had to retype the choice by hand. `sawError` tracks
 * an error row seen while scanning backward and lets exactly ONE such row be
 * crossed, re-arming per error so repeated failed attempts each get crossed.
 * A `queued` row is NEVER crossed: unlike a `user` row it leaves a live entry
 * in `slot._queue`, which only a hard kill clears, so the choice still runs
 * when the queue drains — re-offering the pill would run it a second time.
 * `error` is the role to key on, but NOT on its own: the backend reaches the feed
 * with role `error` for a terminal failure AND for an auto-retry notice whose
 * recovery is already queued, so only a row without `TRANSIENT_RETRY_KIND` may
 * license a crossing — otherwise the pill re-runs a choice already re-running.
 * A failed turn can also flush the text it streamed as a real assistant row
 * before the error, so an option-less assistant row under a live `sawError` is
 * crossed too — otherwise a partial answer shadows the question that is still
 * open. The trade is deliberate: nothing on the row marks it partial rather
 * than complete, so an error arriving after a genuinely finished option-less
 * reply reads the same way and can re-offer the previous turn's choices. A
 * PLAN row reached that way is therefore offered NOTHING: the plan may already
 * have advanced, and demoting to the composer path would not help because
 * Quick Send sends a pill in one click regardless of `followUpIsPlan`. Cost:
 * a plan turn that flushed a partial before failing gets no chips back.
 *
 * The same suppression covers a plan row reached across a failed `user` row.
 * That row records a click already DISPATCHED, and `usePlanActionMutation`
 * treats any 5xx, 408/429 or transport rejection as possibly-committed, so the
 * stage may have advanced before the turn failed. Its go-latch blocks the
 * second Go only within one page load — the latch is a module-level Map — so it
 * cannot cover the rehydrated transcript this exception creates. Plan chips are
 * the narrow case: a Go advances server state by itself, a text pill does not.
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
  if (isStreaming || questionPending) return { followUpOptions: [], followUpRecommended: null, followUpIsPlan: false, followUpSourceKey: null }
  // Errors were already transparent here (no branch matched them); the flag is
  // what makes that transparency mean something.
  let sawError = false
  // Set by EITHER crossing: both leave a plan row that may already have advanced (see above).
  let crossedFailedTurn = false
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    // A deliberate Stop ENDS the turn rather than interrupting it, so the choice is closed
    // by the user's own cancellation — the error licence below must not reach back past it.
    if (isStopEvent(m)) return { followUpOptions: [], followUpRecommended: null, followUpIsPlan: false, followUpSourceKey: null }
    // Only a TERMINAL error licenses a crossing. A retry notice means the recovery is
    // already queued, so re-offering the pill would run the same choice a second time.
    if (m.role === 'error') { if (!isRetryNotice(m)) sawError = true; continue }
    // `queued` is an UNCONDITIONAL stop: its queue entry OUTLIVES the error (only a hard
    // kill clears the queue), so re-offering the pill would run the choice a second time.
    if (m.role === 'queued') return { followUpOptions: [], followUpRecommended: null, followUpIsPlan: false, followUpSourceKey: null }
    if (m.role === 'user') {
      if (!sawError) return { followUpOptions: [], followUpRecommended: null, followUpIsPlan: false, followUpSourceKey: null }
      // Cross this failed turn and keep looking. Re-armed only by another error,
      // so a SUCCESSFUL turn further back still stops the scan.
      sawError = false
      crossedFailedTurn = true
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
        return { followUpOptions: parsed.options, followUpRecommended: parsed.recommended, followUpIsPlan: false, followUpSourceKey: rowIdentity(m, i) }
      }
      continue
    }
    if (m.role === 'assistant' && m.content) {
      const { options, recommended, isPlan } = parseOptions(m.content)
      // A failed turn can flush the text it streamed as a real assistant row before the
      // error, and that option-less row shadowed the question exactly as the `user` row did.
      // Crossing does NOT consume the error licence: the `user` row below still needs it.
      if (!options.length && sawError) { crossedFailedTurn = true; continue }
      // Offer NOTHING for a plan row reached that way. Demoting to the composer path is not
      // enough: with Quick Send on, one click still sends `Go All` as orchestrator-run text.
      if (isPlan && crossedFailedTurn) return { followUpOptions: [], followUpRecommended: null, followUpIsPlan: false, followUpSourceKey: null }
      const followUpSourceKey = options.length > 0 ? rowIdentity(m, i) : null
      return { followUpOptions: options, followUpRecommended: recommended, followUpIsPlan: isPlan, followUpSourceKey }
    }
  }
  return { followUpOptions: [], followUpRecommended: null, followUpIsPlan: false, followUpSourceKey: null }
}
