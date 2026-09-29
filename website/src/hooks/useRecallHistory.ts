import { useMemo, useRef } from 'react'
import { buildRecallEntries, type RecallAttempt, type RecallEntry, type RecallMessage } from '../lib/recallHistory'

/**
 * `buildRecallEntries` with array identities stable enough for a composer.
 *
 * ChatInput takes the text list as a `useCallback` dependency, so a fresh array
 * on every stream chunk — which is what a live transcript selector yields —
 * would rebuild its key handler mid-turn. Returning the PREVIOUS arrays while
 * the contents are unchanged keeps that handler stable; re-keying on the slot
 * stops two conversations with a matching length and tail from sharing one.
 * Shared rather than written per composer because the pane and the main chat
 * mount the same ChatInput, so a second spelling would let ↑ recall drift
 * between them.
 *
 * `entries` is what lets a caller restore a recalled prompt's sidecars by the
 * attempt itself rather than by matching its text.
 */
export function useRecallHistory(
  messages: readonly RecallMessage[],
  slot: string | null | undefined,
  attempted?: readonly RecallAttempt[],
): { history: string[]; entries: RecallEntry[] } {
  const lastRef = useRef<RecallEntry[]>([])
  const slotRef = useRef<string | null>(null)
  const entries = useMemo(() => {
    const key = slot ?? null
    const out = buildRecallEntries(messages, attempted)
    if (slotRef.current !== key) {
      slotRef.current = key
      lastRef.current = out
      return out
    }
    const prev = lastRef.current
    const same = prev.length === out.length
      && prev.every((e, i) => e.text === out[i].text && e.attempt?.sendId === out[i].attempt?.sendId)
    if (same) return prev
    lastRef.current = out
    return out
  }, [messages, slot, attempted])
  const history = useMemo(() => entries.map((e) => e.text), [entries])
  return { history, entries }
}
