import { createContext, useContext, useMemo, type ReactNode } from 'react'

// ── Outer context: search term + current match ──

interface SearchHighlightValue {
  term: string
  caseSensitive: boolean
  currentMessageIdx: number
  currentOccurrenceIdx: number
}

const SearchHighlightContext = createContext<SearchHighlightValue>({
  term: '',
  caseSensitive: false,
  currentMessageIdx: -1,
  currentOccurrenceIdx: -1,
})

export const useSearchHighlight = () => useContext(SearchHighlightContext)
export default SearchHighlightContext

// ── Inner context: per-message scope ──

interface MessageIdxValue {
  messageIdx: number
}

const MessageIdxContext = createContext<MessageIdxValue>({
  messageIdx: -1,
})

export const useMessageIdx = () => useContext(MessageIdxContext)

/**
 * Derive the current occurrence index for the message this component lives in.
 * Returns the 0-based occurrence index if this message is the current match,
 * or -1 if it isn't. Used by both HighlightedText (React) and
 * AssistantMessage (TreeWalker) to avoid duplicating the derivation logic.
 */
export function useCurrentOcc(): number {
  const { currentMessageIdx, currentOccurrenceIdx } = useSearchHighlight()
  const { messageIdx } = useMessageIdx()
  return messageIdx >= 0 && messageIdx === currentMessageIdx ? currentOccurrenceIdx : -1
}

/**
 * Thin per-message context provider. Exists solely to make `messageIdx`
 * available to deeply nested descendants (e.g. `HighlightedText` inside
 * `renderUserContent`) without threading it as a prop through intermediate
 * functions and components that don't otherwise need it.
 */
export function MessageSearchScope({ messageIdx, children }: { messageIdx: number; children: ReactNode }) {
  const value = useMemo(() => ({ messageIdx }), [messageIdx])
  return <MessageIdxContext.Provider value={value}>{children}</MessageIdxContext.Provider>
}
