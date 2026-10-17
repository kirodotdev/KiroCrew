import type { NavigateFunction, NavigationType, Location } from 'react-router-dom'
import { useState, useRef, useCallback, useEffect, useMemo, type MutableRefObject, type Dispatch, type SetStateAction } from 'react'
import { useMotionValue, useTransform } from 'framer-motion'
import type { AppDispatch } from '../../../store'
import type { ChatSlot, ChatFolder } from '../../../types'
import { type SourceLinkKind, parseSourceLinkUrl, commitRevealedSource, type RevealedSources } from '../../../utils/pullRequestLinks'
import { useShellSlot } from '../../../hooks/useShellSlot'
import { isTouchDevice } from '../../../utils/isTouchDevice'
import { safeAreaLeft, registerDrawerTargets, animateDrawer, takeOverDrawer, useDrawerSwipe } from '../../../hooks/useDrawerSwipe'
import { resumeFromHistory, requestFolderReveal, openActivityPanel, openActivityToTab } from '../../../store/chatSlice'
import { api } from '../../../api/client'
import type { useChatPageResourcesController } from '../useChatPageResourcesController'
import { openPanelView } from '../../../hooks/usePanelTabs'
import type { ClosedSessionActions, SidebarFolderActions } from '../../../components/markdown/contexts'
import { PREVIEW_EXPAND_EVENT } from '../../../components/WebPreviewPanel'
import { useHoverIntent } from '../../../hooks/useHoverIntent'
import { shouldMountSidePanel, isSidePanelHidden } from '../sidePanelMount'
import { i18nT } from '../../../i18n/t'

/**
 * Strip of screen the mobile sessions drawer deliberately leaves uncovered, so a
 * sliver of the conversation behind it stays visible.
 *
 * ONE number, because it has to be two things at once: the panel's rendered
 * width (`viewport - this`) and the offset its slide travels. Spelling the width
 * as a `max-w-[calc(100vw-2.5rem)]` class while the travel read `innerWidth` is
 * exactly how they drifted apart — the panel finished sliding 40px before its
 * animation did, so the ease-out's tail moved an already-offscreen panel.
 */
export const DRAWER_UNCOVERED_PX = 40

interface SessionsDrawerOptions {
  embedded?: boolean
  embedMode?: 'chat' | 'sessions'
  dispatch: AppDispatch
  navigate: NavigateFunction
  navigationType: NavigationType
  location: Location
  isMobile: boolean
  filteredSlots: ChatSlot[]
  activeSlot: string | null
  splitMode: boolean
  crewWindow: { instanceId: string; key: string; } | null
  activityOpen: boolean
  showActionError: (message: string, title?: string) => void
  githubSourceHostsRef: MutableRefObject<string[]>
  hasBrowserTab: boolean
  hasLiveAppTab: boolean
  jiraSourceHostsRef: MutableRefObject<string[]>
  bitbucketServerSourceHostsRef: MutableRefObject<string[]>
  search: ReturnType<typeof useChatPageResourcesController>['search']
  selectSource: (kind: SourceLinkKind, url: string, forSlot?: string) => void
  setRevealedSources: Dispatch<SetStateAction<RevealedSources>>
  sourceHostsRef: MutableRefObject<string[]>
  tabsCtl: ReturnType<typeof useChatPageResourcesController>['tabsCtl']
  drawerPopRef: MutableRefObject<boolean>
  chatFolders: ChatFolder[]
  setSidebarPinned: Dispatch<SetStateAction<boolean>>
  sidebarPinned: boolean
  sidebarPinnedRef: MutableRefObject<boolean>
  sidebarAutoHidden: MutableRefObject<boolean | null>
  toggleAct: () => void
}

/**
 * The sessions drawer and the side panel's phone overlay: their compositor-driven
 * slides and swipes, the drawer's Back entry, the sidebar and closed-session context
 * actions, the pane geometry, and the collapsed sidebar's hover flyout.
 *
 * Called from ChatPage at the position its statements held, so React registers its effects in the same order.
 */
export function useSessionsDrawer({
  embedded, embedMode, dispatch, navigate, navigationType, location, isMobile, filteredSlots, activeSlot, splitMode, crewWindow, activityOpen, showActionError, githubSourceHostsRef, hasBrowserTab,
  hasLiveAppTab, jiraSourceHostsRef, bitbucketServerSourceHostsRef, search, selectSource, setRevealedSources, sourceHostsRef, tabsCtl, drawerPopRef, chatFolders, setSidebarPinned, sidebarPinned, sidebarPinnedRef, sidebarAutoHidden,
  toggleAct,
}: SessionsDrawerOptions) {
  /**
   * Mobile sessions drawer, as ONE value rather than an open flag plus a
   * mounted flag. `closing` exists because the panel must stay in the DOM while
   * it slides out — with two booleans that window is exactly where they drift
   * apart, and the panel either unmounts mid-slide or is left mounted after it.
   *
   * `open` is the intent (the toggle reads it, aria reads it); mount is
   * `phase !== 'closed'`. There is one writer per transition below, and the
   * gesture reports through its `onCommit` / `onSettle` callbacks rather than
   * writing the phase itself.
   */
  const [drawerPhase, setDrawerPhase] = useState<'closed' | 'open' | 'closing'>('closed')
  const mobileSessions = drawerPhase === 'open'
  const drawerMounted = drawerPhase !== 'closed'
  /** Panel offset in px: `-innerWidth` offscreen, `0` at rest. A MotionValue so
   *  the drag writes it at frame rate without re-rendering this component. */
  const drawerX = useMotionValue(0)
  /** The sliding panel and the scrim behind it. Handed to
   *  `registerDrawerTargets` so the settle animates THESE ELEMENTS (compositor)
   *  instead of sampling `drawerX` on the main thread — the only way the slide
   *  holds its frame rate while sessions stream. Refs rather than state:
   *  nothing renders off them. */
  const drawerPanelRef = useRef<HTMLDivElement | null>(null)
  const drawerScrimRef = useRef<HTMLDivElement | null>(null)
  /**
   * The drawer's travel: its OWN width, not the screen's.
   *
   * The panel is deliberately narrower than the viewport (`DRAWER_UNCOVERED_PX`
   * of chat stays visible beside it), so a travel of `innerWidth` puts it fully
   * offscreen at ~90% of the slide — and the settle's whole deceleration tail
   * then plays with nothing on screen. Measured on a 390px phone: the panel
   * vanished at 160ms of a 300ms dismissal, leaving 140ms of invisible motion
   * plus a scrim still 10% dark, fading alone. Matching the travel to the width
   * lands the panel's edge and the scrim's zero on the same frame, which is what
   * makes the ease-out readable.
   *
   * Live, not captured: rotation and resize change it, and a settle registered
   * earlier must not animate against a stale span. The safe-area inset is part
   * of it — the panel is pinned at `left-safe`, so it starts that far in and has
   * to cross it too.
   */
  const drawerTravel = useCallback(
    () => Math.max(0, (window.innerWidth || 0) - DRAWER_UNCOVERED_PX + safeAreaLeft()),
    [],
  )
  /**
   * RIGHT-side panel slide (mobile only). The inline side panel used to enter
   * by animating `width: 0 → auto` — a LAYOUT animation the compositor cannot
   * take, which re-laid-out the squeezed chat pane on every frame of the
   * 400ms open. On mobile it covers the full window anyway, so it is rendered
   * as a fixed overlay instead and slides in from the RIGHT on the compositor,
   * mirroring the sessions drawer. Offsets run [0, +width] (closed = +width).
   * Desktop keeps the width reveal: there the chat pane genuinely shares the
   * row and the reflow is the point.
   */
  const sideOverlayX = useMotionValue(0)
  const sideOverlayPanelRef = useRef<HTMLDivElement | null>(null)
  const [sideOverlayPhase, setSideOverlayPhase] = useState<'closed' | 'open' | 'closing'>('closed')
  const sideOverlayPhaseRef = useRef(sideOverlayPhase)
  sideOverlayPhaseRef.current = sideOverlayPhase
  useEffect(() => registerDrawerTargets(sideOverlayX, {
    panel: () => sideOverlayPanelRef.current,
    scrim: () => null,
    travel: () => window.innerWidth || 0,
  }), [sideOverlayX])
  /** The scrim tracks the panel instead of running its own fade, so a half-drag
   *  is half-dimmed and a cancelled drag un-dims with the finger. Divided by the
   *  drawer's OWN travel, so it reaches 0 exactly as the panel clears the edge. */
  const drawerScrim = useTransform(drawerX, x =>
    Math.max(0, Math.min(1, 1 + x / Math.max(1, drawerTravel()))))
  // Point every settle on `drawerX` at real DOM. Registered once for the page's
  // lifetime, reading the elements through the refs at animation time — the
  // panel is mounted and unmounted per open, so binding the nodes themselves
  // here would go stale on the first close. Safe ONLY because the drawer's
  // ChatSidebar renders staticRows (no projection nodes under a compositor-
  // driven transform — see registerDrawerTargets' precondition).
  //
  // `travel` is recomputed per call rather than captured, so a rotation or a
  // resize between registration and the next settle cannot animate against a
  // stale span.
  useEffect(() => registerDrawerTargets(drawerX, {
    panel: () => drawerPanelRef.current,
    scrim: () => drawerScrimRef.current,
    travel: drawerTravel,
  }), [drawerX, drawerTravel])
  // Read for the transition guards below. The animation each transition starts
  // is a side effect, so it must not live inside a setState updater — React may
  // invoke an updater more than once, which would start the settle twice.
  const drawerPhaseRef = useRef(drawerPhase)
  drawerPhaseRef.current = drawerPhase
  /**
   * The drawer's history entry, so the platform back gesture dismisses the
   * drawer instead of leaving `/chat` (#5795).
   *
   * ONE entry, and it exists exactly while the drawer is open: every open mints
   * it and every close that was not itself the Back spends it. The alternative --
   * leaving it behind -- is the twin-entry defect `SidePanelLayout`'s back control
   * documents: two entries with the same URL, so the next back-swipe visibly does
   * nothing.
   *
   * The entry is a bare DUPLICATE of the one below it. The drawer is view state,
   * not a location: a URL that moved would have to be unwound on the pop, and
   * unwinding a `?sid=` is exactly what the sid effect would misread as the user
   * retracing sessions.
   *
   * Deliberately NOT marked in `history.state`, unlike `SUBNAV_PUSH_STATE`. That
   * marker earns its place because a SubNav drill-in CHANGES the url, so a cold
   * deep link can land on the drilled-in entry and the marker is the only way to
   * tell "we pushed this" from "the user arrived here". Nothing can deep-link a
   * drawer open, so there is no such question to answer and a marker would be
   * write-only state. Ownership is this ref, which is also the only form that is
   * correct: a marked entry can outlive the mount that pushed it -- a reload
   * restores `history.state`, and Forward can walk back INTO one -- so reading a
   * marker would have the page consume an entry it never pushed.
   */
  const drawerEntryRef = useRef(false)
  const locationRef = useRef(location)
  locationRef.current = location
  const pushDrawerEntry = useCallback(() => {
    // Desktop's sidebar is a persistent column with its own toggle, not a layer
    // over the content, and Back there already means "leave the route".
    if (!isMobile || drawerEntryRef.current) return
    const loc = locationRef.current
    drawerEntryRef.current = true
    navigate({ pathname: loc.pathname, search: loc.search, hash: loc.hash })
  }, [isMobile, navigate])
  /** Spend the entry, if we still hold one. `drawerPopRef` is what tells the sid
   *  effect this POP is bookkeeping rather than a session the user asked for. */
  const consumeDrawerEntry = useCallback(() => {
    if (!drawerEntryRef.current) return
    drawerEntryRef.current = false
    drawerPopRef.current = true
    navigate(-1)
  }, [navigate, drawerPopRef])
  const openSidebar = useCallback(() => {
    if (drawerPhaseRef.current === 'open') return
    // Seat it offscreen before the mount so the first painted frame is the
    // closed offset, then let the shared settle carry it in.
    if (drawerPhaseRef.current === 'closed') drawerX.set(-drawerTravel())
    drawerPhaseRef.current = 'open'
    setDrawerPhase('open')
    animateDrawer(drawerX, 0)
    pushDrawerEntry()
  }, [drawerX, drawerTravel, pushDrawerEntry])
  // The sidebar-folder chip's roster and handler, for every markdown span on this
  // page (`InlineCode` -> `FolderChip`). A folder path in prose (`goal/worker`)
  // becomes a click that reveals that row in the sidebar -- the same store
  // request the command bar's folder scope issues, so the sidebar answers it the
  // same way (expand ancestors, leave the flat lane, scroll, flash). The click
  // first puts a sidebar ON SCREEN, exactly as the header menu's "Reveal in
  // sidebar" does above: the phone drives its drawer, the desktop pins the pane,
  // and only then is the request dispatched -- the store holds it until the
  // sidebar mounts and consumes it. Embed-chat never mounts a sidebar, so no
  // handler is wired there and `resolveFolderChip` offers no chip: a stored
  // request would outlive the view and replay on whichever sidebar mounts next.
  // Offered while OFFLINE, unlike the session chip: revealing a row is
  // client-side work on a list already cached, where switching a session needs
  // the gateway. Provided once here rather than threaded through
  // MarkdownRenderer props, so subagent and workflow cards and system notices
  // offer the same chip as a message body.
  // A link to a CLOSED session: probe that one key (`GET /api/sessions/{key}/meta`),
  // and resume it the way the sidebar's Older-sessions row does (#9915). Only
  // consulted while the open roster is wired, so offline it is off as well.
  const closedSessionActions = useMemo<ClosedSessionActions>(() => ({
    // Background: a failure leaves the text plain and says nothing, since the
    // reader did not ask for anything yet.
    lookup: async (key: string) => {
      const row = await api.sessionMeta(key)
      return row ? { key: row.key, title: row.title || row.key } : null
    },
    // The click: ask again, because the session may have been deleted since
    // the chip appeared. A failed check is said by the chip itself, inline at
    // the spot clicked, with a retry (`ClosedSessionNotice`).
    open: async ({ key, title }) => {
      let row: Awaited<ReturnType<typeof api.sessionMeta>>
      try {
        row = await api.sessionMeta(key)
      } catch {
        return 'failed'
      }
      if (!row) {
        showActionError(i18nT('store.chatSlice.session_gone_open_failed_named', { name: title }))
        return 'gone'
      }
      void dispatch(resumeFromHistory({ key: row.key, title: row.title || title }))
      return 'opened'
    },
  }), [dispatch, showActionError])
  const sidebarFolderActions = useMemo<SidebarFolderActions>(() => ({
    folders: chatFolders,
    onFolderReveal: embedMode === 'chat' ? undefined : (folderId: string) => {
      sidebarAutoHidden.current = null
      if (isMobile) openSidebar()
      else if (!sidebarPinned) setSidebarPinned(true)
      dispatch(requestFolderReveal(folderId))
    },
  }), [chatFolders, dispatch, embedMode, isMobile, openSidebar, sidebarPinned, setSidebarPinned, sidebarAutoHidden])
  /** Mount the panel for a drag in progress. Deliberately NOT `openSidebar`:
   *  that one runs the settle to the rest position, which would race the finger
   *  for the same value and pull the panel out from under it. The gesture has
   *  already seated the offset and owns it until release.
   *
   *  No history entry here either — the drag has not committed to anything yet,
   *  and one the user drags back would push and immediately pop. The entry is
   *  minted where the gesture COMMITS, in `onCommit`. */
  const beginDrawerDrag = useCallback(() => {
    drawerPhaseRef.current = 'open'
    setDrawerPhase('open')
  }, [])
  /** Run the close animation and nothing else. Split out because the Back that
   *  closes the drawer must NOT consume an entry — that pop already spent it. */
  const runDrawerClose = useCallback(() => {
    if (drawerPhaseRef.current !== 'open') return false
    drawerPhaseRef.current = 'closing'
    setDrawerPhase('closing')
    animateDrawer(drawerX, -drawerTravel(), () => {
      drawerPhaseRef.current = 'closed'
      setDrawerPhase('closed')
    })
    return true
  }, [drawerX, drawerTravel])
  const closeSidebar = useCallback(() => {
    // Phase first, then the pop: the POP effect below skips a drawer that is no
    // longer 'open', which is what keeps this close from being counted twice.
    if (runDrawerClose()) consumeDrawerEntry()
  }, [runDrawerClose, consumeDrawerEntry])
  /**
   * The Back that lands on the entry BELOW the drawer's: close the drawer and
   * stay put.
   *
   * While the drawer is open and we hold an entry, that entry is the one on top,
   * so any POP is a pop off it. Closing WITHOUT `consumeDrawerEntry` is the
   * point — the pop is the consumption. `location.key` in the deps rather than
   * `location`, so this runs once per history entry and not on every search-param
   * rewrite the sid effect makes.
   */
  useEffect(() => {
    if (navigationType !== 'POP') return
    if (!drawerEntryRef.current || drawerPhaseRef.current !== 'open') return
    drawerEntryRef.current = false
    runDrawerClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.key, navigationType])
  // Close the drawer when a session is selected. Routed through closeSidebar so
  // it slides out — flipping straight to 'closed' would unmount it on the spot.
  useEffect(() => { if (isMobile) closeSidebar() }, [activeSlot]) // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => { if (isMobile && crewWindow) closeSidebar() }, [crewWindow]) // eslint-disable-line react-hooks/exhaustive-deps
  // Leaving the mobile viewport: drop the panel with no slide. There is no
  // mobile drawer to animate on the other side of that crossing, and the
  // desktop sidebar owns its own open state. The history entry goes with it —
  // the desktop sidebar is a column, not a layer, so an entry standing for
  // "a drawer is open" would be left with nothing to dismiss.
  useEffect(() => { if (!isMobile) { setDrawerPhase('closed'); consumeDrawerEntry() } }, [isMobile]) // eslint-disable-line react-hooks/exhaustive-deps
  const chatContainerRef = useRef<HTMLDivElement>(null)
  // Measured container height — sizes the sidebar border-box morph (the panel
  // rect the box shrinks from on collapse and grows back to on expand).
  const [containerH, setContainerH] = useState(0)
  useEffect(() => {
    const el = chatContainerRef.current
    if (!el) return
    const measure = () => setContainerH(el.clientHeight)
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  // Full-height activity bar slot in the App shell grid (desktop dashboard
  // only): the Activity panel portals into it so it spans the window
  // top-to-bottom. The header row ends at the slot's left edge,
  // so the top-bar right cluster (capsule, terminal, bell, gear) shifts left
  // when the panel opens. Null on mobile / embed frames -> inline fallback.
  // `useShellSlot` seeds synchronously (no first-render flash into the inline
  // fallback) and watches the DOM across a mobile -> desktop crossing; the
  // phone top-bar slots (pageEnvironment.ts) resolve through the same hook.
  const activitySlot = useShellSlot('activity-bar-slot', !isMobile && !embedMode)
  /** The inline panel's mount predicate, shared by the overlay phase effect
   *  and ChatTitleOverlay's render so the two cannot disagree. */
  const hasTaskDashboard = tabsCtl.tabs.some(tab => tab.kind === 'command-center')
  const sidePanelWantsMount = shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen })
    && !isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen })
  // Mobile right-panel overlay: slide in when the panel wants the screen,
  // slide out (keeping it mounted for the travel) when it stops wanting it.
  useEffect(() => {
    if (!isMobile) { setSideOverlayPhase('closed'); takeOverDrawer(sideOverlayX); return }
    if (sidePanelWantsMount) {
      if (sideOverlayPhaseRef.current === 'open') return
      if (sideOverlayPhaseRef.current === 'closed') sideOverlayX.set(window.innerWidth || 0)
      sideOverlayPhaseRef.current = 'open'
      setSideOverlayPhase('open')
      takeOverDrawer(sideOverlayX)
      animateDrawer(sideOverlayX, 0)
    } else {
      if (sideOverlayPhaseRef.current !== 'open') return
      sideOverlayPhaseRef.current = 'closing'
      setSideOverlayPhase('closing')
      takeOverDrawer(sideOverlayX)
      animateDrawer(sideOverlayX, window.innerWidth || 0, () => {
        sideOverlayPhaseRef.current = 'closed'
        setSideOverlayPhase('closed')
      })
    }
  }, [isMobile, sidePanelWantsMount, sideOverlayX])
  /** Read by the right overlay's release handler, which must know whether the
   *  store currently has the panel open without depending on it. */
  const activityOpenRef = useRef(activityOpen)
  activityOpenRef.current = activityOpen
  /**
   * Mount the right overlay for a drag in progress — the mirror of
   * `beginDrawerDrag`, and deliberately NOT the effect above: that one runs a
   * settle to the rest position, which would race the finger for the same
   * value. The gesture has already seated the offset and owns it until release.
   *
   * The STORE is left alone until the release commits. Writing `activityOpen`
   * here would re-run the effect above mid-gesture, and a drag the user then
   * reconsiders would still have flipped (and persisted) the panel's open flag.
   * The phase alone is enough to mount, because the effect keys on the store's
   * predicate rather than on the phase.
   */
  const beginSideOverlayDrag = useCallback(() => {
    sideOverlayPhaseRef.current = 'open'
    setSideOverlayPhase('open')
  }, [])

  /** True while the INLINE side panel (mobile / embed, no actbar column) is
   *  mounted AND visible.
   *
   *  Mobile has no actbar grid column, so the panel renders as a flex sibling of
   *  the chat pane at the full window width — it covers the content area
   *  outright. Anything the chat pane floats over that area (ChatPaneChrome's sessions
   *  FAB) would land on top of the panel's own controls, so it is gated on
   *  this. Reuses the panel's own mount/visibility predicates rather than
   *  re-deriving them from `activityOpen`, which is only one of their inputs (a
   *  live app or browser tab keeps the panel mounted through a close, and the
   *  find pane hides it while owning the dock). */
  const inlineSidePanelShowing = !activitySlot
    && shouldMountSidePanel({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen })
    && !isSidePanelHidden({ activityOpen, hasLiveAppTab, hasBrowserTab, hasTaskDashboard, searchOpen: search.isOpen })
  // ONE binding per panel, each covering both of ITS directions: a rightward
  // drag opens the sessions drawer, a leftward drag on the open drawer closes
  // it. Which rule applies is read live from `open` inside the hook, so the
  // opening drag does not tear its own listeners down when the panel mounts
  // mid-gesture.
  //
  // The two instances share this element and are told apart by DIRECTION, not by
  // where the touch began — so an opening drag works from anywhere in the pane
  // rather than out of a narrow edge band. The one thing direction cannot
  // separate is a panel that is already OPEN: its closing drag is the other
  // panel's opening drag, so each instance is disabled while the other's panel
  // is on screen.
  const drawerDragging = useDrawerSwipe(chatContainerRef, {
    // Gated on the sibling not being OPEN, not on it having finished closing.
    // The two panels exclude each other because one's closing direction is the
    // other's opening direction — a hazard that lasts only while the sibling is
    // actually open. Requiring `'closed'` also held the gate shut for the whole
    // slide out, so a swipe dismissing one panel could not be followed straight
    // away by a swipe revealing the other.
    enabled: isMobile && !embedded && sideOverlayPhase !== 'open',
    travel: drawerTravel,
    open: mobileSessions,
    x: drawerX,
    onGestureOpen: beginDrawerDrag,
    // The gesture's COMMIT points, which is where its history entry is minted and
    // spent — `beginDrawerDrag` only mounts the panel, and a drag the user
    // reconsiders commits to closed without ever having minted one (both helpers
    // are guarded on `drawerEntryRef`, so the call is a no-op then).
    //
    // Deliberately NOT `onSettle`. That one waits for the 120-450ms slide so a
    // consumer cannot unmount the panel mid-animation, which is exactly what
    // makes it the wrong signal here: the entry stands for "a drawer is open, so
    // Back dismisses it", an INTENT, and the hook reports intent at release.
    // Minting on arrival left the whole opening slide with the panel covering the
    // screen and no entry to pop, so a Back there leaked past the drawer and left
    // /chat — #5795 again, reachable by drag rather than by tap. Spending on
    // arrival had the mirror hazard: a Back during the closing slide popped the
    // still-unspent entry while the POP effect above ignored it (the phase is
    // 'closing', not 'open'), and the settle then spent a second, real entry.
    onCommit: open => {
      if (open) { pushDrawerEntry(); return }
      // Committed to closing: mark it now so the sibling's gate opens immediately.
      // 'closing' rather than 'closed' because the panel is still on screen and
      // `drawerMounted` keys on that — unmounting here would cut the slide short.
      drawerPhaseRef.current = 'closing'
      setDrawerPhase('closing')
      consumeDrawerEntry()
    },
    // Arrival, which is bookkeeping only: the phase flip that unmounts the panel.
    onSettle: open => {
      if (open) return
      drawerPhaseRef.current = 'closed'
      setDrawerPhase('closed')
    },
  })
  // Right-hand side panel, same gesture mirrored. Not bound when the actbar
  // column owns the panel (desktop) or while the find pane holds the dock —
  // there the store's mount predicate refuses to keep the panel open, so a
  // committed drag would be undone by the effect above on the next render.
  useDrawerSwipe(chatContainerRef, {
    enabled: isMobile && !embedded && !activitySlot && !search.isOpen && drawerPhase !== 'open',
    side: 'right',
    open: sideOverlayPhase === 'open',
    x: sideOverlayX,
    onGestureOpen: beginSideOverlayDrag,
    // See the left instance: the sibling's gate has to open at commit time, and
    // 'closing' keeps this panel mounted for the rest of its slide.
    onCommit: open => {
      if (open) return
      sideOverlayPhaseRef.current = 'closing'
      setSideOverlayPhase('closing')
    },
    onSettle: open => {
      // Committed: publish to the store, which is what keeps the panel open
      // across a re-render and remembers it for this chat. The effect above sees
      // a phase already at 'open' and does not re-animate.
      if (open) { dispatch(openActivityPanel()); return }
      // Parked: write 'closed' BEFORE touching the store, so the effect sees a
      // panel that has already arrived and does not run a second slide-out over
      // this one. The store is only flipped when it actually had the panel open
      // — an opening drag that was reconsidered never wrote it in the first
      // place, and toggling here would OPEN the panel it just dismissed.
      sideOverlayPhaseRef.current = 'closed'
      setSideOverlayPhase('closed')
      if (activityOpenRef.current) toggleAct()
    },
  })
  /** Reveal a session's pull request / issue in that session's side panel.
   *
   *  Fires from a sidebar chip AFTER ChatSidebar has dispatched the slot switch,
   *  so `switchSlot.pending` has already published the target slot to the store —
   *  but activeSlotRef is assigned during RENDER and still names the chat being
   *  left, so `slot` is threaded explicitly through every write below.
   *
   *  The url is re-parsed rather than trusted: the chip payload comes from the
   *  BACKEND's scan, and running it through the panel's own parser is what
   *  guarantees the injected link matches the shape (and the host allowlist) the
   *  panels already work with.
   *
   *  Returns whether the panel took the link. FALSE hands the click back to the
   *  chip's own anchor, so a url this parser rejects opens the provider instead
   *  of doing nothing at all. That is reachable rather than theoretical: the two
   *  parsers read the self-managed GitLab allowlist from different places, and
   *  `sourceHosts` is empty until the dashboard-config query resolves (and stays
   *  empty if it fails), so every self-hosted chip parses to null in that window
   *  even though the backend scan accepted it. */
  const revealSourceLink = useCallback((slot: string, chip: { url: string; kind: SourceLinkKind }): boolean => {
    const link = parseSourceLinkUrl(
      chip.url,
      sourceHostsRef.current,
      jiraSourceHostsRef.current,
      githubSourceHostsRef.current,
      bitbucketServerSourceHostsRef.current,
    )
    if (!link) return false
    const view = link.kind === 'issue' ? 'issues' : 'changes'
    // Durable BEFORE the state update, and one key at a time. Writing inside the
    // updater would both make it impure (React may invoke an updater more than
    // once) and publish this window's whole map, deleting a sibling window's
    // reveals — see `commitRevealedSource`.
    commitRevealedSource(slot, link.kind, link.url)
    setRevealedSources(previous => ({
      ...previous,
      [slot]: { ...previous[slot], [link.kind]: link },
    }))
    selectSource(link.kind, link.url, slot)
    // Addressed by slot, not through tabsCtl: that binding is still the chat
    // being left, so the tab would open on the wrong strip.
    openPanelView(slot, view)
    // The find pane owns the right-hand dock exclusively (shouldMountSidePanel
    // returns false while it is open), so revealing into a session with search
    // open would suppress the chip's navigation and then mount nothing at all.
    // Same reason handleFileOpen / handleOpenDiff close it before opening a dock
    // panel.
    search.close()
    dispatch(openActivityToTab(view))
    // The mobile session drawer covers the panel it would reveal into. The
    // activeSlot effect closes it on a real switch, but a chip on the session
    // already open does not change activeSlot.
    if (isMobile) closeSidebar()
    return true
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only `search.close()` is read off `search`, and that member is a useCallback([]) in useMessageSearch; the object around it is rebuilt every render, so depending on it would recreate this reveal handler continuously
  }, [dispatch, isMobile, selectSource, closeSidebar])
  // Web Preview expand mode — broadcast by the Web Preview tab's
  // expand toggle. When on, hide the session list and maximize the side panel
  // (passed to SidePanel), so the preview gets max room and chat shrinks to its
  // minimum. App collapses the left nav off the same event.
  //
  // Hiding the list drives `sidebarPinned` directly instead of overriding
  // `sidebarOpen`: an override leaves the sessions toggle visibly present but
  // inert. Driving the real state keeps that toggle working normally inside
  // expand mode. `sidebarAutoHidden` holds the pre-expand state to restore on
  // exit, and is cleared once the user toggles the list themselves. Neither
  // transition persists `mc-sidebar-pinned` — only a user toggle does.
  //
  // The ref is read and cleared HERE, in the handler, and only plain values
  // reach the setter: a state updater must be pure, and React invokes one twice
  // under StrictMode, which would make the second pass read an already-cleared
  // ref and lose the restore value.
  //
  // The mobile drawer is a separate state, so it is closed outright rather than
  // suppressed — a swipe or a tap still reopens it, which an override would not
  // allow.
  const [previewExpanded, setPreviewExpanded] = useState(false)
  useEffect(() => {
    const onPreviewExpand = (e: Event) => {
      const expanded = !!(e as CustomEvent<{ expanded?: boolean }>).detail?.expanded
      setPreviewExpanded(expanded)
      if (expanded) {
        closeSidebar()
        if (sidebarAutoHidden.current === null) sidebarAutoHidden.current = sidebarPinnedRef.current
        setSidebarPinned(false)
        return
      }
      const prior = sidebarAutoHidden.current
      sidebarAutoHidden.current = null
      if (prior !== null) setSidebarPinned(prior)
    }
    window.addEventListener(PREVIEW_EXPAND_EVENT, onPreviewExpand)
    return () => window.removeEventListener(PREVIEW_EXPAND_EVENT, onPreviewExpand)
    // Still a mount-once registration in practice: closeSidebar closes over only
    // `drawerX` (a useMotionValue, stable for the component's lifetime) and
    // `drawerTravel` (a useCallback([]) that reads window.innerWidth at call time),
    // so its identity never changes. Naming it rather than relying on that keeps the
    // listener from silently capturing a stale copy if closeSidebar ever gains a dep.
  }, [closeSidebar, setSidebarPinned, sidebarAutoHidden, sidebarPinnedRef])
  // The no-sessions force-open yields to expand mode: with an empty list no
  // sessions toggle is rendered, so suppressing it makes nothing inert, and the
  // preview would otherwise stay covered by a list that cannot be dismissed.
  const sidebarOpen = isMobile
    ? mobileSessions
    : (sidebarPinned || (filteredSlots.length === 0 && !previewExpanded))
  // Whether the sidebar -- and with it the banner that says a failed
  // folder-order read -- is on this screen right now. The same facts that mount
  // it in SessionsSidebarChrome's render: embed-chat never mounts one, embed-sessions always does, and
  // the dashboard mounts it in the drawer while the drawer is (mobile) or the panel
  // is open (desktop). The header menu and the folder-suggestion card say the
  // failure themselves only when this is false: one screen, one notice.
  const sidebarOnScreen = embedMode === 'chat'
    ? false
    : embedMode === 'sessions'
      ? true
      : (isMobile ? drawerMounted : sidebarOpen)

  // ── Collapsed-sidebar hover flyout ──────────────────────────────────────
  // Hovering the toggle while collapsed opens a recents list over the chat, so
  // switching sessions stops being expand → switch → collapse. It is purely an
  // overlay: it never touches `sidebarPinned`, because `panelReserve` and
  // `panelFillWidth` (paneLayout.tsx) both read `sidebarOpen`, and flipping it to show a
  // transient popover would re-run the side panel's width maths and visibly
  // resize the chat every time the pointer rested on a 28px button.
  const flyoutTriggerRef = useRef<HTMLButtonElement>(null)
  const flyoutSurfaceRef = useRef<HTMLDivElement>(null)
  // Touch is a second gate beyond isMobile: a desktop-width touch device has no
  // hover, so the flyout would only ever appear as a tap artefact.
  const flyoutEligible = !isMobile && !isTouchDevice() && !splitMode
    && embedMode !== 'chat' && embedMode !== 'sessions'
    && !sidebarOpen && filteredSlots.length > 0
  const flyout = useHoverIntent({
    enabled: flyoutEligible,
    triggerRef: flyoutTriggerRef,
    surfaceRef: flyoutSurfaceRef,
  })
  // Rect the sidebar's clip window should expand FROM, captured at click time
  // from the live flyout element. Null when the expand came from the button
  // alone, which keeps the stock button-rect morph for that path.
  const [expandFrom, setExpandFrom] = useState<{ x: number; y: number; w: number; h: number } | null>(null)
  const expandSidebar = useCallback((fromFlyout: boolean) => {
    const surface = flyoutSurfaceRef.current
    const container = chatContainerRef.current
    if (fromFlyout && surface && container) {
      const s = surface.getBoundingClientRect()
      const c = container.getBoundingClientRect()
      setExpandFrom({ x: s.left - c.left, y: s.top - c.top, w: s.width, h: s.height })
    } else {
      setExpandFrom(null)
    }
    flyout.close()
    window.dispatchEvent(new CustomEvent('toggle-pin-chat-sidebar'))
  }, [flyout])
  // The rect is only valid for the mount it was captured for. Clearing it on
  // collapse means a later button-only expand cannot inherit a stale flyout
  // rect and appear to grow out of nothing.
  useEffect(() => { if (!sidebarOpen) setExpandFrom(null) }, [sidebarOpen])
  return {
    mobileSessions, drawerMounted, drawerX, drawerPanelRef, drawerScrimRef, sideOverlayX, sideOverlayPanelRef, sideOverlayPhase, drawerScrim, openSidebar, closedSessionActions, sidebarFolderActions,
    closeSidebar, chatContainerRef, containerH, activitySlot, hasTaskDashboard, sidePanelWantsMount, inlineSidePanelShowing, drawerDragging, revealSourceLink, previewExpanded, sidebarOpen,
    sidebarOnScreen, flyoutTriggerRef, flyoutSurfaceRef, flyoutEligible, flyout, expandFrom, expandSidebar,
  }
}
