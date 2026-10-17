/**
 * A version number for what a transcript row can read off the OTHER rows.
 *
 * A row's output depends on its own messages and, through
 * `MessageRenderContext.messages`, on its neighbours: the reply footer reads
 * the roles that follow it, the steer chip and the error card read the turn
 * around them, a crewmate bubble reads where its run starts and ends. So a
 * memoized row has to redraw whenever the transcript changes anywhere, with one
 * exception that this counter is built around: a `streaming` row's text
 * growing in place. Every reader above treats a streaming row by its role
 * alone (it counts as a reply that follows, as the crewmate speaking, as an
 * answer below a retry notice), never by its text, so a streaming flush cannot
 * change what any other row draws. The version therefore moves on every change
 * to the list except:
 *
 * - a row that was `streaming` and is still `streaming` at the same position
 *   (its text, `rawText` or meta changed), and
 * - a row replaced by a structurally equal copy (`jsonEqual`): a host that
 *   re-mints an unchanged row, like the fresh-start divider ChatPane builds
 *   per render, hands the neighbours exactly what they read before.
 *
 * A new length, a reorder, a role change (a streaming row settling into a
 * reply) or any edit to a settled row moves it. The one-slot cache holds the
 * last list only, like `createTurnGrouper`; each consumer creates its own.
 */
import type { ChatMessage } from '../../types'
import { jsonEqual } from '../../utils/structuralEqual'

/** Whether every row reads the same off `b[i]` as it read off `a[i]`. */
function neighboursReadTheSame(a: readonly ChatMessage[], b: readonly ChatMessage[]): boolean {
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) {
    const was = a[i]
    const now = b[i]
    if (was === now) continue
    if (was.role === 'streaming' && now.role === 'streaming') continue
    if (!jsonEqual(was, now)) return false
  }
  return true
}

/** Returns a function that maps each successive transcript to its version.
 *  Starts at 1 on the first list and only ever counts up. */
export function createTranscriptVersioner(): (messages: readonly ChatMessage[]) => number {
  let prev: readonly ChatMessage[] | null = null
  let version = 0
  return (messages: readonly ChatMessage[]): number => {
    if (messages === prev) return version
    if (prev === null || !neighboursReadTheSame(prev, messages)) version += 1
    prev = messages
    return version
  }
}
