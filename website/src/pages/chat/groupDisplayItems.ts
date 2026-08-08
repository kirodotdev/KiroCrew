import type { ChatMessage } from '../../types'
import type { DisplayItem, TurnItem } from './types'
import { isSubagentCompletionMessage } from './subagentCompletion'

/** Roles that fold into a collapsible group in the turn view. Thinking is NOT
 *  here: it carries real content and renders as its own standalone block (a
 *  content-bearing reasoning trace), so grouping it into the "N tool calls"
 *  collapsible would bury and mislabel it. */
export const GROUPABLE = new Set(['permission'])

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

  // Phase 2: group into turns (user message → next user message)
  const turns: DisplayItem[] = []
  let turnItems: TurnItem[] = []
  const hasWorkingSteps = (items: TurnItem[]) =>
    items.some(t =>
      (t.kind === 'single' && (t.msg.role === 'tool' || t.msg.role === 'assistant' || t.msg.role === 'streaming')) ||
      t.kind === 'group'
    )
  const flushTurn = (items: TurnItem[], complete: boolean) => {
    if (hasWorkingSteps(items) && items.length > 2) {
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
    if (item.kind === 'single' && (item.msg.role === 'user' || item.msg.role === 'nudge' || item.msg.role === 'subagent')) {
      if (turnItems.length > 0) { flushTurn(turnItems, true); turnItems = [] }
      turns.push(item)
    } else {
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
