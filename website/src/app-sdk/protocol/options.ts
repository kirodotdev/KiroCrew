import type { ChatMessage } from '../../types'
import { isSystemNoticeKind } from '../../lib/systemNotice'
import { isStopEvent } from '../../lib/stopEvent'
import { isRetryNotice } from '../../lib/retryNotice'
import { isNoteRow } from '../../lib/noteContract'
import { OPTION_MARKER_RE, matchActionMarkers, stripActionMarkers } from './optionMarker'

// A plan is recognised by BOTH its header and at least one stage line, so ordinary
// prose that happens to mention a plan is not mistaken for one.
const PLAN_HEADER_RE = /📋\s*Plan for:/i
const STAGE_RE = /^Stage\s+\d+\s*:/m

/**
 * An option whose click runs a LOCAL UI action instead of sending its label as text.
 *
 * The action and the label are separate fields on purpose. `[OPTIONS:]` labels are
 * model-emitted prose, so encoding the action IN the label — a magic word, a prefix —
 * would mean an agent writing documentation about this feature emits a live control that
 * tears down the user's tab. Splitting them makes the label inert: it is shown, never
 * interpreted.
 */
export interface OptionAction {
  /** Strict enum. An unrecognised action is DROPPED at parse time, never dispatched. */
  action: 'close'
  /** Free-text, model-authored, arbitrary. Never parsed for meaning. */
  label: string
}

/** The whole enum, as the value the parser filters against. Adding a member here is the
 *  ONLY way to widen what can be dispatched — an unknown action never reaches a caller,
 *  so a marker from a newer agent than this dashboard degrades to nothing rather than to
 *  an unpredictable local effect. */
const KNOWN_ACTIONS = new Set<OptionAction['action']>(['close'])

/**
 * The ONE dispatchable action in a marker body, or `null`.
 *
 * The first VALID entry wins and the rest are ignored, so there is never a second
 * one to return.
 *
 * It used to accumulate every valid entry into an array, and a separate
 * `visibleActions` dedupe in `FollowUpBar` then collapsed same-kind duplicates back
 * down. Over a ONE-member enum that pairing could never emit a second chip, so the
 * dedupe was unreachable code standing in for a rule nothing could break, and the
 * multi-entry capability was
 * generality no caller could reach and no document promised: the app-kit protocol
 * already specifies `[OPTION-ACTIONS: close=<label>]`, singular. Bounding it here
 * instead makes the code say what the contract says, in one place, with nothing
 * downstream to keep in step. A second action member is the commit that widens this,
 * and it owns the overflow affordance — which will need a real one (a menu), not a
 * silent tail-drop.
 *
 * Entries are still `|`-separated and split on the FIRST `=` only — the canonical label
 * `close=Nothing else, close this session` shows why: a label is free text and may
 * contain both `=` and `,`. That is also why the separator is `|` ALONE, unlike the
 * content-option parser's `|`-else-`,` fallback, which would tear that label in half.
 * Splitting is retained rather than dropped so the FILTERING still works positionally:
 * `reboot=Nope | close=Yes` must still find the valid entry rather than reading the
 * whole body as one malformed one.
 *
 * Three shapes are skipped silently rather than surfaced, because each would otherwise
 * become a button whose click does something the agent did not ask for:
 *   - an unknown action (not in KNOWN_ACTIONS);
 *   - an empty label — a chip with no text is unclickable in practice and unreadable;
 *   - an entry with no `=` at all, which is a content option written under the wrong head.
 * The action name is matched case-INSENSITIVELY, consistent with the head: the marker
 * regex already accepts any casing of `[OPTION-ACTIONS:`, so rejecting `Close=…` there
 * would be an inconsistency the author could not see. The label's own casing is untouched.
 */
function parseActionEntries(body: string): OptionAction | null {
  for (const entry of body.split('|')) {
    const eq = entry.indexOf('=')
    if (eq < 0) continue
    const action = entry.slice(0, eq).trim().toLowerCase() as OptionAction['action']
    const label = entry.slice(eq + 1).trim()
    if (!label || !KNOWN_ACTIONS.has(action)) continue
    return { action, label }
  }
  return null
}

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
  /** The local UI action from the LAST `[OPTION-ACTIONS:]` marker, or `null`.
   *  Independent of `options`: a row may carry either, both, or neither. Singular
   *  because `parseActionEntries` returns on its first valid entry, so a second one
   *  is not constructible — a plural shape would generalize over a member that
   *  cannot exist. */
  action: OptionAction | null
}

export function parseOptions(content: string): ParsedOptions {
  let last: RegExpMatchArray | null = null
  // `matchAll` seeds its internal clone from this regex's `lastIndex`, so a stray `.test()` or
  // `.exec()` anywhere would make the scan start mid-string and miss the marker. Clone per call:
  // the cost is one regex construction, the alternative is a silent parse failure.
  for (const m of content.matchAll(new RegExp(OPTION_MARKER_RE))) last = m
  // Same clone-per-call rule, same reason. Scanned unconditionally and INDEPENDENTLY of the
  // content marker: an action marker is not required to be accompanied by one, and the whole
  // point of the feature is a row that offers only the action.
  let lastAction: RegExpMatchArray | null = null
  for (const m of matchActionMarkers(content)) lastAction = m
  const action = lastAction ? parseActionEntries(lastAction[1]) : null

  if (!last || last.index === undefined) {
    // No content marker. With no action marker either there is nothing to strip, so the
    // content is returned BYTE-IDENTICAL (not trimmed) — the long-standing behaviour for a
    // marker-less message, which callers rely on for ordinary prose.
    if (!lastAction) return { text: content, options: [], multi: true, isPlan: false, action }
    // An action-only row still has to have its marker stripped, or the raw syntax is what
    // the user reads. ALL action markers go, not just the last, for the same reason the
    // content path strips all of them: a stray earlier one must not leak as raw text.
    const stripped = stripActionMarkers(content).trim()
    return { text: stripped, options: [], multi: true, isPlan: false, action }
  }
  const multi = !!last[1] // [OPTIONS:] is the multi-select syntax; [OPTION:] is single
  const sep = last[2].includes('|') ? '|' : ','
  const options = last[2].split(sep).map(o => o.trim()).filter(Boolean)
  const isPlan = PLAN_HEADER_RE.test(content) && STAGE_RE.test(content)
  // Strip ALL markers of BOTH kinds from the displayed text (not just the last of each) so a
  // stray earlier marker can't leak as raw "[OPTION: …]" / "[OPTION-ACTIONS: …]" syntax to the
  // user; options and actions still come from the LAST marker of their own kind (computed
  // above). Both regexes are global, so replace removes every occurrence while preserving the
  // prose around them.
  const text = stripActionMarkers(content.replace(OPTION_MARKER_RE, '')).trim()
  return { text, options, multi, isPlan, action }
}

export interface FollowUpDerivation {
  followUpOptions: string[]
  followUpIsPlan: boolean
  /**
   * Identity of the row the options were derived from — `meta.mid` when
   * present, else the row's `ts`, else an index fallback. `null` when NOTHING is
   * on offer (streaming, question pending, user boundary, none) — note that
   * "nothing" counts actions as well as options, so an action-only row still
   * gets an identity and the bar can re-key on it.
   *
   * Consumers that must know whether the CHIPS THEMSELVES changed — not just
   * their labels — compare this instead of the option labels: consecutive
   * plan footers are byte-identical (`[OPTION: Go | Go All | Cancel]`), so a
   * label key cannot distinguish stage 2's fresh offer from stage 1's stale
   * one after a single-write transcript hydration. The plan-dispatch latch
   * (usePlanActionMutation) is acknowledgement-gated on exactly this value.
   */
  followUpSourceKey: string | null
  /**
   * The local UI action on offer from the same row, `null` whenever there is none.
   *
   * Suppressed by EVERY branch that suppresses `followUpOptions`, and for the same
   * reason: an action offered after the user has already answered is the same defect
   * as a stale pill, and this one is worse than a stale pill because clicking it tears
   * down the tab rather than sending a message that can be ignored.
   */
  followUpAction: OptionAction | null
}

/** Nothing on offer. A factory rather than a shared frozen const: the arrays are handed
 *  to callers, and one caller mutating a shared empty array would corrupt every other
 *  reader of it. */
const noOffer = (): FollowUpDerivation => ({
  followUpOptions: [],
  followUpIsPlan: false,
  followUpSourceKey: null,
  followUpAction: null,
})

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
 * Derive the follow-up `[OPTIONS:]` buttons — and any `[OPTION-ACTIONS:]` local-UI
 * chips — for the current chat by scanning backward for the most recent real
 * assistant turn.
 *
 * The two are derived TOGETHER from the same row rather than by two scans, because
 * every suppression rule below is a statement about the ROW's freshness, not about
 * which marker it carries: an action reached across a boundary is stale for exactly
 * the reasons an option is. A row may carry either marker, both, or neither — so
 * "does this row offer anything" is `options.length || action !== null` everywhere,
 * and a row carrying ONLY an action marker is a first-class offer.
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
  if (isStreaming || questionPending) return noOffer()
  // Errors were already transparent here (no branch matched them); the flag is
  // what makes that transparency mean something.
  let sawError = false
  // Set by EITHER crossing: both leave a plan row that may already have advanced (see above).
  let crossedFailedTurn = false
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    // A deliberate Stop ENDS the turn rather than interrupting it, so the choice is closed
    // by the user's own cancellation — the error licence below must not reach back past it.
    if (isStopEvent(m)) return noOffer()
    // Only a TERMINAL error licenses a crossing. A retry notice means the recovery is
    // already queued, so re-offering the pill would run the same choice a second time.
    if (m.role === 'error') { if (!isRetryNotice(m)) sawError = true; continue }
    // `queued` is an UNCONDITIONAL stop: its queue entry OUTLIVES the error (only a hard
    // kill clears the queue), so re-offering the pill would run the choice a second time.
    if (m.role === 'queued') return noOffer()
    if (m.role === 'user') {
      if (!sawError) return noOffer()
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
      // Actions count as an offer in their own right: a note carrying ONLY
      // `[OPTION-ACTIONS:]` is exactly the zero-turn case this exists for, and keying
      // this on `options.length` alone would silently drop it and scan past the row.
      if (parsed.options.length || parsed.action) {
        // NEVER isPlan: a note is not the orchestrator's plan turn, and `followUpIsPlan` is read
        // only to dispatch /plan-action — so plan-shaped note text would let `Cancel` kill a plan.
        // A note row still gets an identity: the bar keys its render off it, and a note whose
        // options never re-key would let a later identical note reuse the earlier row's key.
        return {
          followUpOptions: parsed.options,
          followUpIsPlan: false,
          followUpSourceKey: rowIdentity(m, i),
          followUpAction: parsed.action,
        }
      }
      continue
    }
    if (m.role === 'assistant' && m.content) {
      const { options, isPlan, action } = parseOptions(m.content)
      // "Offers something" is options OR actions — every test below that used to read
      // `options.length` has to consider both, or an action-only row is treated as an
      // empty one and either crossed or given a null source key.
      const offers = options.length > 0 || action !== null
      // A failed turn can flush the text it streamed as a real assistant row before the
      // error, and that offer-less row shadowed the question exactly as the `user` row did.
      // Crossing does NOT consume the error licence: the `user` row below still needs it.
      if (!offers && sawError) { crossedFailedTurn = true; continue }
      // Offer NOTHING for a plan row reached that way. Demoting to the composer path is not
      // enough: with Quick Send on, one click still sends `Go All` as orchestrator-run text.
      // Actions are withheld here too — a close chip reached across a failed turn would tear
      // down the tab on the strength of a turn whose outcome is unknown.
      if (isPlan && crossedFailedTurn) return noOffer()
      const followUpSourceKey = offers ? rowIdentity(m, i) : null
      return { followUpOptions: options, followUpIsPlan: isPlan, followUpSourceKey, followUpAction: action }
    }
  }
  return noOffer()
}

/** The content options MINUS any label the action already owns.
 *
 * The two markers are parsed INDEPENDENTLY, so a model emitting the same label in both
 * rendered two chips with identical text — and the content one SENDS A MODEL TURN, which is
 * the exact cost the action exists to avoid. The action wins: it is the more specific offer,
 * and its label is what the close confirm quotes back. Folded case and trimmed, because two
 * chips differing only in case or padding are the same collision to a reader.
 *
 * Callers that DROP the action (an embed wiring no `onAction`) must NOT apply this: filtering
 * there would remove the label with nothing left on screen to render it.
 */
export function optionsExcludingAction(
  options: readonly string[],
  action: OptionAction | null,
): string[] {
  if (!action) return [...options]
  const owned = action.label.trim().toLowerCase()
  return options.filter((o) => o.trim().toLowerCase() !== owned)
}
