import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import type { useSearchParams } from 'react-router-dom'

import { api } from '../../api/client'
import type { AutoNudgeLoop } from '../../components/AutoNudgePopover'
import type { KiroCrewAgent } from '../../components/AgentSelector'
import type { ModelInfo } from '../../providers/types'
import { useAgents } from '../../hooks/useAgents'
import { useAvailableModels } from '../../hooks/useAvailableModels'
import { useFilteredDropdown } from '../../hooks/useFilteredDropdown'
import { useListboxKeyboard } from '../../hooks/useListboxKeyboard'
import { usePushToTalk } from '../../hooks/usePushToTalk'
import { useRemoteCapabilities } from '../../hooks/useRemoteCapabilities'
import { useVoiceInput, voiceInputSupported, type TranscriptOrigin } from '../../hooks/useVoiceInput'
import { i18nT } from '../../i18n/t'
import { providerLabel } from '../../lib/sttProviders'
import type { AppDispatch } from '../../store'
import { useAppSelector } from '../../store'
import { addNotification } from '../../store/notificationsSlice'
import { createSlot, resolveOptimisticSteer, setPendingInput, switchSlot } from '../../store/chatSlice'
import { triggerRefresh } from '../../store/dashboardSlice'
import type { ChatMessage, ChatSlot, SessionInfo } from '../../types'
import {
  DRAFT_SAVE_DEBOUNCE_MS,
  loadDrafts,
  saveDrafts as persistDrafts,
  setDraft,
} from '../../utils/chatDrafts'
import {
  loadFileDrafts,
  saveFileDrafts as persistFileDrafts,
  setFileDraft,
} from '../../utils/chatFileDrafts'
import {
  loadPasteDrafts,
  savePasteDrafts as persistPasteDrafts,
  setPasteDraft,
} from '../../utils/chatPasteDrafts'
import {
  loadSessionRefDrafts,
  saveSessionRefDrafts as persistSessionRefDrafts,
  setSessionRefDraft,
} from '../../utils/chatSessionRefDrafts'
import {
  consumeChatHandoff,
  handoffToChat,
  persistClaimedChatHandoffs,
  subscribeChatHandoff,
} from '../../utils/errorReport'
import { parseDirTokens } from '../../utils/fileTokens'
import { PREFILL_STORAGE_KEY, writePrefill } from '../../utils/navIntent'
import { type PasteBlock } from '../../utils/pasteTokens'
import type { ResizeInfo } from '../../utils/resizeImage'
import { safeSetSessionItem } from '../../utils/safeStorage'
import {
  addSessionRef,
  removeSessionRef,
  type SessionRef,
} from '../../utils/sessionRefs'
import { extractPromptFromToken, extractSlackContextFromToken } from '../../utils/tokenPrompt'
import { loadChatConfig, type ChatConfig } from './ChatSettings'
import { useKnowledgeFetch } from './useKnowledgeFetch'
import { useScrollManager } from './useScrollManager'

/**
 * Human-readable reason from a rejected thunk. `unwrap()` rejects with RTK's
 * SERIALIZED error — a plain object, never an `Error` instance — so an
 * `instanceof Error` test always fails and every user would read the developer
 * fallback. Read `message` structurally instead, with a plain-language fallback.
 */
/** Unique `ts` for a client-side notification that the feed can still PARSE.
 *  `addNotification` dedupes on `ts`, so two entries in the same millisecond would
 *  see the second silently dropped — which for a payload-carrying entry discards
 *  the user's message. The disambiguator goes in FRACTIONAL digits because
 *  `parseTs` only accepts `\d+(\.\d+)?`; a `<ms>-<n>` form falls through to
 *  `new Date(string)`, which is Invalid Date in V8 → "Invalid Date" headers and
 *  "NaNd ago" in the bell feed. */
let notificationTsSeq = 0
export const uniqueNotificationTs = (): string => `${Date.now()}.${notificationTsSeq++}`

export const createFailReason = (e: unknown): string => {
  const msg = typeof e === 'object' && e !== null ? (e as { message?: unknown }).message : undefined
  // The fallback is pinned to the English source string and lowercased, because
  // this text is concatenated into a larger sentence: a locale switch must not
  // change either the language or the casing of a rejected send's reason.
  if (typeof msg === 'string' && msg.trim()) return msg
  const fallback = i18nT('pages.chatPage.server_did_not_respond', { lng: 'en' })
  return fallback.charAt(0).toLowerCase() + fallback.slice(1)
}

export interface UseChatPageComposerControllerOptions {
  activeSlot: string | null
  connected: boolean
  dispatch: AppDispatch
  embedded?: boolean
  history: SessionInfo[]
  knowledgeFetch: ReturnType<typeof useKnowledgeFetch>
  messages: ChatMessage[]
  mode?: string
  pendingQuestion: unknown
  refreshTrigger: number
  searchParams: URLSearchParams
  setSearchParams: ReturnType<typeof useSearchParams>[1]
  slotLoading: boolean
  slotRunning: boolean
  slots: ChatSlot[]
  splitMode: boolean
}

export function useChatPageComposerController({
  activeSlot,
  connected,
  dispatch,
  embedded,
  history,
  knowledgeFetch,
  messages,
  mode,
  pendingQuestion,
  refreshTrigger,
  searchParams,
  setSearchParams,
  slotLoading,
  slotRunning,
  slots,
  splitMode,
}: UseChatPageComposerControllerOptions) {
  const knowledgeFetchRef = useRef(knowledgeFetch)
  knowledgeFetchRef.current = knowledgeFetch
  const drafts = useRef<Record<string, string>>(null!)
  if (drafts.current === null) drafts.current = loadDrafts()
  const fileDrafts = useRef<Record<string, string[]>>(null!)
  if (fileDrafts.current === null) fileDrafts.current = loadFileDrafts()
  // Per-slot collapsed-paste blocks backing the `[ Paste #N · M lines ]` tokens
  // in `input`. Persisted (localStorage, same TTL as text drafts) so the chip
  // survives slot switches / refresh instead of degrading to literal text.
  const pasteDrafts = useRef<Record<string, PasteBlock[]>>(null!)
  if (pasteDrafts.current === null) pasteDrafts.current = loadPasteDrafts()
  // Per-slot session references staged by dragging a session onto this pane.
  // Persisted (sessionStorage) so a slot switch restores the refs belonging to
  // the slot being shown — which is also what stops one slot's staged refs from
  // smearing onto another.
  const sessionRefDrafts = useRef<Record<string, SessionRef[]>>(null!)
  if (sessionRefDrafts.current === null) sessionRefDrafts.current = loadSessionRefDrafts()
  const saveDraftsTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const saveDrafts = useCallback(() => { persistDrafts(drafts.current); persistFileDrafts(fileDrafts.current); persistPasteDrafts(pasteDrafts.current); persistSessionRefDrafts(sessionRefDrafts.current) }, [])
  const saveDraftsDebounced = useCallback(() => {
    if (saveDraftsTimer.current) clearTimeout(saveDraftsTimer.current)
    saveDraftsTimer.current = setTimeout(() => { saveDraftsTimer.current = null; saveDrafts() }, DRAFT_SAVE_DEBOUNCE_MS)
  }, [saveDrafts])
  const flushDrafts = useCallback(() => {
    if (saveDraftsTimer.current) { clearTimeout(saveDraftsTimer.current); saveDraftsTimer.current = null }
    saveDrafts()
  }, [saveDrafts])
  // Outgoing-slot flush key, advanced inside the slot-change effect after it
  // flushes that slot's draft. Distinct from composerSlotRef (the live persist
  // key); both must trail their writes or the draft smear returns.
  const prevSlot = useRef<string | null>(null)
  // Latest-value ref for `activeSlot`, updated every render. Used by async
  // upload callbacks (takeScreenshot, uploadFiles) to detect when the user
  // has switched slots between the initial click and the promise resolving,
  // so the uploaded file lands in the original slot's draft instead of
  // silently appearing in whatever slot is now active.
  const activeSlotRef = useRef(activeSlot); activeSlotRef.current = activeSlot
  // The slot the live composer state belongs to; the per-composer persist
  // effects key off this, not `activeSlot`. Advanced by a dedicated effect
  // declared AFTER those effects so a batched keystroke+switch can't smear one
  // slot's draft onto another. See that advance effect for the full rationale.
  const composerSlotRef = useRef(activeSlot)
  const [input, setInput] = useState(() => activeSlot ? drafts.current[activeSlot] ?? '' : '')

  // History suggestions ("Continue a previous chat?") shown above the input on the welcome screen.
  const sendingRef = useRef(false)
  const [historyQuery, setHistoryQuery] = useState('')
  const [historyDismissed, setHistoryDismissed] = useState(false)
  useEffect(() => {
    const q = input.trim()
    if (!q) { setHistoryQuery(''); setHistoryDismissed(false); return }
    setHistoryDismissed(false)
    const t = setTimeout(() => setHistoryQuery(q.toLowerCase()), 300)
    return () => clearTimeout(t)
  }, [input])
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
  const pendingInput = useAppSelector(s => s.chat.pendingInput)

  const [chatConfig, setChatConfig] = useState<ChatConfig>(loadChatConfig)
  useEffect(() => {
    const reload = () => { const next = loadChatConfig(); setChatConfig(prev => JSON.stringify(prev) === JSON.stringify(next) ? prev : next) }
    window.addEventListener('focus', reload)
    window.addEventListener('mc-config-changed', reload)
    return () => { window.removeEventListener('focus', reload); window.removeEventListener('mc-config-changed', reload) }
  }, [])

  // Project is part of the roster's identity: re-pointing this slot at another
  // project changes which project-scoped agents exist. Derived here rather than
  // from `currentSlot`, which is computed further down the render body.
  const activeSlotProject = slots.find(s => s.key === activeSlot)?.project || undefined
  const { agents: installedAgents, defaultAgent } = useAgents(refreshTrigger, activeSlot ?? undefined, activeSlotProject)
  // Is this session bound to a peer crew for execution, and what does that crew
  // offer? Read once here and threaded into the shelf's pickers below, so every
  // control answers from one source rather than each deciding for itself.
  const remoteCrew = useRemoteCapabilities(slots.find(s => s.key === activeSlot))
  const effectiveAgents = useMemo<KiroCrewAgent[]>(() => {
    if (!remoteCrew.isRemote) return installedAgents
    // The peer's roster carries the four fields its picker renders. The rest of
    // KiroCrewAgent describes bindings that only mean something on the machine
    // that owns them (`kiro_agent`, `workspace`, `memory_store`), so they are
    // filled with the empty value rather than this machine's — a local workspace
    // path shown under a remote crew's name would be a straightforward lie.
    return (remoteCrew.capabilities?.agents ?? []).map(a => ({
      name: a.name,
      kiro_agent: '',
      workspace: '',
      memory_store: '',
      model: a.model || '',
      description: a.description,
      source: a.scope || 'remote',
    }))
  }, [remoteCrew.isRemote, remoteCrew.capabilities, installedAgents])
  // A peer-bound session falls back to the PEER's default, never this machine's:
  // the backend deliberately stores no agent for such a slot (the peer picks), so
  // `defaultAgent` here would advertise a crew from the wrong roster while the
  // peer answered with its own.
  const effectiveDefaultAgent = remoteCrew.isRemote ? (remoteCrew.capabilities?.default_agent || '') : defaultAgent
  const [defaultAgentFailed, setDefaultAgentFailed] = useState(false)
  // Promotes an agent to the global default. Set-only: clearing the default lives on
  // the Agent Templates page, where the control is labelled and the outcome is visible.
  // Refresh goes through the store's global trigger rather than local state, because
  // every open picker (this one, each split pane, the Templates page) reads the same
  // setting — a per-hook refresh would leave sibling pickers showing the old default.
  // api.setDefaultAgent is called defensively: component tests mock the api module
  // partially, so the method can be absent under test.
  const toggleDefaultAgent = useCallback((name: string) => {
    setDefaultAgentFailed(false)
    Promise.resolve(api.setDefaultAgent?.(name))
      .then(() => dispatch(triggerRefresh()))
      .catch(() => setDefaultAgentFailed(true))
  }, [dispatch])
  const { open: agentDropdown, setOpen: setAgentDropdown, filter: agentFilter, setFilter: setAgentFilter, dropdownRef: agentDropdownRef, inputRef: agentInputRef, filtered: filteredAgentsByName } = useFilteredDropdown(effectiveAgents)
  const filteredAgents = filteredAgentsByName
  const localModels = useAvailableModels()
  // A peer-bound session's shelf must offer the PEER's rosters. Both hooks above
  // read THIS machine same-origin, so a remote session left on them would list
  // crews and models that do not exist over there — accepted by the picker, then
  // refused on the first send. Substituted rather than merged: the union would let
  // the user pick a local-only model and could not say which side it came from.
  //
  // While the capability read is in flight the lists are EMPTY, not local: a brief
  // empty picker is honest, whereas briefly showing this machine's models for a
  // remote session invites exactly the wrong pick.
  const effectiveModels = useMemo<ModelInfo[]>(() => {
    if (!remoteCrew.isRemote) return localModels
    return (remoteCrew.capabilities?.models ?? []).map(m => ({
      name: m.model_name,
      description: m.description || m.display_name,
      contextWindow: m.context_window || undefined,
    }))
  }, [remoteCrew.isRemote, remoteCrew.capabilities, localModels])
  const availableModels = effectiveModels
  const { open: modelDropdown, setOpen: setModelDropdown, filter: modelFilter, setFilter: setModelFilter, dropdownRef: modelDropdownRef, inputRef: modelInputRef, filtered: filteredModels } = useFilteredDropdown(availableModels)
  // Agent/model switching is declared later in ChatPage today. These refs keep
  // the dropdown keyboard handlers at their original declaration point without
  // forcing those later page callbacks into this controller.
  const switchAgentRef = useRef<((name: string) => void) | null>(null)
  const switchModelRef = useRef<((name: string) => void) | null>(null)

  // Roving-focus keyboard nav for the agent + model dropdowns (shared with StyledSelect/AgentSelector).
  const { onListKeyDown: onAgentListKeyDown } = useListboxKeyboard({
    open: agentDropdown,
    dropdownRef: agentDropdownRef,
    inputRef: agentInputRef,
    hasFilterInput: true,
    filteredCount: filteredAgents.length,
    onEnterSingleMatch: () => {
      const a = filteredAgents[0]
      if (a) { switchAgentRef.current?.(a.name); setAgentDropdown(false) }
    },
    closeToTrigger: () => setAgentDropdown(false),
  })
  const { onListKeyDown: onModelListKeyDown } = useListboxKeyboard({
    open: modelDropdown,
    dropdownRef: modelDropdownRef,
    inputRef: modelInputRef,
    hasFilterInput: true,
    filteredCount: filteredModels.length,
    onEnterSingleMatch: () => { switchModelRef.current?.(filteredModels[0].name); setModelDropdown(false) },
    closeToTrigger: () => setModelDropdown(false),
  })
  const [pendingAgent, _setPendingAgent] = useState('')  // agent for next new slot
  const pendingAgentRef = useRef('')
  const setPendingAgent = useCallback((v: string) => { pendingAgentRef.current = v; _setPendingAgent(v) }, [])
  const [pendingModel, _setPendingModel] = useState('')  // model for next new slot
  const pendingModelRef = useRef('')
  const setPendingModel = useCallback((v: string) => { pendingModelRef.current = v; _setPendingModel(v) }, [])
  const pendingProjectRef = useRef('')
  const setPendingProject = useCallback((v: string) => { pendingProjectRef.current = v }, [])

  // pendingModel is the model for the NEXT new slot, and it is deliberately
  // left EMPTY unless the user explicitly picks one (switchModel below).
  //
  // It used to be seeded at mount from the backend resolver. That resolver
  // answers "what would run", which is right for the composer chip but wrong as
  // a session-create value: a session's model is a permanent pin (the runtime
  // reads `slot.model or agent_model`, so a set slot.model wins for every later
  // turn). Seeding it pinned every new chat to whatever the four-tier chain
  // happened to resolve at page load, so an agent left on Auto never
  // re-resolved and later changes to the agent or the global default never
  // reached the session (#2035).
  //
  // Sending nothing is what preserves the chain. `SessionManager.get_or_create`
  // documents that a `None` model "falls back to the global agent.model config
  // -- but only when the named agent does not pin its own model ... and the
  // global is not a sentinel value like 'auto', in which case it stays None to
  // let the backend resolve from the agent's own JSON config". So omitting it
  // honours the crew pin, the template pin, the global default and Auto, in that
  // order, at session-create time.
  //
  // Sending the literal 'auto' would NOT be equivalent: it is truthy, so it
  // short-circuits `slot.model or agent_model` and would override a template or
  // global pin the user did configure.
  const [modelBtnRect, setModelBtnRect] = useState<DOMRect | null>(null)
  // NOT fire-and-forget: the receipt is the only thing that knows whether the
  // text reached the running turn, and the optimistic bubble asserts that it did.
  const steerMutation = useMutation({
    mutationFn: ({ text, sendId, slot }: { text: string; sendId?: string; slot: string }) => api.steerChat(text, slot, sendId),
    // eslint-disable-next-line no-console -- no toast for a rejected steer, which is otherwise indistinguishable from one the agent ignored
    onError: (e) => { console.error('steer failed', e) },
    onSuccess: (body, { sendId, slot }) => {
      if (!sendId || !slot) return
      const receipt = (body ?? {}) as { ok?: boolean; steered?: boolean; queued?: boolean }
      // `steered` is the one shape the badge's claim is true for; an unreadable
      // body confirms nothing, so it must not rewrite the bubble either.
      if (receipt.steered || !receipt.ok) return
      dispatch(resolveOptimisticSteer({ slot, sendId, outcome: receipt.queued ? 'queued' : 'turn' }))
    },
  })
  const [reasoningEffortDropdown, setReasoningEffortDropdown] = useState(false)
  const [reasoningEffortBtnRect, setReasoningEffortBtnRect] = useState<DOMRect | null>(null)
  const reasoningEffortDropdownRef = useRef<HTMLDivElement>(null)
  const [autoNudgeOpen, setAutoNudgeOpen] = useState(false)
  const [autoNudgeLoop, setAutoNudgeLoop] = useState<AutoNudgeLoop | null>(null)
  const approvalMode = useAppSelector(s => s.dashboard.approvalMode)

  // ── Reasoning effort dropdown click-outside ──
  useEffect(() => {
    if (!reasoningEffortDropdown) return
    const handler = (e: MouseEvent) => {
      if (reasoningEffortDropdownRef.current?.contains(e.target as Node)) return
      if (reasoningEffortBtnRect) {
        const r = reasoningEffortBtnRect
        if (e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom) return
      }
      setReasoningEffortDropdown(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [reasoningEffortDropdown, reasoningEffortBtnRect])

  // ── Auto-nudge: fetch loop state for active slot, subscribe to WS updates ──
  useEffect(() => {
    // Clear stale state and close the popover on slot switch so it remounts
    // with fresh useState initializers sourced from the new slot's loop.
    // Otherwise the popover's internal message/idleSecs/maxCycles retain
    // values from the previously-active slot and a Start click would arm the
    // wrong nudge on the new session.
    setAutoNudgeLoop(null)
    setAutoNudgeOpen(false)
    if (!activeSlot) return
    let cancelled = false
    fetch(`/api/autonudge/slot/${encodeURIComponent(activeSlot)}`)
      .then(r => r.json())
      .then(d => { if (!cancelled) setAutoNudgeLoop(d.loop || null) })
      .catch(() => {})
    const onEvent = (e: Event) => {
      const detail = (e as CustomEvent).detail as { slot?: string; loop?: AutoNudgeLoop; event?: string }
      if (!detail || detail.slot !== activeSlot) return
      setAutoNudgeLoop(detail.event === 'removed' ? null : (detail.loop ?? null))
    }
    window.addEventListener('autonudge_state', onEvent)
    return () => { cancelled = true; window.removeEventListener('autonudge_state', onEvent) }
  }, [activeSlot])
  const {
    scrollerRef,
    scrollToDisplayIndex,
  } = useScrollManager()

  // Single scroll controller: the virtualizer (`virt`, created below) owns
  // follow + scroll-to-bottom. These refs bridge the early effects/handlers
  // (declared before `virt` in source order) to the virtualizer's API without
  // a temporal-dead-zone hazard — they are populated right after `virt` is
  // created and only read inside callbacks/effects that run post-render.
  // `vGetFollowRef` defaults to "following" so a gate that fires on the very
  // first commit (before the mirror populates it) behaves like the fresh-slot
  // bottom pin it accompanies.
  const vGetFollowRef = useRef<() => boolean>(() => true)
  const vScrollToBottomRef = useRef<(behavior?: ScrollBehavior) => void>(() => {})
  const mountIndexRef = useRef<(index: number) => boolean>(() => false)

  const [prefillHint, setPrefillHint] = useState(false)
  const autoSendRef = useRef<string | null>(null)
  const [autoSendTick, setAutoSendTick] = useState(0)
  const newSessionRef = useRef(false)
  // True while the challenge-redirect token effect is creating/linking its
  // session. Blocks the auto-select effect from switching to a different slot
  // (which would orphan the freshly slack-linked session and break mirroring).
  const tokenConsumingRef = useRef(
    typeof window !== 'undefined' && new URLSearchParams(window.location.search).has('token'),
  )
  const inputRef = useRef(input)
  inputRef.current = input
  // Holds the exact text a widget action pre-filled into the composer, so the
  // eventual user-initiated send can be tagged meta.origin='widget' for
 // forensic attribution. Set on widget pre-fill, consumed
  // and cleared in send(). A genuine from-scratch turn never sets this.
  const widgetPrefillRef = useRef<string | null>(null)
  // Token (`${slotKey}:${ts}`) of the most recently consumed composer prefill.
  // Guards the per-slot draft-restore effect against React.StrictMode's mount
  // double-invoke: the first invoke consumes+removes PREFILL_STORAGE_KEY and
  // seeds the composer, so without this the second invoke would find no stored
  // prefill and reset the composer to the (empty) incoming draft — the artifact
  // companion panel mounts ChatPage fresh, so it hits this double-invoke every
  // first open. See the draft-restore effect below.
  const consumedPrefillRef = useRef<string | null>(null)
  // Error hand-offs are claimed into a component-owned FIFO immediately, even
  // while disconnected. That removes the sessionStorage TTL from the reconnect
  // wait, while the processing flag guarantees only one create/switch sequence
  // can run at a time.
  const errorHandoffQueueRef = useRef<string[]>([])
  const errorHandoffActiveRef = useRef<string | null>(null)
  const errorHandoffActiveDurableRef = useRef(false)
  const errorHandoffProcessingRef = useRef(false)
  const errorHandoffConnectedRef = useRef(connected)
  const errorHandoffModeRef = useRef(mode)
  const errorHandoffMountedRef = useRef(false)
  // Invalidates async processors when this effect lifecycle ends. Mounted alone
  // is insufficient because StrictMode can clean up and re-run effects on the
  // same component instance, reusing every ref while an old create is pending.
  const errorHandoffLifecycleRef = useRef(0)
  const processErrorHandoffsRef = useRef<() => void>(() => {})
  const persistErrorHandoffClaims = useCallback(() => {
    const active = errorHandoffActiveRef.current
    persistClaimedChatHandoffs([
      ...(active && !errorHandoffActiveDurableRef.current ? [active] : []),
      ...errorHandoffQueueRef.current,
    ])
  }, [])
  errorHandoffConnectedRef.current = connected
  errorHandoffModeRef.current = mode

  // Auto-dismiss prefill hint after 10 seconds
  useEffect(() => {
    if (!prefillHint) return
    const t = setTimeout(() => setPrefillHint(false), 10000)
    return () => clearTimeout(t)
  }, [prefillHint])

  const processErrorHandoffs = useCallback(async () => {
    if (
      errorHandoffProcessingRef.current
      || !errorHandoffMountedRef.current
      || !errorHandoffConnectedRef.current
    ) return
    const prompt = errorHandoffQueueRef.current[0]
    if (!prompt) return

    const lifecycle = errorHandoffLifecycleRef.current
    const ownsLifecycle = () => (
      errorHandoffMountedRef.current
      && errorHandoffLifecycleRef.current === lifecycle
    )
    let failureRestageAttempted = false
    const restageFailure = (error: unknown) => {
      failureRestageAttempted = true
      const queued = errorHandoffQueueRef.current
      const restaged = handoffToChat([prompt, ...queued])
      if (restaged) {
        queued.splice(0)
        // Ingress now owns the entire FIFO in one atomic write. Clear the
        // claimed copy only after that write succeeds.
        persistClaimedChatHandoffs([])
      } else {
        // Keep a same-document retry path as well as the unchanged claimed
        // crash copy when sessionStorage rejected the ingress write.
        queued.unshift(prompt)
      }
      dispatch(addNotification({
        ts: uniqueNotificationTs(),
        kind: 'agent',
        priority: 'critical',
        title: i18nT('pages.chatPage.could_not_start_a_new_session'),
        body: i18nT('pages.chatPage.could_not_start_session_message_restored', {
          error: createFailReason(error),
        }),
      }))
    }
    errorHandoffProcessingRef.current = true
    errorHandoffActiveRef.current = prompt
    errorHandoffActiveDurableRef.current = false
    // Persist the complete local FIFO before removing its head. A reload can
    // now recover both the active diagnostic and every prompt waiting behind it.
    persistClaimedChatHandoffs(errorHandoffQueueRef.current)
    errorHandoffQueueRef.current.shift()
    try {
      let slotKey: string
      try {
        const slot = await dispatch(createSlot({ mode: errorHandoffModeRef.current, activate: false })).unwrap()
        if (!slot?.key) throw new Error('the server returned no session')
        slotKey = slot.key
      } catch (e) {
        // Cleanup may already have handed this FIFO to a newer ChatPage. An old
        // rejection must not append a duplicate batch or clear its replacement's
        // crash snapshot.
        if (!ownsLifecycle()) return
        restageFailure(e)
        return
      }

      // A route remount may have re-staged this prompt while createSlot was in
      // flight. The abandoned request may leave an unused server slot, but it
      // must not write shared recovery state or steal focus from its successor.
      if (!ownsLifecycle()) return
      // Seed before switching: the draft-restore effect runs in the same commit
      // as switchSlot.pending and would otherwise overwrite pendingInput with
      // the new slot's empty draft. The keyed prefill survives that race.
      if (!writePrefill(slotKey, prompt)) {
        // Do not acknowledge durability or activate an empty session when the
        // keyed prompt was rejected. Preserve active + queued work together.
        restageFailure(new Error('browser storage is unavailable'))
        return
      }
      // The keyed target-slot prefill is now the durable owner. A reload no
      // longer needs to replay this active prompt, but queued prompts still do.
      errorHandoffActiveDurableRef.current = true
      persistErrorHandoffClaims()
      try {
        // `keepTargetOnMissing`: this slot was JUST created, so a 404 from its
        // detail fetch is a create/fetch race on a slot that exists -- the
        // reducer keeps it selected (with the seeded composer) atomically
        // instead of unwinding to the previous chat (#6309), and this catch
        // stays a no-op rather than patching state back from the caller.
        await dispatch(switchSlot({ key: slotKey, keepTargetOnMissing: true })).unwrap()
      } catch {
        // switchSlot.pending already activated the fresh slot. Its detail fetch
        // may fail independently; keep the seeded composer usable in that slot.
      }
      // Do not dispatch pendingInput after the detail fetch. The keyed prefill
      // seeded the composer when switchSlot.pending activated the slot; a late
      // second write would overwrite anything the user typed during the fetch.
      //
      // The prefill channel is single-slot and the seeded prompt only becomes
      // durable-in-slot when the input commit's persist effect records it under
      // the fresh slot's draft key. Hold this turn (bounded well inside the
      // prefill's 30s staleness window) until one of those in-component signals
      // confirms the seed landed: yielding a single task is not enough — the
      // next handoff's slot switch can outrun the consuming commit, and its
      // outgoing-slot save would then overwrite this slot's draft with the
      // stale empty composer, silently dropping the diagnostic.
      for (let i = 0; i < 300 && ownsLifecycle(); i++) {
        // Seed committed: the persist effect keyed a draft to the fresh slot,
        // or the composer already holds exactly this prompt (a same-text
        // setInput bails out of re-rendering, so no draft write follows).
        if (Object.prototype.hasOwnProperty.call(drafts.current, slotKey)) break
        if (inputRef.current === prompt) break
        // User deliberately moved on; the keyed prefill stays staged for the
        // fresh slot and expires on its own clock.
        if (activeSlotRef.current !== slotKey) break
        await new Promise(resolve => setTimeout(resolve, 10))
      }
    } finally {
      // A newer lifecycle owns the shared claim key after unmount/remount. The
      // stale processor may clean up only its abandoned local promise state.
      if (!ownsLifecycle()) return
      errorHandoffActiveRef.current = null
      errorHandoffActiveDurableRef.current = false
      errorHandoffProcessingRef.current = false
      if (!failureRestageAttempted) persistErrorHandoffClaims()
      // Yield a task between sessions. React gets a commit in which the current
      // slot consumes its keyed prefill before another handoff can replace the
      // single prefill channel and activate the next fresh slot. A create failure
      // deliberately stops here: the atomically re-staged FIFO waits for a later
      // user handoff/remount instead of entering an immediate retry loop.
      if (
        !failureRestageAttempted
        && errorHandoffConnectedRef.current
        && errorHandoffQueueRef.current.length
      ) {
        setTimeout(() => processErrorHandoffsRef.current(), 0)
      }
    }
  }, [dispatch, persistErrorHandoffClaims])
  processErrorHandoffsRef.current = () => { void processErrorHandoffs() }

  // Drain the error hand-off channel ("Ask the agent" on an error surface).
  // sessionStorage rather than Redux because the root ErrorBoundary's button has
  // to work after a hard reload, when the store it would have dispatched to is
  // gone. Claim every prompt synchronously into the local FIFO; processing waits
  // for connection and opens one fresh slot at a time.
  //
  // Two triggers: on mount (arriving from another route, or a full reload) and on
  // the subscription (an error surface inside chat hands off with no route
  // change, so nothing remounts).
  useEffect(() => {
    if (embedded) return
    errorHandoffLifecycleRef.current += 1
    errorHandoffMountedRef.current = true
    const handoffQueue = errorHandoffQueueRef.current
    const drain = () => {
      let prompt: string | null
      while ((prompt = consumeChatHandoff()) !== null) {
        // A repeated click while the same diagnostic is creating/retrying is one
        // retry request, not a request for a duplicate session.
        if (
          prompt !== errorHandoffActiveRef.current
          && !handoffQueue.includes(prompt)
        ) handoffQueue.push(prompt)
      }
      persistErrorHandoffClaims()
      processErrorHandoffsRef.current()
    }
    drain()
    const unsubscribe = subscribeChatHandoff(drain)
    return () => {
      errorHandoffMountedRef.current = false
      errorHandoffLifecycleRef.current += 1
      unsubscribe()
      // Atomically return every nondurable item in original FIFO order. The
      // lifecycle token prevents the abandoned processor from later clearing a
      // newer component's claim or switching its active slot.
      const active = errorHandoffActiveRef.current
      const restaged = [
        ...(active && !errorHandoffActiveDurableRef.current ? [active] : []),
        ...handoffQueue,
      ]
      if (handoffToChat(restaged)) {
        handoffQueue.splice(0)
        errorHandoffActiveRef.current = null
        errorHandoffActiveDurableRef.current = false
        errorHandoffProcessingRef.current = false
        persistClaimedChatHandoffs([])
      }
    }
  }, [embedded, persistErrorHandoffClaims])

  // A disconnected mount still CLAIMS the handoff above. Reconnection only
  // starts its queued network work, so waiting longer than the storage TTL cannot
  // discard the diagnostic.
  useEffect(() => {
    if (!embedded && connected) processErrorHandoffsRef.current()
  }, [embedded, connected, mode])

  // Consume pendingInput from Redux (e.g. from "Chat" button on Projects page)
  useEffect(() => {
    if (pendingInput) {
      dispatch(setPendingInput(null))
      const shouldAutoSend = embedded ? false : searchParams.get('autoSend') === '1'
      const wantNew = embedded ? false : searchParams.get('newSession') === '1'
      if (!embedded && (searchParams.get('prefill') || shouldAutoSend)) setSearchParams({}, { replace: true })
      if (shouldAutoSend) {
        autoSendRef.current = pendingInput
        newSessionRef.current = wantNew
        // Bump the tick, because arming the ref alone is not enough when ChatPage is
        // ALREADY mounted. The send effect's deps are `[send, connected,
        // autoSendTick]`: on a cold navigation, mounting and connecting move
        // `connected` and it fires on its own, but a caller already on /chat only
        // changes the search params. `send`'s identity does move with `activeSlot` --
        // yet a seeder that awaits between activating its slot and setting the pending
        // input (the command bar does, since the switch must land first) puts those in
        // two different renders, and by the render that arms the ref none of the deps
        // change. The prompt would then be neither sent nor visible: this branch is the
        // one that does not fall back to the composer.
        //
        // Harmless on the cold path -- the effect runs, finds `connected` still false,
        // and leaves the ref armed for the real connect. Same remedy the no-slot retry
        // in the send effect already uses for the same reason.
        setAutoSendTick(t => t + 1)
      } else {
        if (activeSlot) { setDraft(drafts.current, activeSlot, pendingInput); saveDraftsDebounced() }
        setInput(pendingInput)
        setPrefillHint(true)
      }
    }
  }, [pendingInput, activeSlot, dispatch, searchParams, setSearchParams, saveDraftsDebounced, embedded])

  // Consume chat launch intent from app-sdk (useChatLauncher writes to window.__mc_chat_launch)
  useEffect(() => {
    const launchWindow = window as Window & {
      __mc_chat_launch?: { ts?: number; agent?: string; message?: string }
    }
    const intent = launchWindow.__mc_chat_launch
    if (!intent || Date.now() - (intent.ts ?? 0) > 10_000) return
    delete launchWindow.__mc_chat_launch
    if (intent.agent) setPendingAgent(intent.agent)
    if (intent.message) { autoSendRef.current = intent.message; newSessionRef.current = true }
    // setPendingAgent is a stable useState setter, so including it keeps this a
    // mount-only "consume the one-shot window global" effect.
  }, [setPendingAgent])

  // Consume ?prefill= — the no-main-window fallback path for navigation
  // intents forwarded from a popout (see utils/popoutController.ts). The
  // fallback opens `/chat?sid=<slot>&prefill=<prompt>` in a fresh tab, which
  // has no sessionStorage of its own yet: seed PREFILL_STORAGE_KEY from the
  // param so the slot-restore effect prefills the composer when the ?sid slot
  // activates, then strip the param (keep ?sid) so the prompt doesn't leak
  // into history/bookmarks or re-seed on refresh.
  useEffect(() => {
    if (embedded) return
    const sp = new URLSearchParams(window.location.search)
    const prefill = sp.get('prefill')
    if (prefill === null) return
    const sid = sp.get('sid') || sp.get('slot')
    if (sid && prefill) {
      safeSetSessionItem(
        PREFILL_STORAGE_KEY,
        JSON.stringify({ slotKey: sid, prompt: prefill, ts: Date.now() }),
      )
    }
    sp.delete('prefill')
    const qs = sp.toString()
    window.history.replaceState({}, '', window.location.pathname + (qs ? `?${qs}` : ''))
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Consume prompt from token payload (channel challenge-and-redirect flow).
  // The prompt is HMAC-signed in the token — server validates the signature
  // and sets the session cookie before the SPA loads. No auto-send — the user
  // must press Enter to confirm.
  //
  // Three cases, driven by signed claims in the token:
  //  1. session_key present → the originating Slack thread is already linked to
  //     a dashboard session; reconnect to THAT session instead of making a new
  //     one (fixes "thread reply spawns a disconnected session").
  //  2. channel + thread_ts present (no session_key) → fresh thread; create a
  //     new session and auto-link it back to that Slack thread so agent
  //     responses flow into the thread.
  //  3. neither → plain new session (e.g. a top-level channel message).
  // In all cases the prompt is seeded via PREFILL_STORAGE_KEY (the channel the
  // slot-restore effect honors) AND set directly once the target slot is
  // active, so the previous slot's draft can't clobber it.
  useEffect(() => {
    // tokenConsumingRef is initialized true when a token is in the URL; every
    // early return below MUST clear it, or the auto-select guard stays engaged
    // for the whole session and blocks slot selection.
    if (embedded) { tokenConsumingRef.current = false; return }
    const token = new URLSearchParams(window.location.search).get('token')
    if (!token) { tokenConsumingRef.current = false; return }
    // Always strip token from URL to prevent leakage via referrer/history
    window.history.replaceState({}, '', window.location.pathname)
    const prompt = extractPromptFromToken(token)
    if (!prompt) { tokenConsumingRef.current = false; return }
    const { sessionKey, channel, threadTs } = extractSlackContextFromToken(token)
    // Backend session keys are history keys (dashboard:chat-…); the frontend
    // slot key is the bare form.
    const targetSlot = sessionKey ? sessionKey.replace(/^dashboard:/, '') : null
    tokenConsumingRef.current = true
    ;(async () => {
     try {
      let slotKey: string | null = null
      if (targetSlot) {
        // Case 1: reconnect to the existing linked session.
        try {
          await dispatch(switchSlot(targetSlot)).unwrap()
          slotKey = targetSlot
        } catch {
          // Session vanished (deleted/expired) — fall back to a new one.
        }
      }
      if (!slotKey) {
        // No targetSlot (or reconnect failed): create the session HERE and,
        // for a fresh thread, slack-link it so responses mirror to Slack.
        try {
          const slot = await dispatch(createSlot({ mode })).unwrap()
          slotKey = slot?.key ?? null
        } catch {
          // ignore — fall back to prefilling the current slot
        }
        // Case 2: auto-link the new session back to the originating thread so
        // responses flow into Slack. Best-effort; failure just leaves it
        // unlinked.
        if (slotKey && channel && threadTs) {
          try { await api.slackLink(slotKey, channel, threadTs) } catch { /* non-fatal */ }
        }
      }
      // We have created/reconnected AND made the target slot active. Critically,
      // clear newSessionRef and pin activeSlot to this slot so send() reuses it
      // on Enter — otherwise send()'s forceNew path would spawn a SECOND,
      // unlinked slot and break Slack mirroring.
      if (slotKey) {
        newSessionRef.current = false
        dispatch(switchSlot(slotKey))
        safeSetSessionItem(
          PREFILL_STORAGE_KEY,
          JSON.stringify({ slotKey, prompt, ts: Date.now() }),
        )
      }
      setInput(prompt)
      setPrefillHint(true)
      autoSendRef.current = prompt
      setAutoSendTick(t => t + 1)
     } finally {
      // Release the auto-select guard once the session is created/linked (or
      // failed), so normal slot selection resumes.
      tokenConsumingRef.current = false
     }
    })()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Persist the composer text against the slot it BELONGS to (composerSlotRef),
  // not the live activeSlot (see the composerSlotRef note above).
  // The draft key is composerSlotRef, which a ref does not need to be a
  // dependency of; the slot-change effect below handles the transition.
  useEffect(() => { inputRef.current = input; const s = composerSlotRef.current; if (s) { setDraft(drafts.current, s, input); saveDraftsDebounced() } }, [input, saveDraftsDebounced])
  // Per-slot draft: save current → restore target (persisted to localStorage)
  useEffect(() => {
    // Re-hydrate from localStorage — only pull in keys we don't already have
    // in-memory, so unflushed drafts from rapid slot switches aren't clobbered.
    const stored = loadDrafts()
    for (const [k, v] of Object.entries(stored)) { if (!(k in drafts.current)) drafts.current[k] = v }
    const storedFiles = loadFileDrafts()
    for (const [k, v] of Object.entries(storedFiles)) { if (!(k in fileDrafts.current)) fileDrafts.current[k] = v }
    const storedPastes = loadPasteDrafts()
    for (const [k, v] of Object.entries(storedPastes)) { if (!(k in pasteDrafts.current)) pasteDrafts.current[k] = v }
    const storedSessionRefs = loadSessionRefDrafts()
    for (const [k, v] of Object.entries(storedSessionRefs)) { if (!(k in sessionRefDrafts.current)) sessionRefDrafts.current[k] = v }
    if (prevSlot.current) setDraft(drafts.current, prevSlot.current, inputRef.current)
    if (prevSlot.current) setFileDraft(fileDrafts.current, prevSlot.current, pendingFilesRef.current)
    if (prevSlot.current) setPasteDraft(pasteDrafts.current, prevSlot.current, pasteBlocksRef.current)
    if (prevSlot.current) setSessionRefDraft(sessionRefDrafts.current, prevSlot.current, pendingSessionsRef.current)
    const prevSlotVal = prevSlot.current
    prevSlot.current = activeSlot
    const raw = sessionStorage.getItem(PREFILL_STORAGE_KEY)
    const draftFallback = activeSlot ? drafts.current[activeSlot] ?? '' : ''
    if (raw) {
      try {
        const { slotKey, prompt, ts } = JSON.parse(raw)
        if (Date.now() - (ts ?? 0) > 30_000) { sessionStorage.removeItem(PREFILL_STORAGE_KEY); setInput(draftFallback) }
        else if (slotKey === activeSlot) { sessionStorage.removeItem(PREFILL_STORAGE_KEY); consumedPrefillRef.current = `${slotKey}:${ts}`; setInput(prompt) }
        else { setInput(draftFallback) }
      } catch { sessionStorage.removeItem(PREFILL_STORAGE_KEY); setInput(draftFallback) }
    } else if (prevSlotVal === activeSlot && !!activeSlot && consumedPrefillRef.current?.startsWith(`${activeSlot}:`)) {
      // StrictMode re-invoked this mount effect for the SAME active slot after
      // the first invoke already consumed+removed the prefill. The composer
      // already holds the staged prompt; a setInput(draftFallback) here would
      // wipe it back to the empty draft. Leave the composer as-is. (A genuine
      // slot switch changes activeSlot, so prevSlotVal !== activeSlot and this
      // branch cannot mask a real draft restore.)
    } else { setInput(draftFallback) }
    // Restore the incoming slot's staged file attachments (copy so the
    // live state array and the stored draft don't share a reference).
    setPendingFiles(activeSlot ? (fileDrafts.current[activeSlot] ?? []).slice() : [])
    // Staged folder references need no restore of their own: the chips derive
    // from `@rel/` tokens in the composer text, and the text draft restored
    // above is per-slot. A folder staged in slot A therefore reappears with
    // slot A's draft and never bleeds into slot B.
    // Restore the incoming slot's collapsed-paste blocks (deep copy so the live
    // state and the stored draft don't share references). Without this the
    // token text rehydrates from the text draft but its backing block is gone,
    // leaving a dead `[ Paste #N · M lines ]` literal in the input.
    setPasteBlocks(activeSlot
      ? (pasteDrafts.current[activeSlot] ?? []).map(b => ({ ...b }))
      : [])
    // Restore the incoming slot's staged session references (copy per record so
    // the live state and the stored draft never share a reference).
    setPendingSessions(activeSlot
      ? (sessionRefDrafts.current[activeSlot] ?? []).map(r => ({ ...r }))
      : [])
    knowledgeFetchRef.current.clearResults()
    setUploadError('')
    flushDrafts()
  }, [activeSlot, flushDrafts])
  // Persist drafts on unmount (navigating away from chat page)
  useEffect(() => () => {
    if (saveDraftsTimer.current) { clearTimeout(saveDraftsTimer.current); saveDraftsTimer.current = null }
    if (prevSlot.current) setDraft(drafts.current, prevSlot.current, inputRef.current)
    if (prevSlot.current) setFileDraft(fileDrafts.current, prevSlot.current, pendingFilesRef.current)
    if (prevSlot.current) setPasteDraft(pasteDrafts.current, prevSlot.current, pasteBlocksRef.current)
    if (prevSlot.current) setSessionRefDraft(sessionRefDrafts.current, prevSlot.current, pendingSessionsRef.current)
    flushDrafts()
  }, [flushDrafts])
  // Flush pending draft save on tab close / refresh (debounce may not fire)
  useEffect(() => {
    const h = () => {
      if (prevSlot.current) setDraft(drafts.current, prevSlot.current, inputRef.current)
      if (prevSlot.current) setFileDraft(fileDrafts.current, prevSlot.current, pendingFilesRef.current)
      if (prevSlot.current) setPasteDraft(pasteDrafts.current, prevSlot.current, pasteBlocksRef.current)
      if (prevSlot.current) setSessionRefDraft(sessionRefDrafts.current, prevSlot.current, pendingSessionsRef.current)
      flushDrafts()
    }
    window.addEventListener('beforeunload', h)
    return () => window.removeEventListener('beforeunload', h)
  }, [flushDrafts])
  const [agentBtnRect, setAgentBtnRect] = useState<DOMRect | null>(null)
  const [projectPickerOpen, setProjectPickerOpen] = useState(false)
  const [projectBtnRect, setProjectBtnRect] = useState<DOMRect | null>(null)

  // Prevent Chrome from navigating to dropped files.
  // Must be on document to catch drops anywhere on the page.
  useEffect(() => {
    const preventNav = (e: DragEvent) => {
      if (e.dataTransfer?.types?.includes('Files')) {
        e.preventDefault()
        e.dataTransfer.dropEffect = 'copy'
      }
    }
    document.addEventListener('dragover', preventNav)
    document.addEventListener('drop', preventNav)
    return () => {
      document.removeEventListener('dragover', preventNav)
      document.removeEventListener('drop', preventNav)
    }
  }, [])

  const [uploading, setUploading] = useState(false)
  const [pendingFiles, setPendingFiles] = useState<string[]>([])
  // Staged folder chips DERIVE from the composer text: an `@rel/` token is the
  // only form of a folder reference the agent receives, so token presence is
  // the single source of truth. There is no parallel state to leak across
  // slots, clear on send, or sync against hand-edits — inserting the token
  // stages the chip, deleting the token (by any means) unstages it, and the
  // per-slot text draft persists the reference across slot switches for free.
  const pendingDirs = useMemo(() => parseDirTokens(input).map(t => t.rel), [input])
  // Exact `@rel` composer token recorded per PICKER-PICKED file, so the file
  // chip's remove control can strip precisely the token the pick inserted —
  // the same remove contract folder chips have. Uploaded/dropped files never
  // get an entry (they have no token), so their remove stays state-only. A
  // ref, not state: it never drives rendering. Entries die with their chip.
  const pickedFileTokens = useRef<Record<string, string>>({})
  const [snipFrame, setSnipFrame] = useState<HTMLCanvasElement | null>(null)
  // The slot that INITIATED the current snip. getDisplayMedia + cropping is
  // async and the user may switch slots meanwhile, so the cropped image must
  // land in the slot that started the capture — not whatever is active when the
  // crop completes. Threaded into uploadFiles as an explicit target.
  const snipSlotRef = useRef<string | null>(null)
  const pendingFilesRef = useRef(pendingFiles)
  useEffect(() => {
    pendingFilesRef.current = pendingFiles
    // Key off composerSlotRef, not activeSlot (see the composerSlotRef note).
    const s = composerSlotRef.current
    if (s) {
      setFileDraft(fileDrafts.current, s, pendingFiles)
      saveDraftsDebounced()
    }
    // Draft key is composerSlotRef; the slot-change effect handles that
    // transition.
  }, [pendingFiles, saveDraftsDebounced])
  // Collapsed paste blocks backing the `[ Paste #N · M lines ]` tokens in
  // `input`. Persisted per-slot via chatPasteDrafts (localStorage, 30-day TTL)
  // so they survive slot switches / refresh; cleared on send and slot delete.
  const [pasteBlocks, setPasteBlocks] = useState<PasteBlock[]>([])
  const pasteBlocksRef = useRef(pasteBlocks)
  useEffect(() => {
    pasteBlocksRef.current = pasteBlocks
    // Live-persist the composer's blocks so a slot switch / refresh restores
    // them alongside the text draft (mirrors the pendingFiles effect above).
    // Key off composerSlotRef, not activeSlot (see the composerSlotRef note).
    const s = composerSlotRef.current
    if (s) {
      setPasteDraft(pasteDrafts.current, s, pasteBlocks)
      saveDraftsDebounced()
    }
    // draft key is composerSlotRef; slot-change effect handles that transition.
  }, [pasteBlocks, saveDraftsDebounced])
  // Session references staged by dragging a session from the list onto this
  // pane. Serialized as LINKS on send — never the referenced transcript.
  const [pendingSessions, setPendingSessions] = useState<SessionRef[]>([])
  const pendingSessionsRef = useRef(pendingSessions)
  useEffect(() => {
    pendingSessionsRef.current = pendingSessions
    // Key off composerSlotRef, not activeSlot (see the composerSlotRef note).
    const s = composerSlotRef.current
    if (s) {
      setSessionRefDraft(sessionRefDrafts.current, s, pendingSessions)
      saveDraftsDebounced()
    }
    // draft key is composerSlotRef; slot-change effect handles that transition.
  }, [pendingSessions, saveDraftsDebounced])
  /** Stage a dropped session. Ignores duplicates and overflow (addSessionRef
   *  returns the same array, so this is a no-op re-render-free path). */
  const stageSessionRef = useCallback((ref: SessionRef) => {
    setPendingSessions(prev => addSessionRef(prev, ref))
  }, [])
  const unstageSessionRef = useCallback((key: string) => {
    setPendingSessions(prev => removeSessionRef(prev, key))
  }, [])
  /**
   * Whether a dropped session reference has a composer to land in.
   *
   * This predicate exists because the same defect appeared on three separate
   * surfaces: a drop is accepted, `pendingSessions` is set, and nothing ever
   * renders it — a silent black hole. Naming the condition once means a fourth
   * surface cannot quietly reintroduce it.
   *
   *  - `splitMode`: SessionGridView renders its own ChatInput per cell and
   *    ChatPage's composer is unmounted.
   *  - no `activeSlot`: ChatPage renders an empty state instead of a composer,
   *    the per-slot persist effect has no key to write under, and the
   *    slot-restore effect resets `pendingSessions` to `[]` on the next
   *    activation — so the ref is discarded rather than merely hidden.
   *
   * (embed 'sessions' mode needs no clause: it renders no chat pane at all, so
   * there is no `chatPaneEl` to hand over.)
   */
  const canStageSessionRef = !splitMode && !!activeSlot
  // The chat pane element, held in STATE (not a ref) because ChatSidebar portals
  // its drop zone into it — a ref's assignment does not re-render, so the portal
  // would never mount on the first paint.
  const [chatPaneEl, setChatPaneEl] = useState<HTMLDivElement | null>(null)
  // Advance the composer draft key AFTER the three persist effects above. React
  // runs effects in declaration order, so on a slot switch each persist effect
  // has already written its changed value against the OUTGOING slot before this
  // repoints the key at the incoming one. Declared last on purpose. Moving it
  // earlier (or back into the slot-change effect) would let a file/paste change
  // batched with the switch smear onto the new slot.
  useEffect(() => { composerSlotRef.current = activeSlot }, [activeSlot])
  const [uploadError, setUploadError] = useState('')
  // Resize details keyed by uploaded server path. Rendered as a badge on the
  // attachment chip itself (FilePreviewStrip) instead of a banner — the info
  // describes one staged file, so it lives on that file's chip. Keyed by the
  // unique upload path, entries stay valid across slot switches (drafts
  // restore chips per slot) and stale keys are harmless.
  const [resizedInfo, setResizedInfo] = useState<Record<string, ResizeInfo>>({})
  const isMac = useAppSelector(s => s.dashboard.status?.platform) === 'darwin'
  const { data: sttCfg } = useQuery({
    queryKey: ['sttConfig'],
    queryFn: () => api.sttConfig() as Promise<{ streaming?: boolean; enabled?: boolean; dictation_panel?: boolean; available?: boolean; provider?: string }>,
  })
  const sttStreaming = !!sttCfg?.streaming
  const sttEnabled = !!sttCfg?.enabled
  // The backend probes for the provider's binary and reports `available`.
  // Default true so a not-yet-loaded config doesn't flash the modal; the
  // separate sttConfigLoaded guard already covers the pre-load case.
  const sttAvailable = sttCfg?.available !== false
  // The LOCALISED provider name, not the wire id: the modal puts it in a
  // sentence, and a bare id reads as a typo there ("local is not installed").
  const sttProvider = providerLabel(sttCfg?.provider || '')
  // Default true so the panel is the standard recording surface; the backend
  // sends an explicit boolean, so `undefined` here means "config not loaded yet"
  // rather than "off", and a pre-load recording would otherwise flash the bar.
  const sttDictationPanel = sttCfg?.dictation_panel !== false
  // Treat "config not loaded yet" as disabled so the guard never lets a
  // recording start before STT is confirmed on. Stable boolean so toggleVoice's
  // deps don't churn on every sttCfg object identity from a refetch.
  const sttConfigLoaded = !!sttCfg
  // Opened when the user clicks the mic while STT is disabled — points them at
  // the setting that turns it on instead of starting a recording that would
  // never be transcribed.
  const [voiceSetupOpen, setVoiceSetupOpen] = useState(false)
  const frozenInputRef = useRef<string | null>(null)
  // Caret snapshot taken alongside frozenInputRef, so a streaming partial (and
  // the final that replaces it) keeps inserting at the same spot. The batch
  // path leaves both null and reads the LIVE composer caret instead.
  const frozenCaretRef = useRef<{ start: number; end: number } | null>(null)
  // Live composer caret, kept current by ChatInput (onSelect / click / typing).
  // Dictation splices the transcript in HERE instead of always appending at end.
  const voiceCaretRef = useRef<{ start: number; end: number } | null>(null)
  // Caret offset ChatInput should restore after a dictation-driven value update
  // lands (set by the splice below, consumed + cleared inside ChatInput).
  const voicePendingCaretRef = useRef<number | null>(null)
  // Drops late-arriving partials/finals for the CURRENT slot after a send.
  // `stop()` is async (up to 5s for backend close) — without this guard, a
  // delayed onFinal would repopulate the composer with text the user already
  // sent. Cross-SLOT safety is handled separately by session-scoped routing
  // (see applyVoiceText + voice.sessionOwner).
  const sttDisarmedRef = useRef(false)
  // Narrower sibling of `sttDisarmedRef`, for a MANUAL STOP of a streaming
  // recording that already put a hypothesis in the composer.
  //
  // One flag was doing two jobs, and a manual stop only wants one of them.
  // `applyVoiceText` APPENDS (`base + ' ' + text`), so the close-time final
  // landing on a composer that already holds the hypothesis duplicates the
  // utterance ("hello hello") — that has to stay suppressed. But `onPartial`
  // REPLACES the region at the frozen boundary, and the hook re-emits
  // `finals.join(' ')` through it on every `final` message while `stop()`
  // deliberately leaves the socket draining. Suppressing that too meant every
  // segment Transcribe stabilised AFTER the release was dropped, so the user
  // was left holding the last UNSTABLE hypothesis. On a push-to-talk hold that
  // is the common case, not a corner: the hold is short, so the tail of the
  // utterance is exactly the part still unstable at release.
  //
  // So: this flag suppresses the append only, and leaves the drain's own
  // corrections free to keep replacing the region until the socket closes.
  // Cancel, send and slot-switch still want EVERYTHING suppressed and keep
  // using `sttDisarmedRef` — the user discarded, already sent, or left.
  const sttAppendDisarmedRef = useRef(false)
  // The composer content UP TO the end of the region onPartial last inserted,
  // plus the whole value it wrote. Dictation splices at the caret, so it can sit
  // mid-draft with an existing tail after it — and typing after the release
  // lands at the restored caret, i.e. between the two. Anchoring on the PREFIX
  // (not the whole value) is what lets a drain-time update replace the corrected
  // region and keep everything after it verbatim; anchoring on the whole value
  // would fail its own startsWith check mid-draft and drop the correction.
  // The full value distinguishes "the user typed" from "nothing changed", which
  // decides whether the caret may be moved.
  const lastDictationAnchorRef = useRef<string | null>(null)
  const lastDictationValueRef = useRef<string | null>(null)
  // Sticky for the whole post-stop drain: once the user has typed, the caret is
  // theirs until dictation restarts. Recomputing "did they edit?" per update is
  // not enough — after the first correction carries the suffix across, the
  // composer matches what we wrote again, so a second correction would decide
  // nothing was edited and yank the caret back in front of the typed text.
  const postStopEditedRef = useRef(false)
  // Suppresses ONLY the auto-submit route, and unlike the append flag it is set
  // by EVERY manual stop of a streaming recording — including a cold-stream stop
  // where no partial landed. "Stop capturing" is never "send": without this, a
  // short press against a cold stream leaves the endpointer armed, and a
  // trailing final's endpoint verdict submits the turn the user never asked to
  // send. The append flag cannot carry this, because with no partial landed the
  // close-time final is the only copy of the utterance and must still land.
  const sttEndpointDisarmedRef = useRef(false)
  // A frozen caret is a position in the composer as it stood at the release. Once
  // the user edits after that, it can go stale in two ways, and both corrupt the
  // splice: a RANGE (dictating over a selection replaces it) whose selection they
  // have since typed over, and an OFFSET whose meaning shifts when they edit text
  // BEFORE it. Rebase it onto the current text instead of trusting or discarding
  // it wholesale — discarding it would put the transcript after text they wrote
  // later, trusting it would cut into text they wrote earlier.
  const rebaseFrozenCaret = useCallback(() => {
    if (!sttEndpointDisarmedRef.current) return
    const frozen = frozenCaretRef.current
    const released = lastDictationValueRef.current
    const cur = inputRef.current ?? ''
    // Untouched composer: a selection here is still a legitimate replacement
    // target, which is what dictating over a selection is supposed to do.
    if (!frozen || released === null || cur === released) return
    // Bound the edit to the region between the longest common prefix and suffix.
    let lcp = 0
    while (lcp < released.length && lcp < cur.length && released[lcp] === cur[lcp]) lcp++
    let lcs = 0
    while (
      lcs < released.length - lcp && lcs < cur.length - lcp &&
      released[released.length - 1 - lcs] === cur[cur.length - 1 - lcs]
    ) lcs++
    const start = frozen.start
    let next: number
    if (start <= lcp) next = start                                    // edit is after it
    else if (start >= released.length - lcs) next = start + (cur.length - released.length)
    else next = voiceCaretRef.current?.start ?? start                 // edit straddles it
    next = Math.max(0, Math.min(next, cur.length))
    frozenCaretRef.current = { start: next, end: next }
  }, [])
  // The hook's EFFECTIVE streaming mode: streaming is only truly active when the
  // config asks for it AND the browser supports it (AudioWorklet/WS). Mirrored
  // from voice.streamEnabled (set by the effect below, once `voice` exists) so
  // the disarm + cross-slot-routing decisions gate on what the hook ACTUALLY
  // runs, not the raw config. Keying those on the config alone would, in a
  // browser without AudioWorklet, treat a batch-fallback session as streaming
  // and disarm/drop its (only) transcript.
  const streamEnabledRef = useRef(false)
  // Forward port to the action-side send() so the streaming endpointer — wired
  // into the voice hook here, before actions exist — can auto-submit. ChatPage
  // deliberately runs composer before actions on every render; actions binds
  // this ref before render returns, so external voice events cannot observe the
  // initial null. Keep that order: the null guard is only for teardown safety.
  const sendRef = useRef<((optionText?: string, targetSlot?: string) => void) | null>(null)
  // Deliver a finished transcript to the slot that INITIATED the recording,
  // using the session id useVoiceInput snapshotted at record-start (falling back
  // to the active slot for the ordinary same-slot case). Same-slot splices into
  // the live composer; a background slot gets it appended to its persisted draft
  // (recoverable, shown on return) instead of leaking into the active session or
  // being dropped. Mirrors handleOptimizeResult's cross-slot routing.
  // Splice a dictation transcript into `base` at the caret (frozen snapshot
  // when streaming, else the live caret), returning the new value and the caret
  // offset to restore. Falls back to appending when no caret is known (e.g. the
  // composer was never focused).
  const spliceDictation = useCallback((base: string, text: string): { value: string; caret: number } => {
    const caret = frozenCaretRef.current ?? voiceCaretRef.current
    // An empty transcript (e.g. a silent streaming partial) must NOT mutate the
    // draft: splicing "" across a selection would delete the selected range.
    // Leave the base untouched and collapse the caret to the insertion point.
    if (!text) return { value: base, caret: caret ? Math.min(caret.start, base.length) : base.length }
    if (!caret) {
      const value = base ? (base.endsWith(' ') ? base + text : base + ' ' + text) : text
      return { value, caret: value.length }
    }
    const start = Math.min(caret.start, base.length)
    const end = Math.min(caret.end, base.length)
    const before = base.slice(0, start)
    const after = base.slice(end)
    // Leading space only when joining onto a non-space char, so mid-sentence
    // dictation doesn't glue onto the preceding word.
    // Leading/trailing space uses whitespace-class checks (not only ' ') so a
    // caret beside a newline or tab doesn't get an unwanted literal space.
    const lead = before && !/\s$/.test(before) && !/^\s/.test(text) ? ' ' : ''
    const trail = after && !/^\s/.test(after) && !/\s$/.test(text) ? ' ' : ''
    const insert = lead + text
    return { value: before + insert + trail + after, caret: before.length + insert.length }
  }, [])
  const applyVoiceText = useCallback((text: string, sessionId: string | null, origin: TranscriptOrigin) => {
    // Disarmed after a send (streaming) — the transcript was already sent, so
    // drop it for EVERY route. Checked FIRST (before the cross-slot branch) so a
    // late final can't slip the already-sent text back into the originating
    // slot's draft.
    //
    // `sttAppendDisarmedRef` covers the narrower case: a manual stop whose
    // hypothesis is already in the composer. This route APPENDS, so letting the
    // close-time final through there would duplicate the utterance.
    //
    // Both are STREAMING-only states — every site that arms them is gated on
    // streaming — so they are keyed on where the text came from, not on the mode
    // selected right now. A batch transcription can outlive the page that started
    // it and land after streaming was switched on, and its onstop transcript is
    // always the only copy: suppressing it would delete what the user said.
    if (origin === 'stream' && (sttDisarmedRef.current || sttAppendDisarmedRef.current)) return
    const target = sessionId ?? activeSlotRef.current
    const append = (base: string) => (base ? (base.endsWith(' ') ? base + text : base + ' ' + text) : text)
    // Splice into the LIVE composer only when the target slot is both the active
    // slot AND the slot the composer's `input` currently belongs to. On a slot
    // switch, activeSlotRef updates synchronously in render, but the composer's
    // draft-restore + composerSlotRef advance run in LATER effects — splicing in
    // that unsettled window would let the pending draft restore overwrite the
    // transcript. Otherwise route to the target slot's persisted draft.
    const onScreen = target === activeSlotRef.current && composerSlotRef.current === target
    if (!onScreen) {
      // Off-screen (or not-yet-settled) delivery is BATCH ONLY. Streaming splices
      // its live hypothesis into `input`, which is flushed into the draft on
      // switch, so a cross-slot append would double it — a streaming final that
      // lands off its slot is dropped (pre-existing behaviour). Batch has no
      // partial, so appending to the slot's draft is unambiguous. Keyed on the
      // text's origin rather than the live streaming setting, which is a proxy
      // that goes wrong for a batch transcript arriving after the mode changed.
      if (!target || origin === 'stream') return
      const next = append(drafts.current[target] ?? '')
      setDraft(drafts.current, target, next)
      // Mid-switch guard: if the composer still belongs to `target` (activeSlot
      // has advanced in render but the outgoing-slot persist effect hasn't run
      // yet), that effect will flush inputRef.current into drafts[target] and
      // would overwrite this transcript with the pre-transcript input. Carry the
      // appended value into inputRef too so the flush preserves the transcript.
      if (composerSlotRef.current === target) inputRef.current = next
      saveDrafts()
      return
    }
    // Foreground: streaming seeds frozenInputRef/frozenCaretRef in onPartial
    // (the pre-dictation snapshot); the batch path never fires onPartial so both
    // are null — fall back to the live composer text + caret so the transcript
    // inserts at the cursor instead of overwriting (or blindly appending to)
    // what the user typed.
    rebaseFrozenCaret()
    const spliced = spliceDictation(frozenInputRef.current ?? inputRef.current ?? '', text)
    // Only arm the caret restore when the value actually changes. If a streaming
    // final equals the last partial, setInput is a no-op and the restore effect
    // (keyed on `value`) never fires — leaving a stale pending caret that would
    // hijack the user's NEXT edit.
    if (spliced.value !== inputRef.current) {
      setInput(spliced.value)
      voicePendingCaretRef.current = spliced.caret
    }
    frozenInputRef.current = null
    lastDictationAnchorRef.current = null
    lastDictationValueRef.current = null
    postStopEditedRef.current = false
    frozenCaretRef.current = null
  }, [saveDrafts, spliceDictation, rebaseFrozenCaret])
  const voice = useVoiceInput(
    applyVoiceText,
    {
      streaming: sttStreaming,
      sessionId: activeSlot,
      onPartial: useCallback((text: string, sessionId: string | null) => {
        // Streaming partials only fire while the originating slot is on screen
        // (switching slots stops the stream), so a partial attributed to any
        // other slot is a late straggler — drop it rather than smear a
        // half-word into the wrong session.
        if (sessionId && sessionId !== activeSlotRef.current) return
        // Deliberately NOT gated on `sttAppendDisarmedRef`: after a manual stop
        // the socket is still draining, and this is the route that carries the
        // stabilised text. It REPLACES the region at the frozen boundary rather
        // than appending, so letting it keep firing cannot duplicate anything —
        // it is what turns the last unstable hypothesis into the real transcript.
        if (sttDisarmedRef.current) return
        // Snapshot the pre-dictation text AND caret on the first partial
        // (before setInput, so the updater stays pure — no ref mutation inside a
        // function React may invoke twice) so every later partial and the final
        // insert at the same spot, replacing the growing hypothesis.
        if (frozenInputRef.current === null) {
          frozenInputRef.current = inputRef.current
          // Do not clobber a caret a cold-stream stop already froze: that one is
          // the release-time insertion point, and the live caret is now wherever
          // the user has typed since.
          frozenCaretRef.current = frozenCaretRef.current ?? voiceCaretRef.current
        }
        rebaseFrozenCaret()
        const spliced = spliceDictation(frozenInputRef.current ?? '', text)
        // Everything up to and including the dictated insertion. What follows it
        // in the composer (an existing tail, and anything typed after release) is
        // carried across untouched rather than rebuilt from the snapshot.
        const anchor = spliced.value.slice(0, spliced.caret)
        let next = spliced.value
        // Where the caret should end up. Defaults to the end of the dictated
        // region (the ordinary "we own the composer" case); the post-stop branch
        // overrides it when the text is the user's to steer.
        let caretTarget: number | null = spliced.caret
        if (sttEndpointDisarmedRef.current) {
          // POST-STOP DRAIN. The user has let go, so as far as they are concerned
          // dictation is over and they may already be typing — at the restored
          // caret, which for mid-draft dictation sits in the MIDDLE of the text.
          // Rebuilding from the frozen snapshot would delete that typing, so
          // verify our own prefix is still intact and splice the correction in
          // ahead of whatever now follows it. If the prefix cannot be verified
          // the user edited inside the dictated region; leave the composer alone
          // rather than guess — same policy as cancelVoice, for the same reason:
          // a heuristic here deletes user-authored text.
          //
          // Gated on the ENDPOINT flag, not the append flag: a cold-stream stop
          // deliberately leaves the append armed (the close-time final is the
          // only copy of the utterance), so keying off it would skip this branch
          // in exactly the case where it is still needed.
          //
          // During recording this does not apply: the region is being actively
          // rewritten and that behaviour is unchanged.
          const prev = lastDictationAnchorRef.current
          const cur = inputRef.current ?? ''
          // The composer now holds a copy of the utterance, which is the exact
          // condition the append flag encodes — so close the close-time route
          // here rather than at stop time. stopVoice could not decide this: with
          // frozenInputRef still null it had to leave the append armed, because
          // back then the close-time final really was the only copy. Once a drain
          // partial has landed that is no longer true, and letting the final
          // through would re-splice from the snapshot and delete whatever the
          // user typed after the release.
          sttAppendDisarmedRef.current = true
          // Checked OUTSIDE the anchor guard: on a cold stream the first drain
          // partial has no anchor yet, but the user may already have typed since
          // the release, and their caret must still be left alone.
          if (cur !== lastDictationValueRef.current) postStopEditedRef.current = true
          // A null anchor means no partial has landed yet — the cold-stream stop.
          // This IS the first write: there is nothing to preserve and nothing to
          // verify, and returning here would drop the utterance. Fall through to
          // the plain write, which establishes the anchor for the next update.
          // The typed text is inside the snapshot (taken from the LIVE composer)
          // and the insertion point is the caret stopVoice froze at the release,
          // so the transcript lands where the user was speaking rather than after
          // what they wrote afterwards.
          if (prev !== null) {
            if (!cur.startsWith(prev)) return
            next = anchor + cur.slice(prev.length)
            if (postStopEditedRef.current) {
              // Their caret is in their own text, so it must not be dragged to the
              // end of the dictation — but NOT arming it is not "leaving it
              // alone" either: React replaces the textarea value and the browser
              // resets the DOM caret to the end. Re-arm it at the same LOGICAL
              // spot, shifted by how much the region ahead of it grew or shrank.
              const live = voiceCaretRef.current
              caretTarget = live && live.start >= prev.length
                ? live.start + (anchor.length - prev.length)
                : null
            }
          } else if (postStopEditedRef.current) {
            // Cold-stream first write with typing already done: there is no old
            // anchor to measure a shift against, and the value commit leaves the
            // caret at the end — which is past their text, a sane place to be.
            caretTarget = null
          }
        }
        if (next !== inputRef.current) {
          setInput(next)
          if (caretTarget !== null) voicePendingCaretRef.current = caretTarget
        }
        lastDictationAnchorRef.current = anchor
        lastDictationValueRef.current = next
      }, [spliceDictation, rebaseFrozenCaret]),
      // Semantic endpointing (stt.endpointing) judged the utterance complete:
      // auto-submit. The composer already holds the streamed transcript via
      // onPartial, and send() reads inputRef.current + stops the live capture
      // itself (its recording+streaming branch), so this is the same path as
      // pressing Enter mid-dictation — just triggered by the backend verdict.
      onEndpoint: useCallback(() => {
        // A manual stop is the user saying "stop capturing", so a backend
        // endpoint verdict arriving during the drain must not turn that into an
        // unrequested send. The endpoint flag is what covers a COLD-stream stop,
        // where no partial landed and the append flag is deliberately left unset
        // so the close-time final can still deliver the utterance.
        if (sttDisarmedRef.current || sttAppendDisarmedRef.current || sttEndpointDisarmedRef.current) return
        sendRef.current?.()
      }, []),
    }
  )
  // Keep a ref to the latest `voice` so effects that intentionally omit
  // `voice` from their deps always invoke the current instance — otherwise
  // they'd capture a stale `toggle`/`recording` whenever `voice` identity
  // changes (e.g. when `sttStreaming` flips).
  const voiceRef = useRef(voice)
  useEffect(() => { voiceRef.current = voice }, [voice])
  // Same reason as voiceRef: send() deliberately keeps a minimal dep array (with
  // an exhaustive-deps suppression), so reading `sttStreaming` directly there
  // would close over the value from the render that created that send().
  // Keep streamEnabledRef in sync with the hook's EFFECTIVE streaming mode (see
  // its declaration above). send()/the slot-switch effect/toggleVoice read it to
  // decide whether a draining final should be disarmed — which must reflect what
  // the hook actually runs, not the raw config.
  useEffect(() => { streamEnabledRef.current = voice.streamEnabled }, [voice.streamEnabled])
  // Re-arm when the user explicitly (re)starts recording — wrap toggle.
  // Depend on the individual stable members actually read so this callback
  // is only re-created when they change. `[voice]` would recreate every
  // render (hooks don't memoize their return by default), re-rendering all
  // child components that receive `toggleVoice` as a prop.
  /**
   * Start voice capture, with the gating and state resets every entry point
   * needs. Extracted from `toggleVoice` so the push-to-talk key driver
   * (`usePushToTalk`) goes through the SAME preamble — calling `voice.start()`
   * raw would skip the disarm reset and the frozen-snapshot clear, and a
   * key-started dictation would then be rebuilt from stale pre-dictation text.
   *
   * RETURNS the start promise. Load-bearing, not incidental: `usePushToTalk`
   * chains on it to stop a session whose async startup only finished after the
   * key was already released. Swallowing it here leaves that guard unreachable
   * and the microphone open with nothing holding it.
   *
   * `silent` suppresses the "voice needs setting up" modal. The key binding is a
   * PASSIVE trigger — a bare modifier is also an ordinary typing modifier — so a
   * keystroke that used to type a character must never throw an unsolicited
   * dialog. Clicking the mic button is a deliberate request and still explains
   * itself.
  */
  const startVoice = useCallback((opts?: { silent?: boolean }): Promise<void> | void => {
    // Starting a recording while server-side STT is disabled would capture
    // audio that never gets transcribed. Point the user at the enable setting
    // instead — unless this came from the keyboard (see `silent`).
    if (!sttConfigLoaded || !sttEnabled || !sttAvailable) {
      if (!opts?.silent) setVoiceSetupOpen(true)
      return
    }
    // Exclusive sessions: the mic is a single shared device, so refuse to
    // START a new recording while another session's transcription is still
    // in flight (voice.transcribing). This is what keeps voice single-session
    // — no two recordings/transcriptions ever overlap — so the busy state
    // needs only a single owner and can never be misattributed.
    if (voice.transcribing) return
    sttDisarmedRef.current = false
    sttAppendDisarmedRef.current = false
    sttEndpointDisarmedRef.current = false
    // Reset stale snapshot from a prior session that ended without
    // finals — otherwise onPartial sees a non-null ref, skips
    // re-snapshotting, and text typed between sessions is dropped.
    frozenInputRef.current = null
    lastDictationAnchorRef.current = null
    lastDictationValueRef.current = null
    postStopEditedRef.current = false
    frozenCaretRef.current = null
    return voice.start()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voice.transcribing, voice.start, sttEnabled, sttConfigLoaded, sttAvailable])

  /** Stop voice capture. Always allowed — only starting is gated. */
  const stopVoice = useCallback(() => {
    // Manual stop of a STREAMING recording: streamStop() drains the socket
    // asynchronously, so more of the utterance can still arrive. Two routes are
    // in play and they need OPPOSITE treatment, which is why this sets the
    // narrow flag rather than the blanket one:
    //
    //   - `applyVoiceText` (close-time) APPENDS. The composer already holds the
    //     hypothesis, so letting it through duplicates the utterance. Suppress.
    //   - `onPartial` (drain-time) REPLACES the region at the frozen boundary,
    //     and the hook re-emits `finals.join(' ')` through it as Transcribe
    //     stabilises each segment. That is the authoritative text. Keep armed.
    //
    // Only suppress once the composer actually holds a copy of the speech, which
    // is exactly what frozenInputRef being set means (onPartial snapshots it on
    // the FIRST partial, then writes each hypothesis into `input`).
    //
    // With frozenInputRef still null NO partial has landed, so the composer
    // holds nothing and the close-time final is the ONLY copy of the utterance:
    // suppressing there silently deletes what the user just said. That is the
    // ordinary case for a short press against a COLD stream, where the release
    // beats the server's first partial. (Batch is likewise never suppressed
    // here: its onstop transcript is always the only copy.)
    if (streamEnabledRef.current && frozenInputRef.current !== null) {
      sttAppendDisarmedRef.current = true
    }
    // Unconditional for a streaming stop: the auto-submit route must close even
    // when the append route stays open (the cold-stream case above).
    if (streamEnabledRef.current) {
      sttEndpointDisarmedRef.current = true
    }
    if (streamEnabledRef.current && frozenInputRef.current === null) {
      // COLD STREAM: no partial landed, so nothing has pinned the insertion point
      // yet. Freeze the CARET at the release, so a drain partial arriving after
      // the user has started typing still inserts where they were speaking
      // instead of after the text they wrote afterwards.
      //
      // Deliberately NOT freezing the text as well: with no partial landed the
      // close-time final is the only copy of the utterance and must splice into
      // the LIVE composer. Pinning the text here would make it rebuild from the
      // release-time snapshot and delete anything typed after the release —
      // trading a wrong insertion point for lost text.
      //
      // The value fingerprint is seeded too, so the first drain partial can tell
      // that the user has typed since the release and leave their caret alone.
      frozenCaretRef.current = voiceCaretRef.current
      lastDictationValueRef.current = inputRef.current
    }
    voice.stop()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [voice.stop])

  const toggleVoice = useCallback(() => {
    if (voice.recording) stopVoice()
    else startVoice()
    // Depends on the individual member actually read (`voice.recording`), not the
    // whole `voice` object — `[voice]` would recreate this callback every render
    // and re-render every child that receives `toggleVoice`. No suppression is
    // needed here because the split into startVoice/stopVoice left this list
    // genuinely exhaustive.
  }, [voice.recording, startVoice, stopVoice])
  // Cancel (discard) the in-progress dictation — Esc. Batch simply drops the
  // pending audio (the hook's onstop skips transcription), so nothing lands in
  // the composer. Streaming additionally disarms the draining final AND removes
  // the live dictated region from the composer at the frozenInputRef boundary:
  // the region is recomputed with the same `spliceDictation` call onPartial used
  // (so it matches a mid-draft caret splice, not just an append), and we drop
  // exactly that region — preserving the pre-dictation text verbatim (including
  // its own trailing whitespace) AND any suffix typed after the dictation. When
  // the region can't be verified (the user replaced/edited it), leave the
  // composer unchanged rather than restoring the snapshot and losing that edit.
  // Uses voiceRef.current (not `voice`) so this prop stays referentially stable
  // and does not re-render the composer every render — matching toggleVoice.
  const cancelVoice = useCallback(() => {
    if (streamEnabledRef.current) {
      sttDisarmedRef.current = true
      // Remove the dictated region at the frozenInputRef boundary, preserving
      // the pre-dictation text EXACTLY (including its own trailing whitespace)
      // and any suffix the user typed after the dictation. onPartial rebuilt the
      // composer as `frozen [+ ' ' separator] + partial`, so reconstruct that
      // exact region and drop only it — never a blanket trailing-space strip.
      const cur = inputRef.current ?? ''
      const frozen = frozenInputRef.current
      const p = voiceRef.current.partial
      if (frozen !== null && p) {
        // Reconstruct the composer value through the SAME pure function that
        // wrote it. onPartial splices at the snapshotted caret, so for a
        // mid-draft caret the value is `before + lead + partial + trail + after`
        // — NOT `frozen + separator + partial`. Re-deriving the region with an
        // append-only formula failed `startsWith` for every mid-draft dictation
        // and fell through to the leave-unchanged branch, stranding the partial
        // in the draft. spliceDictation reads the same frozen caret, so this
        // reproduces the write exactly for both the append and mid-caret shapes.
        const written = spliceDictation(frozen, p).value
        if (cur.startsWith(written)) {
          // The composer still begins with exactly the region onPartial wrote.
          // Restore the pre-dictation text verbatim and keep any suffix the user
          // typed after it.
          setInput(frozen + cur.slice(written.length))
        }
        // else: the dictated region can't be verified exactly — the user edited
        // or replaced it (e.g. deleted the separator, or typed their own text
        // that merely ends in the same word as the partial). Leave the composer
        // UNCHANGED: a suffix-match heuristic here would delete user-authored
        // text ("say hello" -> "say"). The disarm above still drops the draining
        // final, so no dictation is committed; at worst the visible partial
        // lingers for the user to clear.
      }
      // (frozen===null, or no current partial: nothing verifiably removable —
      // leave the composer as-is rather than risk clobbering user text.)
      // Clear BOTH halves of the snapshot: they are written together in
      // onPartial and a surviving caret would aim the next session's first
      // splice at a position from the discarded one.
      frozenInputRef.current = null
      lastDictationAnchorRef.current = null
      lastDictationValueRef.current = null
      postStopEditedRef.current = false
      frozenCaretRef.current = null
    }
    voiceRef.current.cancel()
  }, [spliceDictation])

  // Push-to-talk / tap-to-toggle keyboard binding (default: hold right ⌥ on
  // macOS, ⌥⇧Space elsewhere). Routed through startVoice/stopVoice rather than
  // voice.start/stop so a key-driven dictation gets the same gating and
  // snapshot resets as the mic button, and `cancelVoice` — NOT the hook's raw
  // cancel — for the discard. Since capture now opens on the keydown, a fast
  // partial can reach the composer before the press is revealed as a chord or a
  // sub-threshold tap, and the raw cancel would strand that text; `cancelVoice`
  // runs the streaming rollback that removes the dictated region (and no-ops
  // when nothing verifiably removable was written). No `prewarm`: the driver
  // opens capture on the keydown itself, so there is no warm-up step to
  // schedule.
  usePushToTalk(
    {
      recording: voice.recording,
      // silent: a bare modifier is also an ordinary typing modifier, so a
      // keystroke must never raise the voice-setup modal on its own.
      start: () => startVoice({ silent: true }),
      stop: stopVoice,
      cancel: cancelVoice,
    },
    { disabled: !voiceInputSupported },
  )
  // Stop any in-flight recording and clear the streaming prefix when the user
  // switches slots. The mic is a single shared device, so a recording can't
  // follow the user to another session; a BATCH transcript is still delivered
  // to the originating slot via applyVoiceText's session-scoped routing (which
  // prevents cross-slot leakage precisely — no blanket disarm needed here).
  // Clearing frozenInputRef here means a streaming final that lands after a
  // switch-and-return rebases on the LIVE input, so edits made after returning
  // are preserved rather than clobbered by a stale snapshot.
  useEffect(() => {
    frozenInputRef.current = null
    lastDictationAnchorRef.current = null
    lastDictationValueRef.current = null
    postStopEditedRef.current = false
    frozenCaretRef.current = null
    // Drop the previous slot's caret so dictating in a freshly switched-to slot
    // (without touching its composer) appends to that slot's draft instead of
    // inserting at the old slot's offset.
    voiceCaretRef.current = null
    // Streaming ONLY: disarm so a delayed streaming final arriving after this
    // switch is dropped instead of appended. Its live partial was already
    // flushed into the outgoing slot's draft, so appending the full final on
    // return would duplicate the dictated text ("hello hello"). Batch is NOT
    // disarmed — its single final is routed to the originating slot's draft by
    // applyVoiceText. (Cross-slot streaming delivery is a follow-up; streaming
    // is opt-in and off by default.)
    if (streamEnabledRef.current) sttDisarmedRef.current = true
    if (voiceRef.current.recording) voiceRef.current.toggle()
  }, [activeSlot])
  // True when the current voice session (owned by the slot where recording
  // actually started — see useVoiceInput's sessionOwner) is the slot on screen.
  // Gates the recording/transcribing UI so a session transcribing in the
  // background never shows a busy/locked mic in the session the user switched to.
  const voiceOwned = voice.sessionOwner === activeSlot
  return {
    drafts,
    fileDrafts,
    pasteDrafts,
    sessionRefDrafts,
    saveDrafts,
    knowledgeFetchRef,
    prevSlot,
    activeSlotRef,
    composerSlotRef,
    input,
    setInput,
    inputRef,
    sendingRef,
    historySuggestions,
    isWelcomeState,
    showHistorySuggestions,
    chatConfig,
    installedAgents,
    effectiveAgents,
    remoteCrew,
    effectiveDefaultAgent,
    defaultAgent,
    defaultAgentFailed,
    toggleDefaultAgent,
    agentDropdown,
    setAgentDropdown,
    agentFilter,
    setAgentFilter,
    agentDropdownRef,
    agentInputRef,
    filteredAgents,
    availableModels,
    modelDropdown,
    setModelDropdown,
    modelFilter,
    setModelFilter,
    modelDropdownRef,
    modelInputRef,
    filteredModels,
    onAgentListKeyDown,
    onModelListKeyDown,
    pendingAgent,
    pendingAgentRef,
    setPendingAgent,
    pendingModel,
    pendingModelRef,
    setPendingModel,
    pendingProjectRef,
    setPendingProject,
    modelBtnRect,
    setModelBtnRect,
    steerMutation,
    reasoningEffortDropdown,
    setReasoningEffortDropdown,
    reasoningEffortBtnRect,
    setReasoningEffortBtnRect,
    reasoningEffortDropdownRef,
    autoNudgeOpen,
    setAutoNudgeOpen,
    autoNudgeLoop,
    setAutoNudgeLoop,
    approvalMode,
    scrollerRef,
    scrollToDisplayIndex,
    vGetFollowRef,
    vScrollToBottomRef,
    mountIndexRef,
    prefillHint,
    setPrefillHint,
    autoSendRef,
    autoSendTick,
    setAutoSendTick,
    newSessionRef,
    tokenConsumingRef,
    widgetPrefillRef,
    agentBtnRect,
    setAgentBtnRect,
    projectPickerOpen,
    setProjectPickerOpen,
    projectBtnRect,
    setProjectBtnRect,
    uploading,
    setUploading,
    pendingFiles,
    setPendingFiles,
    pendingDirs,
    pickedFileTokens,
    snipFrame,
    setSnipFrame,
    snipSlotRef,
    pendingFilesRef,
    pasteBlocks,
    setPasteBlocks,
    pasteBlocksRef,
    pendingSessions,
    setPendingSessions,
    pendingSessionsRef,
    stageSessionRef,
    unstageSessionRef,
    canStageSessionRef,
    chatPaneEl,
    setChatPaneEl,
    uploadError,
    setUploadError,
    resizedInfo,
    setResizedInfo,
    isMac,
    sttAvailable,
    sttEnabled,
    sttProvider,
    sttDictationPanel,
    voiceSetupOpen,
    setVoiceSetupOpen,
    frozenInputRef,
    voiceCaretRef,
    voicePendingCaretRef,
    sttDisarmedRef,
    lastDictationAnchorRef,
    lastDictationValueRef,
    postStopEditedRef,
    streamEnabledRef,
    sendRef,
    voice,
    voiceRef,
    startVoice,
    stopVoice,
    toggleVoice,
    cancelVoice,
    voiceOwned,
    switchAgentRef,
    switchModelRef,
  }
}

export type ChatPageComposerController = ReturnType<typeof useChatPageComposerController>
