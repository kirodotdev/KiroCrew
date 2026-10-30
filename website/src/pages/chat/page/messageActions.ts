import type { NavigateFunction } from 'react-router-dom'
import { useCallback, useState, useRef, useEffect, useMemo, type RefObject, type MutableRefObject, type Dispatch, type SetStateAction } from 'react'
import type { UseMessageQuote } from '../../../chat-core/composer/useMessageQuote'
import { useAppSelector, store, type AppDispatch } from '../../../store'
import type { ChatMessage, ChatSlot } from '../../../types'
import { useWindowWidth } from '../../../hooks/useWindowWidth'
import { useRailWidth } from '../../../hooks/useRailWidth'
import { openActivityToTab, truncateAfterIndex, appendMessage, replaceMessages, toggleActivity, setVoiceAudio } from '../../../store/chatSlice'
import { useSelectionQuoteAsk } from '../../../chat-core/composer/selectionActions'
import { api, type PlanStepInput } from '../../../api/client'
import { safeSetItem } from '../../../utils/safeStorage'
import { renderUserContent } from '../ChatPageMessageContent'
import type { useChatPageResourcesController } from '../useChatPageResourcesController'
import { useComposerDockMetrics } from '../composerDockMetrics'
import { useSelectionInertOverlays } from '../useSelectionInertOverlays'
import { useStreamIdle } from '../ChatFooter'
import { clampSidebarWidth } from '../sidebarWidth'
import { revealComposer } from '../composerFocus'
import { rewindWithRollback } from '../../../lib/rewindCall'
import { i18nT } from '../../../i18n/t'

interface PageSelectionActionsOptions {
  dispatch: AppDispatch
  activeSlot: string | null
  setInput: (next: string | ((prev: string) => string)) => void
  messageQuote: UseMessageQuote
  scrollerRef: RefObject<HTMLDivElement>
}

/**
 * The composer dock's measured clearance, the overlays a transcript selection treats
 * as inert, and Quote / Ask on selected assistant text (Ask opens the Side Chat).
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function usePageSelectionActions({
  dispatch, activeSlot, setInput, messageQuote, scrollerRef,
}: PageSelectionActionsOptions) {
  // The floating composer dock's measured clearance (see composerDock).
  const { inputAreaRef, dockH, dockGutter, dockRef } = useComposerDockMetrics(scrollerRef)
  useSelectionInertOverlays(scrollerRef)

  // Quote / Ask on selected assistant text — the shared chat-core seam
  // (chat-core/composer/selectionActions): Quote lands in this composer with
  // the transit animation, Ask seeds the Side Chat. This page's Side Chat
  // surface is the activity panel's `side` tab; the /side slash command opens
  // it through the same `openActivityToTab('side')` bridge.
  const openSideChat = useCallback(() => { dispatch(openActivityToTab('side')) }, [dispatch])
  // Whole-message quote: stage the row (`useMessageQuote`). The sent card's
  // jump (`handleJumpToQuote`) is defined beside the pinned-message jump it rides.
  const quoteWholeMessage = messageQuote.quoteMessage
  const { onQuote: handleQuote, onAsk: handleAsk, quoteFlight: flyingQuote, endQuoteFlight } = useSelectionQuoteAsk({
    slot: activeSlot,
    setInput,
    revealComposer,
    openSideChat,
  })
  return {
    inputAreaRef, dockH, dockGutter, dockRef, quoteWholeMessage, handleQuote, handleAsk, flyingQuote, endQuoteFlight,
  }
}

interface MessageActionsOptions {
  dispatch: AppDispatch
  navigate: NavigateFunction
  filteredSlotsRef: MutableRefObject<ChatSlot[]>
  connected: boolean
  activeSlot: string | null
  messages: ChatMessage[]
  /** The page's latest-value ref of `messages`, read by edit-and-resend so it keeps one identity across frames. */
  messagesRef: MutableRefObject<ChatMessage[]>
  slotRunning: boolean
  activityOpen: boolean
  activeSlotRef: MutableRefObject<string | null>
  activeSlotRemoteBound: boolean
  showActionError: (message: string, title?: string) => void
  handleFileOpen: ReturnType<typeof useChatPageResourcesController>['handleFileOpen']
  handleFolderOpen: (dirPath: string) => void
  search: ReturnType<typeof useChatPageResourcesController>['search']
  tabsCtl: ReturnType<typeof useChatPageResourcesController>['tabsCtl']
  sessionTitles: Map<string, string>
  selectSessionTab: (key: string) => void
  linkPreviewsOn: boolean
  planTaskId: string
  setSidebarPinned: Dispatch<SetStateAction<boolean>>
  sidebarAutoHidden: MutableRefObject<boolean | null>
  sidebarWidth: number
  setRegenerating: Dispatch<SetStateAction<boolean>>
}

/**
 * Per-message actions and the transcript facts the rows read: edit-and-resend and
 * the Cmd/Ctrl+Up edit request, the search highlight context, user-row content
 * rendering, the stream tick and transcript heat, the side panel toggle and the
 * activity-tab bridge, read aloud, and apply plan.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useMessageActions({
  dispatch, navigate, filteredSlotsRef, connected, activeSlot, messages, messagesRef, slotRunning, activityOpen, activeSlotRef, activeSlotRemoteBound, showActionError, handleFileOpen, handleFolderOpen, search,
  tabsCtl, sessionTitles, selectSessionTab, linkPreviewsOn, planTaskId, setSidebarPinned, sidebarAutoHidden, sidebarWidth, setRegenerating,
}: MessageActionsOptions) {
  // Reads `messagesRef`, not `messages`: the array is new on every streamed
  // frame, and this callback feeds the transcript renderers memo, so a
  // `messages` dep re-rendered every mounted row on every frame.
  const handleEditResend = useCallback((index: number, ts: string, newContent: string) => {
    if (!activeSlot || slotRunning || activeSlotRemoteBound) return
    const snapshot = [...messagesRef.current]
    dispatch(truncateAfterIndex(index))
    dispatch(appendMessage({ role: 'user', content: newContent, cls: '', ts: new Date().toISOString() }))
    setRegenerating(true)
    // Use /rewind (fork-and-swap) — discards the orphan kiro-cli session so
    // truncated forward turns can't resurface on resume. Mirrors kiro-cli's
    // native /rewind slash command, but swaps the session under the same
    // slot identity so the UI stays in place (no new tab, no title change).
    rewindWithRollback(activeSlot, ts, newContent, () => {
      dispatch(replaceMessages(snapshot))
      setRegenerating(false)
    })
  }, [activeSlot, slotRunning, activeSlotRemoteBound, messagesRef, dispatch, setRegenerating])

  // ⌘↑ / Ctrl+Up: open the LAST user message in the existing
  // Edit & resend editor. The chord itself is claimed by the composer
  // (`ChatInput` / `LexicalComposerInput`), which only fires from an empty
  // composer, so this handler's job is eligibility (the same gates the row's
  // `canEdit` affordance uses) and finding the target row: the newest
  // `role === 'user'` message — agent turns, tool cards and system/steering
  // rows are skipped by the role filter alone. `editLast` carries a rising
  // sequence number plus the target's identity (index AND ts) so only the
  // matching `UserMessage` row reacts and a re-render never re-opens the
  // editor over an in-progress draft. The row ACKNOWLEDGES consumption via
  // `onEditConsumed` (which nulls this state): a persisted request must never
  // survive a row remount, or the rising-edge mount would replay the editor
  // after Escape + session-switch and invite an accidental resend.
  const [editLast, setEditLast] = useState<{ seq: number; ts: string; index: number } | null>(null)
  // Backs `editLast.seq` with a counter that survives consumption — deriving
  // it from `editLast` itself (nulled by `onEditConsumed`) would restart at 1
  // after every consumed request, colliding with a mounted row's already-seen
  // `lastEditRequestRef` and silently dropping the next press.
  const editSeqRef = useRef(0)
  // True only while `renderFarmItem` builds an off-screen measurement row. The
  // farm draws through the same `renderMessage` as the live transcript, so
  // without this a farm-rendered `UserMessage` would receive the pending edit
  // request, consume it (nulling `editLast`) and measure its editor instead of
  // the bubble — the live row never opens and a wrong height gets cached.
  // Read synchronously while the elements are created, so a ref is enough.
  const farmPassRef = useRef(false)
  const handleEditConsumed = useCallback(() => setEditLast(null), [])
  // Same for a session switch: the request is slot-scoped intent, so leaving
  // the slot discards any unconsumed request instead of letting a later,
  // unrelated remount treat it as fresh.
  useEffect(() => { setEditLast(null) }, [activeSlot])

  const searchCtxValue = useMemo(() => ({
    term: search.term,
    caseSensitive: search.caseSensitive,
    currentMessageIdx: search.currentMessageIdx,
    currentOccurrenceIdx: search.currentOccurrenceIdx,
  }), [search.term, search.caseSensitive, search.currentMessageIdx, search.currentOccurrenceIdx])

  const renderUserContentCb = useCallback(
    // The session triple matches what the assistant / note rows hand
    // MarkdownRenderer (see the AssistantMessage and inject branches below),
    // so a `/chat?sid=…` link behaves identically across row kinds (#8253).
    (c: string, mt: Record<string, unknown> | undefined, ts?: string) => renderUserContent({
      content: c,
      meta: mt,
      onFileOpen: handleFileOpen,
      onFolderOpen: handleFolderOpen,
      linkPreviews: linkPreviewsOn,
      onSessionOpen: selectSessionTab,
      sessions: connected ? sessionTitles : undefined,
      activeSession: activeSlot || undefined,
      // The short-name chip needs the row's write time; without it no short name
      // resolves here, because slot numbers are reused.
      messageTs: ts,
    }),
    [handleFileOpen, handleFolderOpen, linkPreviewsOn, selectSessionTab, connected, sessionTitles, activeSlot]
  )

  useEffect(() => {
    const togglePin = () => {
      // Always-available collapse. Only guard is no-sessions (the sidebar is
      // force-open then anyway, so there is nothing to collapse).
      if (filteredSlotsRef.current.length === 0) return
      // Explicit user intent outranks the preview-expand auto-hide, so exiting
      // expand mode leaves this choice alone.
      sidebarAutoHidden.current = null
      setSidebarPinned(p => {
        const next = !p
        safeSetItem('mc-sidebar-pinned', String(next))
        return next
      })
    }
    window.addEventListener('toggle-pin-chat-sidebar', togglePin)
    return () => window.removeEventListener('toggle-pin-chat-sidebar', togglePin)
  }, [filteredSlotsRef, setSidebarPinned, sidebarAutoHidden])

  const lastRole = messages[messages.length - 1]?.role ?? ''
  // Advances with every streamed chunk, so ChatFooter can tell "text is arriving"
  // apart from "the stream went quiet mid-turn" (the model generating a tool call,
  // or a tool group holding the trailing 'streaming' message open). 0 whenever no
  // streaming message is in flight.
  const streamTick = lastRole === 'streaming' ? (messages[messages.length - 1]?.content.length ?? 0) : 0
  // Transcript heat: advances on ANY transcript mutation (streamed chunk, tool
  // row, thinking burst), so useStreamIdle can tell a high-frequency burst from
  // a quiet running turn. Render-phase ref bump, guarded on the identity change
  // — the same pattern as a lazy initializer, so it is StrictMode-safe.
  const heatMessagesRef = useRef<ChatMessage[] | null>(null)
  const heatTickRef = useRef(0)
  if (heatMessagesRef.current !== messages) { heatMessagesRef.current = messages; heatTickRef.current++ }
  // Hot while the slot runs and mutations landed within the idle window. The
  // state update inside useStreamIdle commits AFTER the render that delivered a
  // mutation, so a row mounting on the first mutation after a quiet spell still
  // reads idle=true (hot=false) and keeps its entrance ease; only rows mounting
  // inside a burst (a second mutation within 700ms) snap. Passed down to
  // ToolCallLine to gate its height animations — see `transcriptHot` there.
  const transcriptIdle = useStreamIdle(heatTickRef.current, slotRunning)
  const transcriptHot = slotRunning && !transcriptIdle
  // Precompute: index of last finalized assistant message (tools after this are "trailing")
  // The activity panel has exactly two modes, and the question that picks one
  // is NOT "how wide is the window" — it is "how much width is left for the
  // chat". Subtract the shell's nav rail and the session sidebar (both of which
  // the user can hide) from the viewport: if what remains still seats the panel
  // at its minimum PLUS a usable chat pane, the panel sits BESIDE the chat.
  // Otherwise it FILLS the chat column, with the sidebar and rail untouched.
  //
  // Consequences worth stating:
  //  - Hiding the rail (162px) or the sidebar (~260px) can promote fill -> beside
  //    at a viewport width that could not seat both a moment earlier.
  //  - Mobile needs no special case: rail 0 + sidebar 0 (its drawer is fixed,
  //    not a flex sibling) always lands under the threshold. isMobile is still
  //    forced to fill so a 700px phone-class viewport cannot go beside.
  //  - The measurement is loop-free ON PURPOSE. It reads the rail TRACK and the
  //    sidebar's own state, never the chat container's painted width — that
  //    shrinks when the panel opens, which would oscillate beside <-> fill.
  const railWidth = useRailWidth()
  const winW = useWindowWidth()
  // sidebarWidth is the width ChatSidebar reports its root paints at, already
  // held to this view and this window (see sidebarPaintWidth); the stored
  // preference stays with the sidebar for a wider window or for board view.
  const effectiveSidebarWidth = clampSidebarWidth({ stored: sidebarWidth, winW, railW: railWidth })
  const toggleAct = useCallback(() => {
    // Opening with no tabs shows the empty-state launcher grid (no seeded
    // default view) -- the user picks what to open.
    dispatch(toggleActivity())
  }, [dispatch])
  // Shell-launched toggle: the panel-toggle shortcut (`onToggleSidePanel`,
  // shell/shortcuts/shellKeyboard.ts) dispatches this event so the panel-close
  // coordination above stays in ChatPage.
  useEffect(() => {
    const h = () => toggleAct()
    window.addEventListener('toggle-activity-panel', h)
    return () => window.removeEventListener('toggle-activity-panel', h)
  }, [toggleAct])
  // Bridge explicit view requests (e.g. the /side slash command dispatches
  // openActivityToTab('side')) into the tab model.
  const activityTab = useAppSelector(s => s.chat.activityTab)
  // Keyed on the REQUEST counter, never on the tab's value. `activityTab` also
  // changes when a chat switch restores the incoming chat's cached tab (Files
  // when it has none), and bridging that would force-focus Files — or whatever
  // view was last requested in that chat — over the tab the tab strip has
  // remembered and the user actually left the chat on. Only openActivityToTab
  // bumps the counter, so only a deliberate request moves focus.
  const activityTabRequest = useAppSelector(s => s.chat.activityTabRequest)
  // Skip the mount invocation: the counter is already non-zero after any earlier
  // request this page load, so firing on mount would re-open that view on top of
  // the now-persisted strip every time ChatPage remounts after a route change.
  const activityTabBridged = useRef(false)
  useEffect(() => {
    if (!activityTabBridged.current) { activityTabBridged.current = true; return }
    if (activityOpen) tabsCtl.openView(activityTab === ('nav' as string) ? 'files' : activityTab)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activityTabRequest])
  // Stable row callbacks. Inline lambdas in the row renderer would hand
  // AssistantMessage a fresh function identity every render, so its memo()
  // could never bail out — the boundary would break at the call site, not in the
  // renderer. Both read live state from a ref / the store rather than closing over
  // it, so neither needs a dependency that churns while a turn streams.
  const handleSpeak = useCallback((content: string) => {
    if (store.getState().chat.voicePlaying) {
      window.dispatchEvent(new Event('voice-stop'))
      dispatch(setVoiceAudio(null))
      return
    }
    dispatch(setVoiceAudio(null))
    // A synthesis still loading its model has no playing audio yet, but owns
    // the same output channel. Replace it before starting a manual request.
    window.dispatchEvent(new Event('voice-stop'))
    // clientFallback: on a headless remote gateway that cannot synthesize
    // (voice_unavailable), speak the reply with the webview's own engine instead
    // of only showing an error notice. A working gateway never emits that code,
    // so this leaves the normal path untouched.
    api.voiceSynthesize(activeSlotRef.current || '', content, { clientFallback: true }).catch(() => {})
  }, [dispatch, activeSlotRef])

  const handleApplyPlan = useCallback(async (steps: PlanStepInput[]) => {
    try {
      const r = await api.planFromChat(steps, planTaskId)
      if (r.ok) { navigate('/projects?applied=' + (r.task_id || planTaskId)); return true }
    } catch { /* API error */ }
    showActionError(i18nT('pages.chatPage.failed_to_apply_plan'))
    return false
  }, [planTaskId, navigate, showActionError])
  return {
    handleEditResend, editLast, setEditLast, editSeqRef, farmPassRef, handleEditConsumed, searchCtxValue, renderUserContentCb, lastRole, streamTick, transcriptHot, railWidth, winW,
    effectiveSidebarWidth, toggleAct, handleSpeak, handleApplyPlan,
  }
}
