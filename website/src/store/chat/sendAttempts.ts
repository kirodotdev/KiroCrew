// Pruning for `ChatState.sendAttempts` (the prompts ↑ can still recall
// after their bubble left the transcript).

import type { ChatMessage } from '../../types'
import type { ChatState } from './state'
import { rowSendIds } from '../../utils/rowSendIds'
import { isUnsafeKey, safeKey } from './wire'

/** Drop `slot`'s send records up to and including the newest one whose
 *  delivery is confirmed (by its receipt, a correlated echo, or a history
 *  load carrying its `sendId`). A delivered send is not a lost one, and the
 *  sends before it were moved past, so none of them can come back on ↑ --
 *  not even after another tab rewinds the row away. Newer, still
 *  unconfirmed records are kept. */
export function pruneDeliveredAttempts(state: ChatState, slot: string | null | undefined, sendIds: Iterable<string>): void {
  if (!slot || isUnsafeKey(slot)) return
  const map = state.sendAttempts
  const key = safeKey(slot)
  const list = map?.[key]
  if (!map || !list?.length) return
  const delivered = new Set<string>()
  for (const id of sendIds) if (id) delivered.add(id)
  if (!delivered.size) return
  for (let i = list.length - 1; i >= 0; i--) {
    if (!delivered.has(list[i].sendId)) continue
    map[key] = list.slice(i + 1)
    return
  }
}

/** The send ids carried by server-persisted user rows in `messages`,
 *  including every send a merged queued row stands for (`rowSendIds`). */
export function persistedSendIds(messages: readonly ChatMessage[]): string[] {
  const ids: string[] = []
  for (const m of messages) if (m.role === 'user' && m.meta?.mid) ids.push(...rowSendIds(m.meta))
  return ids
}
