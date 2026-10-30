import type { NavigateFunction } from 'react-router-dom'
import { useRef, useEffect, useMemo, useState, useCallback } from 'react'
import { toolDisclosureKey } from '../transcriptRenderers'
import { useAppSelector } from '../../../store'
import { selectComposerBusy, selectProjectChipLocked, selectSlotSubagentsActive, pendingQuestionFor, type FollowupItem } from '../../../store/chatSlice'
import type { TurnItem } from '../types'
import { messageRowKey } from '../ChatPageMessageContent'
import { devWatchMessages, inspectorOn, devLog } from '../../../dev/scrollInspector'
import { type PromptHistoryItem, promptHistoryFromMessages, samePromptHistory } from '../../../components/composerPromptHistory'
import { countCompletedTurns } from '../../../lib/completedTurns'
import { runBelongsToSlot } from '../../../apps/workflows/runModel'
import { useTipTrigger } from '../../../components/TipCard'
import { useKnowledgeFetch } from '../useKnowledgeFetch'
import { closeCrewWindow, useCrewWindow } from '../crew-window/crewWindowStore'
import { useGuideRevealScope } from '../../../guide/GuideRevealScope'
import { slotRowByKey } from '../../../store/slotIndex'
import { crewRootOf } from '../../../lib/crewMode'
import { useEnvironmentStartLabel } from '../../../components/sessionEnvironmentHooks'

// Stable identity for the "no follow-up cards" case: returning a fresh {} from
// the selector would make it a new reference on every store update.
const EMPTY_FOLLOWUPS: Record<string, { items: FollowupItem[]; ts: number }> = {}

interface ActiveSlotStateOptions {
  embedded?: boolean
  embedMode?: 'chat' | 'sessions'
  popout?: boolean
  navigate: NavigateFunction
  searchParams: URLSearchParams
  activeSlot: string | null
}

/**
 * The active session's store-backed state the page renders from: its messages and
 * prompt history, run / stop / load flags, the cards waiting above the composer, the
 * turn and tool-call disclosure the virtualised rows cannot keep themselves, the ambient
 * tip, split-view and crew-window state, context usage, the activity panel's open flag,
 * the older-history cursor, and the session history list.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useActiveSlotState({
  embedded, embedMode, popout, navigate, searchParams, activeSlot,
}: ActiveSlotStateOptions) {
  const messages = useAppSelector(s => s.chat.messages)
    const probeServerTotal = useAppSelector(s => (activeSlot ? (s.chat.slotServerTotal?.[activeSlot] ?? -1) : -1))
  const probePrevMsgsRef = useRef(-1)
  useEffect(() => {
    devWatchMessages(messages.length, probeServerTotal)
    const prev = probePrevMsgsRef.current
    probePrevMsgsRef.current = messages.length
    if (prev >= 0 && messages.length !== prev && inspectorOn()) {
      const d = messages.length - prev
      devLog(d > 0 ? 'MSGS+' : 'MSGS-', `${prev}->${messages.length} (${d > 0 ? '+' : ''}${d}) total=${probeServerTotal}`)
    }
  }, [messages.length, probeServerTotal])
  const messagesRef = useRef(messages)
  messagesRef.current = messages
  const kiroCrewVersion = useAppSelector(s => s.dashboard.status?.version) || ''
  // Count COMPLETED back-and-forths (one user message answered by an assistant
  // reply), not raw assistant-role messages — see countCompletedTurns for why a
  // plain assistant-message tally over-counts. Extracted to a pure helper so the
  // counting rule is unit-tested directly (completedTurns.test.ts).
  const completedTurnCount = useMemo(() => countCompletedTurns(messages), [messages])
  const knowledgeFetch = useKnowledgeFetch(activeSlot)
  const knowledgeFetchRef = useRef(knowledgeFetch)
  knowledgeFetchRef.current = knowledgeFetch
  // User-sent messages (oldest → newest) for ↑/↓ prompt history in the input.
  // Deduplicate consecutive identical prompts to match shell/REPL behavior.
  // `messages` gets a new reference on every streaming chunk; preserve the
  // previous array when user-message content is unchanged so `sentMessages`
  // stays referentially stable and doesn't re-run downstream effects.
  const sentMessagesRef = useRef<PromptHistoryItem[]>([])
  const sentMessagesSlotRef = useRef<string | null>(null)
  // Per-slot timestamp (ms) of the last soft-stop press, used to arm the
  // force-kill. A force press (second click while soft_pending) arriving
  // within FORCE_KILL_ARMING_MS of that slot's soft stop is treated as an
  // accidental rapid double-tap and ignored, so users can't hard-kill by
  // mashing Stop. Keyed by slot so switching slots can't measure one slot's
  // press against another slot's timestamp.
  const softStopAtMapRef = useRef<Map<string, number>>(new Map())
  const sentMessages = useMemo(() => {
    const out = promptHistoryFromMessages(messages)
    // Reset the cached reference when switching slots — otherwise two
    // conversations with matching length+tail would share the prior array.
    if (sentMessagesSlotRef.current !== activeSlot) {
      sentMessagesSlotRef.current = activeSlot ?? null
      sentMessagesRef.current = out
      return out
    }
    // Append-only within a slot — full element-wise compare (array is small).
    const prev = sentMessagesRef.current
    if (samePromptHistory(prev, out)) {
      return prev
    }
    sentMessagesRef.current = out
    return out
  }, [messages, activeSlot])
  const slotRunning = useAppSelector(s => s.chat.slotRunning)
  // Live mirror for `autoFollowAllowed`, a stable callback several effects
  // depend on: taking `slotRunning` as a dependency would re-attach those
  // observers on every turn boundary.
  const slotRunningRef = useRef(!!slotRunning)
  slotRunningRef.current = !!slotRunning
  // A first remote turn spends its opening minute(s) creating compute; the
  // turn indicator names that stage instead of "Thinking…".
  // A reply on record means this session's environment connected before, so a
  // later engine restart is a reconnect, not setup.
  const sessionHasReplied = useMemo(() => messages.some(m => m.role === 'assistant'), [messages])
  const environmentStart = useEnvironmentStartLabel(activeSlot, slotRunning, sessionHasReplied)
  // Turn disclosure ("N tool calls" / "Worked through N steps"), keyed by the
  // virtualizer's stable row key. This lives HERE rather than in TurnBlock
  // because the transcript is virtualised: a row is unmounted once it leaves
  // the mounted window, which streaming does routinely as it scrolls content
  // past, and row-local state would be destroyed every time. An entry exists
  // only for a turn the user has explicitly toggled; absent means "use the
  // default", so the automatic collapse-on-completion is untouched.
  const [turnDisclosure, setTurnDisclosure] = useState<Record<string, boolean>>({})
  const setTurnDisclosureFor = useCallback((key: string, expanded: boolean) => {
    setTurnDisclosure(prev => (prev[key] === expanded ? prev : { ...prev, [key]: expanded }))
  }, [])
  // Same problem, same shape, for the per-tool-call pill (ToolCallLine): its
  // expanded panel is also row-local and also dies when the virtualizer
  // recycles the row. Keyed by the pill's own message key.
  const [toolDisclosure, setToolDisclosure] = useState<Record<string, boolean>>({})
  const setToolDisclosureFor = useCallback((key: string, expanded: boolean) => {
    setToolDisclosure(prev => (prev[key] === expanded ? prev : { ...prev, [key]: expanded }))
  }, [])
  // A pill the user opened may still report itself live, so a closed settled
  // fold keeps it mounted (TurnBlock `mayBeLive`); keyed exactly as the
  // transcript renderer keys the pill's own disclosure.
  const turnRowMayBeLive = useCallback((it: TurnItem) =>
    it.kind === 'single' && it.msg.role === 'tool' && toolDisclosure[toolDisclosureKey(it.msg, messageRowKey(it.msg, it.idx))] === true,
  [toolDisclosure])
  // Row keys are only unique within a slot, so carrying them across a slot
  // switch would apply one session's choices to another's turns.
  useEffect(() => { setTurnDisclosure({}); setToolDisclosure({}) }, [activeSlot])
  // Shared composer-busy rule (chatSlice.selectComposerBusy). Drives the
  // composer's busy/queue affordance so a message sent during a sub-agent run
  // reads as "will queue".
  const composerBusy = useAppSelector(s => selectComposerBusy(s, s.chat.activeSlot))
  // Any attached sub-agent locks the project chip (see selectProjectChipLocked).
  const projectLocked = useAppSelector(s => selectProjectChipLocked(s, s.chat.activeSlot))
  const slotStopping = useAppSelector(s => s.chat.slotStopping)
  const slotLoading = useAppSelector(s => s.chat.slotLoading)
  // While a session-switch history fetch is still in flight for the active
  // slot, this equals activeSlot (even during the cached-provisional window
  // where slotLoading is already false). Used to defer the session-pulse
  // survey's baseline capture until the real transcript has settled.
  const slotSwitchTarget = useAppSelector(s => s.chat.slotSwitchTarget)
  const pendingQuestion = useAppSelector(s => pendingQuestionFor(s.chat.pendingQuestions, s.chat.activeSlot))
  const restoredQuestionNotice = useAppSelector(s => (s.chat.activeSlot ? s.chat.restoredQuestionNotices?.[s.chat.activeSlot] : undefined))
  const pendingFollowup = useAppSelector(s => (s.chat.activeSlot ? s.chat.followups?.[s.chat.activeSlot] : undefined))
  const folderSuggestion = useAppSelector(s => (s.chat.activeSlot ? s.chat.folderSuggestions?.[s.chat.activeSlot] : undefined))
  const followupTsBySlot = useAppSelector(s => s.chat.followups) ?? EMPTY_FOLLOWUPS
  // The ambient tip yields to functional surfaces that own the above-composer band
  const tipSuppressed = useAppSelector(s =>
    s.chat.messages.some(m => m.role === 'queued') ||
    // Question card only renders for its OWNING slot (see the render-site
    // slot check in ChatPage) -- suppression must match, or a question pending in
    // another running slot suppresses tips here forever.
    !!pendingQuestionFor(s.chat.pendingQuestions, s.chat.activeSlot) ||
    // The follow-up card occupies the same above-composer band. Cards are
    // slot-keyed, so read only the ACTIVE slot's entry — a card parked in
    // another session must not suppress tips here.
    (!!s.chat.activeSlot && !!s.chat.followups?.[s.chat.activeSlot]) ||
    // The folder-suggestion card takes the same slot inside the composer box the
    // tip does, and it can land on the FIRST turn — exactly when a tip is most
    // likely to be offered. It is actionable and one-shot where the tip is
    // ambient and re-offered, so the tip yields. Slot-keyed like the follow-up
    // card, so a card parked in another session must not suppress tips here.
    (!!s.chat.activeSlot && !!s.chat.folderSuggestions?.[s.chat.activeSlot]) ||
    // Active subagents render the progress bar in the same above-composer
    // zone the floating tip occupies — the tip always yields: never crowd
    // the queue/subagent surfaces.
    (!!s.chat.activeSlot && selectSlotSubagentsActive(s, s.chat.activeSlot)) ||
    // Workflow runs render WorkflowProgressBar in the same band — but only
    // runs belonging to THIS slot show a bar here, so filter by ownership or
    // a terminal run parked in another slot would suppress tips everywhere
    // forever.
    Object.values(s.chat.workflowRuns ?? {}).some(r => runBelongsToSlot(r.sessionKey, s.chat.activeSlot) && (r.status === 'running' || r.status === 'finished' || r.status === 'failed' || r.status === 'cancelled'))
  ) || knowledgeFetch.loading || knowledgeFetch.results.length > 0
  // Split View state is declared up here (not at its usage site) because the
  // tip hook below must know about it: in split mode SessionGridView replaces
  // the composer, TipCard never renders, and an unblocked hook would fetch a
  // tip + record it as shown, silently burning the 6h cadence.
  const [splitMode, setSplitMode] = useState(false)
  /**
   * Run by ChatSidebar's `onSelectSlot`. Stable BY CONTRACT, not by
   * convenience: `ChatSidebar` is wrapped in `memo`, and an inline arrow here
   * makes that memo bail on EVERY ChatPage render. ChatPage re-renders once per
   * frame while anything is streaming (`useWebSocket` batches chunks per rAF
   * and this page subscribes to the whole `chat.messages`), so an unstable
   * identity re-rendered the entire sidebar ~60 times across the 0.32s mobile
   * drawer slide — on the same main thread that drives the slide's transform.
   * Keep every prop handed to ChatSidebar referentially stable.
   */
  const clearSplitOnSelect = useCallback(() => { setSplitMode(false); closeCrewWindow() }, [])
  // A create that activated its new session leaves Split View, the rule the
  // sidebar's New already follows through `onSelectSlot` (#21935). The grid
  // seeds from the active slot only when empty, so the new chat would
  // otherwise show nowhere. Keyed on the store's record of the activation, so
  // the creates that never call `onSelectSlot` (the new-chat shortcut, the
  // command palette, the command bar, the collapsed sidebar's flyout) follow
  // it too. A background create or one the user switched away from records no
  // activation, so it keeps the split. The value present at mount is not a
  // new activation: Split View starts off, and an auto-enter must not be undone.
  const lastCreatedActivation = useAppSelector(s => s.chat.lastCreatedActivation)
  const seenActivationRef = useRef(lastCreatedActivation)
  useEffect(() => {
    if (lastCreatedActivation === seenActivationRef.current) return
    seenActivationRef.current = lastCreatedActivation
    if (lastCreatedActivation) setSplitMode(false)
  }, [lastCreatedActivation])
  // A crew session opens as a window over the pane (see CrewChatWindow); a
  // local session taking focus closes it. Only a MOVE between two local
  // sessions counts, so the reload restore (none -> restored) keeps it open.
  const storeCrewWindow = useCrewWindow()
  // An embedded chat frame shares this tab's sessionStorage, so it shows a
  // crew window only when its own URL names one (the embedded Sessions list
  // navigates here with `?crew=&key=`); never the tab's stored one.
  const embedCrew = searchParams.get('crew')
  const embedCrewKey = searchParams.get('key')
  const crewWindow = useMemo(
    () => (embedded
      ? (embedMode === 'chat' && embedCrew && embedCrewKey ? { instanceId: embedCrew, key: embedCrewKey } : null)
      : popout ? null : storeCrewWindow),
    [embedded, embedMode, popout, embedCrew, embedCrewKey, storeCrewWindow],
  )
  const openEmbeddedCrewWindow = useCallback((instanceId: string, key: string) => navigate(
    `/embed/chat?crew=${encodeURIComponent(instanceId)}&key=${encodeURIComponent(key)}`), [navigate])
  const prevActiveSlotRef = useRef(activeSlot)
  useEffect(() => {
    if (prevActiveSlotRef.current && activeSlot !== prevActiveSlotRef.current) closeCrewWindow()
    prevActiveSlotRef.current = activeSlot
  }, [activeSlot])
  // A Crew Mode worker opened from its root's Workers panel leaves Split View, as
  // a sidebar pick does: the grid never shows a worker, so it would hide it.
  const activeIsCrewWorker = useAppSelector(s => !!crewRootOf(s.dashboard.slots.find(sl => sl.key === s.chat.activeSlot)))
  useEffect(() => { if (activeIsCrewWorker) setSplitMode(false) }, [activeIsCrewWorker, activeSlot])
  /** Same contract as `clearSplitOnSelect`, for the `embedMode === 'sessions'`
   *  frame where the sidebar IS the whole page. */
  const navigateToEmbeddedSlot = useCallback((key: string) => navigate(`/embed/chat/${key}`), [navigate])
  const [splitAnchor, setSplitAnchor] = useState<string | null>(null)
  // Temporary sessions ("no memory reads or writes") must never show
  // memory-personalized tips.
  const tipTemporary = useAppSelector(s => slotRowByKey(s.dashboard.slots, s.chat.activeSlot)?.memory_mode === 'temporary')
  const tipBlocked = tipTemporary || splitMode || embedMode === 'sessions'
  const { tip: activeTip, dismiss: dismissTip } = useTipTrigger(!!slotRunning, tipSuppressed, activeSlot, tipBlocked)
  const slotState = useAppSelector(s => s.chat.slotState)
  const contextPct = useAppSelector(s => s.chat.slotContextPct[s.chat.activeSlot ?? ''] ?? 0)
  const contextTokens = useAppSelector(s => s.chat.slotContextTokens?.[s.chat.activeSlot ?? ''])
  // Length only. The two arrays themselves are mutated per streamed sub-agent /
  // tool chunk, and their only consumer is the Activity panel (SidePanel), which
  // is closed by default and now subscribes to them itself. Subscribing to the
  // arrays here re-rendered this whole component per chunk for data it never
  // read.
  const activityOpen = useAppSelector(s => s.chat.activityOpen)
  // The chat side panel's scope owner (`chat.side-panel`): a guide's step
  // through its opener completes the moment it reads open.
  useGuideRevealScope('chat.side-panel', activityOpen)
  const slotHasMore = useAppSelector(s => s.chat.slotHasMore)
  const slotOldestIndex = useAppSelector(s => s.chat.slotOldestIndex)
  const cursorIsForActiveSlot = useAppSelector(s => s.chat.slotCursorKey === s.chat.activeSlot)
  const loadingOlder = useAppSelector(s => s.chat.loadingOlder)
  const olderFailed = useAppSelector(s => s.chat.slotOlderError)
  // switchSlot.pending seeds the active view from the pane cache, which for a
  // background pane is a BOUNDED page; the record is present only while it is.
  const activeViewIsBoundedPage = useAppSelector(s => activeSlot ? s.chat.slotPaneBounded?.[activeSlot] !== undefined : false)
  const history = useAppSelector(s => s.chat.history)
  const historyHasMore = useAppSelector(s => s.chat.historyHasMore)
  return {
    messages, messagesRef, kiroCrewVersion, completedTurnCount, knowledgeFetch, knowledgeFetchRef, softStopAtMapRef, sentMessages, slotRunning, slotRunningRef, turnDisclosure, setTurnDisclosureFor,
    toolDisclosure, setToolDisclosureFor, turnRowMayBeLive, composerBusy, projectLocked, slotStopping, slotLoading, slotSwitchTarget, pendingQuestion, restoredQuestionNotice, pendingFollowup,
    folderSuggestion, followupTsBySlot, splitMode, setSplitMode, clearSplitOnSelect, crewWindow, openEmbeddedCrewWindow, navigateToEmbeddedSlot, splitAnchor, setSplitAnchor, activeTip, dismissTip,
    slotState, contextPct, contextTokens, activityOpen, slotHasMore, slotOldestIndex, cursorIsForActiveSlot, loadingOlder, olderFailed, activeViewIsBoundedPage, history, historyHasMore, environmentStart,
  }
}
