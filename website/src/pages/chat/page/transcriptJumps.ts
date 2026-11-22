import { useCallback, useEffect, useLayoutEffect, useRef, type MutableRefObject, type RefObject } from 'react'

import type { useMessageSearch } from '../../../hooks/useMessageSearch'
import { clearMessageJump, usePendingMessageJump } from '../../../lib/pendingMessageJump'
import { store, useAppSelector } from '../../../store'
import type { ChatMessage } from '../../../types'
import { pickSearchScrollBehavior, scrollCurrentMatchIntoView } from '../../../utils/searchScroll'
import { resolveMsgIndex } from '../../../utils/shareUrl'
import type { MarkJump } from './jumpMark'
import { drawnSuccessorIdx } from './transcriptPins'

interface TranscriptJumpsOptions {
  search: ReturnType<typeof useMessageSearch>
  messages: ChatMessage[]
  /** The same list through a ref, which `search`'s indices point into. */
  messagesRef: MutableRefObject<ChatMessage[]>
  /** Changes when the rows on screen do: a target drawn late is looked for again. */
  messageToDisplayIdx: Map<number, number>
  /** True while the rows on screen draw an older list than `messages`: a landed
   *  page waits to render, so a loaded message can have no row yet. */
  rowsTrailList: boolean
  /** Scroll to the row that draws a message; false when no row on screen draws it. */
  navToMessage: (msg: ChatMessage, opts?: { behavior?: ScrollBehavior; align?: ScrollLogicalPosition; offset?: number }) => boolean
  /** The display index of the row on screen that draws a message, or -1. */
  rowOfMessage: (msg: ChatMessage) => number
  /** The transcript scroller, whose mounted rows carry `data-display-index`. */
  scrollerRef: RefObject<HTMLElement>
  activeSlot: string | null
  cursorIsForActiveSlot: boolean
  slotHasMore: boolean
  slotOldestIndex: number
  /** The session controller's mount-time `?msg=` / `?mid=` / `?sid=` capture. */
  initialMsgRef: MutableRefObject<string | null>
  initialMidRef: MutableRefObject<string | null>
  initialSidRef: MutableRefObject<string | null>
  /** The landing highlight of a row this hook was sent to (`landedTs`). */
  markJump: MarkJump
  /** The pins' jump, which pages back for a message not yet loaded. */
  handleJumpToPinnedMessage: (messageTs: string, mid: string | undefined, opts: { origin: 'link' }) => void
}

/**
 * Scroll intent that targets one message: stepping and clicking through search
 * results, the approval bar's "Show in chat", and a `?msg=` deep link on a cold
 * load (which hands a target outside the loaded window to the pins' paging jump).
 */
export function useTranscriptJumps({
  search,
  messages,
  messagesRef,
  messageToDisplayIdx,
  rowsTrailList,
  navToMessage,
  rowOfMessage,
  scrollerRef,
  activeSlot,
  cursorIsForActiveSlot,
  slotHasMore,
  slotOldestIndex,
  initialMsgRef,
  initialMidRef,
  initialSidRef,
  markJump,
  handleJumpToPinnedMessage,
}: TranscriptJumpsOptions) {
  // Track the timestamp of the previous search-nav step so we can tell "user is
  // holding Enter through many matches" apart from "user landed on one match".
  // Rapid consecutive steps snap instantly (behavior:'auto') — a smooth glide
  // would be interrupted and restarted on every keypress, producing the stutter
  // of half-finished eased scrolls. A lone step (or the final one after a pause)
  // glides smoothly and centers. navToDisplayIndex still forces 'auto' for FAR
  // jumps regardless; this only governs NEAR jumps, which is where the queued-
  // animation jank lived.
  const lastSearchStepAtRef = useRef(0)
  // Set when the user clicks a row in the results panel (vs. Enter/Arrow
  // stepping). A click is a direct jump that's usually FAR and to an unmeasured
  // virtualized row — a smooth scroll animates to the *estimated* offset and
  // then visibly corrects once the row mounts. Snapping instantly collapses
  // that into one jump.
  const searchClickJumpRef = useRef(false)
  // Cancel handle for the re-click converge loop (below) so repeated re-clicks
  // of the same result don't stack concurrent loops + window listeners.
  const reclickScrollCancelRef = useRef<(() => void) | null>(null)
  const jumpToSearchResult = useCallback((i: number) => {
    // Re-clicking the already-selected result won't change currentIdx, so the
    // nav effect won't fire — scroll back to it imperatively so a click always
    // returns to the match even after the user has scrolled away from it.
    if (i === search.currentIdx) {
      const m = search.matches[i]
      const target = m ? messagesRef.current[m.msgIdx] : undefined
      if (target && rowOfMessage(target) >= 0) {
        requestAnimationFrame(() => {
          navToMessage(target, { behavior: 'auto', align: 'center' })
          // currentOcc is unchanged so the message's occurrence-scroll effect
          // won't re-run; converge-center the already-rendered active mark.
          reclickScrollCancelRef.current?.()
          reclickScrollCancelRef.current = scrollCurrentMatchIntoView()
        })
      }
      return
    }
    searchClickJumpRef.current = true
    search.goTo(i)
  }, [search, navToMessage, rowOfMessage, messagesRef])
  // The find action this effect last answered, and the message it was for.
  const answeredRevealRef = useRef(0)
  const revealedRowRef = useRef<string | null>(null)
  // A layout effect, as in the Bot chat (`useChatPaneFind`): the row poll it
  // starts claims the scroll before the row's own passive occurrence poll,
  // which then takes it over and centers the occurrence. Started after it, the
  // row poll would take the scroll from that poll and center the whole turn.
  useLayoutEffect(() => {
    // Moves the reader for a find action only, once each: rows arriving
    // renumber the current match without one, and leave the reader in place.
    const reveal = search.reveal
    if (!reveal || reveal === answeredRevealRef.current) return
    // The match's message, from the list the search's indices point into; its
    // row is looked up in the rows on screen, which trail that list while a
    // landed page waits to render.
    const target = search.currentMessageIdx < 0 ? undefined : messagesRef.current[search.currentMessageIdx]
    const di = target ? rowOfMessage(target) : -1
    // The message is loaded and its row is one deferred commit away: the action
    // stays unanswered until the rows catch up (`messageToDisplayIdx`,
    // `rowsTrailList`), and is spent below only if they still draw no row for it.
    if (target && di < 0 && rowsTrailList) return
    answeredRevealRef.current = reveal
    // By the message's key, not its object: the store copies a message when it
    // updates it, and the same message must still count as the same one.
    const stayed = search.currentRowKey !== null && search.currentRowKey === revealedRowRef.current
    revealedRowRef.current = search.currentRowKey
    // Nowhere to move to: the action is spent, so the row cannot use it later,
    // when the reader's own scroll brings it into the window.
    if (!target || di < 0) { search.revealLedger.consume(reveal); return }
    const now = performance.now()
    const behavior = searchClickJumpRef.current
      ? 'auto'
      : pickSearchScrollBehavior(now, lastSearchStepAtRef.current)
    searchClickJumpRef.current = false
    lastSearchStepAtRef.current = now
    // A step inside the message the last action was for, whose row is in the
    // window: the row centers the occurrence itself, and a row poll started
    // here would take the scroll from it and center the whole turn instead.
    if (stayed && scrollerRef.current?.querySelector(`[data-display-index="${di}"]`)) return
    navToMessage(target, { behavior, align: 'center' })
  }, [search.reveal, search.currentMessageIdx, search.currentRowKey, search.revealLedger, navToMessage, rowOfMessage, messagesRef, scrollerRef, messageToDisplayIdx, rowsTrailList])

  // "Show in chat" button on the approval bar dispatches openActivityToTool,
  // which sets `focusToolCallId`. Pulling a virtualised pill back into the DOM
  // requires Virtuoso's own scrollToIndex — direct DOM scrollIntoView fails
  // because the element doesn't exist. ToolCallLine's own effect then takes
  // over once it mounts: refines the scroll position and clears the focus.
  const focusToolCallId = useAppSelector(s => s.chat.focusToolCallId)
  useEffect(() => {
    if (!focusToolCallId) return
    const target = messages.find(m =>
      m.role === 'tool' && m.meta?.tool_call_id === focusToolCallId
    )
    // `messageToDisplayIdx` runs this again once the rows on screen draw it.
    if (target) navToMessage(target, { behavior: 'smooth', align: 'center' })
  }, [focusToolCallId, messages, messageToDisplayIdx, navToMessage])

  // Deep-link: scroll to ?msg= timestamp on cold load.
  // When ?mid= is also present (copied from a pinned-message link), resolve by
  // mid first (stable per-message identity) and fall back to ts for legacy links.
  // The scroll-to-bottom effect above is suppressed while initialMsgRef is set.
  // Safety net: clear both refs after 5s to restore scroll-to-bottom if deep-link fails.
  useEffect(() => {
    if (!initialMsgRef.current) return
    const timer = setTimeout(() => { initialMsgRef.current = null; initialMidRef.current = null }, 5000)
    return () => clearTimeout(timer)
  }, [initialMsgRef, initialMidRef])
  useEffect(() => {
    const targetTs = initialMsgRef.current
    const targetMid = initialMidRef.current
    if (!targetTs || messages.length === 0) return
    // `messages` can still be the chat being left while a ?sid= switch settles,
    // so decide only once this window is known to belong to the target chat.
    if (initialSidRef.current && initialSidRef.current !== activeSlot) return
    if (!cursorIsForActiveSlot) return
    // The captured pair predates the mount effect that dispatches `switchSlot`, whose
    // `pending` nulls the cursor key even on a same-key switch -- so read it live.
    const liveChat = store.getState().chat
    if (liveChat.slotCursorKey !== liveChat.activeSlot) return
    const resolved = resolveMsgIndex(messages, targetTs, targetMid)
    // A mid that is merely OFF-PAGE falls back to ts in the helper, and that is a
    // DIFFERENT row of the same tick -- treat it as unresolved so the hand-off runs.
    const exactIdx = targetMid && messages[resolved]?.meta?.mid !== targetMid ? -1 : resolved
    // A link to a superseded steer lands on the row drawn in its place.
    const msgIdx = exactIdx < 0 ? exactIdx : drawnSuccessorIdx(messages, exactIdx)
    if (msgIdx < 0) {
      // A bounded first page need not contain the target; the jump path already
      // gates on the cursor and reports a dead link, so the decision lives there.
      initialMsgRef.current = null
      // Carries `targetMid`: paging back re-resolves, and ts alone would pick the
      // wrong message of a same-ts pair that the mid exists to disambiguate.
      handleJumpToPinnedMessage(targetTs, targetMid ?? undefined, { origin: 'link' })
      return
    }
    // Its row is looked for in the rows on screen, and the jump aims at the
    // message: a page that lands in the next 500ms renumbers the rows.
    const target = messages[msgIdx]
    if (rowOfMessage(target) < 0) return
    initialMsgRef.current = null
    initialMidRef.current = null
    setTimeout(() => {
      navToMessage(target, { behavior: 'auto', align: 'center' })
      const markedMid = (target.meta as Record<string, unknown> | undefined)?.mid
      markJump(target.ts ?? targetTs, true, typeof markedMid === 'string' ? markedMid : undefined)
    }, 500)
  }, [messages, messageToDisplayIdx, slotHasMore, slotOldestIndex, handleJumpToPinnedMessage, activeSlot, cursorIsForActiveSlot]) // eslint-disable-line react-hooks/exhaustive-deps

  // A jump another surface asked for (Search all chats), taken once its chat is
  // the active one and the loaded window belongs to it. It goes through the pins'
  // jump, which scrolls to a loaded message or pages back to an older one, and
  // reports a message that is gone as the dead link it is.
  const pendingJump = usePendingMessageJump()
  useEffect(() => {
    if (!pendingJump || pendingJump.slotKey !== activeSlot || !cursorIsForActiveSlot) return
    const liveChat = store.getState().chat
    if (liveChat.slotCursorKey !== liveChat.activeSlot) return
    clearMessageJump(pendingJump)
    handleJumpToPinnedMessage(pendingJump.ts, pendingJump.mid, { origin: 'link' })
  }, [pendingJump, activeSlot, cursorIsForActiveSlot, handleJumpToPinnedMessage])
  return { jumpToSearchResult }
}
