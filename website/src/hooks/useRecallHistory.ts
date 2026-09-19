import { useMemo, useRef } from 'react'
import { buildRecallHistory, type RecallAttempt, type RecallMessage } from '../lib/recallHistory'

/**
 * `buildRecallHistory` with an array identity stable enough for a composer.
 *
 * ChatInput takes the list as a `useCallback` dependency, so a fresh array on
 * every stream chunk — which is what a live transcript selector yields — would
 * rebuild its key handler mid-turn. Returning the PREVIOUS array while the
 * contents are unchanged keeps that handler stable; re-keying on the slot stops
 * two conversations with a matching length and tail from sharing one. Shared
 * rather than written per composer because the pane and the main chat mount the
 * same ChatInput, so a second spelling would let ↑ recall drift between them.
 */
export function useRecallHistory(
  messages: readonly RecallMessage[],
  slot: string | null | undefined,
  attempted?: readonly RecallAttempt[],
): string[] {
  const lastRef = useRef<string[]>([])
  const slotRef = useRef<string | null>(null)
  return useMemo(() => {
    const key = slot ?? null
    const out = buildRecallHistory(messages, attempted)
    if (slotRef.current !== key) {
      slotRef.current = key
      lastRef.current = out
      return out
    }
    const prev = lastRef.current
    if (prev.length === out.length && prev.every((v, i) => v === out[i])) return prev
    lastRef.current = out
    return out
  }, [messages, slot, attempted])
}
