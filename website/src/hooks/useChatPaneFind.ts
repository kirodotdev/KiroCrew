import { useCallback, useLayoutEffect, useMemo, useRef, type MutableRefObject, type RefObject } from 'react'
import { useMessageSearch } from './useMessageSearch'
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
 *
 * The reader moves once per find action (the search's `reveal`), as in the
 * Sessions chat (`useTranscriptJumps`): rows arriving, which renumber the
 * current match, and streamed text move nobody.
 */
export function useChatPaneFind({ enabled, messages, slotKey, displayItemsRef, listRef, scrollerRef }: ChatPaneFindOptions) {
  const search = useMessageSearch(messages, slotKey, enabled)
  const { term, caseSensitive, currentMessageIdx, currentOccurrenceIdx, currentRowKey, reveal, revealLedger } = search
  const highlight = useMemo(
    () => ({ term, caseSensitive, currentMessageIdx, currentOccurrenceIdx, reveal, revealLedger }),
    [term, caseSensitive, currentMessageIdx, currentOccurrenceIdx, reveal, revealLedger],
  )

  const mountIndex = useCallback(
    (index: number, opts?: { unionOnly?: boolean }) => listRef.current?.mountIndex(index, opts) ?? false,
    [listRef],
  )
  const scrollToDisplayIndex = useCallback(
    (index: number, opts?: NavToDisplayIndexOptions) => scrollToDisplayIndexIn(scrollerRef.current, index, opts),
    [scrollerRef],
  )
  const { navToMessage, rowOfMessage } = useNavToDisplayIndex({ scrollerRef, displayItemsRef, mountIndex, scrollToDisplayIndex })

  // Stepping through matches quickly snaps instead of gliding, as in the
  // Sessions chat (`useTranscriptJumps`).
  const lastStepAtRef = useRef(0)
  // The find action this effect last answered, and the message it was for.
  const answeredRevealRef = useRef(0)
  const revealedRowRef = useRef<string | null>(null)
  // A layout effect, so the row poll it starts claims the scroll before the
  // message's own passive occurrence poll, which then takes it over.
  useLayoutEffect(() => {
    if (!reveal || reveal === answeredRevealRef.current) return
    answeredRevealRef.current = reveal
    // By the message's key, not its object: the store copies a message when it
    // updates it, and the same message must still count as the same one.
    const stayed = currentRowKey !== null && currentRowKey === revealedRowRef.current
    revealedRowRef.current = currentRowKey
    const target = currentMessageIdx < 0 ? undefined : messages[currentMessageIdx]
    const di = target ? rowOfMessage(target) : -1
    // Nowhere to move to: the action is spent, so the row cannot use it later,
    // when the reader's own scroll brings it into the window.
    if (!target || di < 0) { revealLedger.consume(reveal); return }
    // A step inside the message the last action was for, whose row is in the
    // window: the message's own occurrence poll is already centering it, and a
    // row jump would take the scroll from that poll and center the whole row.
    if (stayed && scrollerRef.current?.querySelector(`[data-display-index="${di}"]`)) return
    const now = performance.now()
    const behavior = pickSearchScrollBehavior(now, lastStepAtRef.current)
    lastStepAtRef.current = now
    navToMessage(target, { behavior, align: 'center' })
  }, [reveal, currentMessageIdx, currentRowKey, revealLedger, messages, rowOfMessage, navToMessage, scrollerRef])

  return { search, highlight }
}
