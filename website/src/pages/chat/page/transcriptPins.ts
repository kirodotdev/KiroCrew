import { useMemo, useCallback, useState, useRef, useEffect, type MutableRefObject, type RefObject, type Dispatch, type SetStateAction } from 'react'
import type { AppDispatch } from '../../../store'
import { loadOlderMessages, isSupersededPagingRejection, openActivityPanel } from '../../../store/chatSlice'
import type { MessageQuote } from '../../../chat-core/composer/messageQuote'
import type { DisplayItem } from '../types'
import type { useChatPageResourcesController } from '../useChatPageResourcesController'
import { inspectorOn, devLog } from '../../../dev/scrollInspector'
import { useTurnStepper } from '../useTurnStepper'
import { openPanelView } from '../../../hooks/usePanelTabs'
import { useChatNavigation } from '../../../hooks/useChatNavigation'
import { useChatPins } from '../../../hooks/useChatPins'
import type { ChatMessage } from '../../../types'
import { i18nT } from '../../../i18n/t'

/** Map message index → displayItems index, for scroll-to-match and the turn minimap. */
function buildMessageToDisplayIdx(items: DisplayItem[]): Map<number, number> {
  const map = new Map<number, number>()
  items.forEach((item, di) => {
    if (item.kind === 'turn') {
      for (const ti of item.items) {
        if (ti.kind === 'single') map.set(ti.idx, di)
        else if (ti.kind === 'group') ti.msgs.forEach((_, mi) => map.set(ti.startIdx + mi, di))
      }
    } else if (item.kind === 'single') map.set(item.idx, di)
    else if (item.kind === 'group') item.msgs.forEach((_, mi) => map.set(item.startIdx + mi, di))
  })
  return map
}

/**
 * Where a jump-to-message came from, because the three entry points owe the
 * reader different copy when the target cannot be found.
 *
 *  - `pin`     the pins list, so pin wording is accurate;
 *  - `earlier` the earlier-messages control, which has its own paging copy;
 *  - `link`    a `?msg=` share link, minted by copy-link-to-message for ANY
 *              message. That reader may never have pinned anything, so naming a
 *              pin would report an action they did not take.
 */
type PendingJumpOrigin = 'pin' | 'earlier' | 'link' | 'quote'

/** SINGLE writer for the not-found copy, so a new origin cannot reach the reader
 *  wearing another origin's wording. */
const jumpUnavailableNotice = (origin: PendingJumpOrigin): string =>
  origin === 'earlier' ? i18nT('components.chatPane.earlier_messages_unavailable')
    : origin === 'link' ? i18nT('pages.chat.deepLink.message_unavailable')
      : origin === 'quote' ? i18nT('pages.chat.quoteCard.message_unavailable')
        : i18nT('pages.chat.pins.message_unavailable')

interface TranscriptPinsOptions {
  embedded?: boolean
  dispatch: AppDispatch
  isMobile: boolean
  activeSlot: string | null
  messages: ChatMessage[]
  messagesRef: MutableRefObject<ChatMessage[]>
  slotRunning: boolean
  splitMode: boolean
  crewWindow: { instanceId: string; key: string; } | null
  slotHasMore: boolean
  slotOldestIndex: number
  cursorIsForActiveSlot: boolean
  loadingOlder: boolean
  activeViewIsBoundedPage: boolean
  isWelcomeState: boolean
  activeSlotRemoteBound: boolean
  scrollerRef: RefObject<HTMLDivElement>
  search: ReturnType<typeof useChatPageResourcesController>['search']
  navToDisplayIndex: (idx: number, opts?: { behavior?: ScrollBehavior; align?: ScrollLogicalPosition; offset?: number; }) => void
  pinnedJumpChrome: () => number
  scrollToPinnedPrompt: (target: number) => void
  setHighlightTs: Dispatch<SetStateAction<string | null>>
  splitFeatureEnabled: boolean
  regenerating: boolean
  setEditLast: Dispatch<SetStateAction<{ seq: number; ts: string; index: number; } | null>>
  editSeqRef: MutableRefObject<number>
  renderedDisplayItems: DisplayItem[]
  renderedTranscript: { messages: ChatMessage[]; displayItems: DisplayItem[]; }
}

/**
 * Navigating the transcript: turn navigation and the Alt+Up/Down stepper, pinned
 * messages (pin, unpin, notices), and the jumps to a pinned, quoted or last-edited
 * message, including paging back until the target loads.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useTranscriptPins({
  embedded, dispatch, isMobile, activeSlot, messages, messagesRef, slotRunning, splitMode, crewWindow, slotHasMore, slotOldestIndex, cursorIsForActiveSlot, loadingOlder, activeViewIsBoundedPage,
  isWelcomeState, activeSlotRemoteBound, scrollerRef, search, navToDisplayIndex, pinnedJumpChrome, scrollToPinnedPrompt, setHighlightTs, splitFeatureEnabled, regenerating, setEditLast, editSeqRef,
  renderedDisplayItems, renderedTranscript,
}: TranscriptPinsOptions) {
  // Search, pins, tool focus, and deep links navigate the rows the virtualizer
  // actually renders, including post-defer MCP App coalescing.
  const messageToDisplayIdx = useMemo(
    () => buildMessageToDisplayIdx(renderedDisplayItems),
    [renderedDisplayItems],
  )

  const navigateToTurn = useCallback((displayIndex: number, opts?: { instant?: boolean }) => {
    // instant: used by the minimap's drag-scrub — a smooth glide would lag the
    // pointer and queue easings on every marker crossing.
    navToDisplayIndex(displayIndex, { behavior: opts?.instant ? 'auto' : 'smooth', align: 'start', offset: -24 })
  }, [navToDisplayIndex])

  // The transcript renders the deferred `renderedTranscript` snapshot; while a
  // history page lands, live indexes lead the rows on screen. The minimap's
  // messages and `messageToDisplayIdx` map come from that same rendered frame,
  // so a marker's display index always names the virtualizer row on screen.
  // One per-turn derivation: `sections` feeds the turn minimap; `links` feeds the
  // Navigation tab. Both read the deferred snapshot, so the link list trails a
  // landing history page by one deferred commit -- deliberate, and harmless for
  // a side panel.
  const chatNav = useChatNavigation(renderedTranscript.messages, messageToDisplayIdx)
  // Alt+↑ / Alt+↓ step through the same turns by way of the pinned-prompt jump.
  // Single chat only: split view draws its sessions as panes, a crew window
  // covers this transcript, and an embedded chat is not the page the keyboard
  // drives.
  const turnStepAnnouncement = useTurnStepper({
    active: !embedded && !(splitMode && splitFeatureEnabled) && !crewWindow && !isWelcomeState && !!activeSlot,
    turns: chatNav.sections,
    windowed: slotHasMore && cursorIsForActiveSlot,
    scrollerRef,
    landingInset: pinnedJumpChrome,
    jump: scrollToPinnedPrompt,
    setHighlightTs,
  })

  // ── Chat Pins ──────────────────────────────────────────────────────────────
  const {
    pins: chatPins,
    loading: chatPinsLoading,
    error: chatPinsError,
    clearError: clearChatPinsError,
    isPinned,
    pinMessage,
    unpinMessage,
    unpinById,
  } = useChatPins(activeSlot ?? undefined)
  const [pinNotice, setPinNotice] = useState<string | null>(null)
  // A pinned-message page load that was REFUSED — kept apart from `pinNotice`
  // (which carries the not-found / unavailable answers) so it renders as an
  // error rather than as status text.
  const [pinLoadError, setPinLoadError] = useState<string | null>(null)
  const [pendingPinnedJump, setPendingPinnedJump] = useState<{
    slotKey: string
    messageTs: string
    mid?: string
    // Required, not optional: the entry points render different copy, and a new
    // caller that omitted it would silently show pin wording.
    origin: PendingJumpOrigin
  } | null>(null)
  const pinnedJumpPageLoadsRef = useRef(0)
  // Reads `messagesRef`, not `messages`: the array is new on every streamed
  // frame, and this resolver sits under `handleJumpToQuote`, a transcript
  // renderers memo dep, so a `messages` dep re-rendered every mounted row on
  // every frame. The pending-jump effect below lists `messages` itself, so it
  // still re-resolves when paged rows land.
  const jumpToLoadedPinnedMessage = useCallback((messageTs: string, mid?: string): boolean => {
    const messages = messagesRef.current
    // Mid-based resolution when a mid is known; ts ONLY for legacy pins that carry none.
    // Falling through to ts with a mid in hand takes a same-tick twin, which is the wrong row.
    const msgIdx = mid
      ? messages.findIndex(m => (m.meta as Record<string, unknown> | undefined)?.mid === mid)
      : messages.findIndex(m => m.ts === messageTs)
    if (msgIdx < 0) return false
    const di = messageToDisplayIdxRef.current.get(msgIdx)
    if (di === undefined) return false
    setPinNotice(null)
    setPinLoadError(null)
    navToDisplayIndex(di, { behavior: 'smooth', align: 'center' })
    setHighlightTs(messageTs)
    setTimeout(() => setHighlightTs(null), 3000)
    return true
  }, [messagesRef, navToDisplayIndex, setHighlightTs])
  // ⌘↑ / Ctrl+Up target resolution, placed next to the virtualizer seam it
  // depends on: the transcript is virtualized, so the newest user message can
  // sit OUTSIDE the mounted window (a long agent reply just streamed past it)
  // and no `UserMessage` row would exist to see the rising edge. Scroll the
  // target into view FIRST and raise the request in the same pass — the row
  // then receives the request at mount and acknowledges it on consumption.
  const handleEditLastRequest = useCallback(() => {
    if (!activeSlot || slotRunning || regenerating || activeSlotRemoteBound) return
    const msgs = messagesRef.current
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role !== 'user') continue
      // A steer taken back draws no bubble, so it has nothing to edit.
      if (msgs[i].meta?.steerState === 'retracted') continue
      const di = messageToDisplayIdxRef.current.get(i)
      if (di === undefined) return
      navToDisplayIndex(di, { behavior: 'auto', align: 'center' })
      setEditLast({ seq: ++editSeqRef.current, ts: msgs[i].ts || '', index: i })
      return
    }
  }, [activeSlot, slotRunning, regenerating, activeSlotRemoteBound, navToDisplayIndex, editSeqRef, messagesRef, setEditLast])
  const handleJumpToPinnedMessage = useCallback((messageTs: string, mid: string | undefined, { origin }: { origin: PendingJumpOrigin }) => {
    if (jumpToLoadedPinnedMessage(messageTs, mid)) return
    if (activeSlot && (!cursorIsForActiveSlot || (slotHasMore && slotOldestIndex > 0))) {
      pinnedJumpPageLoadsRef.current = 0
      setPinNotice(null)
      setPinLoadError(null)
      setPendingPinnedJump({ slotKey: activeSlot, messageTs, mid, origin })
      return
    }
    // Same writer as the async branch below, so the synchronous dead-link case
    // cannot drift into pin wording while the paging case reports the truth.
    setPinNotice(jumpUnavailableNotice(origin))
  }, [activeSlot, cursorIsForActiveSlot, jumpToLoadedPinnedMessage, slotHasMore, slotOldestIndex])
  // A sent quote card jumps to the quoted row through the pinned-message jump,
  // which already pages older history in when the target is off the loaded window.
  const handleJumpToQuote = useCallback((q: MessageQuote) => {
    if (!q.ts) return
    handleJumpToPinnedMessage(q.ts, q.mid, { origin: 'quote' })
  }, [handleJumpToPinnedMessage])
  // The pins list's own entry point, so pin copy is claimed HERE by a caller that
  // means it rather than inherited by one that passed nothing.
  const handleJumpToPin = useCallback((messageTs: string, mid?: string) => {
    handleJumpToPinnedMessage(messageTs, mid, { origin: 'pin' })
  }, [handleJumpToPinnedMessage])
  useEffect(() => {
    if (!pendingPinnedJump) return
    if (pendingPinnedJump.slotKey !== activeSlot) {
      pinnedJumpPageLoadsRef.current = 0
      setPendingPinnedJump(null)
      return
    }
    // Captured per effect run so the async branches below report the entry point
    // this jump came from, not whichever one ran last.
    const notFoundNotice = jumpUnavailableNotice(pendingPinnedJump.origin)
    // A fetch that errored is transient, so the not-found copy would tell the reader
    // their history is gone. `link` shares the retry copy: it makes no origin claim.
    const loadFailedNotice = pendingPinnedJump.origin === 'earlier' || pendingPinnedJump.origin === 'link'
      ? i18nT('components.chatPane.earlier_messages_load_failed')
      : notFoundNotice
    if (jumpToLoadedPinnedMessage(pendingPinnedJump.messageTs, pendingPinnedJump.mid)) {
      pinnedJumpPageLoadsRef.current = 0
      // A jump resolved against the bounded page is provisional: the full
      // transcript prepends older rows, so re-resolve once it has replaced it.
      if (!activeViewIsBoundedPage) setPendingPinnedJump(null)
      return
    }
    // The cursor still describes the chat we left; wait for the switch to settle
    // rather than read its has-more as this chat's.
    if (!cursorIsForActiveSlot) return
    if (!slotHasMore || slotOldestIndex <= 0) {
      pinnedJumpPageLoadsRef.current = 0
      setPinNotice(notFoundNotice)
      setPendingPinnedJump(null)
      return
    }
    if (loadingOlder) return

    // Counted for diagnostics only, deliberately NOT compared against a ceiling:
    // an arbitrary page cap is what made a distant pin in a resumed session report
    // itself "unavailable" when it simply needed more pages, and it was removed for
    // that reason (chatPins.test.ts, 'no arbitrary page-load cap'). The loop is
    // bounded by the history itself -- the target is found, `slotHasMore` goes
    // false, or `slotOldestIndex` reaches 0 -- and it is walking history the reader
    // ASKED for by tapping the pin, which is not the unasked loading this branch is
    // about. The real fix for a very distant pin is a server query that fetches
    // AROUND a message id; until that exists, paging is the honest behaviour.
    pinnedJumpPageLoadsRef.current += 1
    if (inspectorOn()) devLog('OLDER', `jump p${pinnedJumpPageLoadsRef.current}`)
    let cancelled = false
    void dispatch(loadOlderMessages()).unwrap().then(result => {
      if (!cancelled && result === null) {
        pinnedJumpPageLoadsRef.current = 0
        setPinNotice(notFoundNotice)
        setPendingPinnedJump(null)
      }
    }).catch(err => {
      // Cancelled or refused means the user switched chat, not that the pin is
      // unreachable.
      if (isSupersededPagingRejection(err)) return
      if (!cancelled) {
        pinnedJumpPageLoadsRef.current = 0
        setPinLoadError(loadFailedNotice)
        setPendingPinnedJump(null)
      }
    })
    return () => { cancelled = true }
  }, [
    activeSlot,
    activeViewIsBoundedPage,
    cursorIsForActiveSlot,
    dispatch,
    jumpToLoadedPinnedMessage,
    loadingOlder,
    // The resolver reads the rows through `messagesRef`, so a page of older
    // rows landing must re-run this effect on its own.
    messages,
    pendingPinnedJump,
    slotHasMore,
    slotOldestIndex,
  ])
  const handleTogglePinForMessage = useCallback((mid: string, messageTs: string, role: 'user' | 'assistant', content: string) => {
    if (isPinned(mid)) {
      void unpinMessage(mid).catch(() => {}) // useChatPins exposes the localized error state.
      return
    }
    // A session's FIRST pin opens the Pins tab, so the pin has a visible
    // destination -- the same shape as the Issues reveal, and for the same
    // reason: Pins is an on-demand view, so nothing would surface it otherwise.
    // A session pinned earlier reaches it through the + menu (Issues' zero
    // option for pre-existing links), which is what keeps this free of a
    // persisted reveal claim.
    // Read before the mutation so the optimistic insert has not landed yet.
    const isFirstPin = chatPins.length === 0
    void pinMessage({ mid, message_ts: messageTs, role, preview: content }).catch(() => {})
    if (isFirstPin && activeSlot) {
      // Addressed by slot, not through tabsCtl, for the same reason as the
      // source-reveal path: that binding can be a chat being left.
      openPanelView(activeSlot, 'pins')
      // Pinning is NOT a navigation request, so it must not cost the user state
      // they are mid-way through. Unlike the source-reveal path this does not
      // close the find pane: someone who searched the transcript to FIND the
      // message they are pinning would lose the pane and its results on the very
      // click that acts on a result. Below the mobile breakpoint the panel opens
      // full width, so opening it would navigate them off the chat entirely.
      // The tab is still created above -- it is revealed quietly instead.
      if (!search.isOpen && !isMobile) dispatch(openActivityPanel())
    }
  }, [activeSlot, chatPins.length, dispatch, isMobile, isPinned, pinMessage, search.isOpen, unpinMessage])
  const handleUnpinById = useCallback((id: string) => {
    void unpinById(id).catch(() => {})
  }, [unpinById])
  // Split by kind. `pinError` is something that FAILED — a refused page load,
  // a pin/unpin request the server rejected (useChatPins keeps that state, so
  // the `.catch(() => {})` at the call sites is not a swallow). `pinStatus` is
  // an answer — the message is not in this history, the pin limit is reached.
  const pinError = pinLoadError ?? (chatPinsError && chatPinsError !== 'pin_limit'
    ? i18nT(chatPinsError === 'pin' ? 'pages.chat.pins.pin_failed' : 'pages.chat.pins.unpin_failed')
    : null)
  const pinStatus = pinNotice ?? (chatPinsError === 'pin_limit' ? i18nT('pages.chat.pins.pin_limit_reached') : null)
  const dismissPinStatus = useCallback(() => {
    setPinNotice(null)
    setPinLoadError(null)
    clearChatPinsError()
  }, [clearChatPinsError])
  // Only the answer times out. A failure stays until the user dismisses it or
  // hands it to the agent — an error that vanishes after eight seconds is the
  // toast shape this sweep is removing.
  useEffect(() => {
    if (!pinStatus) return
    const timeout = window.setTimeout(() => {
      setPinNotice(null)
      if (chatPinsError === 'pin_limit') clearChatPinsError()
    }, 8000)
    return () => window.clearTimeout(timeout)
  }, [pinStatus, chatPinsError, clearChatPinsError])

  // Read the display-index map via a ref so the search scroll effect
  // (useTranscriptJumps, called later from ChatPage) does NOT
  // re-fire when the map is rebuilt (every new message / stream chunk rebuilds
  // it). Otherwise an open search pane would yank the chat back to the current
  // match each time the agent emits output. The effect should scroll only on
  // deliberate search navigation (currentIdx / currentMessageIdx change).
  const messageToDisplayIdxRef = useRef(messageToDisplayIdx)
  messageToDisplayIdxRef.current = messageToDisplayIdx
  return {
    messageToDisplayIdx, navigateToTurn, chatNav, turnStepAnnouncement, chatPins, chatPinsLoading, isPinned, setPendingPinnedJump, handleEditLastRequest, handleJumpToPinnedMessage, handleJumpToQuote,
    handleJumpToPin, handleTogglePinForMessage, handleUnpinById, pinError, pinStatus, dismissPinStatus, messageToDisplayIdxRef,
  }
}
