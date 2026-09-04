import { isSystemNoticeKind } from './systemNotice'

/**
 * Minimal message shape this counter needs. `ChatMessage` satisfies it
 * structurally, so callers pass their real message array unchanged while tests
 * can build tiny literals without every ChatMessage field.
 */
export interface TurnCountMessage {
  role: string
  kind?: string
  meta?: { kind?: unknown } | null
}

/**
 * Count COMPLETED back-and-forths: one user message answered by an assistant
 * reply. A single exchange can emit several assistant-role messages (the reply
 * plus a tool step, a stage separator, or a follow-up card), so a plain
 * assistant-message tally over-counts and trips the survey threshold sooner
 * than the user perceives one "turn".
 *
 * The walk counts only the FIRST assistant reply after each user message
 * (`awaitingReply` flips false once counted and only a new user message re-arms
 * it), so any run of assistant messages inside one turn collapses to exactly 1.
 * Assistant-role system notices (compaction, session_reload) are skipped
 * entirely so a status line never stands in for a real turn.
 */
export function countCompletedTurns(messages: readonly TurnCountMessage[]): number {
  let count = 0
  let awaitingReply = false
  for (const m of messages) {
    if (isSystemNoticeKind(m.kind ?? (m.meta?.kind as string | undefined))) continue
    if (m.role === 'user') {
      awaitingReply = true
    } else if (m.role === 'assistant' && awaitingReply) {
      count += 1
      awaitingReply = false
    }
  }
  return count
}
