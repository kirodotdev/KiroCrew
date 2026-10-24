import { useCallback, useEffect, useRef, useState, type MutableRefObject } from 'react'
import { unwrapResult } from '@reduxjs/toolkit'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import type { NavigateFunction, NavigationType } from 'react-router-dom'

import { api } from '../../api/client'
import type { ChatLaunchOptions } from '../../app-sdk'
import { i18nT } from '../../i18n/t'
import { useSessionTabs } from '../../hooks/useSessionTabs'
import { useAppSelector, useAppStore, type AppDispatch } from '../../store'
import {
  createSlot,
  deleteSlot,
  fetchHistory,
  resumeFromHistory,
  setActiveSlot,
  dropActiveSlot,
  setPendingInput,
  switchSlot,
} from '../../store/chatSlice'
import type { ChatSlot, SessionInfo } from '../../types'
import { isChatPageSurface } from '../../utils/channelOrigin'
import type { PasteBlock } from '../../utils/pasteTokens'
import { isSafeReload } from '../../lib/safeReload'
import { safeSetItem } from '../../utils/safeStorage'
import { PANE_SLOT_HINT_PARAM, relayActiveSlotToParent } from '../../lib/paneLastSlot'
import { shouldReplaceSessionUrl, popMaySwitchSession } from '../../utils/sessionUrlHistory'
import { sessionShortNameFrom, shortNameMatches, type ShortNameMatch } from '../../utils/sessionKeys'
import { toSlug } from '../../utils/shareUrl'
import { focusComposer } from './composerFocus'
import { PENDING_OPEN_LEASE_MS, resumeStillOpening } from '../../store/chatSlice'

/** Read a URL-derived key from the close-hold record without traversing its prototype. */
export function closeHoldForUrl<T>(closing: Record<string, T>, key: string | null): T | undefined {
  return key && Object.prototype.hasOwnProperty.call(closing, key) ? closing[key] : undefined
}
// How long after the not-found deadline a slots frame may still be treated as
// the gateway finishing its restore. The deadline itself is 5s; the recovery
// path exists for gateway restarts and large session restores, which settle on
// the same seconds scale. Past this bound a frame carrying the key still proves
// the banner false — but it no longer proves the user is waiting for that link,
// so it may only retire the notice, never move them.
const SID_RECOVERY_WINDOW_MS = 30_000
export { PENDING_OPEN_LEASE_MS }

/** The key of the history entry the browser stands on, which can be ahead of
 *  the rendered `location.key` while a router transition is uncommitted. The
 *  fallback mirrors react-router's own for an entry it never wrote state to. */
function liveHistoryKey(): string {
  const state = window.history.state as { key?: unknown } | null
  return typeof state?.key === 'string' && state.key ? state.key : 'default'
}

interface UseChatPageSessionControllerArgs {
  activeSlot: string | null
  activeSlotRef: MutableRefObject<string | null>
  connected: boolean
  dispatch: AppDispatch
  drafts: MutableRefObject<Record<string, string>>
  embedMode?: 'chat' | 'sessions'
  embedded?: boolean
  fileDrafts: MutableRefObject<Record<string, string[]>>
  filteredSlots: ChatSlot[]
  filteredSlotsRef: MutableRefObject<ChatSlot[]>
  history: SessionInfo[]
  /** Whether the composer holds any non-whitespace text. A boolean, not the
   *  text: the page must not re-render per keystroke to feed this hook. */
  inputNonBlank: boolean
  isMobile: boolean
  locationKey: string
  locationPathname: string
  locationHash: string
  mode?: string
  navigate: NavigateFunction
  navigationType: NavigationType
  newSessionRef: MutableRefObject<boolean>
  noUrlSync?: boolean
  pasteDrafts: MutableRefObject<Record<string, PasteBlock[]>>
  popout?: boolean
  prevSlot: MutableRefObject<string | null>
  saveDrafts: () => void
  searchParams: URLSearchParams
  slots: ChatSlot[]
  tokenConsumingRef: MutableRefObject<boolean>
}

/**
 * Owns ChatPage's session-entry state machine: history seeding, the tab working
 * set, URL/deep-link activation, active-slot recovery, and history resume.
 *
 * Keep the effect order in this hook aligned with the former inline ChatPage
 * block. Several URL paths intentionally hand ownership to the next effect by
 * mutating refs before it runs in the same commit.
 */
export function useChatPageSessionController({
  activeSlot,
  activeSlotRef,
  connected,
  dispatch,
  drafts,
  embedMode,
  embedded,
  fileDrafts,
  filteredSlots,
  filteredSlotsRef,
  history,
  inputNonBlank,
  isMobile,
  locationKey,
  locationPathname,
  locationHash,
  mode,
  navigate,
  navigationType,
  newSessionRef,
  noUrlSync,
  pasteDrafts,
  popout,
  prevSlot,
  saveDrafts,
  searchParams,
  slots,
  tokenConsumingRef,
}: UseChatPageSessionControllerArgs) {
  // Older-sessions history is fetched lazily, not on mount: the
  // sidebar's "Older sessions" section self-fetches when expanded (see
  // chat-sidebar/HistoryPane.tsx's footer toggle -- the section starts collapsed and its open
  // state is not persisted, so it can never be open at mount), which leaves
  // the welcome-screen "Continue a previous chat?" suggestions as the only
  // consumer that can need the payload before that. They need it only once
  // the user has typed something, so seed on the FIRST keystroke (raw input,
  // not the 300ms-debounced historyQuery -- keying off the debounce would
  // stack a round-trip after it, and on the high-RTT tunnels this targets
  // the suggestions could land after the user already hit Enter; this way
  // the fetch rides inside the debounce window at the same request cost).
  // An unconditional mount fetch cost one round-trip on every warm reload
  // for a list that is usually never shown. Once-only: the ref latches even
  // when the list is already populated (the sidebar fetched first), so
  // typing never re-fetches.
  const historySeededRef = useRef(false)
  useEffect(() => {
    if (historySeededRef.current || !inputNonBlank) return
    historySeededRef.current = true
    if (history.length === 0) dispatch(fetchHistory(false))
  }, [inputNonBlank, history.length, dispatch])

  // Persist active slot to localStorage for refresh recovery (per-mode)
  const slotStorageKey = `mc-active-slot-${mode || 'chat'}`
  const slotStorageKeyRef = useRef(slotStorageKey); slotStorageKeyRef.current = slotStorageKey
  useEffect(() => {
    if (activeSlot && filteredSlots.some(s => s.key === activeSlot)) {
      safeSetItem(slotStorageKey, activeSlot)
      // In a remote-crew pane this storage dies with the tunnel's port, so the
      // hub keeps a copy and hands it back as ?lastSid= (lib/paneLastSlot).
      if (!embedded) relayActiveSlotToParent(activeSlot)
    }
  }, [activeSlot, slotStorageKey, filteredSlots, embedded])
  useEffect(() => () => { if (activeSlotRef.current && filteredSlotsRef.current.find(s => s.key === activeSlotRef.current)) safeSetItem(slotStorageKeyRef.current, activeSlotRef.current) }, [activeSlotRef, filteredSlotsRef])

  /* ── Session tabs ───────────────────────────────────────────────────────
   *  The working set drawn by SessionTabStrip. The hook keeps the active
   *  session in the set, so a user who never opens a second tab holds a
   *  one-element set and the strip renders nothing.
   *
   *  `ownsSessionTabs` is the ONE predicate deciding both who draws the strip
   *  and who owns the persisted set. It has to be one predicate: ChatPage is
   *  also mounted by embedded hosts — a popped-out window, the artifact
   *  companion panel, Papyrus's co-author panel, the app-SDK chat panel — and
   *  they share the dashboard's origin, therefore its `localStorage`. Two
   *  separate conditions would let a host that cannot draw a strip still
   *  reconcile the key, overwriting the dashboard's working set with a session
   *  it never opened. `embedded` is exactly that line: every one of those hosts
   *  passes it, and the routed /chat surface passes none of these flags.
   *
   *  Switching is dispatched HERE rather than inside the hook: `switchSlot` is
   *  the surface's one session-entry path (URL sync, transcript hydration and
   *  the composer all hang off it), and a second caller inside a layout hook
   *  would be a second place that decides what "activate" means. */
  const ownsSessionTabs = !embedded
  // Read at settle time by the resume callbacks: whether the resume took the
  // view is the store's answer, not the thunk's.
  const store = useAppStore()
  // Read at click time, not captured: the callbacks below are memoized and the
  // gateway can drop between renders.
  const connectedRef = useRef(connected)
  connectedRef.current = connected
  const sessionTabs = useSessionTabs(mode, activeSlot, filteredSlots, ownsSessionTabs)
  /**
   * Every tab path that activates a session is gated on `connected`, for the
   * reason the sidebar row's own click already documents: an offline
   * `switchSlot` never resolves its fetch, `switchSlot.rejected` clears
   * `messages` to `[]`, and the user is left looking at the WelcomeView where
   * their transcript was. A tab is a second door onto the same action, so it
   * needs the same lock — and the strip is marked aria-disabled so the click
   * visibly refuses instead of silently doing nothing.
   */
  // `requestSeq`: the foreground tab opens a create that just answered, with
  // the create's origin number, so a request clicked after it still opens.
  const openSlotInNewTab = useCallback((key: string, opts?: { background?: boolean; requestSeq?: number }) => {
    sessionTabs.openInNewTab(key)
    // A BACKGROUND open (middle-click, modifier-click) queues the session and
    // leaves the user where they are — the browser/editor meaning of the
    // gesture, and the whole point of using it to triage several rows in a row.
    // The row menu is a deliberate "take me there", so it opens in foreground.
    if (opts?.background) return
    if (!connectedRef.current) return
    // Foreground open-in-tab is a user gesture on a session reference: the
    // announced class.
    if (key !== activeSlotRef.current) dispatch(switchSlot({ key, announceOnMissing: true, ...(opts?.requestSeq !== undefined ? { requestSeq: opts.requestSeq } : {}) }))
    // Depends on the ONE member it calls, not on `sessionTabs` — that hook
    // returns a fresh object literal every render, so the whole object as a dep
    // makes this callback (and thus ChatSidebar's `onOpenSlotInNewTab`, and thus
    // the sidebar's `memo`) churn on every ChatPage render. `openInNewTab` is
    // itself dep-free, so this identity is genuinely stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionTabs.openInNewTab, dispatch, activeSlotRef])
  // Read at click time, like `connectedRef`: the callback below is memoized.
  const tabsRef = useRef(sessionTabs.tabs)
  tabsRef.current = sessionTabs.tabs
  /**
   * Give `key` its own tab beside the active one, unless it has one. Backs
   * "Open Sidebar Sessions in a Session Tab": a sidebar or flyout click calls it
   * right after dispatching `switchSlot`. The hook replaces the active tab's
   * session only from an effect that runs after this update is queued, and
   * that effect's functional update then finds `key` in the set, so the tab
   * the user was on keeps its session. An existing tab gets no cue, unlike
   * `openSlotInNewTab`: the switch is the response.
   */
  const ensureSlotTab = useCallback((key: string) => {
    if (!tabsRef.current.includes(key)) sessionTabs.openInNewTab(key)
    // Same stable-identity reason as `openSlotInNewTab` above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionTabs.openInNewTab])
  const selectSessionTab = useCallback((key: string) => {
    if (key === activeSlotRef.current || !connectedRef.current) return
    // Tab-strip select is a user gesture on a session reference: the announced
    // class.
    dispatch(switchSlot({ key, announceOnMissing: true }))
  }, [dispatch, activeSlotRef])
  const closeSessionTab = useCallback((key: string) => {
    const next = sessionTabs.closeTab(key)
    // Only the ACTIVE tab's close moves the user; closing any other tab must
    // leave the transcript they are reading alone (nextActiveAfterClose returns
    // the unchanged active key in that case, so this compare is the whole gate).
    // Closing a tab is local, so it still works offline — only the switch that
    // would follow is withheld, leaving the user on the transcript they have.
    if (next && next !== activeSlotRef.current && connectedRef.current) dispatch(switchSlot(next))
    // Below two tabs the strip unmounts, so a keyboard close has no tab left to
    // land on and the strip cannot hand focus off itself. Without this, focus
    // falls to document.body and the user Tabs in from the top of the page.
    // The composer is the surface's own default focus target.
    if (sessionTabs.tabs.filter(k => k !== key).length < 2) focusComposer()
  }, [sessionTabs, dispatch, activeSlotRef])

  // Handle ?sid= (or legacy ?slot=) query parameter — activate the given session
  // Capture initial ?sid= at mount time before any effect can overwrite it
  // noUrlSync also disables the sid-READ paths, not just the URL write. The host
  // route (e.g. /artifacts/:slug) is not required to be sid-free: land on
  // /artifacts/foo?sid=other and an ungated read effect would switchSlot() the
  // embedded panel onto an unrelated session, so the composer would send into
  // it. Zeroing the ref here neutralizes the mount-activation effect AND the 5s
  // "session not found" timeout that keys off it; the POP effect reads
  // searchParams live and is gated separately below.
  const initialSidRef = useRef(noUrlSync ? null : (searchParams.get('sid') || searchParams.get('slot')))
  // The active slot as of MOUNT. Redux outlives this component, so `activeSlot`
  // being set says nothing about whether the USER chose it during this visit —
  // only a change away from this snapshot does.
  const mountSlotRef = useRef(activeSlot)
  // A deep link (?sid=) naming a DIFFERENT session than the one Redux carried
  // over owns the first switch of this mount — see the mount re-fetch effect.
  const deepLinkPendingRef = useRef(!!initialSidRef.current && initialSidRef.current !== activeSlot)
  const initialMsgRef = useRef(searchParams.get('msg'))
  const initialMidRef = useRef(searchParams.get('mid'))
  const initialNewRef = useRef(searchParams.get('new') === '1')
  const newRequestConsumedRef = useRef(false)
  const appDraftAgentRef = useRef<string | undefined>(undefined)
  /**
   * A prompt to seed the new session's composer with, carried by the SAME cold
   * URL that asks for the session: `/chat?new=1&prefill=<text>`. This is the deep
   * link an external launcher (a Slack card, a bookmarklet, a CLI `--open`) can
   * build, since the only other cold-URL prompt channel is the signed `?token=`
   * one it cannot mint. It SEEDS ONLY — auto-send stays behind that token path,
   * so a link can never spend a model turn without a human pressing Enter.
   *
   * Read only when `?new=1` is also present, which is the whole guard. A bare
   * `?prefill=<v>` is an in-app SENTINEL in this dashboard — the file explorer's
   * "Chat about this file" navigates to `/chat?prefill=1` and a project idea's
   * "Edit in chat" to `/chat?prefill=plan`, both with the real text riding Redux
   * `pendingInput` — so honouring one here would spawn a spurious empty session
   * and drop the literal `1` / `plan` into its composer.
   *
   * Captured at MOUNT because ChatPage's own `?prefill=` reader strips the param
   * from the URL, and handed to `createSlot` by `newSlotMutation` below, which
   * stages it under the created session's key before activating that session.
   */
  const initialPrefillRef = useRef(initialNewRef.current ? (searchParams.get('prefill') ?? '') : '')
  // Deep-link mount activation in progress — stops the sync effect from stripping
  // ?sid before activation lands. Cleared once activeSlot is truthy.
  const pendingSidRef = useRef(!!initialSidRef.current)
  // Back/Forward (POP) in flight — set ONLY by the POP effect. Kept separate from
  // pendingSidRef so a deep-link load doesn't trip the POP bail and freeze the
  // first sidebar switch.
  const popInFlightRef = useRef(false)
  // react-router reports the initial render as navigationType 'POP'. That first
  // run is the deep-link load (owned by initialSidRef), not a real Back/Forward —
  // skip it so the POP effect doesn't wrongly arm popInFlightRef on mount.
  const popReadyRef = useRef(false)
  // Last history entry key honored by the POP effect — distinguishes a genuine
  // Back/Forward (new location.key) from a re-render where navigationType is
  // still stuck at 'POP'.
  const lastLocKeyRef = useRef<string | null>(null)
  /**
   * True while the page's own `navigate(-1)` — consuming the history entry the
   * mobile sessions drawer pushed — is in flight.
   *
   * Owned here, alongside the other pop refs, because the effect below is what
   * reads it; the drawer that mints and spends the entry only writes it, through
   * the ref this controller returns. That pop is not the user retracing sessions:
   * the entry it lands on carries the `?sid=` from before the drawer opened,
   * which is the OUTGOING session whenever the drawer was closed by picking a
   * different one. Honoring it would switch the user straight back to the session
   * they just left. The URL is corrected by the `activeSlot → ?sid` effect below,
   * which replaces (never pushes) on mobile, so the entry ends up naming the
   * session actually on screen at the pre-drawer stack depth.
   */
  const drawerPopRef = useRef(false)
  /** Keys of history entries a session-switch PUSH created or left behind — the
   *  only ones a POP may legitimately return to as a session. Written by the
   *  `activeSlot -> ?sid` sync effect, read by the POP reader; see
   *  `popMaySwitchSession`. */
  const pushedSessionEntryKeys = useRef<Set<string>>(new Set())
  /** A push has been issued and its destination key is not minted yet — the sync
   *  effect records it on the commit that push lands in. */
  const pushedEntryPending = useRef(false)
  /**
   * The sync effect's last URL write while the router has not committed it.
   *
   * `<BrowserRouter>` commits location changes inside `startTransition`, so
   * after `navigate()` the browser is already on the new entry while the
   * rendered `locationKey` and `searchParams` still describe `fromKey`, the
   * entry the write left. Until a commit moves `locationKey` off `fromKey`, the
   * rendered URL is stale: a re-run must compare against `url`, not decide a
   * second push from the old `?sid=`. `sid` is the session the write named, so
   * a re-run can tell a same-session amend from a further session switch.
   */
  const inFlightWriteRef = useRef<{ fromKey: string; url: string; sid: string; pushed: boolean } | null>(null)
  /**
   * Set when Back lands on `fromKey` before the push off it committed. The
   * router then commits the key it already showed, which the POP reader would
   * otherwise treat as a re-render it has already honoured, and no URL input of
   * the sync effect changes. The reader consumes it; the sync effect holds
   * while it is set, because its rendered `?sid=` still looks like the switch.
   */
  const poppedToOriginKeyRef = useRef<string | null>(null)
  const [popOverWriteTick, setPopOverWriteTick] = useState(0)
  /** The one URL write a close holds back while its DELETE is unanswered: the
   *  entry naming the closing `sid`, standing under the `landing` session the
   *  close switched to. Per-mount, cleared once the URL moves off that sid. */
  const closeLandingRef = useRef<{ sid: string; landing: string } | null>(null)
  const [sidError, setSidError] = useState('')
  const [newSlotFailed, setNewSlotFailed] = useState(false)
  const [highlightTs, setHighlightTs] = useState<string | null>(null)
  const [appSlotLaunch, setAppSlotLaunch] = useState<ChatLaunchOptions | null>(null)
  const appSlotLaunchRef = useRef<ChatLaunchOptions | null>(null)

  // An app's explicit target is a session-entry action, not an ordinary PUSH
  // URL to ignore. Claim it before URL sync can restore the outgoing slot.
  useEffect(() => {
    if (embedded || !connected) return
    const launchWindow = window as Window & {
      __mc_chat_launch?: ChatLaunchOptions & { ts: number }
    }
    const intent = launchWindow.__mc_chat_launch
    if (!intent || Date.now() - intent.ts > 10_000) return
    // A newer launch, including a new-session request, supersedes a pending
    // target activation so its later completion cannot release an old message.
    appSlotLaunchRef.current = intent
    setAppSlotLaunch(null)
    if (!intent.slotKey || intent.slotKey !== searchParams.get('sid')) return
    delete launchWindow.__mc_chat_launch
    initialSidRef.current = null
    pendingSidRef.current = false
    // The app activation owns the mount fetch just like a normal deep link.
    deepLinkPendingRef.current = true
    popInFlightRef.current = true
    setSidError('')
    // Keep the claimed message across slow activation. Only a fulfilled switch
    // releases it to the composer; an HTTP failure must never become a send.
    void dispatch(switchSlot(intent.slotKey)).then(result => {
      if (appSlotLaunchRef.current !== intent) return
      popInFlightRef.current = false
      if (switchSlot.fulfilled.match(result)) {
        setAppSlotLaunch(intent)
      } else {
        const title = filteredSlots.find(slot => slot.key === intent.slotKey)?.title
        const failure = title
          ? i18nT('pages.chatPage.could_not_open_this_session', { title })
          : i18nT('appChatLaunch.targetUnavailable')
        setSidError(intent.message ? i18nT('appChatLaunch.unsent', { error: failure, message: intent.message }) : failure)
      }
    })
  }, [connected, dispatch, embedded, filteredSlots, locationKey, searchParams])

  // ?new=1: create a blank slot for an embed or a fresh desktop window.
  const newSlotMutation = useMutation({
    // The launcher's prompt rides the create itself: the thunk stages it before
    // the action that activates the slot, which is the only commit on which
    // ChatPage's slot-restore effect reads `kirocrew_prefill`. Staging it in
    // onSuccess would land after that read, so nothing would consume it.
    mutationFn: async () => {
      const prompt = initialPrefillRef.current
      const action = await dispatch(createSlot({ mode, agent: appDraftAgentRef.current, prefill: prompt || undefined }))
      // A rejected create throws here and keeps the prompt for the retry.
      const slot = unwrapResult(action)
      // Spent once: a retry of a failed create still carries the same intent, but
      // must not re-seed a session the user has since typed into.
      if (prompt && slot?.key) {
        // Storage can refuse the write (disabled, or full past the store's own
        // reclaim). Fall back to the in-memory `pendingInput` channel the command
        // bar and file explorer already seed composers through, so a refused write
        // degrades to "the prompt still arrives" rather than "the prompt is gone" —
        // the launcher's URL is consumed by the navigate in onSuccess either way.
        // `pendingInput` seeds whichever chat is active when it is dispatched.
        // That is the new slot unless the user switched chats during the create:
        // the fulfilled reducer then leaves their choice active, and the prompt
        // seeds that chat instead.
        if (!(createSlot.fulfilled.match(action) && action.meta.prefillStaged)) dispatch(setPendingInput(prompt))
        // Cleared only after the prompt is staged SOMEWHERE, so neither branch can
        // drop it: this is what makes the seed spent-once rather than lost-once.
        initialPrefillRef.current = ''
      }
      return slot
    },
    onSuccess: (slot) => {
      newSessionRef.current = false
      setNewSlotFailed(false)
      setSidError('')
      if (!slot?.key) return
      navigate(
        embedMode ? `/embed/chat/${slot.key}` : `/chat?sid=${encodeURIComponent(slot.key)}`,
        { replace: true },
      )
    },
    onError: () => {
      // Keep the failed window on its blank-session surface. Clearing only the
      // ref lets the auto-select effect silently fall back to an older session,
      // which makes a failed "New Window" look as if it copied that session.
      newSessionRef.current = false
      setNewSlotFailed(true)
      setSidError(i18nT('pages.chatPage.could_not_start_a_new_session'))
    },
  })
  useEffect(() => {
    if (searchParams.get('new') !== '1') { newRequestConsumedRef.current = false; return }
    if (newRequestConsumedRef.current || (embedded && !embedMode) || popout) return
    // URL synchronization can replace the history entry while creation is in
    // flight. The new=1 intent is spent once, not once per history key.
    newRequestConsumedRef.current = true
    initialNewRef.current = false
    initialPrefillRef.current = searchParams.get('prefill') ?? initialPrefillRef.current
    const launchWindow = window as Window & {
      __mc_chat_launch?: { ts?: number; message?: string; agent?: string; slotKey?: string; autoSend?: boolean }
    }
    const launch = launchWindow.__mc_chat_launch
    appDraftAgentRef.current = undefined
    if (!embedded && launch?.autoSend === false && !launch.slotKey
      && Date.now() - (launch.ts ?? 0) <= 10_000) {
      // Retain the claimed draft through create failure/retry. The create
      // mutation is the single owner of the prefill handoff.
      initialPrefillRef.current = launch.message ?? ''
      appDraftAgentRef.current = launch.agent
      delete launchWindow.__mc_chat_launch
    }
    newSessionRef.current = true
    setNewSlotFailed(false)
    setSidError('')
    if (!embedMode) dispatch(setActiveSlot(null))
    newSlotMutation.mutate()
  }, [locationKey, searchParams, embedded, embedMode, popout, dispatch, newSessionRef, newSlotMutation])

  // Choosing a real session explicitly abandons a failed blank-window intent;
  // its banner must not follow the user into the selected conversation.
  useEffect(() => {
    if (!activeSlot || !newSlotFailed) return
    setNewSlotFailed(false)
    setSidError('')
  }, [activeSlot, newSlotFailed])

  // On mount, URL ?sid= drives which session is active (URL wins over localStorage)
  useEffect(() => {
    if (embedded && !embedMode) return
    if (!connected) return  // offline: defer URL-driven switchSlot until reconnect
    const urlSlot = initialSidRef.current
    if (!urlSlot) return
    // The deep-link ?sid only sets the INITIAL active slot. The slot list can
    // populate AFTER the user has already clicked a different session in the
    // sidebar (switchSlot.pending sets activeSlot synchronously); without this
    // guard the delayed activation would override that click and snap the UI
    // back to the deep-linked session.
    //
    // The comparison is against the slot as of MOUNT, not against "is there any
    // active slot at all". `activeSlot` lives in Redux, which outlives this
    // component: a deep link followed from another dashboard page (the System
    // page's Session & Task Memory rows, Telemetry's conversation links) mounts
    // here with the previously-visited session already active, and a bare
    // truthiness check read that as "the user already chose" and silently
    // dropped the link — you clicked a session and landed on a different one.
    // Only a switch that happened AFTER this mount is a real user choice.
    // Both abandon paths clear the in-flight flag, because arming happens BELOW
    // and this effect re-runs: an earlier run can have armed it while waiting for
    // a slot that had not arrived, and the run that abandons the link is a
    // different one. Leaving it set would kill URL sync for the rest of the mount
    // — and the not-found timeout is no backstop here, since it only acts while
    // `initialSidRef` is still set, which these branches clear.
    if (activeSlot !== mountSlotRef.current) {
      initialSidRef.current = null
      popInFlightRef.current = false
      return
    }
    if (activeSlot === urlSlot) {
      initialSidRef.current = null
      popInFlightRef.current = false
      return
    }
    // Armed BEFORE the slot is known to exist, because the wait is exactly when
    // the damage happens: a session created and linked in one go (the app pages'
    // create-then-navigate) puts `?sid=` in the URL before its slots frame
    // arrives, and during that window the URL-sync effect below sees a `sid` it
    // cannot match and PUSHes a history entry for the carried-over session — so
    // Back opens that session instead of the page the link came from. Same
    // stale-closure hazard a Back/Forward has, so it takes the same guard.
    // Released by the sync effect once activeSlot matches the URL, and by the
    // not-found timeout, so a link that never resolves cannot wedge URL sync.
    popInFlightRef.current = true
    // `some` on an empty list is false, so an unpopulated slot list waits here
    // too; this effect re-runs when `filteredSlots` arrives.
    if (filteredSlots.some(s => s.key === urlSlot)) {
      initialSidRef.current = null
      popInFlightRef.current = true
      dispatch(switchSlot(urlSlot))
    }
    // Don't error immediately — slot may arrive via SSE shortly
    // embedded/embedMode are read in the guard above; they are stable for the
    // session, so listing them satisfies the linter without changing behavior.
  }, [filteredSlots, activeSlot, dispatch, connected, embedded, embedMode])

  // React to ?sid= changes AFTER mount — required for plugin tab switching
  // where the URL is updated via react-router navigate() (soft nav). The
  // mount-only initialSidRef approach above misses these updates because
  // the component doesn't remount across soft navs. Without this effect
  // the "activeSlot → URL" sync below would rewrite the URL back to the
  // current activeSlot instead of switching to the slot the URL is asking
  // for.
  //
  // Embed mode: react to ANY ?sid change (the host app drives the URL).
  // Where the session the URL names stands in its own close: 'in-flight' while
  // our DELETE is unanswered, 'confirmed' once the server popped it, 'none'
  // otherwise (live, never closed here, or a close that failed and released).
  const urlSidKey = searchParams.get('sid') || searchParams.get('slot')
  const urlSidClose = useAppSelector(s => {
    const closing = s.dashboard.closingSlots ?? {}
    const hold = closeHoldForUrl(closing, urlSidKey)
    if (!hold) return 'none'
    return hold.inFlightUntil !== null ? 'in-flight' : 'confirmed'
  })
  const urlSidRemoved = useAppSelector(s => !!closeHoldForUrl(
    s.dashboard.durablyRemoved ?? {},
    urlSidKey,
  ))
  // Confirmed holds are bounded, so remember every key this mount observed in
  // that phase. Primitive selector results stay stable across unrelated store
  // updates while exposing both the confirmed and retained hold populations.
  const confirmedCloseKeys = useAppSelector(s => Object.entries(s.dashboard.closingSlots ?? {})
    .filter(([, hold]) => hold.inFlightUntil === null)
    .map(([key]) => key)
    .sort()
    .join('\u0000'))
  const allHoldKeys = useAppSelector(s => Object.keys(s.dashboard.closingSlots ?? {})
    .sort()
    .join('\u0000'))
  const slotsGeneration = useAppSelector(s => s.dashboard.slotsGeneration)
  const confirmedClosedRef = useRef<Map<string, { retiredAtGen: number | null }>>(new Map())
  useEffect(() => {
    const held = new Set(allHoldKeys ? allHoldKeys.split('\u0000') : [])
    if (confirmedCloseKeys) {
      for (const key of confirmedCloseKeys.split('\u0000')) {
        if (!confirmedClosedRef.current.has(key)) confirmedClosedRef.current.set(key, { retiredAtGen: null })
      }
    }
    for (const [key, close] of confirmedClosedRef.current) {
      if (held.has(key)) {
        close.retiredAtGen = null
      } else if (close.retiredAtGen === null) {
        close.retiredAtGen = slotsGeneration
      }
    }
  }, [confirmedCloseKeys, allHoldKeys, slotsGeneration])
  // A resume or fork under a closed key makes it live again: forget it, so a
  // later list that omits the live session cannot get its entry rewritten.
  useEffect(() => {
    for (const s of slots) confirmedClosedRef.current.delete(s.key)
  }, [slots])
  const rewriteEligible = useCallback((key: string) => {
    if (key === urlSidKey && urlSidRemoved) return true
    const close = confirmedClosedRef.current.get(key)
    return close?.retiredAtGen !== null && close?.retiredAtGen !== undefined
      && slotsGeneration > close.retiredAtGen
  }, [slotsGeneration, urlSidKey, urlSidRemoved])
  // Embed only: the history entry and `?sid=` last seen agreeing with
  // `activeSlot`. A mismatch on that same entry is a switch made in this
  // frame (a fork, a new chat), not a host request; see the reader below.
  const embedAgreedSidRef = useRef<{ locationKey: string; sid: string } | null>(null)
  // Back that lands before our push committed. The browser reports it at once;
  // the router reports it only when its transition commits, and then with the
  // entry key it was already showing. Read the live entry here, because nothing
  // the reader sees in render can tell this POP from a stale re-render. The embed
  // chat popout also records writes, so it drops the stale record here; only the
  // main dashboard's POP reader honours the mark.
  useEffect(() => {
    if (embedMode === 'sessions' || noUrlSync) return
    const onPopState = () => {
      const write = inFlightWriteRef.current
      if (!write?.pushed) return
      if (liveHistoryKey() !== write.fromKey) return
      inFlightWriteRef.current = null
      pushedEntryPending.current = false
      if (embedMode) return
      poppedToOriginKeyRef.current = write.fromKey
      setPopOverWriteTick(n => n + 1)
    }
    window.addEventListener('popstate', onPopState)
    return () => window.removeEventListener('popstate', onPopState)
  }, [embedMode, noUrlSync])
  // Main dashboard: react ONLY to a genuine Back/Forward (navigationType POP).
  // Our own activeSlot→URL writes are PUSH/REPLACE, so they never re-enter here
  // — that is what avoids the activeSlot↔URL ping-pong. A session switch pushes
  // a ?sid history entry (sync effect
  // below), so native browser/Electron Back/Forward (and Alt+←/→) retrace the
  // sessions you've visited.
  //
  // Also gated on `connected`: when offline the switchSlot dispatch fails
  // (fetchSlotDetail rejects) and clears messages, leaving an activeSlot
  // with empty messages — the WelcomeView fallback then renders. Defer
  // the switch until reconnect so cached state stays put.
  useEffect(() => {
    // noUrlSync: the host page owns the URL and the panel's session is chosen by
    // the host, never by a query param. This effect otherwise treats embedMode as
    // "the host drives ?sid" and would switch the panel onto whatever session the
    // host route happens to carry.
    if (noUrlSync) return
    // Rewrite the entry we are STANDING ON so its `?sid=` names the session
    // actually on screen. Shared by the two POP paths that must not honour a
    // stale sid, because the alternative — leaving the URL wrong and relying on
    // the `activeSlot -> ?sid` effect below to catch up — has already needed a
    // follow-up fix once. Replacing (never pushing) keeps the stack
    // depth the pop just restored, and `activeSlotRef` is the render-current
    // value even when the pop originated in this same commit.
    const repairPoppedSid = () => {
      const target = activeSlotRef.current
      const urlSlot = searchParams.get('sid') || searchParams.get('slot')
      if (!target || target === urlSlot) return
      const next = new URLSearchParams(searchParams)
      next.set('sid', target)
      next.delete('slot')
      // Never re-write a credential the token effect already stripped.
      next.delete('token')
      navigate(
        { pathname: locationPathname, search: `?${next}`, hash: locationHash },
        { replace: true },
      )
    }
    // Our own drawer-entry consumption, not a Back/Forward the user asked for.
    // Correct the POP's stale outgoing `?sid=` HERE, in the effect that owns the
    // POP, rather than relying on the separate activeSlot -> URL effect below.
    // In a real mobile browser those two navigation effects can be committed in
    // either order: a fast New Chat activates the newborn slot, closes the
    // drawer, then this POP lands on the duplicate's predecessor naming the old
    // slot. If the generic sync is still gated by a prior POP claim, the old URL
    // wins and its reader switches Redux straight back, leaving an empty New
    // Session row behind. MemoryRouter settles synchronously and hid that race.
    //
    // `activeSlotRef` is the render-current value even when the close originated
    // from createSlot.fulfilled in this same commit. Replacing only this popped
    // entry also preserves the one-entry drawer invariant: the duplicate is gone,
    // and the surviving history entry names the session actually on screen.
    if (drawerPopRef.current) {
      drawerPopRef.current = false
      lastLocKeyRef.current = locationKey
      popInFlightRef.current = false
      repairPoppedSid()
      return
    }
    // Embed: host app drives the URL — react to any ?sid change.
    // Main dashboard: honor only a genuine Back/Forward POP. react-router reports
    // the initial render as 'POP' and stays 'POP' until our own switch navigates
    // (PUSH/REPLACE); a real Back/Forward is a POP that follows one of those. So
    // arm on the first non-POP nav and only honor POP once armed — this ignores
    // the mount POP (deep-link load, owned by initialSidRef) so it can't wrongly
    // arm popInFlightRef and freeze the next switch.
    // A Back the browser took off our uncommitted push (see `poppedToOriginKeyRef`)
    // is honoured whatever the router has committed so far: the entry we stand on
    // is the one the push left, and it names the session to return to. The mark
    // is consumed before the `connected` gate, as any POP is, so an offline Back
    // cannot leave it set and hold URL sync on this entry.
    const poppedOverWrite = !embedMode && poppedToOriginKeyRef.current === locationKey
    if (poppedOverWrite) {
      poppedToOriginKeyRef.current = null
      popReadyRef.current = true
      lastLocKeyRef.current = locationKey
    } else if (!embedMode) {
      if (navigationType !== 'POP') { popReadyRef.current = true; lastLocKeyRef.current = locationKey; return }
      if (!popReadyRef.current) return
      // navigationType stays 'POP' after a Back/Forward until our own navigate()
      // runs. Without this guard the effect re-fires on every activeSlot change
      // (a sidebar click) while still 'POP', reads the stale URL sid, and reverts
      // the click — locking the URL to one chat. location.key changes only on a
      // genuine history navigation, so honor a POP exactly once per new entry.
      if (locationKey === lastLocKeyRef.current) return
      lastLocKeyRef.current = locationKey
    }
    if (!connected) return
    const urlSid = searchParams.get('sid') || searchParams.get('slot')
    if (embedMode && urlSid && urlSid === activeSlot) embedAgreedSidRef.current = { locationKey, sid: urlSid }
    if (!urlSid || urlSid === activeSlot) return
    // Embed: follow the host when it moves the URL, never because `activeSlot`
    // moved. A popout is an embed whose URL nobody else drives, and a fork in it
    // switches to the child while `?sid=` still names the source on the same
    // entry. Obeying that stale value would switch straight back in the same
    // commit and leave the URL writer below holding.
    // Skipping here lets the writer name the new session, which also re-keys the
    // popout's registration (PopoutFrame reads `?sid=`). A host navigation, even
    // to the same sid, is a new entry and is still obeyed.
    const agreed = embedAgreedSidRef.current
    if (embedMode && agreed && agreed.sid === urlSid && agreed.locationKey === locationKey) return
    // Mobile replaces on every session switch, so an entry here was pushed by a
    // switch only if the write side recorded it as one — see
    // `popMaySwitchSession`. Repair an unrecorded stale sid rather than obeying
    // it, which is what walked the pane back into the outgoing chat when the
    // sessions drawer's pop arrived a commit late.
    const entryPushedBySwitch = pushedSessionEntryKeys.current.has(locationKey)
    if (!embedMode && !popMaySwitchSession({ isMobile, entryPushedBySwitch })) {
      repairPoppedSid()
      return
    }
    if (filteredSlots.some(s => s.key === urlSid)) {
      popInFlightRef.current = true
      dispatch(switchSlot(urlSid))
      return
    }
    if (embedMode || !activeSlotRef.current || slots.some(s => s.key === urlSid)) return
    // A durable `removed` frame is post-pop truth for this tab, so repair this
    // history entry immediately even while its row hold is still retained.
    if (urlSidRemoved) {
      popInFlightRef.current = true
      repairPoppedSid()
      return
    }
    // Without that frame, keep the budget-plus-one-list fallback: another tab
    // may have resumed the key while the hold filters its row.
    if (urlSidClose !== 'none' || (confirmedClosedRef.current.has(urlSid) && !rewriteEligible(urlSid))) {
      closeLandingRef.current = { sid: urlSid, landing: activeSlotRef.current }
      return
    }
    if (!rewriteEligible(urlSid)) return
    popInFlightRef.current = true
    repairPoppedSid()
  }, [searchParams, filteredSlots, slots, slotsGeneration, urlSidClose, urlSidRemoved, rewriteEligible, activeSlot, activeSlotRef, dispatch, embedMode, navigationType, locationKey, locationPathname, locationHash, connected, noUrlSync, navigate, isMobile, popOverWriteTick])

  // Timeout: if slot never appears after 5s, show an error. Keep the denied key
  // so a later authoritative slots frame can revoke that verdict; keeping
  // `initialSidRef` itself set would re-arm popInFlightRef on every render and
  // wedge URL synchronization.
  const deniedSidRef = useRef<{
    key: string
    activeSlot: string | null
    activeSlotChanged: boolean
    error: string
    deniedAt: number
  } | null>(null)
  // Equality at recovery time cannot distinguish "stayed on A" from A -> B -> A.
  // Latch the first committed change while a denied link is pending; returning
  // to the deadline's slot must not give the old deep link ownership again.
  useEffect(() => {
    const denied = deniedSidRef.current
    if (denied && activeSlot !== denied.activeSlot) denied.activeSlotChanged = true
  }, [activeSlot])
  // Gated on `connected` so the timer only runs while the gateway is reachable
  // — otherwise an offline tab would burn its 5s while the resolve effects
  // above are deferred, fire a false "Session not found", clear initialSidRef,
  // and the resolve never happens once the gateway comes back. Re-runs the
  // effect when connected flips so the timer starts fresh on reconnect.
  // Also gated on `slotsLoaded`: arming before the list lands would reject every
  // slow first frame. A landed list starts the deadline, but is not assumed to
  // be the final restored list.
  const slotsLoaded = useAppSelector(s => s.dashboard.slotsLoaded)
  useEffect(() => {
    if (!connected || !slotsLoaded) return
    const urlSlot = initialSidRef.current
    // A short name is never in the roster under that spelling; its own lookup
    // below gives the verdict, so a slow answer is not pre-empted as missing.
    if (!urlSlot || sessionShortNameFrom(urlSlot)) return
    const timer = setTimeout(() => {
      if (initialSidRef.current) {
        const error = i18nT('pages.chatPage.session_not_found', { name: urlSlot })
        deniedSidRef.current = {
          key: urlSlot,
          activeSlot: activeSlotRef.current,
          activeSlotChanged: false,
          error,
          deniedAt: Date.now(),
        }
        initialSidRef.current = null
        pendingSidRef.current = false
        popInFlightRef.current = false
        setSidError(error)
        // Deliberately does NOT refresh the session on screen. The deep link did
        // own this mount's fetch, so that session's messages can be as stale as
        // Redux left them — but a refresh here races the user: five seconds is
        // long enough to type and send, and the in-flight response would land
        // after the optimistic row and replace both it and `running`, making the
        // turn they just sent disappear. Stale-until-next-interaction is the
        // lesser fault, and the banner above tells them the link failed.
      }
    }, 5000)
    return () => clearTimeout(timer)
  }, [connected, slotsLoaded, activeSlotRef])

  // A slots frame can arrive after the deadline while the gateway restores its
  // full session list. Once it carries the denied key, the old banner is false.
  // Resolve the original link only if the user is still on the session that was
  // active when the deadline fired; otherwise clear the lie without snapping
  // them back over a later choice.
  useEffect(() => {
    const denied = deniedSidRef.current
    if (!denied || !filteredSlots.some(slot => slot.key === denied.key)) return
    deniedSidRef.current = null
    // "Active slot unchanged" reads as "still waiting for the link" only while
    // the restore it was denied by could still be running. The record outlives
    // that window — no expiry, and reconnects re-deliver the full list — so a
    // frame arriving minutes later would otherwise yank a user who has been
    // reading or composing here the whole time. Retire the false banner, stay put.
    if (Date.now() - denied.deniedAt > SID_RECOVERY_WINDOW_MS) {
      setSidError(current => current === denied.error ? '' : current)
      return
    }
    if (denied.activeSlotChanged || activeSlotRef.current !== denied.activeSlot) {
      setSidError(current => current === denied.error ? '' : current)
      return
    }
    // The late frame has disproved the deadline verdict before transcript loading
    // begins. Retire that stale local notice now; `switchSlot` owns any load
    // failure through its announced, localized ErrorNotice and structured report.
    setSidError(current => current === denied.error ? '' : current)
    popInFlightRef.current = true
    void dispatch(switchSlot({ key: denied.key, announceOnMissing: true })).then(() => {
      popInFlightRef.current = false
    })
  }, [filteredSlots, activeSlotRef, dispatch])

  // Sync activeSlot → ?sid= in URL (persistent deep-link)
  // Skip entirely when embedded — URL belongs to the host app
  const basePath = popout ? '/popout/chat' : embedMode === 'chat' || embedMode === 'sessions' ? '/embed/chat' : '/chat'
  const searchParamsRef = useRef(searchParams)
  searchParamsRef.current = searchParams

  // A deep link that names a session by its SHORT name (`?sid=chat-7`), which is
  // how the sidebar, agent tools and people name it. The mount effect above
  // matches `?sid=` exactly, so it holds URL sync and waits; this asks the
  // gateway for every transcript under that name, adds the open roster, and
  // opens the oldest. A slot number is reused across gateway generations, so the
  // rest are listed in a notice. No match says so; nothing opens or is created.
  // A full key never takes this path.
  const [sidMatches, setSidMatches] = useState<{ name: string; opened: ShortNameMatch; others: ShortNameMatch[] } | null>(null)
  const shortSidLookupRef = useRef<string | null>(null)
  const unmountedRef = useRef(false)
  // Guard component lifetime, not lookup-effect reruns: the once-per-link lookup must keep its answer.
  useEffect(() => {
    unmountedRef.current = false
    return () => { unmountedRef.current = true }
  }, [])
  const queryClient = useQueryClient()
  const openShortNameMatch = useCallback((match: ShortNameMatch) => {
    if (!connectedRef.current) return
    // The roster is read at click time: a listed closed session may have been
    // resumed since the notice appeared, and an open one is switched to.
    const open = filteredSlotsRef.current.some(s => s.key === match.key)
    if (match.stem && !open) void dispatch(resumeFromHistory({ key: match.stem, title: match.title }))
    else dispatch(switchSlot({ key: match.key, announceOnMissing: true }))
  }, [dispatch, filteredSlotsRef])
  useEffect(() => {
    if (embedded && !embedMode) return
    if (!connected || !slotsLoaded) return
    const urlSlot = initialSidRef.current
    const short = urlSlot ? sessionShortNameFrom(urlSlot) : null
    if (!urlSlot || !short || shortSidLookupRef.current === urlSlot) return
    shortSidLookupRef.current = urlSlot
    // One attempt, never cached: a retry would hold the link open on a dead
    // gateway, and a cached list would miss a session created since.
    void queryClient.fetchQuery({
      queryKey: ['sessions-named', short],
      queryFn: () => api.sessionsNamed(short),
      retry: false,
      staleTime: 0,
      gcTime: 0,
    }).then(r => r.sessions ?? [], () => null).then(onDisk => {
      if (unmountedRef.current) return
      // The mount effect clears the ref when the user picked a session first.
      if (initialSidRef.current !== urlSlot) return
      initialSidRef.current = null
      const matches = shortNameMatches(short, filteredSlotsRef.current, onDisk ?? [])
      if (!matches.length) {
        // Keep the URL held while no session is active; a user switch releases
        // pendingSidRef in URL sync without letting a slots frame auto-select.
        popInFlightRef.current = false
        // A failed lookup cannot say no session has this name.
        setSidError(onDisk
          ? i18nT('pages.chatPage.no_session_named', { name: short })
          : i18nT('pages.chatPage.session_not_found', { name: short }))
        return
      }
      const [target, ...others] = matches
      const mine = others.length ? { name: short, opened: target, others } : null
      setSidMatches(mine)
      // Name the full key before activating it: URL sync stays held until
      // `activeSlot` equals `?sid=`, and a short name never does.
      const next = new URLSearchParams(searchParamsRef.current)
      next.set('sid', target.key)
      next.delete('slot')
      navigate({ pathname: locationPathname, search: `?${next}`, hash: locationHash }, { replace: true })
      if (!target.stem) {
        dispatch(switchSlot(target.key))
        return
      }
      void dispatch(resumeFromHistory({ key: target.stem, title: target.title })).then(result => {
        // The user left ChatPage while the resume ran: its captured route
        // is stale, and a navigate here would pull them back to chat.
        if (unmountedRef.current) return
        // A refused, renamed or off-surface resume never reaches `?sid=`
        // (the reducer leaves activeSlot alone off-surface); release URL sync.
        // Nor does one the reducer declined to open because the user moved
        // while it ran, so the live active slot decides, as it does in
        // handleResumeSession.
        const resumed = resumeFromHistory.fulfilled.match(result) && result.payload.ok
          && result.payload.key === target.key && isChatPageSurface(result.payload.surface)
          && store.getState().chat.activeSlot === target.key
        if (resumed) return
        popInFlightRef.current = false
        // The URL was rewritten to name the target, and URL sync skipped
        // every switch while it was held. Name the session on screen
        // instead, in place: a push would leave a Back entry for a session
        // that never showed. URL sync then normalizes the slug.
        const viewing = store.getState().chat.activeSlot
        if (viewing && viewing !== target.key) {
          const back = new URLSearchParams(searchParamsRef.current)
          back.set('sid', viewing)
          back.delete('slot')
          navigate({ pathname: locationPathname, search: `?${back}`, hash: locationHash }, { replace: true })
        }
        // "Opened <title>" would name a session that never opened; the
        // unresumable notice explains why. Leave a later link's notice alone.
        setSidMatches(cur => (cur === mine ? null : cur))
      })
    })
    // Read once per link: the refs carry the live roster and URL into the answer.
  }, [connected, slotsLoaded, embedded, embedMode, dispatch, navigate, locationPathname, locationHash, filteredSlotsRef, queryClient, store])

  useEffect(() => {
    if (embedded && !embedMode) return
    // noUrlSync (artifact companion chat panel): the host page owns the URL
    // entirely (e.g. /artifacts/:slug) and passes embedMode="chat" only for its
    // single-session chrome (no sessions sidebar). Never write ?sid= or
    // navigate to basePath — an in-place navigate would swap the host route out
    // from under the panel. The sid-READ paths are gated for the same flag
    // above (initialSidRef + the post-mount POP effect); do not assume a
    // noUrlSync host route is sid-free.
    if (noUrlSync) return
    // In sessions embed mode, the URL is `/embed/sessions` regardless of
    // activeSlot. Navigation away from sessions is driven by the explicit
    // onSelectSlot callback in ChatSidebar — never auto-navigate from here,
    // since activeSlot may change due to background state (initial load,
    // localStorage hydration, WS updates) which would unwantedly bounce
    // the user back into chat view.
    if (embedMode === 'sessions') return
    // Land the key claimed by our own push below. This effect re-runs on
    // `locationKey`, so the first run after that navigate is standing ON the
    // entry the push created — the Forward target. Placed ahead of every early
    // return: the run that lands here is exactly the one where the URL already
    // agrees with `activeSlot`, which is the earliest bail.
    // A run still standing on the entry our last write left is a re-run before
    // that write committed, not the landing.
    if (inFlightWriteRef.current && inFlightWriteRef.current.fromKey !== locationKey) inFlightWriteRef.current = null
    const inFlight = inFlightWriteRef.current
    if (pushedEntryPending.current && !inFlight) {
      pushedEntryPending.current = false
      pushedSessionEntryKeys.current.add(locationKey)
    }
    // Back already took the browser off our uncommitted push; the POP reader owns
    // this entry, and the stale `?sid=` here would push the URL forward again.
    if (poppedToOriginKeyRef.current === locationKey) return
    const sp = searchParamsRef.current
    // Back/Forward (POP) activation in flight: the browser already set the URL to
    // the target session and activeSlot is catching up via the switchSlot the
    // ?sid→activeSlot effect above just dispatched. Writing the URL here would run
    // with a STALE activeSlot (the slot we're leaving) and push a spurious history
    // entry for it — corrupting multi-step Back/Forward. Bail until activeSlot
    // matches the URL, then fall through for replace-only slug normalization (a POP
    // must never produce a push).
    if (popInFlightRef.current) {
      // `sid || slot` — the same pair the READ paths accept. A legacy `?slot=`
      // link resolves through this flag too, and matching on `sid` alone would
      // never release it: the flag would stay armed for the life of the mount,
      // so URL sync would be dead and a later session switch would leave the
      // URL (and therefore a reload) pointing at the wrong session.
      const urlSlot = sp.get('sid') || sp.get('slot')
      if (!activeSlot || activeSlot !== urlSlot) return
      popInFlightRef.current = false
    }
    if (!activeSlot) {
      // A popout's `?sid=` names the session it was opened for. Dropping it
      // would leave a reload with no sid of its own, and PopoutFrame would
      // register the window for whatever chat it picked next.
      if (popout) return
      if (sp.has('sid') && !initialSidRef.current && !pendingSidRef.current) {
        navigate(basePath, { replace: true })
      }
      return
    }
    pendingSidRef.current = false
    const slot = filteredSlots.find(s => s.key === activeSlot)
    const slug = slot?.title && slot.title !== slot.key ? toSlug(slot.title) : ''
    const expectedPath = `${basePath}${slug ? '/' + slug : ''}`
    const next = new URLSearchParams(sp)
    next.set('sid', activeSlot)
    next.delete('slot')
    next.delete('prefill')
    next.delete('autoSend')
    next.delete('newSession')
    next.delete('msg')
    // Never re-write a credential the token effect already stripped
    // (session deep links arrive as `/chat?sid=…&token=…`).
    next.delete('token')
    const url = `${expectedPath}?${next}`
    // Our last write has not committed, so `sp` and `locationPathname` name the
    // entry it left and cannot decide anything. The browser is on the written
    // entry: leave it if it already says this, and amend it in place when the
    // session is the same, as when the title lands and the slug changes. A
    // different session is a further switch, decided below against the written
    // entry.
    if (inFlight) {
      if (inFlight.url === url) return
      if (inFlight.sid === activeSlot) {
        inFlight.url = url
        navigate(url, { replace: true })
        return
      }
    } else if (sp.get('sid') === activeSlot && locationPathname === expectedPath) return
    // `current` is the key the entry we stand on names, legacy `?slot=` included,
    // so close eligibility below reads the same key as the POP reader. The no-op
    // bail above compares the canonical `?sid=` alone: a legacy URL naming the
    // active session still falls through and is rewritten to `?sid=`.
    const current = inFlight ? inFlight.sid : (sp.get('sid') || sp.get('slot'))
    // Push vs replace — see `shouldReplaceSessionUrl` for why mobile never
    // pushes. Kept as a named predicate rather than an inline boolean so the
    // reasoning has somewhere to live and a test can pin it.
    // Closing the session on screen leaves the URL naming it. While our DELETE
    // is unanswered, only the LANDING write waits: a failed close brings the
    // session back, and its entry must still lead there. A later switch during
    // that window pushes as usual, leaving the closing entry behind for the POP
    // reader to repair once this tab sees the close confirmed. Once the server
    // confirms while the landing is still on screen, the entry is dead —
    // replace it, because a push would leave it reachable and the POP reader
    // would have to repair it on every Back.
    const currentAbsent = !!current && current !== activeSlot && !slots.some(s => s.key === current)
    const currentConfirmed = !!current && confirmedClosedRef.current.has(current)
    const eligibleByRule = !!current && rewriteEligible(current)
    // The durable patch is immediate history truth. If this tab missed it, the
    // retired-hold generation rule remains the conservative fallback.
    const currentGone = currentAbsent && (
      urlSidRemoved || (urlSidClose === 'none' && eligibleByRule)
    )
    const pendingClose = currentAbsent && !currentGone && (
      urlSidClose !== 'none' || (currentConfirmed && !eligibleByRule)
    )
    // Keep the landing latch throughout in-flight -> confirmed -> retired ->
    // eligible. If the key becomes live again, or the URL moves elsewhere, the
    // latch no longer describes the entry we are standing on.
    if (closeLandingRef.current && (closeLandingRef.current.sid !== current || (!pendingClose && !currentGone))) {
      closeLandingRef.current = null
    }
    if (pendingClose) {
      // The first session on screen while the URL still names the closing key
      // is the landing. A Back onto a dying entry lands the same way.
      closeLandingRef.current ??= { sid: current, landing: activeSlot }
      if (activeSlot === closeLandingRef.current.landing) return
    }
    const isSessionSwitch = !!current && current !== activeSlot && !currentGone
    const replace = shouldReplaceSessionUrl({ isSessionSwitch, isMobile })
    // A push leaves the entry we are standing on behind, still naming the session
    // it was showing, and CREATES another naming the session switched to. Both
    // were made by a session switch, so both are legitimate history targets — the
    // first for Back, the second for Forward — and the POP reader must honour
    // either even if the layout has since crossed the mobile breakpoint. Recorded
    // here because this is the only place that knows a push happened; the reader
    // would otherwise have to guess from the viewport, which is read at a
    // different time (see `popMaySwitchSession`). Per-mount, like every other ref
    // this reader keeps: a reload starts the history bookkeeping over anyway.
    //
    // The destination's key does not exist yet — the router mints it — so it is
    // claimed here and recorded by the block at the top of this effect on the
    // commit the push lands in. Recording only the entry left behind made Forward
    // read as stale, and the reader REPAIRS what it declines, so Forward
    // overwrote the very session it should have restored.
    //
    // A push on top of an uncommitted write leaves the written entry behind, so
    // its live key is recorded too. `fromKey` stays the rendered key, because
    // only a commit off it ends the in-flight window.
    if (!replace) {
      pushedSessionEntryKeys.current.add(inFlight ? liveHistoryKey() : locationKey)
      pushedEntryPending.current = true
    }
    inFlightWriteRef.current = { fromKey: locationKey, url, sid: activeSlot, pushed: !replace }
    navigate(url, { replace })
    // `locationKey` and not just `locationPathname`: consuming the mobile
    // drawer's history entry lands on a DIFFERENT entry whose pathname is
    // IDENTICAL (the entry was a duplicate), so pathname alone reports no change
    // and this effect would not re-run — leaving `?sid=` naming the session the
    // user just switched AWAY from, which a reload would then restore. The key
    // changes on any history move, which is the thing that actually happened.
    // POPs are still funnelled through the `popInFlightRef` bail above, so this
    // adds a re-check, not a new writer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSlot, filteredSlots, slots, slotsGeneration, urlSidClose, urlSidRemoved, rewriteEligible, navigate, basePath, locationPathname, locationKey, embedded, noUrlSync, isMobile, popout])

  // Re-fetch slot messages on mount (handles nav away + back).
  // Skip when newSession=1 — createSlot in send() will set the active slot;
  // dispatching switchSlot here would race and overwrite it.
  //
  // Also skipped while a deep link (?sid=) names a DIFFERENT session: this
  // effect runs after the sid-activation effect above, so re-fetching the slot
  // Redux carried over from the previous page would switch straight back and
  // silently undo the link — clicking a session on the System page landed you
  // in whatever chat you had open before. The sid effect's own switchSlot
  // fetches, so nothing is lost by skipping here.
  useEffect(() => { if (!deepLinkPendingRef.current && activeSlot && !newSessionRef.current && filteredSlotsRef.current.find(s => s.key === activeSlot)) dispatch(switchSlot(activeSlot)) }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Clear activeSlot when it belongs to a different mode (page switch)
  useEffect(() => {
    if (activeSlot && slots.length > 0 && !filteredSlots.find(s => s.key === activeSlot)) {
      dispatch(dropActiveSlot())
    }
  }, [activeSlot, slots.length, filteredSlots, dispatch])

  // Auto-select slot after refresh — restore from localStorage or pick first
  // If no slots exist at all, auto-create one so the user lands in a ready chat
  const autoCreatedRef = useRef(false)
  const hadActiveSlotRef = useRef(false)
  // The last chat THIS window showed. A popout is bound to it.
  const lastShownSlotRef = useRef<string | null>(null)
  // This window's own recency stack (activeSlot is never in it). The stored
  // key is shared by every tab and popout of the origin, so it cannot say
  // what this window was doing.
  // Read through a ref: `switchSlot` rewrites the array on every dispatch, so
  // as an effect dependency it would re-run the pick it just made.
  const slotHistory = useAppSelector(s => s.chat.slotHistory)
  const slotHistoryRef = useRef(slotHistory); slotHistoryRef.current = slotHistory
  // The hub's record of this pane's last chat (#16009), used for the first
  // pick only. A soft hint, unlike ?sid=: a chat that is gone falls through.
  const slotHintRef = useRef(embedded ? null : searchParams.get(PANE_SLOT_HINT_PARAM))
  // A create, fork or resume started here that will open its own chat when it lands.
  // A resume past the lease is hung: its entry stays until another resume
  // starts, and it must not hold the page empty for another lease.
  const openPending = useAppSelector(s => s.chat.foregroundCreateId != null || (s.chat.pendingForkOpens ?? 0) > 0 || resumeStillOpening(s.chat, Date.now()))
  // The wait is a lease, not a promise: create and fork POSTs carry no
  // deadline, so a request the connection never settles would otherwise keep
  // the page empty for good. The lease starts when the page loses its chat
  // with a request pending and lapses after PENDING_OPEN_LEASE_MS, the bound
  // that also ends a pending resume's repeat guard; the pick
  // below then runs as if nothing were pending. It never touches the pending
  // count or foregroundCreateId, so a late settlement still lowers exactly
  // its own share, and a late success still opens its chat (the pick was an
  // automatic move, which `forkMayOpen` lets it undo).
  const waitingForOpen = !activeSlot && openPending
  const [openWaitLapsed, setOpenWaitLapsed] = useState(false)
  useEffect(() => {
    if (!waitingForOpen) return
    const lease = setTimeout(() => setOpenWaitLapsed(true), PENDING_OPEN_LEASE_MS)
    return () => { clearTimeout(lease); setOpenWaitLapsed(false) }
  }, [waitingForOpen])
  useEffect(() => {
    if (activeSlot) {
      hadActiveSlotRef.current = true
      lastShownSlotRef.current = activeSlot
      slotHintRef.current = null
      return
    }
    // The user asked for a new chat and it is on its way. Opening another
    // session now would show one nobody picked until the request lands. A
    // request that fails or does not open its chat ends the wait, and the
    // pick below runs then, as it does once the lease lapses.
    if (openPending && !openWaitLapsed) return
    // Don't auto-select/auto-create while the challenge-redirect token effect
    // is still creating + slack-linking its session; otherwise we'd switch to
    // a different slot and orphan the linked one (breaking Slack mirroring).
    if (tokenConsumingRef.current) return
    if (newSessionRef.current || newSlotFailed) return
    const listed = new Set(filteredSlots.map(s => s.key))
    // A popout only ever opens its own chat: the one it showed last, which its
    // `?sid=` still names (the URL writer keeps it). With that chat unlisted it
    // shows no chat and creates none, also after a reload, and it reopens the
    // chat when a later frame lists it again.
    const bound = popout && hadActiveSlotRef.current ? lastShownSlotRef.current : null
    if (popout && !(bound && listed.has(bound))) return
    if (!bound && (searchParams.get('slot') || searchParams.get('sid') || initialSidRef.current)) return
    // A crash-recovery reload opens nothing. The remembered chat -- or the first
    // one, which is often the same busy chat -- may be what froze the renderer,
    // and reopening it would freeze and kill it again (#12907). The user picks.
    // Only the first auto-open is skipped: once the user has opened a chat,
    // closing it moves to a sibling as usual.
    if (filteredSlots.length > 0 && isSafeReload() && !hadActiveSlotRef.current) return
    if (filteredSlots.length > 0) {
      // The stored key is a refresh memory, read for the first pick only. Every
      // tab and popout rewrites it on each slots frame, so once this window has
      // shown a chat, its own recency stack decides where it lands.
      const recent = hadActiveSlotRef.current ? (slotHistoryRef.current ?? []).filter(k => listed.has(k)).pop() : undefined
      const saved = hadActiveSlotRef.current ? null : localStorage.getItem(slotStorageKey)
      const hint = slotHintRef.current
      slotHintRef.current = null
      const target = bound ? bound
        : hint && listed.has(hint) ? hint
        : recent ?? (saved && listed.has(saved) ? saved : filteredSlots[0].key)
      dispatch(switchSlot({ key: target, automatic: true }))
    } else if (connected && slotsLoaded && !autoCreatedRef.current) {
      // Connected, slots fetched, and truly empty — auto-create one
      autoCreatedRef.current = true
      // Agent-less: the server stamps the default custom agent (a template),
      // never the default crewmate alias. `automatic`: the page's own move,
      // which leaves a create or fork the user started free to open.
      dispatch(createSlot({ mode, automatic: true }))
    }
  }, [activeSlot, filteredSlots, searchParams, dispatch, slotStorageKey, connected, slotsLoaded, mode, newSlotFailed, newSessionRef, tokenConsumingRef, popout, openPending, openWaitLapsed])

  // Slot switch: the virtualizer (keyed on sessionId = activeSlot) owns entry
  // placement — it force-pins to the bottom (arming follow) or restores a
  // saved reading position (leaving follow released). The gating effects read
  // that live state via vGetFollowRef, so nothing here needs re-arming; forcing
  // "at bottom" on switch used to yank a restored mid-history reader the moment
  // a tip band or a running-state tick fired.

  const handleResumeSession = useCallback(async (key: string, title: string) => {
    try {
      const resuming = dispatch(resumeFromHistory({ key, title }))
      const result = await resuming.unwrap()
      // The cleanup below is the SECOND half of a swap: it retires the tab the
      // resumed session is replacing. A resume that answered with a surface
      // this page cannot display never performs the first half — the reducer
      // short-circuits, so `activeSlot` still names the tab the user is in and
      // the history row is still in the list. Running the cleanup anyway
      // deleted that tab and discarded the text just typed into it, while the
      // session the user asked for never opened. `ok` alone cannot tell the two
      // apart: the wire request succeeds either way, which is why the thunk
      // returns `surface` at all.
      if (!result.ok || !isChatPageSurface(result.surface)) return
      // Nor when the resume did not take the view: the user opened another
      // session (or clicked a newer row) while it was pending, so the tab it
      // would retire is not being replaced.
      if (store.getState().chat.activeSlot !== result.key) return
      // Nor when it took the view from another session than the tab captured
      // here: a repeated click from a session the user opened since, or a
      // create that opened first, is what the resume replaced.
      const opened = store.getState().chat.lastResumeActivation
      if (opened?.requestId !== resuming.requestId || opened.replaced !== activeSlot) return
      if (activeSlot && activeSlot !== key) {
        delete drafts.current[activeSlot]; delete fileDrafts.current[activeSlot]; delete pasteDrafts.current[activeSlot]; prevSlot.current = null; saveDrafts()
        dispatch(deleteSlot(activeSlot)).unwrap().catch(() => {})
      }
    } catch { /* resume failed — keep current slot */ }
  }, [activeSlot, dispatch, drafts, fileDrafts, pasteDrafts, prevSlot, saveDrafts, store])

  return {
    closeSessionTab,
    drawerPopRef,
    ensureSlotTab,
    handleResumeSession,
    appSlotLaunch,
    setAppSlotLaunch,
    highlightTs,
    initialMidRef,
    initialMsgRef,
    initialSidRef,
    newSlotFailed,
    newSlotMutation,
    openSlotInNewTab,
    ownsSessionTabs,
    selectSessionTab,
    sessionTabs,
    setHighlightTs,
    setNewSlotFailed,
    setSidError,
    sidError,
    sidMatches,
    setSidMatches,
    openShortNameMatch,
  }
}
