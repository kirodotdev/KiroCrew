/**
 * The phone drawers on the Crewmates page. `useMembersPageDrawers` owns the
 * roster drawer over the open chat (its phase, its history entry and the pick
 * that waits for that entry's pop), the side panel's slide in from the right,
 * and the relay that lets a sandboxed Dashboard frame drive the panel's close.
 * `useMembersPageDrawerSwipes` binds the two drags on the page root and
 * retires the drawer when something else takes the column.
 *
 * The `MembersPage` facade calls each hook here at the point its code sits in
 * the page's hook order, which `MembersPage.owners.test.tsx` pins: React runs
 * a component's effects in registration order, so moving one is a behaviour
 * change even when every test of the moved code still passes.
 */
import { useCallback, useEffect, useRef, useState, type MutableRefObject } from 'react'
import { type NavigateFunction, type Location, type NavigationType } from 'react-router-dom'
import { type MemberRosterRow } from '../../api/client'
import { createPortal } from 'react-dom'
import { useMotionValue, useTransform } from 'framer-motion'
import { useDrawerSwipe, animateDrawer, registerDrawerTargets, takeOverDrawer, safeAreaLeft } from '../../hooks/useDrawerSwipe'
import { useMobileNavRail } from '../../components/MobileNavRailContext'
import { useVisualViewport } from '../../hooks/useVisualViewport'
import { isTrapEntry } from '../../lib/routeHistoryPosition'
import { ROSTER_DRAWER_UNCOVERED_PX } from './membersPageModel'
import { type MembersPageLayout } from './useMembersPageLayout'
import { type MembersPageTeamsRead } from './useMembersPageTeams'

/** How long a drawer pick waits for the router to report the pop of the
 *  drawer's duplicate entry. Past it the pick is dropped, never run on the
 *  wrong entry. A pop normally lands within a frame or two. */
const ROSTER_DRAWER_POP_TIMEOUT_MS = 1000

interface MembersPageDrawersOptions {
  teamsRead: MembersPageTeamsRead
  layout: MembersPageLayout
  navigate: NavigateFunction
  location: Location
  activeName: string
  createOpen: boolean
  navigationType: NavigationType
  active: MemberRosterRow | undefined
}

/** The phone roster drawer and the side panel slide. */
export function useMembersPageDrawers({
  teamsRead, layout, navigate, location, activeName, createOpen, navigationType, active,
}: MembersPageDrawersOptions) {
  const { activeTeam } = teamsRead
  const { isMobile, overlayOpen } = layout
  /**
   * Phone: the roster as a LEFT drawer over the open chat, the chat page's
   * sessions drawer with the crewmate list in place of the session list. The
   * drawer holds the shell's navigation rail (MobileNavRailContext) beside the
   * roster, finger-tracks a rightward drag (`useDrawerSwipe`), and settles on
   * the compositor (`registerDrawerTargets`). One phase value rather than an
   * open flag plus a mounted flag: `closing` keeps the panel in the DOM for the
   * slide out. Only while a chat or a team is open — with nothing open the
   * roster IS the page and there is nothing to slide over.
   */
  const [rosterDrawerPhase, setRosterDrawerPhase] = useState<'closed' | 'open' | 'closing'>('closed')
  const rosterDrawerPhaseRef = useRef(rosterDrawerPhase)
  rosterDrawerPhaseRef.current = rosterDrawerPhase
  const rosterDrawerOpen = rosterDrawerPhase === 'open'
  const rosterDrawerMounted = rosterDrawerPhase !== 'closed'
  const rosterDrawerX = useMotionValue(0)
  const rosterDrawerPanelRef = useRef<HTMLDivElement | null>(null)
  const rosterDrawerScrimRef = useRef<HTMLDivElement | null>(null)
  /** The element inside the drawer the roster aside is portaled into. State,
   *  not a ref, so the portal renders once the drawer has mounted it. */
  const [rosterDrawerHost, setRosterDrawerHost] = useState<HTMLDivElement | null>(null)
  /** The shell's main navigation as a 72px rail, provided on a phone. */
  const mobileNavRail = useMobileNavRail()
  // The drawer autofocuses nothing, but its roster search raises the software
  // keyboard, and iOS Safari shrinks only the VISUAL viewport for it. Inset the
  // drawer and its scrim to the visible band, as the chat page's drawer does,
  // so the lower roster rows are not left behind the keyboard. Both terms are
  // 0 at rest.
  const vv = useVisualViewport()
  const keyboardInset =
    typeof window === 'undefined' ? 0 : Math.max(0, window.innerHeight - vv.offsetTop - vv.height)
  /** The drawer's own width as its travel, plus the safe-area inset it is
   *  pinned behind — see `drawerTravel` in pages/chat/page/sessionsDrawer.ts for why
   *  the viewport width would leave the settle's tail playing offscreen. */
  const rosterDrawerTravel = useCallback(
    () => Math.max(0, (window.innerWidth || 0) - ROSTER_DRAWER_UNCOVERED_PX + safeAreaLeft()),
    [],
  )
  const rosterDrawerScrim = useTransform(rosterDrawerX, (x) =>
    Math.max(0, Math.min(1, 1 + x / Math.max(1, rosterDrawerTravel()))))
  // Safe only because the roster rows drop their `layout` projection while
  // hosted in the drawer (`reduceMotion` is forced on there) — see
  // registerDrawerTargets' precondition.
  useEffect(() => registerDrawerTargets(rosterDrawerX, {
    panel: () => rosterDrawerPanelRef.current,
    scrim: () => rosterDrawerScrimRef.current,
    travel: rosterDrawerTravel,
  }), [rosterDrawerX, rosterDrawerTravel])
  /**
   * The drawer's history entry, so the platform Back dismisses the drawer
   * instead of leaving the crewmate. A bare duplicate of the entry below, held
   * exactly while the drawer is open — the chat page's `pushDrawerEntry` rule.
   */
  const rosterDrawerEntryRef = useRef(false)
  /** The duplicate's own `location.key`, learnt on the render after the push,
   *  so a close can tell whether that entry is still the current one. */
  const rosterDrawerEntryKeyRef = useRef<string | null>(null)
  const locationRef = useRef(location)
  locationRef.current = location
  const pushRosterDrawerEntry = useCallback(() => {
    if (!isMobile || rosterDrawerEntryRef.current) return
    const loc = locationRef.current
    // Standing on the leave guard's own duplicate (`NavigationBackGuard` arms
    // one while a Profile schedule draft is unsaved): a drawer entry above it
    // would be popped back ONTO that duplicate on close, which the guard reads
    // as a press to carry on past, disarming it and letting the next Back drop
    // the draft unasked. So no entry here: Back with the drawer open is then
    // the guard's own question, and closing the drawer touches no history.
    if (isTrapEntry(loc.state)) return
    rosterDrawerEntryRef.current = true
    rosterDrawerEntryKeyRef.current = null
    navigate({ pathname: loc.pathname, search: loc.search, hash: loc.hash })
  }, [isMobile, navigate])
  useEffect(() => {
    if (rosterDrawerEntryRef.current && rosterDrawerEntryKeyRef.current === null) {
      rosterDrawerEntryKeyRef.current = location.key
    }
  }, [location.key])
  /**
   * Spend the duplicate entry, then run `after` on the entry below it.
   *
   * A pick in the drawer changes `?member=`, and the pop has to land FIRST: a
   * replace written onto the duplicate would be popped straight off again,
   * reopening the crewmate the user just left. So `after` runs ONLY from the
   * router's report of that pop (the POP effect below), never ahead of it. A
   * pop that never reports within the window drops the pick instead of
   * running it on the wrong entry: the drawer is closed and the user taps
   * again, which then needs no pop at all.
   */
  const afterRosterDrawerPopRef = useRef<(() => void) | null>(null)
  const afterRosterDrawerPopTimerRef = useRef<number | undefined>(undefined)
  const runAfterRosterDrawerPop = useCallback(() => {
    const after = afterRosterDrawerPopRef.current
    afterRosterDrawerPopRef.current = null
    window.clearTimeout(afterRosterDrawerPopTimerRef.current)
    after?.()
  }, [])
  useEffect(() => () => window.clearTimeout(afterRosterDrawerPopTimerRef.current), [])
  const consumeRosterDrawerEntry = useCallback((after?: () => void) => {
    if (!rosterDrawerEntryRef.current) { after?.(); return }
    rosterDrawerEntryRef.current = false
    if (after) {
      afterRosterDrawerPopRef.current = after
      window.clearTimeout(afterRosterDrawerPopTimerRef.current)
      afterRosterDrawerPopTimerRef.current = window.setTimeout(() => {
        afterRosterDrawerPopRef.current = null
      }, ROSTER_DRAWER_POP_TIMEOUT_MS)
    }
    navigate(-1)
  }, [navigate])
  const openRosterDrawer = useCallback(() => {
    if (rosterDrawerPhaseRef.current === 'open') return
    if (rosterDrawerPhaseRef.current === 'closed') rosterDrawerX.set(-rosterDrawerTravel())
    rosterDrawerPhaseRef.current = 'open'
    setRosterDrawerPhase('open')
    takeOverDrawer(rosterDrawerX)
    animateDrawer(rosterDrawerX, 0)
    pushRosterDrawerEntry()
  }, [rosterDrawerX, rosterDrawerTravel, pushRosterDrawerEntry])
  /** The close animation alone: the Back that closes the drawer already spent
   *  the entry, so it must not spend another. */
  const runRosterDrawerClose = useCallback(() => {
    if (rosterDrawerPhaseRef.current !== 'open') return false
    rosterDrawerPhaseRef.current = 'closing'
    setRosterDrawerPhase('closing')
    takeOverDrawer(rosterDrawerX)
    animateDrawer(rosterDrawerX, -rosterDrawerTravel(), () => {
      rosterDrawerPhaseRef.current = 'closed'
      setRosterDrawerPhase('closed')
    })
    return true
  }, [rosterDrawerX, rosterDrawerTravel])
  const closeRosterDrawer = useCallback((after?: () => void) => {
    runRosterDrawerClose()
    consumeRosterDrawerEntry(after)
  }, [runRosterDrawerClose, consumeRosterDrawerEntry])
  // A Back that lands on the entry below the drawer's closes the drawer and
  // stays on the crewmate; a pop this page issued for a drawer pick carries the
  // pick out now that it has landed. `location.key`, so it runs once per entry.
  useEffect(() => {
    if (navigationType !== 'POP') return
    if (afterRosterDrawerPopRef.current) { runAfterRosterDrawerPop(); return }
    if (!rosterDrawerEntryRef.current || rosterDrawerPhaseRef.current !== 'open') return
    rosterDrawerEntryRef.current = false
    runRosterDrawerClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.key, navigationType])
  /** Mount the drawer for a drag in progress, without the settle that would
   *  race the finger; the entry is minted where the gesture commits. */
  const beginRosterDrawerDrag = useCallback(() => {
    rosterDrawerPhaseRef.current = 'open'
    setRosterDrawerPhase('open')
  }, [])

  /**
   * Phone: the side panel's overlay slides in from the RIGHT on the same
   * finger-tracked, compositor-settled path, so a leftward drag over the chat
   * opens it — the chat page's right-hand overlay. On a phone the overlay is
   * the full window wide, so it carries no scrim of its own. The offset starts
   * offscreen: an open that is not a discrete event (a document read, a
   * resolved guard) runs the seating effect below only after a paint, and the
   * panel must not show at rest for that frame.
   */
  const sidePanelX = useMotionValue(typeof window !== 'undefined' ? window.innerWidth : 0)
  const sidePanelSlideRef = useRef<HTMLDivElement | null>(null)
  const [sidePanelPhase, setSidePanelPhase] = useState<'closed' | 'open' | 'closing'>('closed')
  const sidePanelPhaseRef = useRef(sidePanelPhase)
  sidePanelPhaseRef.current = sidePanelPhase
  const sidePanelTravel = useCallback(() => window.innerWidth || 0, [])
  useEffect(() => registerDrawerTargets(sidePanelX, {
    panel: () => sidePanelSlideRef.current,
    scrim: () => null,
    travel: sidePanelTravel,
  }), [sidePanelX, sidePanelTravel])
  const overlayOpenRef = useRef(overlayOpen)
  overlayOpenRef.current = overlayOpen
  // Slide in when the overlay is wanted, out (mounted for the travel) when it
  // stops being wanted. A gesture that already carried the panel to rest has
  // set the phase first, so this does not run a second settle over it.
  useEffect(() => {
    if (!isMobile) {
      sidePanelPhaseRef.current = 'closed'
      setSidePanelPhase('closed')
      takeOverDrawer(sidePanelX)
      return
    }
    if (overlayOpen) {
      if (sidePanelPhaseRef.current === 'open') return
      if (sidePanelPhaseRef.current === 'closed') sidePanelX.set(sidePanelTravel())
      sidePanelPhaseRef.current = 'open'
      setSidePanelPhase('open')
      takeOverDrawer(sidePanelX)
      animateDrawer(sidePanelX, 0)
    } else {
      if (sidePanelPhaseRef.current !== 'open') return
      sidePanelPhaseRef.current = 'closing'
      setSidePanelPhase('closing')
      takeOverDrawer(sidePanelX)
      animateDrawer(sidePanelX, sidePanelTravel(), () => {
        sidePanelPhaseRef.current = 'closed'
        setSidePanelPhase('closed')
      })
    }
  }, [isMobile, overlayOpen, sidePanelX, sidePanelTravel])
  const beginSidePanelDrag = useCallback(() => {
    sidePanelPhaseRef.current = 'open'
    setSidePanelPhase('open')
  }, [])
  /**
   * Phone: replay a drag that began over a frame in the open side panel onto the
   * frame element, so the panel's closing gesture can follow it.
   *
   * The Dashboard tab is a sandboxed iframe, and a touch on one goes to the
   * frame's own document, never to this page. Its document relays its touches
   * (`relayTouches`, see FRAME_TOUCH_RELAY_BODY in lib/widgetSrcdoc), and they
   * are dispatched here as touch events on the frame element, whose ancestors
   * carry the swipe listeners. Accepted only from a frame inside the open panel
   * and only while it is open, which is when the one gesture they can drive is
   * the panel's own close: the frame's script can post the same shape, and this
   * keeps that from reaching anything else.
   */
  useEffect(() => {
    if (!isMobile) return
    const types = { start: 'touchstart', move: 'touchmove', end: 'touchend', cancel: 'touchcancel' } as const
    const onMessage = (event: MessageEvent) => {
      const d = event.data as { type?: unknown; phase?: unknown; x?: unknown; y?: unknown } | null
      if (!d || d.type !== 'mc-frame-touch') return
      if (sidePanelPhaseRef.current !== 'open') return
      const type = typeof d.phase === 'string' ? types[d.phase as keyof typeof types] : undefined
      if (!type || typeof d.x !== 'number' || typeof d.y !== 'number' || !Number.isFinite(d.x) || !Number.isFinite(d.y)) return
      const panel = sidePanelSlideRef.current
      if (!panel) return
      const frame = Array.from(panel.querySelectorAll('iframe')).find((f) => f.contentWindow === event.source)
      if (!frame) return
      const box = frame.getBoundingClientRect()
      const touch = { clientX: box.left + d.x, clientY: box.top + d.y }
      const ev = new Event(type, { bubbles: true })
      const lifted = type === 'touchend' || type === 'touchcancel'
      Object.defineProperty(ev, 'touches', { value: lifted ? [] : [touch] })
      Object.defineProperty(ev, 'changedTouches', { value: [touch] })
      frame.dispatchEvent(ev)
    }
    window.addEventListener('message', onMessage)
    return () => window.removeEventListener('message', onMessage)
  }, [isMobile])
  const rosterDrawerAvailable = isMobile && !!(activeName || activeTeam) && !createOpen
  const sidePanelSwipeAvailable = isMobile && !!active && !createOpen
  /** The roster aside's home: the drawer while it is the roster's place, the
   *  page otherwise. Nothing renders while the drawer is closed — the chat is
   *  the screen then. */
  const placeRoster = (aside: React.ReactElement) => {
    if (!rosterDrawerAvailable) return aside
    return rosterDrawerMounted && rosterDrawerHost ? createPortal(aside, rosterDrawerHost) : null
  }

  return {
    rosterDrawerPhase, setRosterDrawerPhase, rosterDrawerPhaseRef, rosterDrawerOpen, rosterDrawerMounted,
    rosterDrawerX, rosterDrawerPanelRef, rosterDrawerScrimRef, setRosterDrawerHost, mobileNavRail, vv,
    keyboardInset, rosterDrawerTravel, rosterDrawerScrim, rosterDrawerEntryRef, rosterDrawerEntryKeyRef,
    locationRef, pushRosterDrawerEntry, consumeRosterDrawerEntry, openRosterDrawer, runRosterDrawerClose,
    closeRosterDrawer, beginRosterDrawerDrag, sidePanelX, sidePanelSlideRef, setSidePanelPhase, sidePanelPhase,
    sidePanelPhaseRef, overlayOpenRef, beginSidePanelDrag, rosterDrawerAvailable, sidePanelSwipeAvailable,
    placeRoster,
  }
}

export type MembersPageDrawers = ReturnType<typeof useMembersPageDrawers>

interface MembersPageDrawerSwipesOptions {
  teamsRead: MembersPageTeamsRead
  layout: MembersPageLayout
  drawers: MembersPageDrawers
  activeName: string
  createOpen: boolean
  membersRootRef: MutableRefObject<HTMLDivElement | null>
}

/** The two phone drags on the page root, and the drawer's retirement when the column changes. */
export function useMembersPageDrawerSwipes({
  teamsRead, layout, drawers, activeName, createOpen, membersRootRef,
}: MembersPageDrawerSwipesOptions) {
  const { urlTeam } = teamsRead
  const { isMobile, setOverlayOpen } = layout
  const {
    rosterDrawerPhase, setRosterDrawerPhase, rosterDrawerPhaseRef, rosterDrawerOpen, rosterDrawerX,
    rosterDrawerTravel, rosterDrawerEntryRef, rosterDrawerEntryKeyRef, locationRef, pushRosterDrawerEntry,
    consumeRosterDrawerEntry, runRosterDrawerClose, closeRosterDrawer, beginRosterDrawerDrag, sidePanelX,
    setSidePanelPhase, sidePanelPhase, sidePanelPhaseRef, overlayOpenRef, beginSidePanelDrag,
    rosterDrawerAvailable, sidePanelSwipeAvailable,
  } = drawers
  const rosterDrawerDragging = useDrawerSwipe(membersRootRef, {
    enabled: rosterDrawerAvailable && sidePanelPhase !== 'open',
    travel: rosterDrawerTravel,
    open: rosterDrawerOpen,
    x: rosterDrawerX,
    onGestureOpen: beginRosterDrawerDrag,
    // The entry stands for intent, so it is minted and spent where the gesture
    // commits, not when the slide arrives (ChatPage's sessions drawer).
    onCommit: (open) => {
      if (open) { pushRosterDrawerEntry(); return }
      rosterDrawerPhaseRef.current = 'closing'
      setRosterDrawerPhase('closing')
      consumeRosterDrawerEntry()
    },
    onSettle: (open) => {
      if (open) return
      rosterDrawerPhaseRef.current = 'closed'
      setRosterDrawerPhase('closed')
    },
  })
  useDrawerSwipe(membersRootRef, {
    enabled: sidePanelSwipeAvailable && rosterDrawerPhase !== 'open',
    side: 'right',
    open: sidePanelPhase === 'open',
    x: sidePanelX,
    onGestureOpen: beginSidePanelDrag,
    onCommit: (open) => {
      if (open) return
      sidePanelPhaseRef.current = 'closing'
      setSidePanelPhase('closing')
    },
    onSettle: (open) => {
      // Committed open: publish it, so the panel stays across a re-render. The
      // slide effect sees a phase already at 'open' and does not re-animate.
      if (open) { setOverlayOpen(true); return }
      // Parked: 'closed' first, so the effect does not run a second slide-out,
      // and the flag is only flipped when the overlay really was open — an
      // opening drag that was reconsidered never set it.
      sidePanelPhaseRef.current = 'closed'
      setSidePanelPhase('closed')
      if (overlayOpenRef.current) setOverlayOpen(false)
    },
  })
  // A crewmate or team opened (from the drawer or anywhere else), or a creation
  // surface taking the column, retires the drawer. An open that came through a
  // drawer pick has already closed it and spent its entry; any other leaves the
  // entry behind as a plain step back to where the user was.
  useEffect(() => {
    if (rosterDrawerPhaseRef.current !== 'open') return
    rosterDrawerEntryRef.current = false
    runRosterDrawerClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeName, urlTeam])
  useEffect(() => {
    // A "+" in the drawer opens the card on the drawer's own duplicate entry, so
    // it is spent like any close. A guide opens it by PUSHING its create link
    // over that duplicate: popping then would take the guide's entry instead,
    // so the duplicate stays as a plain step back, the way an open from outside
    // the drawer leaves it (the effect above).
    if (!createOpen || rosterDrawerPhaseRef.current !== 'open') return
    if (rosterDrawerEntryRef.current && locationRef.current.key === rosterDrawerEntryKeyRef.current) {
      closeRosterDrawer()
      return
    }
    rosterDrawerEntryRef.current = false
    runRosterDrawerClose()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [createOpen])
  // Leaving the phone viewport drops the drawer with no slide; the desktop
  // roster is a column, so an entry standing for an open drawer goes with it.
  useEffect(() => {
    if (isMobile) return
    rosterDrawerPhaseRef.current = 'closed'
    setRosterDrawerPhase('closed')
    consumeRosterDrawerEntry()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isMobile])

  return {
    rosterDrawerDragging,
  }
}

export type MembersPageDrawerSwipes = ReturnType<typeof useMembersPageDrawerSwipes>
