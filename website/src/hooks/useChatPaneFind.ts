import { useCallback, useLayoutEffect, useMemo, useRef, type MutableRefObject, type RefObject } from 'react'
import { useMessageSearch } from './useMessageSearch'
import { buildMessageToDisplayIdx } from '../pages/chat/groupDisplayItems'
import { useNavToDisplayIndex, type NavToDisplayIndexOptions } from '../pages/chat/useNavToDisplayIndex'
import { scrollToDisplayIndexIn } from '../pages/chat/useScrollManager'
import type { DisplayItem } from '../pages/chat/types'
import type { ChatMessage } from '../types'
import type { VirtualTranscriptHandle } from '../app-sdk/ChatMessageList'
import { pickSearchScrollBehavior } from '../utils/searchScroll'

interface ChatPaneFindOptions {
  /** Whether ⌘F / Ctrl+F opens the find for this pane; false closes it. */
  enabled: boolean
  /** The messages the pane's list draws: match indices point into this array. */
  messages: ChatMessage[]
  slotKey: string
  /** The list's grouped rows, as `onDisplayItems` last reported them. */
  displayItemsRef: MutableRefObject<DisplayItem[]>
  listRef: RefObject<VirtualTranscriptHandle | null>
  scrollerRef: RefObject<HTMLDivElement | null>
}

/**
 * Find in chat for a pane whose transcript is virtualized (a Bot's chat): the
 * same search the Sessions chat runs, and the same jump to the current match's
 * row (`useNavToDisplayIndex`), which mounts a row scrolled out of the window
 * and waits for it to settle. The browser's own find cannot reach such a row,
 * because it is not in the page at all.
 */
export function useChatPaneFind({ enabled, messages, slotKey, displayItemsRef, listRef, scrollerRef }: ChatPaneFindOptions) {
  const search = useMessageSearch(messages, slotKey, enabled)
  const highlight = useMemo(() => ({
    term: search.term,
    caseSensitive: search.caseSensitive,
    currentMessageIdx: search.currentMessageIdx,
    currentOccurrenceIdx: search.currentOccurrenceIdx,
  }), [search.term, search.caseSensitive, search.currentMessageIdx, search.currentOccurrenceIdx])

  const mountIndex = useCallback(
    (index: number, opts?: { unionOnly?: boolean }) => listRef.current?.mountIndex(index, opts) ?? false,
    [listRef],
  )
  const scrollToDisplayIndex = useCallback(
    (index: number, opts?: NavToDisplayIndexOptions) => scrollToDisplayIndexIn(scrollerRef.current, index, opts),
    [scrollerRef],
  )
  const { navToDisplayIndex } = useNavToDisplayIndex({ scrollerRef, mountIndex, scrollToDisplayIndex })

  // Stepping through matches quickly snaps instead of gliding, as in the
  // Sessions chat (`useTranscriptJumps`).
  const lastStepAtRef = useRef(0)
  const lastNavigationRef = useRef<{
    term: string
    messageIdx: number
    matchIdx: number
    matches: typeof search.matches
  } | null>(null)
  useLayoutEffect(() => {
    if (search.currentMessageIdx < 0) { lastNavigationRef.current = null; return }
    const last = lastNavigationRef.current
    if (last?.term === search.term && last.messageIdx === search.currentMessageIdx && last.matchIdx === search.currentIdx) {
      last.matches = search.matches
      return
    }
    // A new term still exposes the old matches until its debounce finishes.
    // Once it lands, jump again even if both indices stayed the same; streamed
    // messages with the same term and indices must not pull the reader back.
    if (last && last.term !== search.term && last.matches === search.matches) return
    const di = buildMessageToDisplayIdx(displayItemsRef.current).get(search.currentMessageIdx)
    if (di === undefined) return
    const sameMatch = last?.messageIdx === search.currentMessageIdx && last.matchIdx === search.currentIdx
    lastNavigationRef.current = { term: search.term, messageIdx: search.currentMessageIdx, matchIdx: search.currentIdx, matches: search.matches }
    // Only the term changed and its row is mounted: the message's own
    // occurrence poll is already centring the new word, as in the Sessions
    // chat, whose jump keys on the indices alone. A row jump would take scroll
    // ownership from that poll and centre the whole row instead.
    if (sameMatch && scrollerRef.current?.querySelector(`[data-display-index="${di}"]`)) return
    const now = performance.now()
    const behavior = pickSearchScrollBehavior(now, lastStepAtRef.current)
    lastStepAtRef.current = now
    navToDisplayIndex(di, { behavior, align: 'center' })
  }, [search.term, search.matches, search.currentMessageIdx, search.currentIdx, displayItemsRef, scrollerRef, navToDisplayIndex])

  return { search, highlight }
}
