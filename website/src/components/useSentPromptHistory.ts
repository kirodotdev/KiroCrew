import { useMemo, useRef } from 'react'
import { useAppSelector } from '../store'
import { selectSendAttempts } from '../store/chatSlice'
import type { ChatMessage } from '../types'
import { type PromptHistoryItem, promptHistoryWithAttempts, samePromptHistory } from './composerPromptHistory'

/**
 * The ↑/↓ prompt-history list for one slot's composer: the transcript's user
 * prompts, then any prompt this tab submitted there that no row carries.
 *
 * `messages` gets a new reference on every streaming chunk, so the previous
 * array is kept while the list is unchanged; `sentMessages` then stays
 * referentially stable and does not re-run downstream effects. The cache is
 * reset on a slot change, or two conversations with the same prompts would
 * share one array.
 */
export function useSentPromptHistory(messages: readonly ChatMessage[], slot: string | null | undefined): PromptHistoryItem[] {
  const attempts = useAppSelector(s => selectSendAttempts(s, slot))
  const cacheRef = useRef<PromptHistoryItem[]>([])
  const cacheSlotRef = useRef<string | null>(null)
  return useMemo(() => {
    const out = promptHistoryWithAttempts(messages, attempts)
    if (cacheSlotRef.current !== (slot ?? null)) {
      cacheSlotRef.current = slot ?? null
      cacheRef.current = out
      return out
    }
    if (samePromptHistory(cacheRef.current, out)) return cacheRef.current
    cacheRef.current = out
    return out
  }, [messages, attempts, slot])
}
