import type { ChatMessage } from '../../types'
import { isSystemNoticeKind } from '../../lib/systemNotice'
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
}

/**
 * Derive the follow-up `[OPTIONS:]` buttons for the current chat by scanning
 * backward for the most recent real assistant turn.
 *
 * Three messages short-circuit the scan:
 *  - a `user` message ends the previous turn, so its options no longer apply →
 *    return none.
 *  - a `queued` message means the user already acted (Quick Send while the
 *    slot was busy). The optimistic user bubble was suppressed, but the intent
 *    is identical — hide options immediately so they don't linger until the
 *    queue drains.
 *  - a `compaction` notice is skipped. Auto-compaction appends a
 *    "✅ Conversation compacted" message with the `assistant` role but tagged
 *    `kind="compaction"` (see `chat_utils._broadcast_compaction_result`). It
 *    carries no `[OPTIONS:]` marker, so without this skip it would shadow the
 *    real options-bearing turn it follows and the buttons would vanish after a
 *    compaction. The marker is read from `kind` (live websocket path) or
 *    `meta.kind` (history-reload path).
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
  if (isStreaming || questionPending) return { followUpOptions: [], followUpIsPlan: false }
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i]
    if (m.role === 'user' || m.role === 'queued') return { followUpOptions: [], followUpIsPlan: false }
    if (isSystemNoticeKind(m.kind ?? (m.meta?.kind as string | undefined))) continue
    if (m.role === 'assistant' && m.content) {
      const { options, isPlan } = parseOptions(m.content)
      return { followUpOptions: options, followUpIsPlan: isPlan }
    }
  }
  return { followUpOptions: [], followUpIsPlan: false }
}
