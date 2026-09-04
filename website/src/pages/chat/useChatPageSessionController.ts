import { useCallback, useEffect, useRef, useState, type MutableRefObject } from 'react'
import { useMutation } from '@tanstack/react-query'
import type { NavigateFunction, NavigationType } from 'react-router-dom'

import { i18nT } from '../../i18n/t'
import { useSessionTabs } from '../../hooks/useSessionTabs'
import { useAppSelector, type AppDispatch } from '../../store'
import {
  createSlot,
  deleteSlot,
  fetchHistory,
  resumeFromHistory,
  setActiveSlot,
  switchSlot,
} from '../../store/chatSlice'
import type { ChatSlot, SessionInfo } from '../../types'
import { isChatPageSurface } from '../../utils/channelOrigin'
import type { PasteBlock } from '../../utils/pasteTokens'
import { safeSetItem } from '../../utils/safeStorage'
import { shouldReplaceSessionUrl, popMaySwitchSession } from '../../utils/sessionUrlHistory'
import { toSlug } from '../../utils/shareUrl'
import { focusComposer } from './composerFocus'

interface UseChatPageSessionControllerArgs {
  activeSlot: string | null
  activeSlotRef: MutableRefObject<string | null>
  connected: boolean
  defaultAgent?: string
  dispatch: AppDispatch
  drafts: MutableRefObject<Record<string, string>>
  embedMode?: 'chat' | 'sessions'
  embedded?: boolean
  fileDrafts: MutableRefObject<Record<string, string[]>>
  filteredSlots: ChatSlot[]
  filteredSlotsRef: MutableRefObject<ChatSlot[]>
  history: SessionInfo[]
  input: string
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
  defaultAgent,
  dispatch,
  drafts,
  embedMode,
  embedded,
  fileDrafts,
  filteredSlots,
  filteredSlotsRef,
  history,
  input,
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
  // ChatSidebar's footer toggle -- the section starts collapsed and its open
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
    if (historySeededRef.current || !input.trim()) return
    historySeededRef.current = true
    if (history.length === 0) dispatch(fetchHistory(false))
  }, [input, history.length, dispatch])

  // Persist active slot to localStorage for refresh recovery (per-mode)
  const slotStorageKey = `mc-active-slot-${mode || 'chat'}`
  const slotStorageKeyRef = useRef(slotStorageKey); slotStorageKeyRef.current = slotStorageKey
  useEffect(() => {
    if (activeSlot && filteredSlots.some(s => s.key === activeSlot)) {
      safeSetItem(slotStorageKey, activeSlot)
    }
  }, [activeSlot, slotStorageKey, filteredSlots])
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
  const openSlotInNewTab = useCallback((key: string, opts?: { background?: boolean }) => {
    sessionTabs.openInNewTab(key)
    // A BACKGROUND open (middle-click, modifier-click) queues the session and
    // leaves the user where they are — the browser/editor meaning of the
    // gesture, and the whole point of using it to triage several rows in a row.
    // The row menu is a deliberate "take me there", so it opens in foreground.
    if (opts?.background) return
    if (!connectedRef.current) return
    if (key !== activeSlotRef.current) dispatch(switchSlot(key))
    // Depends on the ONE member it calls, not on `sessionTabs` — that hook
    // returns a fresh object literal every render, so the whole object as a dep
    // makes this callback (and thus ChatSidebar's `onOpenSlotInNewTab`, and thus
    // the sidebar's `memo`) churn on every ChatPage render. `openInNewTab` is
    // itself dep-free, so this identity is genuinely stable.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionTabs.openInNewTab, dispatch, activeSlotRef])
  const selectSessionTab = useCallback((key: string) => {
    if (key === activeSlotRef.current || !connectedRef.current) return
    dispatch(switchSlot(key))
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
  const [sidError, setSidError] = useState('')
  const [newSlotFailed, setNewSlotFailed] = useState(false)
  const [highlightTs, setHighlightTs] = useState<string | null>(null)

  // ?new=1: create a blank slot for an embed or a fresh desktop window.
  const newSlotMutation = useMutation({
    mutationFn: () => dispatch(createSlot({ mode })).unwrap(),
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
    if (!initialNewRef.current || (embedded && !embedMode) || popout) return
    initialNewRef.current = false
    newSessionRef.current = true
    setNewSlotFailed(false)
    setSidError('')
    if (!embedMode) dispatch(setActiveSlot(null))
    newSlotMutation.mutate()
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

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
    if (!embedMode) {
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
    if (!urlSid || urlSid === activeSlot) return
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
    }
  }, [searchParams, filteredSlots, activeSlot, activeSlotRef, dispatch, embedMode, navigationType, locationKey, locationPathname, locationHash, connected, noUrlSync, navigate, isMobile])

  // Timeout: if slot never appears after 5s, show error.
  // Gated on `connected` so the timer only runs while the gateway is reachable
  // — otherwise an offline tab would burn its 5s while the resolve effects
  // above are deferred, fire a false "Session not found", clear initialSidRef,
  // and the resolve never happens once the gateway comes back. Re-runs the
  // effect when connected flips so the timer starts fresh on reconnect.
  // Also gated on `slotsLoaded`: firing is one-way (it clears `initialSidRef`),
  // so arming before the list lands makes a late arrival unresolvable.
  const slotsLoaded = useAppSelector(s => s.dashboard.slotsLoaded)
  useEffect(() => {
    if (!connected || !slotsLoaded) return
    const urlSlot = initialSidRef.current
    if (!urlSlot) return
    const timer = setTimeout(() => {
      if (initialSidRef.current) {
        initialSidRef.current = null
        pendingSidRef.current = false
        popInFlightRef.current = false
        setSidError(i18nT('pages.chatPage.session_not_found', { name: urlSlot }))
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
  }, [connected, slotsLoaded])

  // Sync activeSlot → ?sid= in URL (persistent deep-link)
  // Skip entirely when embedded — URL belongs to the host app
  const basePath = popout ? '/popout/chat' : embedMode === 'chat' || embedMode === 'sessions' ? '/embed/chat' : '/chat'
  const searchParamsRef = useRef(searchParams)
  searchParamsRef.current = searchParams
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
    if (pushedEntryPending.current) {
      pushedEntryPending.current = false
      pushedSessionEntryKeys.current.add(locationKey)
    }
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
      if (sp.has('sid') && !initialSidRef.current && !pendingSidRef.current) {
        navigate(basePath, { replace: true })
      }
      return
    }
    pendingSidRef.current = false
    const current = sp.get('sid')
    const slot = filteredSlots.find(s => s.key === activeSlot)
    const slug = slot?.title && slot.title !== slot.key ? toSlug(slot.title) : ''
    const expectedPath = `${basePath}${slug ? '/' + slug : ''}`
    if (current === activeSlot && locationPathname === expectedPath) return
    const next = new URLSearchParams(sp)
    next.set('sid', activeSlot)
    next.delete('slot')
    next.delete('prefill')
    next.delete('autoSend')
    next.delete('newSession')
    next.delete('msg')
    // Push vs replace — see `shouldReplaceSessionUrl` for why mobile never
    // pushes. Kept as a named predicate rather than an inline boolean so the
    // reasoning has somewhere to live and a test can pin it.
    const isSessionSwitch = !!current && current !== activeSlot
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
    if (!replace) {
      pushedSessionEntryKeys.current.add(locationKey)
      pushedEntryPending.current = true
    }
    navigate(`${basePath}${slug ? '/' + slug : ''}?${next}`, { replace })
    // `locationKey` and not just `locationPathname`: consuming the mobile
    // drawer's history entry lands on a DIFFERENT entry whose pathname is
    // IDENTICAL (the entry was a duplicate), so pathname alone reports no change
    // and this effect would not re-run — leaving `?sid=` naming the session the
    // user just switched AWAY from, which a reload would then restore. The key
    // changes on any history move, which is the thing that actually happened.
    // POPs are still funnelled through the `popInFlightRef` bail above, so this
    // adds a re-check, not a new writer.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSlot, filteredSlots, navigate, basePath, locationPathname, locationKey, embedded, noUrlSync, isMobile])

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
      dispatch(setActiveSlot(null))
    }
  }, [activeSlot, slots.length, filteredSlots, dispatch])

  // Auto-select slot after refresh — restore from localStorage or pick first
  // If no slots exist at all, auto-create one so the user lands in a ready chat
  const autoCreatedRef = useRef(false)
  useEffect(() => {
    if (activeSlot) return
    // Don't auto-select/auto-create while the challenge-redirect token effect
    // is still creating + slack-linking its session; otherwise we'd switch to
    // a different slot and orphan the linked one (breaking Slack mirroring).
    if (tokenConsumingRef.current) return
    if (newSessionRef.current || newSlotFailed) return
    if (searchParams.get('slot') || searchParams.get('sid') || initialSidRef.current) return
    if (filteredSlots.length > 0) {
      const saved = localStorage.getItem(slotStorageKey)
      const target = saved && filteredSlots.find(s => s.key === saved) ? saved : filteredSlots[0].key
      dispatch(switchSlot(target))
    } else if (connected && slotsLoaded && !autoCreatedRef.current) {
      // Connected, slots fetched, and truly empty — auto-create one
      autoCreatedRef.current = true
      dispatch(createSlot({ agent: defaultAgent || undefined, mode }))
    }
  }, [activeSlot, filteredSlots, searchParams, dispatch, slotStorageKey, connected, slotsLoaded, defaultAgent, mode, newSlotFailed, newSessionRef, tokenConsumingRef])

  // Slot switch: the virtualizer (keyed on sessionId = activeSlot) owns entry
  // placement — it force-pins to the bottom (arming follow) or restores a
  // saved reading position (leaving follow released). The gating effects read
  // that live state via vGetFollowRef, so nothing here needs re-arming; forcing
  // "at bottom" on switch used to yank a restored mid-history reader the moment
  // a tip band or a running-state tick fired.

  const handleResumeSession = useCallback(async (key: string, title: string) => {
    try {
      const result = await dispatch(resumeFromHistory({ key, title })).unwrap()
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
      if (activeSlot && activeSlot !== key) {
        delete drafts.current[activeSlot]; delete fileDrafts.current[activeSlot]; delete pasteDrafts.current[activeSlot]; prevSlot.current = null; saveDrafts()
        dispatch(deleteSlot(activeSlot)).unwrap().catch(() => {})
      }
    } catch { /* resume failed — keep current slot */ }
  }, [activeSlot, dispatch, drafts, fileDrafts, pasteDrafts, prevSlot, saveDrafts])

  return {
    closeSessionTab,
    drawerPopRef,
    handleResumeSession,
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
  }
}
