import type { ChatMessage } from '../../types'
import type { DisplayItem, TurnItem } from './types'
import { isSubagentCompletionMessage } from './subagentCompletion'

/** Roles that fold into a collapsible group in the turn view. Thinking is NOT
 *  here: it carries real content and renders as its own standalone block (a
 *  content-bearing reasoning trace), so grouping it into the "N tool calls"
 *  collapsible would bury and mislabel it. */
export const GROUPABLE = new Set(['permission'])

/**
 * The reasoning roles, and what "content-bearing reasoning" means. These are
 * THE single definition of the classification, shared by every display-layer
 * site that acts on it:
 *
 *  - the wrap gate below (`contentThinkingCount` via `isReasoningBurst`, which
 *    decides when a batch is routed into a {kind:'turn'} wrapper),
 *  - the per-turn fold that gate feeds (`mergeTurnThinking` in TurnBlock.tsx,
 *    which folds a turn's bursts into one hoisted row),
 *  - ChatPage's `renderMessage` (content-bearing → ThinkingBlock, empty
 *    placeholder → nothing, via `hasReasoningContent` + `isReasoningRole`),
 *  - the shared-transcript registry entry (`transcriptRenderers.tsx`, whose
 *    `roles` key and render guard both derive from here).
 *
 * These sites used to keep hand-written copies of the same condition; any
 * future refinement (a new reasoning role, a whitespace guard, a meta flag)
 * must happen HERE so the wrap threshold, the fold, and the row renderers can
 * never drift apart — that drift is exactly how the duplicate
 * "Thought process" rows of #6376 would regrow. (The store's burst-lifecycle
 * mechanics in chatSlice are a different concern — they manage streaming
 * placeholders, not display classification — and deliberately stay separate.)
 */
export const REASONING_ROLES = ['thinking'] as const

const REASONING_ROLE_SET: ReadonlySet<string> = new Set(REASONING_ROLES)

/** Is this message a reasoning trace (regardless of whether it has content)?
 *  Structurally typed so raw-snapshot (wire-shape) surfaces can reuse it. */
export const isReasoningRole = (msg: { role: string }): boolean =>
  REASONING_ROLE_SET.has(msg.role)

/** A content-bearing reasoning message; empty placeholders render nothing and never count. */
export const hasReasoningContent = (msg: { role: string; content: string }): boolean =>
  isReasoningRole(msg) && !!msg.content

/** Item-level form of {@link hasReasoningContent} for TurnItem scans. */
export const isReasoningBurst = (t: TurnItem): t is Extract<TurnItem, { kind: 'single' }> =>
  t.kind === 'single' && hasReasoningContent(t.msg)

/**
 * Roles that OPEN a turn, and are therefore the rows a reader can be anchored to.
 *
 * `nudge` and `subagent` are machine-injected but they ARE the thing that started
 * the turn below them, so a reader looking for "what am I inside" needs them. This
 * set is exported because the pinned-prompt scan has to agree with the grouping
 * exactly: when the two lists were maintained by hand they drifted, and a role
 * that opened a turn without being pinnable made the pin scan walk past every one
 * of them — measured at a 61-display-row gap in a loop-driven session.
 */
export const TURN_OPENER_ROLES = new Set(['user', 'nudge', 'subagent'])

export interface GroupedTurns {
  turns: DisplayItem[]
  /** Index into `turns` of the turn object produced by the TRAILING flush, or
   *  -1 when the trailing group did not collapse into a turn (it was spread as
   *  loose items instead, and so carries no `complete` flag). This is the only
   *  element whose `complete` value depends on whether the slot is still
   *  running. */
  trailingTurnIdx: number
}

/**
 * Group a slot's messages into transcript display items.
 *
 * Split out of ChatPage for two reasons. It is pure and O(N) over the whole
 * message list, so it must be memoized on `messages` ALONE — bundling the
 * `slotRunning` flag into the same memo re-ran this entire pass on every turn
 * start/stop just to flip one boolean, and the resulting new identity cascaded
 * into the display-index maps and the virtualizer. And it decides what the user
 * actually sees, which makes it worth testing directly rather than through a
 * 4,000-line component.
 *
 * The trailing turn is always flushed as `complete: true`; the caller applies
 * the running state in O(1) via `trailingTurnIdx`.
 */
export function groupDisplayItems(messages: ChatMessage[]): GroupedTurns {
  // Phase 1: build raw items (singles + groups)
  const raw: TurnItem[] = []
  let group: ChatMessage[] = [], groupStart = 0
  for (let i = 0; i < messages.length; i++) {
    // Permission messages handled by pinned ApprovalBar — skip entirely
    if (messages[i].role === 'permission') continue
    // A sub-agent completion the card cannot parse stays internal: the LLM sees
    // it, the user does not. One it CAN parse renders as a compact outcome row,
    // which is the only scrollback record that a wave's results arrived.
    if (messages[i].role === 'subagent' && !isSubagentCompletionMessage(messages[i])) continue
    if (GROUPABLE.has(messages[i].role)) {
      if (!group.length) groupStart = i
      group.push(messages[i])
    } else {
      if (group.length) { raw.push({ kind: 'group', msgs: group, startIdx: groupStart }); group = [] }
      raw.push({ kind: 'single', msg: messages[i], idx: i })
    }
  }
  if (group.length) raw.push({ kind: 'group', msgs: group, startIdx: groupStart })

  // Phase 2: group into turns (user message → next user message).
  // Track whether the last subagent completion had synthesisPending set — if so,
  // the assistant response in its turn is a redundant per-completion summary that
  // synthesis will restate. Hide it from the transcript so the user only sees the
  // completion card + the final synthesis. This check lives here (not in a
  // pre-filter) because TurnBlock's visibility system (isVisibleInline, etc.)
  // is the authority on what stays visible; we suppress ONLY when the backend
  // explicitly marked the completion as having pending synthesis.
  let _lastSubagentHadSynthesis = false
  const turns: DisplayItem[] = []
  let turnItems: TurnItem[] = []
  const hasWorkingSteps = (items: TurnItem[]) =>
    items.some(t =>
      (t.kind === 'single' && (t.msg.role === 'tool' || t.msg.role === 'assistant' || t.msg.role === 'streaming')) ||
      t.kind === 'group'
    )
  // A batch that carries TWO OR MORE content-bearing reasoning bursts must be
  // wrapped as a {kind:'turn'}, even when it has no tool/assistant "working
  // steps" and even when it is short. The per-turn reasoning-burst dedup
  // (mergeTurnThinking in TurnBlock) — which folds a turn's many `thinking`
  // bursts into ONE row hoisted above the answer — runs ONLY on {kind:'turn'}
  // items. Left as loose singles (the else branch), each burst renders as its
  // own standalone "Thought process" row via ChatPage's renderMessage,
  // bypassing the dedup entirely: the duplicate-row wall of #6376. This bites a
  // reasoning-only trailing turn (a monitor/nudge cycle that has only emitted
  // reasoning so far) and any turn whose reasoning bursts land as a short/
  // answerless batch — and it became common because finer-grained models (e.g.
  // claude-opus-5) emit many small bursts per turn. The threshold is TWO: a
  // single burst renders as exactly one row whether loose or wrapped (nothing
  // to dedup), so wrapping it would only re-home it needlessly. `thinking` is
  // deliberately NOT counted in hasWorkingSteps (it is a reasoning trace, not a
  // working step that gates the "Worked through N steps" collapse), so this is
  // a separate predicate. Empty placeholder bursts do not count (they render
  // nothing, and mergeTurnThinking ignores them too).
  const contentThinkingCount = (items: TurnItem[]) =>
    items.reduce((n, t) => n + (isReasoningBurst(t) ? 1 : 0), 0)
  const flushTurn = (items: TurnItem[], complete: boolean) => {
    if ((hasWorkingSteps(items) && items.length > 2) || contentThinkingCount(items) >= 2) {
      turns.push({ kind: 'turn', items, complete })
    } else {
      turns.push(...items)
    }
  }
  for (const item of raw) {
    // A nudge opens a new turn exactly like a user message does — it IS the
    // turn's prompt. Without this it gets swallowed into the previous turn's
    // collapsed step group and the cycle chip disappears. A sub-agent
    // completion is the same case: the gateway injects it as the next turn's
    // input, so the agent's reply belongs BELOW the card, not beside it.
    if (item.kind === 'single' && TURN_OPENER_ROLES.has(item.msg.role)) {
      if (turnItems.length > 0) { flushTurn(turnItems, true); turnItems = [] }
      // Track whether this subagent completion has synthesis pending
      _lastSubagentHadSynthesis = item.msg.role === 'subagent' &&
        !!(item.msg.meta as Record<string, unknown> | undefined)?.synthesisPending
      turns.push(item)
    } else if (
      _lastSubagentHadSynthesis &&
      item.kind === 'single' &&
      (item.msg.role === 'assistant' || item.msg.role === 'streaming') &&
      !isSubagentCompletionMessage(item.msg)
    ) {
      // Per-completion response with synthesis pending: skip it from the
      // transcript. The synthesis turn will restate the findings.
      _lastSubagentHadSynthesis = false
    } else {
      _lastSubagentHadSynthesis = false
      turnItems.push(item)
    }
  }
  // Flush the trailing group as complete, and remember whether that flush
  // actually produced a turn object (flushTurn spreads the items instead when
  // the turn is too short to collapse). Only that element carries a `complete`
  // flag for the running state to affect.
  let trailingTurnIdx = -1
  if (turnItems.length > 0) {
    const before = turns.length
    flushTurn(turnItems, true)
    const last = turns[turns.length - 1]
    if (turns.length === before + 1 && last && last.kind === 'turn') {
      trailingTurnIdx = turns.length - 1
    }
  }
  return { turns, trailingTurnIdx }
}

/**
 * Apply the slot's running state to the grouped output. O(1): when the slot is
 * still running the trailing turn is not complete yet, so exactly one element is
 * replaced and every other item keeps its identity.
 */
export function applyRunningState(grouped: GroupedTurns, slotRunning: boolean): DisplayItem[] {
  const { turns, trailingTurnIdx } = grouped
  if (trailingTurnIdx < 0 || !slotRunning) return turns
  const out = turns.slice()
  const t = out[trailingTurnIdx]
  if (t && t.kind === 'turn') out[trailingTurnIdx] = { ...t, complete: false }
  return out
}
