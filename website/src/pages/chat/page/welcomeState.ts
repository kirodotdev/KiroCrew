import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject } from 'react'

import type { pendingQuestionFor } from '../../../store/chatSlice'
import type { ChatMessage, SessionInfo } from '../../../types'
import type { useKnowledgeFetch } from '../useKnowledgeFetch'

interface WelcomeStateOptions {
  messages: ChatMessage[]
  slotRunning: boolean
  slotLoading: boolean
  /** True while send() is creating the session it will send into. */
  sendingRef: MutableRefObject<boolean>
  knowledgeFetch: ReturnType<typeof useKnowledgeFetch>
  pendingQuestion: ReturnType<typeof pendingQuestionFor>
  history: SessionInfo[]
}

/**
 * The empty-transcript welcome screen: whether it shows, and the "Continue a
 * previous chat?" suggestions above its composer, matched against what the
 * user types (debounced) and dismissed with Escape.
 */
export function useWelcomeState({ messages, slotRunning, slotLoading, sendingRef, knowledgeFetch, pendingQuestion, history }: WelcomeStateOptions) {
  const [historyQuery, setHistoryQuery] = useState('')
  const [historyDismissed, setHistoryDismissed] = useState(false)
  // Fed from the composer-draft commit below (`onComposerDraftCommit`), once per
  // committed text change. The refs keep a keystroke from calling a setter with
  // the value it already holds, which is what keeps typing off this page.
  const historyQueryRef = useRef(historyQuery); historyQueryRef.current = historyQuery
  const historyDismissedRef = useRef(historyDismissed); historyDismissedRef.current = historyDismissed
  const historyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const updateHistoryQuery = useCallback((text: string) => {
    if (historyTimerRef.current) { clearTimeout(historyTimerRef.current); historyTimerRef.current = null }
    if (historyDismissedRef.current) setHistoryDismissed(false)
    const q = text.trim()
    if (!q) { if (historyQueryRef.current) setHistoryQuery(''); return }
    const next = q.toLowerCase()
    historyTimerRef.current = setTimeout(() => {
      historyTimerRef.current = null
      if (historyQueryRef.current !== next) setHistoryQuery(next)
    }, 300)
  }, [])
  useEffect(() => () => { if (historyTimerRef.current) clearTimeout(historyTimerRef.current) }, [])
  const historySuggestions = useMemo(() =>
    historyQuery && history.length
      ? history.filter(s => (s.title || '').toLowerCase().includes(historyQuery) || s.key.toLowerCase().includes(historyQuery)).slice(0, 5)
      : [],
    [historyQuery, history])
  /* `!pendingQuestion`: the welcome hero is vertically centred in the empty
     transcript, which is the same space the question card occupies above the
     composer -- with both mounted they visibly overlap. An agent that asks
     before producing any output is a real case (it happens on the very first
     turn), so the card wins and the welcome content stands down. */
  const isWelcomeState = messages.length === 0 && !slotRunning && !slotLoading && !sendingRef.current && !knowledgeFetch.results.length && !knowledgeFetch.loading && !knowledgeFetch.pendingKnowledge && !pendingQuestion
  const showHistorySuggestions = isWelcomeState && historySuggestions.length > 0 && !historyDismissed
  useEffect(() => {
    if (!showHistorySuggestions) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setHistoryDismissed(true) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [showHistorySuggestions])
  return { updateHistoryQuery, historySuggestions, isWelcomeState, showHistorySuggestions }
}
