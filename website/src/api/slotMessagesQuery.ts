import type { QueryClient } from '@tanstack/react-query'

/**
 * The per-pane transcript hydrate query (`ChatPane`), spelled once.
 *
 * `ChatPane` reads a slot's history through
 * `['slot-messages', slotKey, limit]` with `staleTime: Infinity`: the fetch is
 * one-shot by design, and every later row reaches the pane through the
 * websocket store routing. That contract holds only while the socket holds.
 * Frames are broadcast fire-and-forget with no per-client replay, so a
 * `tool_result`, a later `tool_call` or the final `_done` that the gateway
 * emitted while this client's socket was down never arrives, and nothing in
 * the pane's own query refetches it: the pane keeps rendering the row it
 * held at the drop.
 *
 * The reconnect branch of `useWebSocket` closes that window for the ACTIVE
 * slot (`refreshSlot`) and for the active slot's persisted split members
 * (`warmSlotCache`). A pane hosting a slot that is neither — the Crew
 * Members DM thread, whose `member-<slug>` slot is never the Redux active
 * slot and never a split member — is covered by neither on its own. Rather
 * than a second registry of "which panes exist", the observed queries of this
 * key ARE that registry: a mounted `ChatPane` holds an observer on its slot's entry, an
 * unmounted one releases it, and react-query already tracks the count.
 */
export const SLOT_MESSAGES_QUERY_KEY = 'slot-messages'

export const slotMessagesQueryKey = (slotKey: string, limit: number | undefined) =>
  [SLOT_MESSAGES_QUERY_KEY, slotKey, limit] as const

/**
 * Every slot some mounted `ChatPane` is currently rendering, de-duplicated.
 *
 * Reads only the query cache's observer counts (the same signal
 * `forgetUnobservedMemberThreads` keys on), so it costs no render and no
 * network. Entries with no observer are panes that have since unmounted or
 * re-pointed to another slot; their slot is not on screen and is left alone.
 */
export function observedPaneSlots(queryClient: QueryClient): string[] {
  const seen = new Set<string>()
  for (const query of queryClient.getQueryCache().findAll({ queryKey: [SLOT_MESSAGES_QUERY_KEY] })) {
    if (query.getObserversCount() === 0) continue
    const slot = query.queryKey[1]
    if (typeof slot === 'string' && slot) seen.add(slot)
  }
  return [...seen]
}
