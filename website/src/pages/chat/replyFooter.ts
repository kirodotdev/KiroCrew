import type { ChatMessage } from '../../types'
import { isHiddenInvisibleAssistantRow } from '../../utils/invisibleText'
import type { TurnStats } from './AssistantMessage'
import { isSystemNoticeRow } from './CompactionCard'
import { opensTurn } from './RecoveryCard'

/**
 * Whether the reply at `index` draws its end-of-turn footer: the stats line
 * (model, credits, elapsed) and the action row under the bubble (Copy, Fork,
 * More). The one rule for every surface that draws a reply -- the single-chat
 * page's row and the SDK's `renderAssistantBubble` -- so the two cannot drift.
 *
 * A reply carrying positive `meta.turn_stats` always draws it. The runner
 * stamps them on the last reply row of a turn however the turn ended
 * (`_attach_turn_stats` for a completed turn, `_persist_partial_reply` for any
 * other end), so whatever is drawn below that row -- an auto-nudge cycle, the
 * recovery after an error or a capacity retry, a cron run, a turn another tab
 * started -- belongs to a later turn and must not take the reply's footer
 * away. Taking it away also costs a reader parked at the bottom their place:
 * the stats line and the action row leave the bottom edge, the engine clamps
 * the reader down, and nothing pushes them back up when the footer returns.
 *
 * A reply without stats waits for its turn to be over: a user row follows (a
 * steer too), or a row that opens a turn of its own (`opensTurn`: an auto-nudge
 * cycle, a sub-agent completion, a cron, synthesis or app inject), or nothing
 * follows and the session is idle. Another reply first means the turn goes on.
 * A hidden say-nothing row and a system notice draw no reply, so the walk passes
 * over both. A streaming row never draws a footer.
 *
 * `m` is the row being drawn and `messages` the list the walk reads after
 * `index`. They are separate because a page draws a deferred snapshot of its
 * rows while handing the walk the live list, so `messages[index]` is not always
 * the row on screen.
 */
export function replyShowsFooter(m: ChatMessage, messages: readonly ChatMessage[], index: number, running: boolean): boolean {
  if (m.role === 'streaming') return false
  const stats = m.meta?.turn_stats as Partial<TurnStats> | undefined
  if (typeof stats?.elapsed_ms === 'number' && stats.elapsed_ms > 0) return true
  for (let j = index + 1; j < messages.length; j++) {
    const later = messages[j]
    if (later.role === 'user' || opensTurn(later)) return true
    if (isHiddenInvisibleAssistantRow(later) || isSystemNoticeRow(later)) continue
    if (later.role === 'assistant' || later.role === 'streaming') return false
  }
  return !running
}
