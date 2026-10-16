import { useState, useRef, useCallback, useEffect, useMemo } from 'react'
import { X } from 'lucide-react'
import { SplitGlyph } from './SplitGlyph'
import { useQuery } from '@tanstack/react-query'
import ChatMessageList from '../app-sdk/ChatMessageList'
import type { VirtualTranscriptHandle } from '../app-sdk/ChatMessageList'
import { EdgeFade, JumpToBottomButton } from '../app-sdk/ChatScrollChrome'
import { createTranscriptVersioner } from '../chat-core/transcript/transcriptVersion'
import { DOCK_CLEARANCE_PX, useComposerDockMetrics } from '../pages/chat/composerDockMetrics'
import ChatInput from './ChatInput'
import { busySteerFlag } from './chat-input/busySend'
import { filterCrewmateChat, withQueuedAsBubbles } from './chat/crewmateBubbles'
import { AWAITING_REPLY_ACTIVITY, useCrewmateAwaitingReply } from './chat/useCrewmateAwaitingReply'
import CrewmateLiveActivity from './chat/CrewmateLiveActivity'
import { useCrewmateGreetingShown } from './chat/useCrewmateGreetingShown'
import ErrorNotice from './ErrorNotice'
import StatusNotice from './StatusNotice'
import { Btn } from './ui'
import ChatDropOverlay from './ChatDropOverlay'
import PaneDim from './PaneDim'
import PendingQuestionCard from './PendingQuestionCard'
import QueueStack, { SubagentDeliveryProgress, splitPaneMessages } from './QueueStack'
import SubagentProgressBar from '../pages/chat/SubagentProgressBar'
import ChatFooter from '../pages/chat/ChatFooter'
import { useEnvironmentStartLabel } from './sessionEnvironmentHooks'
import PinnedPrompt from '../pages/chat/PinnedPrompt'
import SessionTitleControl from '../pages/chat/SessionTitleControl'
import { pinCandidateKey, usePinnedPrompt } from '../pages/chat/usePinnedPrompt'
import { useJevAutoSend } from '../pages/chat/useJevAutoSend'
import type { DisplayItem } from '../pages/chat/types'
import { useSlotActivity } from '../pages/members/useSlotActivity'
import { agentOrDefaultLabel } from '../utils/agentLabel'
import { SlotProvider } from '../providers/SlotContext'
import { useProvider } from '../providers'
import { useConnectionsUiEnabled } from '../hooks/useConnectionsUi'
import { legacyCodexEffort } from '../hooks/useInteractiveModels'
import { selectionCapabilitiesFailed } from '../lib/effort'
import { isUnpinnedModel } from '../lib/jevRoute'
import { useQueuedMessageActions } from '../hooks/useQueuedMessageActions'
import { useAppSelector, useAppDispatch, store } from '../store'
import { PANE_HYDRATE_LIMIT, selectSlotMessages, selectSlotStreamState, selectComposerBusy, hydrateSlotMessages, appendSlotMessage, loadOlderMessages, loadOlderSlotMessages, stageToMainComposer, pendingQuestionFor } from '../store/chatSlice'
import { deriveFollowUpOptions } from '../app-sdk/protocol'
import { appendFollowUpOption, removeFollowUpOption, selectSingleFollowUpOption, type OwnedSuffix } from '../lib/followUpToggle'
import { CONTENT_WIDTH, loadChatConfig, type ChatConfig } from '../pages/chat/ChatSettings'
import { scaleContentWidth } from '../pages/chat/contentWidth'
import { tryQuickSend } from '../lib/quickSend'
import { mergeCarriedDraft } from '../utils/pasteTokens'
import { useSelectionQuoteAsk } from '../chat-core/composer/selectionActions'
import FlyingQuote from './FlyingQuote'
import { revealComposer } from '../pages/chat/composerFocus'
import { api } from '../api/client'
import { slotMessagesQueryKey } from '../api/slotMessagesQuery'
import { Composer, type ComposerHandle, type ComposerVoiceOptions } from '../chat-core/composer/Composer'
import { slotApprovalMode } from '../utils/slotApprovalMode'
import { isComposerSendHeld } from '../utils/composerSendHolds'
import { i18nT } from '../i18n/t'
import { answerRejectedMessage } from '../utils/questionAnswers'
import { fetchDashboardConfig } from '../api/dashboardConfigQuery'
import type { ChatPaneProps } from './ChatPaneProps'
import { useChatPaneDraft } from './useChatPaneDraft'
import { useChatPaneUploads } from './useChatPaneUploads'
import { useChatPanePickerModels, useChatPanePickerActions } from './useChatPanePickers'
import { ChatPaneAgentMenu, ChatPaneModelMenu } from './ChatPanePickerMenus'
import { useChatPaneSend } from './useChatPaneSend'
import { useChatPaneStop, useChatPaneRunSync } from './useChatPaneTurnControl'
import { useChatPaneContinue } from './useChatPaneContinue'
import { useChatPaneRenderers, ChatPaneAboveRows } from './ChatPaneTranscript'

export type { PaneLeading } from './ChatPaneProps'

/**
 * ChatPane — one live chat session in the native session grid.
 *
 * Renders the REAL native <ChatInput> inside <SlotProvider> with the full
 * per-slot composer (model/agent/approval-mode pickers, attachments, QueueStack).
 * Messages stream live from the store; per-slot metadata comes from
 * s.dashboard.slots. Server reads/writes go through React Query + the api client.
 */

export default function ChatPane({
  slotKey,
  focused,
  onFocus,
  onRemove,
  onSplitRight,
  onSplitDown,
  onOpenFull,
  agentLocked,
  composerInput: ComposerInput = ChatInput,
  frameless,
  topInset = 0,
  followContentWidth,
  hideEmptyHint,
  openSideChat,
  leading,
  onFileOpen,
  onArtifactOpen,
  busyMode = 'split',
  crewmate,
  crewmateGreeting,
  onOpenCrewWorkLog,
  onOpenSubagents,
  threads,
  onSessionOpen,
  sessions,
  activeSession,
  foldBefore,
}: ChatPaneProps) {
  const dispatch = useAppDispatch()
  const provider = useProvider()
  // Same gate the main chat uses: hide a Connections-owned OAuth banner only
  // while the card that owns that flow is reachable.
  const connectionsUiOn = useConnectionsUiEnabled()
  const draft = useChatPaneDraft(slotKey)
  const {
    input, setInput, pendingFiles, setPendingFiles, pendingDirs, composerPastes, carryIntoComposer,
    slotKeyRef, inputRef, pendingFilesRef, mountedRef, restoreIntoComposer,
  } = draft
  // In-pane report of a title rename / regenerate that did not land (#9727):
  // the main header routes the same failure into its action banner.
  const [titleError, setTitleError] = useState<{ title: string; message: string } | null>(null)
  // The transcript is virtualized (chat-core P5-e): ChatMessageList owns the
  // scroller and the stick-to-bottom follow through VirtualTranscript. The pane
  // keeps the element ref for the pinned-prompt hook, a handle for the jump
  // pill, and the rendered at-bottom state that shows it.
  const scrollerRef = useRef<HTMLDivElement | null>(null)
  const listRef = useRef<VirtualTranscriptHandle | null>(null)
  const [isAtBottom, setIsAtBottom] = useState(true)
  const scrollToBottom = useCallback(() => { listRef.current?.scrollToBottom() }, [])
  // Pinned-prompt banner — the same hook the main chat's transcript controller
  // wears (chat-core P5-d). The list to index comes from ChatMessageList
  // (`onDisplayItems` turns its row indexing on); with only the viewport
  // window mounted, a gap at the hand-off line is unmounted spacer, so the
  // hook must wait for the row (`requiresMountedHandoff`) exactly as the main
  // chat does.
  const pin = usePinnedPrompt({ scrollerRef, requiresMountedHandoff: true })
  const { displayItemsRef: pinItemsRef, updatePinnedPrompt, onScrollPin, setPinned, setPinExpanded } = pin
  const onDisplayItems = useCallback((items: DisplayItem[]) => {
    pinItemsRef.current = items
    // A new turn shifts geometry with no scroll event of its own (ChatPage
    // recomputes on its rendered list for the same reason). Layout-effect
    // timing: the rows carrying the new indices are already in the DOM.
    updatePinnedPrompt()
  }, [pinItemsRef, updatePinnedPrompt])
  // A different session starts collapsed with nothing pinned.
  useEffect(() => { setPinned(null); setPinExpanded(false) }, [slotKey, setPinned, setPinExpanded])

  const allMessages = useAppSelector((s) => selectSlotMessages(s, slotKey))
  const activeSlot = useAppSelector((s) => s.chat.activeSlot)
  // Consume a Side Chat → composer hand-off staged for THIS pane's slot. Keyed
  // by slot so a member's hand-off merges into that member's own composer and
  // is never picked up by the dashboard ChatPage (which guards on its active
  // slot too). Merge into the live composer — never replace — mirroring the
  // pane's own draft-recovery seam; the user sends when they choose.
  const mainComposerAppend = useAppSelector((s) => s.chat.mainComposerAppend)
  useEffect(() => {
    if (!mainComposerAppend || mainComposerAppend.slot !== slotKey) return
    const text = mainComposerAppend.text
    dispatch(stageToMainComposer(null))
    const carried = carryIntoComposer(text, [])
    setInput((cur) => mergeCarriedDraft(cur, carried))
  }, [mainComposerAppend, slotKey, dispatch, carryIntoComposer, setInput])
  const streamState = useAppSelector((s) => selectSlotStreamState(s, slotKey))
  const running = streamState !== 'idle'
  // Per-slot context-window usage for the input-bar ring (mirrors ChatPage; the
  // store keys these by slot). Default 0 so the ring always renders, exactly
  // like single chat.
  const contextPct = useAppSelector((s) => s.chat.slotContextPct[slotKey] ?? 0)
  const contextTokens = useAppSelector((s) => s.chat.slotContextTokens?.[slotKey])
  // Prefer the warm's value: this pane's own query is staleTime:Infinity, so its
  // has_more freezes at mount while a later bounded warm can truncate the cache.
  const warmHasMore = useAppSelector((s) => s.chat.slotPaneHasMore?.[slotKey])
  const paneSlot = useAppSelector((s) => s.dashboard.slots.find((x) => x.key === slotKey))
  // Older history for the ACTIVE slot. That slot reads the store's main list,
  // which `switchSlot` loads one bounded page of; the full chat page walks the
  // rest with `loadOlderMessages`, and a pane showing the active slot (the
  // crewmate DM on the Members page) must offer the same walk or it is stuck
  // on the newest page -- a patroller's page filters down to a few bubbles.
  // Gated on the cursor key exactly like the thunk, so a mid-switch cursor
  // that still describes the previous chat never draws the bar.
  const pagesActiveSlot = useAppSelector((s) => slotKey === s.chat.activeSlot && s.chat.slotCursorKey === slotKey)
  const activeHasMore = useAppSelector((s) => s.chat.slotHasMore)
  const loadingOlder = useAppSelector((s) => s.chat.loadingOlder)
  const olderFailed = useAppSelector((s) => !!s.chat.slotOlderError)
  const loadOlder = useCallback(() => {
    if (store.getState().chat.loadingOlder) return
    void dispatch(loadOlderMessages())
  }, [dispatch])
  // Older history for a BACKGROUND slot, from the pane's own cursor. The
  // Members page never makes a DM the active slot, so this is the door a warm
  // DM pane actually has; the active branch above covers the other case.
  const paneCursor = useAppSelector((s) => s.chat.slotPaneNextBefore?.[slotKey] ?? 0)
  const paneLoadingOlder = useAppSelector((s) => !!s.chat.slotPaneLoadingOlder?.[slotKey])
  const paneOlderFailed = useAppSelector((s) => !!s.chat.slotPaneOlderError?.[slotKey])
  const pagesPane = slotKey !== activeSlot && warmHasMore === true && paneCursor > 0
  const loadOlderPane = useCallback(() => {
    void dispatch(loadOlderSlotMessages(slotKey))
  }, [dispatch, slotKey])
  // The composer is a `Composer` root around the ChatInput preset (chat-core
  // P3-b). Its Voice atom is what gives the pane a microphone: the pane wires no
  // voice props, only the two things the atom cannot know — the endpointer's
  // auto-submit, and (through the root) that a pane's composer IS its slot's, so
  // the atom's default on-screen predicate is exact. No push-to-talk here: that
  // key binding is document-wide and ChatPage owns it (follow-up: focused-pane
  // ownership). The pane's steer-not-queue rule (#8852) stays on `canSteer`
  // below until the Send atom exists.
  const composerRef = useRef<ComposerHandle>(null)
  const autoSubmitRef = useRef<(() => void) | null>(null)
  const composerVoiceOptions = useMemo<ComposerVoiceOptions>(() => ({
    onAutoSubmit: () => { autoSubmitRef.current?.() },
  }), [])
  // Shared composer-busy rule (chatSlice.selectComposerBusy): main turn
  // streaming OR sub-agents running (dual signal). Drives the queue affordance
  // and skips the optimistic user bubble (the backend returns a "queued"
  // message instead, so an optimistic bubble would render a duplicate).
  const busy = useAppSelector((s) => selectComposerBusy(s, slotKey))
  // Parent link for the "↳ fork of <parent>" tag. forked_from is the parent's
  // history key (dashboard:<slot>); strip the prefix to match the bare slot key.
  const parentKey = paneSlot?.forked_from ? paneSlot.forked_from.replace(/^dashboard:/, '') : null
  const parentTitle = useAppSelector((s) =>
    parentKey ? s.dashboard.slots.find((x) => x.key === parentKey)?.title : undefined,
  )
  const approvalMode = useAppSelector((s) => s.dashboard.approvalMode)
  const title = paneSlot?.title || slotKey
  const displayMode = slotApprovalMode(approvalMode, paneSlot)
  // Queued messages render in the QueueStack, not inline in the message list.
  // System injections are excluded from the interactive stack (isNonInteractiveQueued):
  // sub-agent deliveries collapse into one progress line, and synthetic
  // turn-recovery injections drain automatically and render as a RecoveryCard.
  // Mirrors ChatPage — split view (⌘D) is a second live QueueStack consumer.
  //
  // Memoized on `allMessages`: this pane OWNS the composer `input` state, so it
  // re-renders on every keystroke. Recomputing these in the render body would
  // hand `messages` a fresh array identity per character, defeating the memo()
  // on ChatMessageList and re-running its O(N) turn grouping while the user
  // types.
  const { messages: paneMessages, queuedMessages, systemDeliveryCount } = useMemo(
    () => splitPaneMessages(allMessages),
    [allMessages],
  )
  // A crewmate's chat draws only what the crewmate says (see the `crewmate`
  // prop). Filtered HERE, above the list, so the run positions the assistant
  // rows compute from their neighbours see the drawn list, and so the pinned
  // prompt, the earlier-messages anchor and the empty hint all agree with what
  // is on screen. Same array identity back when nothing is dropped. The turn
  // in flight is filtered like any other: what the crewmate is doing right now
  // is the status line above the footer (`liveActivity`), not a transcript
  // row, so the working indicator holds still while steps come and go.
  const crewmateLive = running || !!paneSlot?.running
  // The typing bubble shows from the person's Send, not only once the turn
  // that answers is live (useCrewmateAwaitingReply).
  const { awaitingReply, markSent: markCrewmateSent } = useCrewmateAwaitingReply(!!crewmate, slotKey, allMessages, crewmateLive)
  const crewmateTyping = crewmateLive || awaitingReply
  // Queued rows are drawn in place from the slot's full row list, so a parked
  // message sits where it was sent (`withQueuedAsBubbles`).
  const messages = useMemo(
    () => (crewmate ? filterCrewmateChat(withQueuedAsBubbles(allMessages, queuedMessages)) : paneMessages),
    [crewmate, allMessages, paneMessages, queuedMessages],
  )
  // The host's greeting is drawn while it is still the newest word: a reply
  // landing after it (a patrol / wake turn keeps it up) takes it down.
  const greetingShown = useCrewmateGreetingShown(!!crewmate && !!crewmateGreeting, messages)
  // What the crewmate is doing right now, for the status line above the
  // working indicator: read through the SAME seam the DM header's identity
  // pill reads (`useSlotActivity` over the slot's live status record), so the
  // two can never name one moment differently (same `delegatedOnly` input).
  // The slots frame's `stopping` flag goes in as a seam input (the footer this
  // caption replaces read it beside the stream state; a Stop from another tab
  // reaches this pane only as the flag), so the header pill and this caption
  // keep resolving one moment from one seam.
  const liveActivity = useSlotActivity(crewmate ? slotKey : '', {
    running: crewmateLive,
    delegatedOnly: !!paneSlot?.subagents_running && !paneSlot?.running,
    stopping: !!paneSlot?.stopping,
  })
  // The host declares the conversation surface; only the gateway decides
  // how to prioritize the human's message on the validated member DM.
  const composerBusyMode = crewmate ? 'responsive' : busyMode
  // A reply on record means this session's environment connected before, so a
  // later engine restart is a reconnect, not setup (see useEnvironmentStartLabel).
  const paneHasReplied = useMemo(() => paneMessages.some(m => m.role === 'assistant'), [paneMessages])
  const paneEnvironmentStart = useEnvironmentStartLabel(slotKey, !!paneSlot?.running, paneHasReplied)
  // The unfiltered rows, handed to the row set for the one read that must see
  // what the filter dropped (the steer-chip decision reads the policy-block
  // inject row). `undefined` for an ordinary chat, so its renderer set does not
  // rebuild on every appended message.
  //
  // Handed as a GETTER, not the array: every streaming flush replaces
  // `paneMessages`, and a new array here would rebuild the renderer set -- and
  // with it every row ChatMessageList has mounted -- once per streamed token.
  // The getter reads the newest rows whenever a row renders. The set itself
  // moves with the transcript version (chat-core/transcript/transcriptVersion):
  // on every change to these rows except a streaming row's text growing, which
  // `crewmateRunPosition` never reads (a streaming row continues a run by its
  // role alone).
  const crewmateTranscriptRef = useRef<typeof paneMessages | undefined>(undefined)
  crewmateTranscriptRef.current = crewmate ? paneMessages : undefined
  const transcriptVersionOf = useMemo(() => createTranscriptVersioner(), [])
  const transcriptVersion = crewmate ? transcriptVersionOf(paneMessages) : 0
  const crewmateTranscript = useMemo(
    () => (crewmate ? () => crewmateTranscriptRef.current : undefined),
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the transcript version is the dependency, see above
    [crewmate, transcriptVersion],
  )
  // EVERY queued row, cards and hidden system deliveries alike. A reorder
  // submits the full sequence — see useQueuedMessageActions — so the
  // non-interactive rows `splitPaneMessages` strips out are still needed here.
  const allQueuedMessages = useMemo(
    () => allMessages.filter(m => m.role === 'queued'),
    [allMessages],
  )

  // Follow-up [OPTIONS:] pills for this pane's composer — the same
  // derive-and-pass wiring ChatPage uses, adapted to the pane's own signals.
  // Derived from `allMessages`, NOT the queued-stripped `messages` above:
  // deriveFollowUpOptions short-circuits on a `queued` row (the user already
  // acted), and splitPaneMessages removes exactly those rows, so deriving from
  // the filtered list would keep stale pills alive past a queued send.
  // The pane's composer-busy rule (main turn streaming OR sub-agents running)
  // stands in for ChatPage's isStreaming as the mid-turn gate: the pane already
  // treats `busy` as its one busy signal everywhere else (queue affordance,
  // optimistic-bubble skip), so the pills follow the same rule rather than
  // introducing a second busy variant. A pending question card suppresses them
  // for the same reason as ChatPage: both would offer the same choices, and
  // only the card can answer the blocked tool call.
  const pendingQuestion = useAppSelector((s) => pendingQuestionFor(s.chat.pendingQuestions, slotKey))
  const { followUpOptions, followUpSourceKey, followUpMulti } = useMemo(
    () => deriveFollowUpOptions(allMessages, busy, !!pendingQuestion),
    [allMessages, busy, pendingQuestion],
  )
  // Visual-only highlight state; the composer text is the source of truth for
  // what gets sent. Cleared whenever the options list changes (new assistant
  // message) or the pane is re-bound to another slot — both signal a fresh turn.
  const [followUpPicked, setFollowUpPicked] = useState<Set<string>>(() => new Set())
  // Read by the option handler instead of the state: two clicks landing before
  // a re-render would both see the same set and both take the append branch.
  const followUpPickedRef = useRef(followUpPicked); followUpPickedRef.current = followUpPicked
  // Ownership of the appended suffix, not content-matching (#7616). See
  // lib/followUpToggle: the chips own a recorded (base, options) span of the
  // draft, options kept as an ARRAY so a comma-bearing label is one element.
  // Advanced SYNCHRONOUSLY in the click handler (never in a render-time state
  // updater), so React StrictMode's double-invocation cannot rebase it on stale
  // state (the #7616 F2 defect).
  const followUpInsertedRef = useRef<OwnedSuffix | null>(null)
  // Any DIRECT user edit of the composer invalidates chip ownership (#7616):
  // the recorded span describes a draft the chips produced, and once the user
  // types the span no longer maps to the live text. Clearing here — the single
  // path a user keystroke takes into `input` — means the un-toggle path can
  // never act on a stale span, even one the user edited and then restored
  // byte-for-byte. Chip append/remove set the ref themselves and go through
  // setInput directly, not this handler, so they are unaffected.
  const handleUserInput = useCallback((next: string | ((prev: string) => string)) => {
    followUpInsertedRef.current = null
    setInput(next)
  }, [setInput])
  const followUpOptionsKey = followUpOptions.join('\x00')
  // A single-select offer is keyed on its source row too: a newer reply can repeat
  // the same labels, and a single-select pick must not replace text the user picked
  // from the older offer. Multi-select rows keep the label-only reset.
  const singleSourceKey = followUpMulti ? null : followUpSourceKey
  useEffect(() => { setFollowUpPicked(new Set()); followUpInsertedRef.current = null }, [followUpOptionsKey, singleSourceKey, followUpMulti, slotKey])
  // Quick Send parity with ChatPage: same query key, so the cache is shared
  // with the page and no extra request is made for a pane.
  const { data: dashCfg, error: dashCfgError } = useQuery<{ quick_send?: boolean; decisions_enabled?: boolean; upload_max_mb?: number }>({ queryKey: ['dashboardConfig'], queryFn: fetchDashboardConfig, staleTime: 30_000 })
  // Whether the split send button may offer `Auto (Jev)`: the fleet ceiling and
  // the owner's consent, both the gateway's answers (see useJevAutoSend).
  const jevAutoConsented = useJevAutoSend()
  // Follow-up bar layout: the same persisted setting ChatPage reads, kept live
  // the same way (ChatPage.tsx's reload listener) — a pane is long-lived, so a
  // one-shot read would leave it on the old layout after the user changes the
  // setting while split view is open.
  const [chatConfig, setChatConfig] = useState<ChatConfig>(loadChatConfig)
  useEffect(() => {
    const reload = () => { const next = loadChatConfig(); setChatConfig(prev => JSON.stringify(prev) === JSON.stringify(next) ? prev : next) }
    window.addEventListener('focus', reload)
    window.addEventListener('mc-config-changed', reload)
    return () => { window.removeEventListener('focus', reload); window.removeEventListener('mc-config-changed', reload) }
  }, [])
  // Same enablement the main chat honours (Settings → Chat → pin last prompt),
  // read through the hook's ref so the scroll recompute never closes over a
  // stale config. Never in a crewmate's chat (#18346): the banner is for long
  // agent runs where the question scrolls away while the answer streams; a DM
  // with a colleague has short turns, the user's bubble is one row back, and
  // under the floating glass header (#18325) a second floating card read as
  // clutter. Ordinary chats and split panes keep it.
  const pinOn = chatConfig.pinLastPrompt && !crewmate
  useEffect(() => {
    pin.pinEnabledRef.current = pinOn
    if (!pinOn) setPinned(null)
  }, [pinOn, pin.pinEnabledRef, setPinned])
  // The transcript row whose bubble the banner is standing in for. The list
  // hides it (ts-keyed, index fallback — see ChatMessageList.hiddenRow);
  // memoised so the memo'd list does not re-render on every pane render.
  // `stripUncovered` is the hook's BOOLEAN "the row's action strip is still on
  // screen below the card" (derived from occlusion, see usePinnedPrompt), not
  // the fold height: liveH moves every scroll frame of the fold, and keying the
  // memo on it would re-render the list per frame for a marker that only flips
  // at its two edges.
  const pinnedState = pin.pinned
  const pinnedTs = pinnedState?.ts
  const pinnedIdx = pinnedState?.idx
  const pinnedStripUncovered = pinnedState?.stripUncovered === true
  const pinHiddenRow = useMemo(
    () => (pinnedIdx == null ? undefined : { ts: pinnedTs, index: pinnedIdx, stripUncovered: pinnedStripUncovered }),
    [pinnedTs, pinnedIdx, pinnedStripUncovered],
  )

  const pickerModels = useChatPanePickerModels({ slotKey, paneSlot, provider, dispatch, dashCfg })
  const {
    installedAgents, defaultAgent, paneAgentName, queryClient, navigate, agentDD, selectionCapabilitiesQ, codexPairModels,
    jevRouteOn, modelDD, anchorAgentBtn, anchorModelBtn, shownModel, effortSupported, defaultEffort, effectiveEffort,
    chipDefault, modelMarker,
  } = pickerModels

  // One-time hydrate of this slot's message history via React Query + the api
  // client (caching + cross-pane dedup; staleTime Infinity keeps it one-shot —
  // live updates arrive through the WS store routing, not a refetch).
  // Bounded whether or not the slot is running. The handler collapses chunk runs
  // BEFORE it computes total and slices, even mid-stream, so a limit cannot cut
  // a streaming response apart, and the page's `has_more` still marks older
  // history. The rows a turn produces arrive over the WS routing, not this
  // fetch. Lifting the bound for a running slot pulled its WHOLE transcript on
  // every open: on a long, busy session that froze the renderer, and the crash
  // recovery reopened the same chat into the same freeze (#12907).
  const limitRef = useRef<number | undefined>(PANE_HYDRATE_LIMIT)
  const limitLatched = useRef(false)
  // A crewmate's chat that filters a BOUNDED window down to no speech has
  // proved nothing: the last thing it said may sit just behind the window,
  // under fifty newer patrol rows. The never-spoken hint is a claim about the
  // whole history, so that read is upgraded to the whole transcript first
  // (same latch as the streaming upgrade) and the hint waits for it. An
  // unbounded read that is still empty is the real never-spoken case.
  const crewmateQuietUnproven = !!crewmate && warmHasMore === true && paneMessages.length > 0 && messages.length === 0
  if (!limitLatched.current && crewmateQuietUnproven) {
    limitRef.current = undefined
    limitLatched.current = true
  }
  const hydrateLimit = limitRef.current
  const { data: slotDetail, isError: slotDetailFailed, refetch: refetchSlotDetail } = useQuery({
    queryKey: slotMessagesQueryKey(slotKey, hydrateLimit),
    queryFn: () => api.chatSlotDetail(slotKey, hydrateLimit),
    staleTime: Infinity,
  })
  useEffect(() => {
    if (slotDetail?.messages) dispatch(hydrateSlotMessages({ slot: slotKey, messages: slotDetail.messages, queue: slotDetail.queue, hasMore: slotDetail.has_more, bounded: hydrateLimit !== undefined, total: slotDetail.total, running: slotDetail.running, nextBefore: slotDetail.next_before }))
  }, [slotDetail, slotKey, dispatch, hydrateLimit])

  // Scroll follow (auto-pin, release, jump pill) is owned by the virtualizer
  // inside ChatMessageList — growth on EARLIER rows (a tool result updating, a
  // thinking block expanding) and turn-collapse shrink re-pin too.


  const pickerActions = useChatPanePickerActions({ slotKey, paneSlot, dispatch, queryClient, codexPairModels, agentDD, modelDD })
  const { switchError, setSwitchError } = pickerActions

  const {
    uploadError, setUploadError, uploadHint, setUploadHint, sendHeld, uploadCancellable, uploadMutation,
    cancelUpload, uploadFiles, dragOver, dropTargetProps,
  } = useChatPaneUploads({ slotKey, dispatch, slotKeyRef, mountedRef, setInput, dashCfg, dashCfgError })


  const { messageQuote, paneHiddenCommands, slashError, setSlashError, doSend, doSteer, onDirectQuestionSend } = useChatPaneSend({
    slotKey, crewmate, dispatch, draft, composerRef, busy, running, paneSlot, scrollToBottom, openSideChat, agentLocked, paneAgentName, pendingQuestion, markCrewmateSent,
  })
  const { recoverInto: recoverQuoteInto } = messageQuote

  const { stopError, dismissStopError, paneStopState, onStop } = useChatPaneStop({ slotKey, busy, paneSlot, dispatch })
  // The endpointer auto-submit (handed to the Voice atom above) is an Enter
  // press: the composer's default busy decision with this pane's own composer
  // inputs (never the flipped chord), so it steers where Send would steer.
  autoSubmitRef.current = () => {
    // An attachment still uploading holds the composer: doSend owns that hold
    // (it ends the dictation and sends nothing) and doSteer has no copy of it.
    if (isComposerSendHeld(slotKey)) { doSend(); return }
    const flag = busySteerFlag({ slotKey, busy, turnRunning: running, stopState: paneStopState, jevAutoConsented, busyMode: composerBusyMode })
    if (flag) doSteer(flag === 'auto' ? { auto: true } : undefined)
    else doSend()
  }
  useChatPaneRunSync({ slotKey, paneSlot, dispatch })
  // Resume for a turn that ended on an error or never answered, as the main chat
  // offers it: in the composer and on the newest error row. This pane's slot is
  // rarely the active one (a crewmate's chat on the Members page never is), so
  // the hook reads THIS slot's rows and run state. `paneSlot.running` joins the
  // stream state because a background slot only hears about a turn from frames
  // this tab was connected for.
  const resume = useChatPaneContinue({ slotKey, messages, turnRunning: busy || !!paneSlot?.running })
  // The same queue-card recipe the single-chat surface runs (#5891), owned once
  // so the two cannot drift again the way #2240 found them drifted.
  //
  // Restore is this pane's own composer helper, which MERGES rather than
  // assigns: a pane's composer is local state with no per-slot draft store, so
  // clobbering it would destroy whatever the user had started typing. Before
  // this, cancelling here restored nothing at all and the text was simply gone.
  const {
    onCancel: onCancelQueued,
    onInterrupt: onInterruptQueued,
    onEdit: onEditQueued,
    onReorder: onReorderQueued,
    pendingIds: queuePendingIds,
  } = useQueuedMessageActions({
    slot: slotKey,
    allQueued: allQueuedMessages,
    visibleQueued: queuedMessages,
    // Adapter: the hook's third argument is the queued send's mention-alias
    // snapshot, which only ChatPage's composer can re-adopt (pendingFileTokens
    // and the reconciliation effect live there). This pane's composer has no
    // alias store -- restoreIntoComposer's own third parameter is its
    // paste-block list -- so the adapter pins the two-argument call and the
    // alias map is dropped here by construction rather than misread as pastes.
    restoreDraft: (text, files, _aliases, quote) => restoreIntoComposer(recoverQuoteInto(text, quote ?? null), files),
    // restoreIntoComposer merges, so a mention written back must not share a
    // spelling with a file this pane's composer already stages.
    stagedFiles: () => pendingFilesRef.current,
    // Spell a mention for a file in this pane's project the way the pane's
    // picker inserts it, relative to that project.
    project: () => paneSlot?.project,
  })
  const { listMessages, renderers } = useChatPaneRenderers({ slotKey, busyMode, onFileOpen, crewmate, crewmateTranscript, onSessionOpen, onOpenSubagents, sessions, activeSession, messages, foldBefore, resume })

  // The composer dock floats over the bottom of the scroller (ChatPage's
  // layout, shared measurement): `dockH` is what the scroller pays below its
  // last row, `inputAreaRef` the quote flight's landing box.
  const { inputAreaRef, dockH, dockGutter, dockRef } = useComposerDockMetrics(scrollerRef)
  // Quote / Ask on selected assistant text — the same chat-core seam the main
  // chat uses (chat-core/composer/selectionActions), bound to THIS pane's
  // composer and slot. Before this the pane's selection toolbar offered Copy
  // only: the SDK's assistant row draws the actions the host hands it, and no
  // host but ChatPage handed any.
  const { onQuote, onAsk, quoteFlight, endQuoteFlight } = useSelectionQuoteAsk({ slot: slotKey, setInput, revealComposer, openSideChat })

  return (
    <SlotProvider slotId={slotKey}>
      <div
        onMouseDownCapture={onFocus}
        /* Focus capture keeps the grid's focused-pane state true under
           KEYBOARD navigation: tabbing into a pane (or into its portaled
           pickers, whose React events propagate through this component tree
           even though their DOM lives under document.body) claims grid focus
           exactly like a click. Without it only mousedown moved the marker,
           and a keyboard user could type into one pane while another stayed
           marked focused. */
        onFocusCapture={onFocus}
        /* Stable pane boundary for focus scoping: `queryComposer()` resolves
           the composer inside the pane that owns `document.activeElement` via
           this attribute, and falls back to the value "focused" — the grid's
           focused pane — when the active element has no pane ancestor (the
           pane's pickers portal to document.body). A data hook, not a class
           name: classes here are styling and can churn without anyone
           auditing focus behaviour. */
        data-chat-pane={focused ? 'focused' : ''}
        /* WHICH session this pane sends to, for a lookup that already knows
           the slot it wants -- a quick-search open in split view
           (`queryComposerForSlot` in pages/chat/composerFocus.ts, #15937).
           `data-chat-pane` above answers a different question (is this the
           grid-focused pane), and the grid's focus never follows
           `activeSlot`, so without this nothing in the DOM could say which
           pane renders the session the user just opened. */
        data-pane-slot={slotKey}
        {...dropTargetProps}
        className={`relative flex flex-col h-full min-h-0 overflow-hidden bg-bg ${
          frameless
            ? ''
            : `rounded-lg border transition-colors ${focused ? 'border-accent' : 'border-border'}`
        }`}
        style={{
          '--mc-content-width': followContentWidth ? scaleContentWidth(CONTENT_WIDTH[chatConfig.contentWidth], chatConfig.contentWidth, chatConfig.messageFontSize).messages : '100%',
          // Split-view panes leave --mc-input-width UNSET so ChatInput keeps
          // its own fallback — byte-for-byte the pre-prop behavior.
          ...(followContentWidth ? { '--mc-input-width': scaleContentWidth(CONTENT_WIDTH[chatConfig.contentWidth], chatConfig.contentWidth, chatConfig.messageFontSize).input } : {}),
          // Unlike content width, message font size is not a follow/independent
          // choice per pane — it is one reading preference, so every pane gets it.
          '--mc-message-font-size': `${chatConfig.messageFontSize}px`,
        } as React.CSSProperties}
      >
        {!frameless && (
        <div data-pane-title-row className={`group/header relative z-50 flex items-center gap-2 pr-3 py-2 border-b border-border bg-card shrink-0 transition-[padding-left] duration-[240ms] [transition-timing-function:cubic-bezier(.32,.72,0,1)] ${leading?.inset ? 'pl-[49px]' : 'pl-3'}`}>
          {/* Leading edge (#10585): in split view this pane may stand in for
              the single-chat title row at the surface's top-left. `inset`
              clears the shell's stationary sidebar toggle: the pane starts at
              container x 3 (2px grid inset + 1px border) and the toggle spans
              container x 8..36 (TOGGLE_RECT), so the hairline sits at
              container 44 = pane 41 and the title starts at container 52 =
              pane 49 — the same columns the single-chat row uses (its
              left-[52px] / pl-[60px] are measured from container x -8).
              Absolute, so the divider never joins the row's flex layout.
              `control` renders the toggle inline ahead of the title. */}
          {leading?.inset && <span aria-hidden="true" data-pane-leading-divider className="absolute left-[41px] top-1/2 -translate-y-1/2 w-px h-5 bg-border" />}
          {leading?.control}
          <span className={`w-2 h-2 rounded-full shrink-0 ${running ? 'bg-ok animate-pulse' : 'bg-accent'}`} />
          {/* Same rename / regenerate control as the single-session header
              (#9727): the title row is the `group/header` hover target that
              reveals the Pen and the Sparkles button. */}
          <SessionTitleControl
            slotKey={slotKey}
            title={title}
            compact
            onError={(message, lead) => setTitleError({ title: lead, message })}
            onAttempt={() => setTitleError(null)}
          />
          {parentKey && (
            <span
              className="shrink-0 text-[10px] text-accent bg-accent/10 rounded-full px-1.5 py-0.5 truncate max-w-[38%]"
              title={i18nT('components.chatPane.forked_from', { name: parentTitle || parentKey })}
            >
              ↳ {parentTitle || parentKey}
            </span>
          )}
          <span className="flex-1" />
          {running && <span className="shrink-0 text-[10px] text-ok font-mono">{streamState}</span>}
          {onSplitRight && (
            <button onClick={onSplitRight} title={i18nT('components.chatPane.split_right_d')} aria-label={i18nT('components.chatPane.split_right')} className="shrink-0 p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors">
              <SplitGlyph />
            </button>
          )}
          {onSplitDown && (
            <button onClick={onSplitDown} title={i18nT('components.chatPane.split_down')} aria-label={i18nT('components.chatPane.split_down')} className="shrink-0 p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none transition-colors">
              <SplitGlyph down />
            </button>
          )}
          {onRemove && (
            <button onClick={onRemove} title={i18nT('components.chatPane.close_pane')} aria-label={i18nT('components.chatPane.close_pane')} className="shrink-0 rounded text-muted hover:text-danger-text hover:bg-danger/10 cursor-pointer p-1 transition-colors bg-transparent border-none">
              <X size={15} />
            </button>
          )}
        </div>
        )}

        {/* Split view only: `focused` is a boolean from the grid. A pane that
            owns the whole surface (undefined) is never dimmed. Sits above the
            title row (z-10) and the message chrome, below the drop overlay
            (z-[60]) and every shell layer (>= 46). */}
        {focused !== undefined && <PaneDim dimmed={!focused} />}
        <ChatDropOverlay active={dragOver} />

        {/* Zero-height anchor so the top fade overlays the scroller's first
            24px, dissolving content under the header edge (shared chrome —
            see ChatScrollChrome's layout contract). */}
        <div className="relative z-[1]">
          <EdgeFade side="top" />
        </div>
        {/* Pinned-prompt band. Zero-height in flow, so the banner OVERLAYS the
            scroller's top exactly as the main chat's does under its title row;
            the fold sentinel's top edge is the line the banner sticks to — the
            scroller's own top edge here, directly under the pane's title bar,
            or under the HOST's header in frameless mode (the Members DM), where
            the pane root is what this band is anchored to, so it can never
            paint over that header. right-1.5 keeps it off the scrollbar
            track, as on the main chat. */}
        {/* `top: topInset` (a relative offset, no flow height) moves the fold
            sentinel — the line the banner sticks to, read by its
            getBoundingClientRect — and the banner with it under a host band
            floating over the pane's top, so the pinned prompt never hides
            under that band. */}
        <div className="relative z-[2]" style={topInset ? { top: topInset } : undefined}>
          <div ref={pin.pinFoldRef} aria-hidden className="h-0" />
          {pinnedState && (
            <div className="absolute top-0 left-0 right-1.5 pointer-events-none">
              <PinnedPrompt
                text={pinnedState.text}
                fullText={pinnedState.full}
                images={pinnedState.images}
                bodyBeyondPreview={pinnedState.bodyBeyondPreview}
                pushUp={pinnedState.push}
                liveH={pinnedState.liveH}
                maxH={pinnedState.maxH}
                bannerH={pinnedState.bannerH}
                expanded={pin.pinExpanded}
                onToggleExpanded={() => setPinExpanded(p => !p)}
                onJump={() => pin.jumpToPinnedPromptInPlace(pinnedState.idx, {
                  mountIndex: (index, opts) => listRef.current?.mountIndex(index, opts) ?? false,
                  estimateRowTop: (index) => listRef.current?.estimateRowTop(index) ?? null,
                })}
                cardRef={pin.pinCardRef}
                onCollapsedHeight={pin.onPinCollapsedHeight}
                promptKey={pinCandidateKey(pinnedState.idx, pinnedState.ts)}
                scrollTranscriptBy={pin.scrollTranscriptBy}
              />
            </div>
          )}
        </div>

        {/* The scroller (theming hook 'chat-container', overflow contract,
            sentinels/spacers) is ChatMessageList's virtualized mount — see
            TranscriptScrollShell for the style contract it enforces. */}
        <ChatMessageList
          ref={listRef}
          messages={listMessages}
          // The slot's own liveness too, not only this session's stream: a
          // DM/member pane observing a turn driven elsewhere still follows.
          running={running || !!paneSlot?.running}
          renderers={renderers}
          hideCardOwnedOAuth={connectionsUiOn}
          onDisplayItems={onDisplayItems}
          hiddenRow={pinHiddenRow}
          onQuote={onQuote}
          onQuoteMessage={messageQuote.quoteMessage}
          onAsk={onAsk}
          threads={threads}
          onFileOpen={onFileOpen}
          onArtifactOpen={onArtifactOpen}
          transcript={{
            sessionId: `pane:${slotKey}`,
            scrollerRef,
            onScroll: onScrollPin,
            onAtBottomChange: setIsAtBottom,
            // `topInset`: a host band floating over the pane's top (the
            // Members DM header) is paid for here, so the first rows clear it
            // at scroll top and pass under it once the reader scrolls. The
            // dock floats over the scroller's bottom edge, so the scroller
            // pays for that strip too, plus the same clearance ChatPage keeps
            // between its last line and the glass.
            scrollerStyle: { paddingTop: 12 + topInset, paddingBottom: dockH + DOCK_CLEARANCE_PX, minHeight: 0 },
            // handOff off: its navigation would discard this pane's unsaved draft.
            earlier: pagesActiveSlot
              ? { hasMore: activeHasMore, loading: loadingOlder, failed: olderFailed, onLoad: loadOlder, handOff: false }
              : pagesPane
                ? { hasMore: true, loading: paneLoadingOlder, failed: paneOlderFailed, onLoad: loadOlderPane, handOff: false }
                : undefined,
            aboveRows: (
              <ChatPaneAboveRows slotKey={slotKey} activeSlot={activeSlot} messages={messages} paneMessages={paneMessages} crewmate={crewmate} crewmateLive={crewmateLive} running={running} slotDetailFailed={slotDetailFailed} refetchSlotDetail={refetchSlotDetail} hideEmptyHint={hideEmptyHint} crewmateQuietUnproven={crewmateQuietUnproven} onOpenCrewWorkLog={onOpenCrewWorkLog} warmHasMore={warmHasMore} pagesPane={pagesPane} onOpenFull={onOpenFull} />
            ),
            belowRows: (
              /* The working indicator. Inside the scroll container, after the
                 last message, so it reads as "the reply is coming" exactly where
                 the reply will land. Stop/regenerate chrome stays page-level: the
                 pane derives the indicator's inputs from its own per-slot stream
                 state.
                 A crewmate's chat draws a typing bubble (three dots in the
                 crewmate's own bubble) with its current step as the one-line
                 caption under it, mounted for the whole live turn so nothing
                 moves while steps come and go (#18238, #19997); the chat page's
                 ghost-pose carousel is NOT mounted beside it, or two things
                 would move at once. Keyed by the slot: the caption holds each
                 step briefly (useHeldActivity), and the Members page rebinds
                 this one pane to another crewmate without remounting it, so
                 without the key B's chat would open on A's held step. A new key
                 is a fresh mount: B's reading at once.
                 Every other pane — a split pane of an ordinary chat — shows the
                 same indicator the full chat page shows (the ghost-pose
                 carousel, theme-swappable via themeBranding): a running turn
                 was otherwise invisible between tool steps. */
              <>
              {greetingShown && crewmateGreeting}
              {crewmate
                ? crewmateTyping && (
                  <CrewmateLiveActivity
                    key={slotKey}
                    // Before the answering turn runs there is no live reading
                    // to name (the slot's status record is the last turn's),
                    // so the caption says it is working on it.
                    activity={crewmateLive ? liveActivity : AWAITING_REPLY_ACTIVITY}
                    streamingText={messages[messages.length - 1]?.role === 'streaming' && streamState === 'streaming'}
                    streamTick={
                      messages[messages.length - 1]?.role === 'streaming'
                        ? (messages[messages.length - 1]?.content.length ?? 0)
                        : 0
                    }
                  />
                )
                : (
                  <ChatFooter
                    statusLabel={paneEnvironmentStart}
                    running={running || !!paneSlot?.running}
                    stopping={streamState === 'stopping' || !!paneSlot?.stopping}
                    state={streamState}
                    lastRole={messages[messages.length - 1]?.role ?? ''}
                    streamTick={
                      messages[messages.length - 1]?.role === 'streaming'
                        ? (messages[messages.length - 1]?.content.length ?? 0)
                        : 0
                    }
                  />
                )}
              </>
            ),
          }}
        />
        {/* Composer dock. Floats over the bottom of the transcript scroller
            instead of sitting under it in the flex column (ChatPage's iOS
            toolbar layout, decided in
            docs/decisions/2026-10-02-chat-transcript-scrolls-under-the-composer-glass.md):
            the scroller runs the full height of the pane and the conversation
            scrolls UNDER the glass; the scroller pays for the covered strip
            with `paddingBottom: dockH + DOCK_CLEARANCE_PX`, measured from this
            box by `dockRef`. No opaque bottom fade any more: the material's
            blur and tint keep the dock legible. The root is plain
            `pointer-events-none`; `dock-inert` (index.css) goes on the TWO
            inner wrapper boxes below, as on ComposerDockView, so every bar, card and
            the composer still catches input while the empty width beside them
            lets wheel and touch reach the transcript. NOT on the root: the
            jump pill is a direct child of the root, and its wrapper is a
            full-width `pointer-events-none` strip (ChatScrollChrome) that
            `.dock-inert>*` would turn back on, swallowing clicks, selection
            and wheel over the rows behind it whenever the pill is showing.
            No z-index on purpose: the dock paints over the scroller because
            it follows it in DOM order. `right: dockGutter` keeps the
            scrollbar column clear. */}
        <div ref={dockRef} className="absolute left-0 bottom-0 pointer-events-none" style={{ right: dockGutter }} data-testid="composer-dock-root">
        <JumpToBottomButton visible={!isAtBottom && messages.length > 0} onClick={scrollToBottom} />

        <div className="dock-inert" data-testid="composer-status-stack">
        {!crewmate && <SubagentProgressBar slot={slotKey} />}

        {!crewmate && <SubagentDeliveryProgress count={systemDeliveryCount} />}
        {/* Rendered on server state only. A `steer-only` host never ASKS for a
            queue, so in normal operation this stays empty there; it is not
            hidden by mode, because a message the server did park (a backend
            without a steer channel, a mid-plan send) must stay visible and
            cancellable — hiding real state is worse than showing a card the
            surface did not intend. */}
        {/* A crewmate's chat draws a queued message as the person's own
            bubble in the thread instead (`withQueuedAsBubbles`). */}
        {!crewmate && queuedMessages.length > 0 && (
          <QueueStack messages={queuedMessages} onCancel={onCancelQueued} onInterrupt={crewmate ? undefined : onInterruptQueued} onEdit={onEditQueued} onReorder={onReorderQueued} pendingIds={queuePendingIds} />
        )}

        {/* The pending ask_question card shares the message column's gutter and
            --mc-content-width clamp, so it lines up with the messages above it
            and the composer below at every content-width setting. QuestionCard
            carries no horizontal margin of its own, so this px-4 is its only
            gutter. Change cards and guide offers are NOT here: each is
            a row of the conversation, drawn where it was proposed
            (cards/ConversationCard). */}
        <div className="px-4 mx-auto w-full" style={{ maxWidth: 'var(--mc-content-width, 900px)' }} data-testid="chat-card-column">
          {/* The pending ask_question card renders per pane: in split mode the
              agent that asked may not be the pane the user is looking at, and
              without this its card never appears anywhere, so it waits out its
              full window. */}
          <PendingQuestionCard
            slotKey={slotKey}
            onFallbackSend={(text, questions) => {
              // The wait is gone (404), so the answer cannot become a tool result
              // any more. Hand it back to the composer rather than sending it: the
              // text quotes the agent's own questions, and only the user may
              // decide to send that as their message. Same recovery as ChatPage.
              restoreIntoComposer(text, [], [], slotKey)
              // The submit was rejected, so this is an error row (ErrorNotice), not a warn notice.
              dispatch(appendSlotMessage({ slot: slotKey, message: answerRejectedMessage(questions) }))
            }}
            onDirectSend={onDirectQuestionSend}
          />
        </div>

        <StatusNotice
          className="mx-4 mt-2 mb-0 animate-rise"
          testId="chat-pane-upload-hint"
          message={uploadHint}
          onDismiss={() => setUploadHint('')}
        />
        {/* No hand-off: the composer draft (`input`) below is unsaved local state. */}
        <ErrorNotice
          className="mx-4 mt-2 mb-0 animate-rise"
          testId="chat-pane-upload-error"
          message={uploadError}
          onDismiss={() => setUploadError('')}
        />
        {/* No hand-off: same composer draft. The shared notice toast (App.tsx)
            is transient; a per-slot setting write that did not persist must
            also be reported in the pane whose control failed. */}
        <ErrorNotice
          variant="inline"
          className="mx-4 mt-2"
          testId="chat-pane-switch-error"
          message={switchError}
          onDismiss={() => setSwitchError('')}
        />
        {/* No hand-off: this pane holds an unsent draft. A failed ACP capability
            read hides the stale control; the query retries in place. */}
        <ErrorNotice
          variant="inline"
          className="mx-4 mt-2"
          testId="chat-pane-effort-capabilities-error"
          message={provider.capabilities.reasoningEffort && selectionCapabilitiesFailed(selectionCapabilitiesQ)
            ? i18nT('pages.chatPage.effort_options_unavailable') : ''}
        />
        {/* No hand-off: this pane holds an unsent draft; the reads retry in place. */}
        <ErrorNotice
          variant="inline"
          className="mx-4 mt-2"
          testId="chat-pane-model-default-error"
          message={chipDefault.failed ? i18nT('pages.settings.chatPanel.failed_to_load_config') : ''}
        />
        {/* No hand-off: the composer draft is untouched by a failed stop; the
            turn is still running, so the Stop button stays for a retry. */}
        {/* No hand-off: a refused command leaves its text in the composer, and
            the pane parks that draft when the Chat page link unmounts it. */}
        <ErrorNotice
          className="mx-4 mt-2"
          testId="chat-pane-slash-error"
          message={slashError?.slot === slotKey ? slashError.message : ''}
          onDismiss={() => setSlashError(null)}
          footer={slashError?.slot === slotKey && slashError.elsewhere ? (
            <Btn type="button" onClick={() => { setSlashError(null); navigate('/chat') }}>{i18nT('pages.chatPage.open_chat_page')}</Btn>
          ) : null}
        />
        <ErrorNotice
          variant="inline"
          className="mx-4 mt-2"
          testId="chat-pane-stop-error"
          message={stopError}
          onDismiss={dismissStopError}
        />
        {/* No hand-off: the composer draft is untouched by a refused Resume.
            The reason is the gateway's own (a turn already running, a stop in
            progress, a pending approval); the turn starting retires it. */}
        <ErrorNotice
          variant="inline"
          className="mx-4 mt-2"
          testId="chat-pane-continue-error"
          title={resume.continueError ? i18nT('pages.chatPage.could_not_continue') : undefined}
          message={resume.continueError}
          onDismiss={() => resume.setContinueError('')}
        />
        {/* No hand-off: the composer draft is untouched by a failed rename; the
            title in the bar is the one the store still holds, so the user can
            simply try again. */}
        <ErrorNotice
          variant="inline"
          className="mx-4 mt-2"
          testId="chat-pane-title-error"
          title={titleError?.title}
          message={titleError?.message}
          onDismiss={() => setTitleError(null)}
        />

        </div>
        {/* Quote transit: the selection flies from where it was taken into this
            pane's composer (the wrapper below is the landing target — same
            shape as ChatPage's inputAreaRef). */}
        {quoteFlight && <FlyingQuote text={quoteFlight.text} from={quoteFlight.from} targetRef={inputAreaRef} onComplete={endQuoteFlight} />}
        <div ref={inputAreaRef} className="relative z-10 dock-inert">
        <Composer
          ref={composerRef}
          slotKey={slotKey}
          value={input}
          onChange={handleUserInput}
          voice={composerVoiceOptions}
          pastes={composerPastes}
        >
        <ComposerInput
          value={input}
          onChange={handleUserInput}
          onSend={doSend}
          terminalCommands={!paneSlot ? 'pending' : paneSlot.executor === 'remote' ? 'remote' : 'local'}
          hiddenCommands={paneHiddenCommands}
          isRunning={busy}
          onStop={onStop}
          isQueued={streamState === 'stopping' || !!paneSlot?.stopping}
          stopState={paneStopState}
          // Resume for an interrupted turn, as ChatPage composes it: safe to
          // continue AND the transcript shows the turn ending badly, minus the
          // cases the newest error card handles with a remedy of its own.
          continuable={resume.composerResume}
          continueIsRecovery={resume.interrupted}
          onContinue={resume.onContinue}
          continuing={resume.continuing}
          // Steer path on the pane too (it was queue-only before): busy panes
          // get the same mid-turn choice as the main chat, and a
          // `steer-only` host gets a plain send that steers.
          canSteer={!crewmate && busy}
          // `doSteer` takes no `text`: the pane derives its chips from the same
          // `busy` that gates the steer path, so a chip is never on screen while
          // a send would steer, and the composer draft is the only steer payload.
          onSteer={crewmate ? undefined : doSteer}
          // AND a turn actually running: `busy` also covers a slot whose
          // sub-agents are still working, where the send starts a fresh turn and
          // there is no running turn for the point to decide about.
          jevAutoAvailable={jevAutoConsented && running}
          busyMode={composerBusyMode}
          autoFocusKey={slotKey}
          environmentSlot={slotKey}
          agentName={paneAgentName}
          // The chip shows the inherited-default marker; `agentName` stays the
          // raw resolved alias for the skills query and switch title. Uses the
          // SLOT's stored agent (not `paneAgentName`, which has already
          // collapsed empty->default) so an agent-less slot reads
          // `<default> · default` and a pinned one reads the bare alias (#8770).
          agentLabel={agentOrDefaultLabel(paneSlot?.agent, defaultAgent)}
          agentIsInheritedDefault={!paneSlot?.agent && !!defaultAgent}
          agentSource={installedAgents.find((a) => a.name === paneAgentName)?.source}
          modelName={shownModel}
          reasoningEffort={effectiveEffort}
          effortIsDefault={!paneSlot?.reasoning_effort && !legacyCodexEffort(paneSlot?.model || '', '', codexPairModels) && !!defaultEffort}
          // Effort is edited inside the model picker below; the chip only
          // names the level in force.
          hasEffort={effortSupported}
          modelIsInheritedDefault={modelMarker === 'default'}
          modelIsAutoChosen={modelMarker === 'auto'}
          // See ComposerDockView: the slot's RAW model, because `shownModel` substitutes
          // the served id and would hide every routed turn.
          modelIsJevRouted={jevRouteOn && isUnpinnedModel(paneSlot?.model)}
          contextPct={contextPct}
          contextUsedTokens={contextTokens?.used}
          contextWindowTokens={contextTokens?.window || provider.getContextWindow(shownModel)}
          onAgentClick={!agentLocked && provider.capabilities.agentTemplates ? (rect, trigger) => { anchorAgentBtn(rect, trigger); agentDD.setOpen(!agentDD.open) } : undefined}
          onModelClick={(rect, trigger) => { anchorModelBtn(rect, trigger); modelDD.setOpen(!modelDD.open) }}
          approvalMode={displayMode}
          followUpOptions={followUpOptions}
          followUpPicked={followUpPicked}
          followUpLayout={chatConfig.followUpLayout}
          quickSend={dashCfg?.quick_send}
          followUpSourceKey={followUpSourceKey}
          followUpMulti={followUpMulti}
          onFollowUpSelect={(o: string, e: React.MouseEvent, _key: string | null | undefined, sendNow: (text: string) => void) => {
            // One-click Quick Send takes the same gate as ChatPage: enabled +
            // no shift + not busy + not already in multi-select.
            if (tryQuickSend(o, dashCfg?.quick_send, e.shiftKey, busy, followUpPickedRef.current.size, sendNow)) return
            // Regular options: toggle. Click unpicked → append + mark; click
            // picked → try to remove the text + unmark (if the user edited the
            // text so it no longer matches, leave the text alone — the chip
            // still un-highlights for consistency).
            if (followUpPickedRef.current.has(o)) {
              const next = new Set(followUpPickedRef.current); next.delete(o)
              followUpPickedRef.current = next
              // Compute the transform SYNCHRONOUSLY in the event handler from
              // the live draft + ownership refs, then advance both refs and set
              // the value. No functional state updater is involved, so React
              // StrictMode's double-invocation cannot rebase ownership on stale
              // state (the #7616 F2 defect); and because the refs advance before
              // the next click, two clicks landing in one uncommitted tick still
              // compose. The refs are the source of truth between renders;
              // render re-affirms inputRef.current from committed state.
              const r = removeFollowUpOption(inputRef.current, followUpInsertedRef.current, o)
              followUpInsertedRef.current = r.owned
              inputRef.current = r.value
              setInput(r.value)
              setFollowUpPicked(next)
            } else {
              // `[OPTION:]` is single-select: the new pick replaces the previous one.
              const next = followUpMulti ? new Set(followUpPickedRef.current) : new Set<string>(); next.add(o)
              followUpPickedRef.current = next
              const r = followUpMulti
                ? appendFollowUpOption(inputRef.current, followUpInsertedRef.current, o)
                : selectSingleFollowUpOption(inputRef.current, followUpInsertedRef.current, o)
              followUpInsertedRef.current = r.owned
              inputRef.current = r.value
              setInput(r.value)
              setFollowUpPicked(next)
            }
          }}
          onFollowUpSend={(text?: string) => doSend(text)}
          project={paneSlot?.project ?? ''}
          // A crewmate's chat is a DM with one named crewmate, so the composer
          // addresses it by name rather than the product ("Message Kiro Crew…").
          placeholder={crewmate ? i18nT('components.chatInput.message_placeholder', { bot: crewmate.label || crewmate.name }) : undefined}
          onUploadFiles={uploadFiles}
          onCancelUpload={uploadCancellable ? cancelUpload : undefined}
          pendingFiles={pendingFiles}
          pendingDirs={pendingDirs}
          pendingQuote={messageQuote.pendingQuote}
          onRemoveQuote={messageQuote.clearQuote}
          onRemoveFile={(p) => setPendingFiles((prev) => prev.filter((x) => x !== p))}
          onRemoveDir={rel => {
            const esc = `@${rel}`.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
            setInput(prev => prev.replace(new RegExp(`(^|\\s)${esc}(?: |(?=\\s)|$)`, 'g'), '$1'))
          }}
          /* `isPending` tracks only the latest mutation; the counted hold keeps the spinner up until the last concurrent upload settles. Cancel is gated separately above. */
          uploading={uploadMutation.isPending || sendHeld}
          holdSend={sendHeld}
          onDrop={dropTargetProps.onDrop}
          onDragOver={dropTargetProps.onDragOver}
          onDragLeave={dropTargetProps.onDragLeave}
        />
        </Composer>
        </div>
        </div>

        <ChatPaneAgentMenu paneSlot={paneSlot} models={pickerModels} actions={pickerActions} navigate={navigate} />

        <ChatPaneModelMenu slotKey={slotKey} paneSlot={paneSlot} models={pickerModels} actions={pickerActions} navigate={navigate} />

      </div>
    </SlotProvider>
  )
}
