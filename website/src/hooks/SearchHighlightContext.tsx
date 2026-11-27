import { createContext, useCallback, useContext, useMemo, type ReactNode } from 'react'
import { PierreFarmHoldContext } from '../components/pierreStaging'
import { scrollCurrentMatchIntoView } from '../utils/searchScroll'

// ── Outer context: search term + current match ──

/**
 * Which find actions have already moved the reader. One per search, shared by
 * every row, so a row that mounts again later (the reader scrolled away and
 * back) never moves the reader for an action that has already been used.
 */
export interface RevealLedger {
  /** True while find action `reveal` has not been used to move the reader. */
  pending: (reveal: number) => boolean
  /** Find action `reveal` is used: nothing moves the reader for it again. */
  consume: (reveal: number) => void
}

interface SearchHighlightValue {
  term: string
  caseSensitive: boolean
  currentMessageIdx: number
  currentOccurrenceIdx: number
  /** The find action that asked to move the reader to the current match
   *  (`useMessageSearch`'s `reveal`); 0 or absent when none did. */
  reveal?: number
  /** Absent: no row ever moves the reader. */
  revealLedger?: RevealLedger
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
 *
 * Always -1 inside the measuring farm (MeasureFarm): its hidden copy of a row
 * exists only to be measured, and the page keeps a single current match range
 * (`domHighlight`), which the reader's own row must keep while a copy of it
 * comes and goes.
 */
export function useCurrentOcc(): number {
  const { currentMessageIdx, currentOccurrenceIdx } = useSearchHighlight()
  const { messageIdx } = useMessageIdx()
  const farmCopy = useContext(PierreFarmHoldContext)
  return !farmCopy && messageIdx >= 0 && messageIdx === currentMessageIdx ? currentOccurrenceIdx : -1
}

/**
 * Centers this row's current occurrence for the find action that asked for it,
 * once: `revealMatch(el)` starts the converging scroll when this row holds the
 * current match and that action has not been used yet, and returns its cancel.
 * The action is used when that scroll is done (`onSpent`), so a row that
 * unmounts or re-runs its effect before then finishes the centering on its next
 * run, within the action's backstop.
 * A row that is merely current (rows arrived, the row mounted again as the
 * reader scrolled past it, its text streamed) leaves the reader where they are.
 * Re-create the effect with `revealMatch` in its deps: it changes with each
 * find action for this row, and only for the row that holds the current match,
 * so the other rows' effects do not run again for it.
 */
export function useRevealMatch(): (el: HTMLElement) => (() => void) | undefined {
  const { reveal = 0, revealLedger } = useSearchHighlight()
  // A farm copy is never current (useCurrentOcc), so it never centers either.
  const rowReveal = useCurrentOcc() >= 0 ? reveal : 0
  return useCallback((el: HTMLElement) => {
    if (!revealLedger?.pending(rowReveal)) return undefined
    return scrollCurrentMatchIntoView(el, { onSpent: () => revealLedger.consume(rowReveal) })
  }, [rowReveal, revealLedger])
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
