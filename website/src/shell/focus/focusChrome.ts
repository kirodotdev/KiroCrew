import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { useFocusMode, useFocusChromeVisible, setFocusChromeVisible, setFocusModeEnabled, focusModeEnabled } from '../../hooks/useFocusMode'
import { useHoverIntent } from '../../hooks/useHoverIntent'
import { railWidthFor } from '../../hooks/useRailWidth'
import { computeHeaderDragGaps, type DragGap } from '../../lib/dragGaps'
import { isEmbeddedPane } from '../../lib/embedded'
import { isMacElectron } from '../../lib/electron'
import { setNativeFocusChrome, subscribeNativeFocusModeToggle } from '../platform/electronBridge'

/**
 * Focus mode's chrome: the top bar and the nav rail as edge-summoned overlays
 * (the peek triggers, the edge slam, one overlay at a time, a header popover
 * holding the header open) and the window chrome that follows them — the body
 * classes, the native traffic lights and drag bar, the embedded-pane relay and
 * the macOS drag gaps of the local header.
 */
export function useFocusChrome({ isMobile, navCollapsed, activeInstanceId, topReservePx = 0 }: {
  isMobile: boolean
  navCollapsed: boolean
  activeInstanceId: string | null
  /** CSS px the shell sits below the window top (macOS fullscreen). */
  topReservePx?: number
}) {
  // Focus mode: the top bar and nav rail leave the shell grid and become
  // edge-triggered hover overlays, so the active surface fills the window.
  // Desktop only — on mobile the top bar carries the ONLY route back to
  // navigation (the hamburger), so hiding it there strands the user.
  const { enabled: focusMode, toggle: toggleFocusMode } = useFocusMode()
  const focusActive = focusMode && !isMobile
  // Peek overlays. Edge strips are deliberate targets (the pointer has to reach
  // the very edge), so the open delay is much shorter than the hover-card
  // default — a 320ms wait on an intentional gesture reads as lag.
  const topPeekTrigger = useRef<HTMLDivElement | null>(null)
  const topPeekSurface = useRef<HTMLElement | null>(null)
  const railPeekTrigger = useRef<HTMLDivElement | null>(null)
  const railPeekSurface = useRef<HTMLElement | null>(null)
  const topPeek = useHoverIntent({
    enabled: focusActive, openMs: 120, closeMs: 260,
    triggerRef: topPeekTrigger, surfaceRef: topPeekSurface,
    // The revealed header doubles as the window-drag surface, and a drag region
    // eats pointer events before hit-testing — so closing must be POSITIONAL:
    // only a mousemove observed below the header band closes the bar, and event
    // silence (pointer resting on the draggable empty region, or dragging the
    // window) can never hide it. 42 is the header's height (its
    // inline style in `App.tsx`); +6 slack so grazing the band's bottom edge does not
    // count as departure. In macOS fullscreen the header starts topReservePx
    // down the window, so the band moves down with it.
    departWhen: e => e.clientY > topReservePx + 48,
    // The pointer LEAVING the window is the one case positional close cannot
    // see, and the slam below opens the bar in exactly that state.
    dismissOnWindowExit: true,
  })
  const railPeek = useHoverIntent({
    enabled: focusActive, openMs: 120, closeMs: 260,
    triggerRef: railPeekTrigger, surfaceRef: railPeekSurface,
    // Positional close, same contract as the top peek: only a mousemove observed
    // to the RIGHT of the rail band closes it. Needed once edge-slam opening
    // exists — an overlay opened with the pointer OFF-window has no
    // enter/leave history for the event-based close to work from. The band is
    // the rail track at the user's collapse state; +12 slack.
    departWhen: e => e.clientX > railWidthFor({ isMobile: false, collapsed: navCollapsed }) + 12,
    dismissOnWindowExit: true,
  })
  // Edge-slam reveal: overshooting a trigger straight OUT of the window must
  // OPEN the overlay, not cancel it (the overshoot fires mouseleave on its way
  // out, which reads as departure — yet it is the strongest possible statement
  // of intent, the same gesture that reveals the macOS Dock). `mouseout` with
  // relatedTarget null is "the pointer left the document"; the event's
  // coordinates are the last in-window sample, so a small clientY says it left
  // through the top and a small clientX through the left. 20px is wider than the
  // 10px trigger strips on purpose: a slam is coarse. Corner exits prefer the
  // top bar (clientY checked first).
  //
  // Applies on every surface, including embedded instance panes (iframes with
  // no Electron bridge) and browser tabs. In a browser a trip to the tab strip
  // or URL bar also exits through the top and pops the header; that false
  // positive is transient (the header closes as soon as the pointer re-enters
  // below the band, or on blur or an outside click) and is accepted in exchange
  // for the slam working uniformly. In the
  // desktop app the same trip is not a false positive at all: the tab strip is
  // inches away, so the cursor never crosses the dismissal distance.
  //
  // Depends on the two `openNow` callbacks, NOT on the hover-intent objects that
  // carry them: useHoverIntent returns a fresh object literal every render, so
  // depending on the objects would tear down and re-add this document listener on
  // every render of the whole app shell. `openNow` is a useCallback keyed on
  // `enabled` (= focusActive), so the listener is re-subscribed exactly when focus
  // mode flips — which is also when the effect's own guard changes answer.
  const { openNow: openTopPeek } = topPeek
  const { openNow: openRailPeek } = railPeek
  useEffect(() => {
    if (!focusActive) return
    const onOut = (e: MouseEvent) => {
      if (e.relatedTarget !== null) return
      if (e.clientY <= 20) openTopPeek()
      else if (e.clientX <= 20) openRailPeek()
    }
    document.addEventListener('mouseout', onOut)
    return () => document.removeEventListener('mouseout', onOut)
  }, [focusActive, openTopPeek, openRailPeek])
  // One overlay at a time. The top-left corner sits on both trigger strips, so
  // hovering or slamming there can open the header and the rail together. The
  // one that opened LAST is the one the user just asked for, so it wins and the
  // other is put away at once. A layout effect so the pair is never painted.
  const { close: closeTopPeek } = topPeek
  const { close: closeRailPeek } = railPeek
  const prevPeekOpen = useRef({ top: false, rail: false })
  useLayoutEffect(() => {
    const prev = prevPeekOpen.current
    const topRose = topPeek.open && !prev.top
    const railRose = railPeek.open && !prev.rail
    prevPeekOpen.current = { top: topPeek.open, rail: railPeek.open }
    if (!(topPeek.open && railPeek.open)) return
    // Both rising in one commit has no "latest"; prefer the header, the same
    // tie-break the corner slam uses.
    if (topRose) closeRailPeek()
    else if (railRose) closeTopPeek()
  }, [topPeek.open, railPeek.open, closeTopPeek, closeRailPeek])
  // A header-owned popover keeps the header on screen.
  //
  // The instance switcher's menu is portaled to document.body (Radix), so moving
  // the pointer into it reads as leaving BOTH the trigger strip and the header:
  // the close grace elapses, the header slides away, and the menu's anchor moves
  // out from under it while the user is still using it.
  //
  // The signal is `aria-haspopup` AND `aria-expanded="true"`, not aria-expanded
  // alone: the readout capsule's connection dot is an inline expand/collapse that
  // ships `aria-expanded="true"` by default with nothing popped open, so an
  // aria-expanded-only query would pin the header permanently from first paint.
  //
  // CONTRACT for header controls: any popover anchored in the header MUST render
  // `aria-haspopup` on its trigger (Radix primitives do; hand-rolled ones must
  // add it) — without it the header slides away under the open popover in focus
  // mode. That is also the accessible-markup the control owes a screen reader,
  // so the heuristic deliberately rides on it rather than on a bespoke attribute.
  const [headerPopoverOpen, setHeaderPopoverOpen] = useState(false)
  useEffect(() => {
    if (!focusActive) { setHeaderPopoverOpen(false); return }
    const header = topPeekSurface.current
    if (!header) return
    const read = () => setHeaderPopoverOpen(!!header.querySelector('[aria-haspopup][aria-expanded="true"]'))
    read()
    // childList as well as the attribute: a trigger can be mounted already-open
    // (or unmounted while open), which an attribute-only filter never sees.
    const mo = new MutationObserver(read)
    mo.observe(header, { subtree: true, childList: true, attributes: true, attributeFilter: ['aria-expanded'] })
    return () => mo.disconnect()
  }, [focusActive])
  // Is the dashboard header on screen right now? ONE fact, because the two
  // pieces of Electron chrome that cannot be reached from the DOM both follow it
  // and must not disagree: the native macOS traffic lights (AppKit views painted
  // at a window coordinate) and the injected 42px window-drag bar.
  const topChromeShown = topPeek.open || headerPopoverOpen
  // An embedded pane cannot reach the host window's chrome itself: it is a
  // cross-origin iframe with no preload, so the native traffic lights and the
  // injected drag bar are unreachable from here. Relay the state up and let the
  // host apply it — which is what makes the lights appear over a PANE's peeked
  // header, not just the local one.
  useEffect(() => {
    if (!isEmbeddedPane()) return
    try {
      // nosemgrep: javascript.browser.security.wildcard-postmessage-configuration.wildcard-postmessage-configuration
      window.parent?.postMessage({ type: 'mc-focus-chrome', v: 1, on: !focusActive || topChromeShown }, '*')
    } catch {
      /* no parent / cross-origin restriction — the next change re-posts */
    }
  }, [focusActive, topChromeShown])
  const focusChromeVisible = useFocusChromeVisible()
  // Control-free spans of the LOCAL header's band, for the macOS drag strips
  // `App.tsx` renders — the same geometry a remote pane relays via mc-drag-gaps, computed
  // directly since the local header lives in this document. Measured when the
  // header is revealed (its controls are laid out by then; the slide is a
  // transform, which does not move layout rects).
  const [localHeaderDragGaps, setLocalHeaderDragGaps] = useState<DragGap[]>([])
  useEffect(() => {
    if (!(focusActive && isMacElectron && topChromeShown)) { setLocalHeaderDragGaps([]); return }
    const header = topPeekSurface.current
    if (!header) return
    const measure = () => setLocalHeaderDragGaps(computeHeaderDragGaps(header, window.innerWidth))
    measure()
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [focusActive, topChromeShown])
  useEffect(() => {
    // The drag bar reads these classes (see the #electron-drag-bar rules in
    // electron/main.js). Left at 42px while the header is hidden it is a drag
    // region over the content focus mode just reclaimed: a drag region is
    // resolved by the compositor before hit-testing, so the top band stops
    // answering hover — including the hover that summons the header back. At
    // 42px while the header IS shown it is what makes the revealed bar draggable
    // by its empty regions, since the injected rules exempt every control on it.
    document.body.classList.toggle('mc-focus-mode', focusActive)
    document.body.classList.toggle('mc-focus-chrome', focusChromeVisible)
    // The rail's own drop shadow is gated the same way, for the same reason the
    // header's is: both stay MOUNTED and slide, so a shadow that is always on
    // paints its tail into the content while the surface itself is off screen.
    document.body.classList.toggle('mc-focus-rail', railPeek.open)
    setNativeFocusChrome(focusChromeVisible)
  }, [focusActive, focusChromeVisible, railPeek.open])
  // Same re-assert on window focus. Button visibility is window state this
  // renderer does not own, so a fullscreen round-trip or the OS re-showing the
  // buttons leaves the effect above with nothing to react to. Idempotent.
  useEffect(() => {
    if (!focusActive) return
    const reassert = () => {
      setNativeFocusChrome(focusChromeVisible)
    }
    window.addEventListener('focus', reassert)
    return () => window.removeEventListener('focus', reassert)
  }, [focusActive, focusChromeVisible])
  // Unmount-only restore, deliberately separate from the effect above: folding it
  // into that cleanup would fire on every peek and flicker the buttons back on
  // between the two commits.
  useEffect(() => () => {
    document.body.classList.remove('mc-focus-mode', 'mc-focus-chrome', 'mc-focus-rail')
    setNativeFocusChrome(true)
  }, [])
  // Publish "is the chrome on screen" for the window, but only while no remote
  // pane is filling it. When one is, the PANE owns the answer and relays it up
  // (see the mc-focus-chrome handler in InstancesViewport): the peek the user is
  // driving is the pane's, and this shell is display:none behind it. Two writers,
  // one active at a time, so they cannot fight over the value.
  useEffect(() => {
    if (activeInstanceId !== null) return
    setFocusChromeVisible(!focusActive || topChromeShown)
  }, [activeInstanceId, focusActive, topChromeShown])
  // Re-assert the Electron chrome state when the visible PANE changes. Not because
  // the answer depends on which pane is showing — it does not — but because
  // switching hides the local shell without necessarily changing the value above,
  // so nothing re-sent it and the last send was simply trusted to have stuck. It
  // had not: the traffic lights came back.
  useEffect(() => {
    if (!focusActive) return
    setNativeFocusChrome(focusChromeVisible)
  }, [activeInstanceId, focusActive, focusChromeVisible])
  // The native View > Toggle Focus Mode item: the exit that stays visible while
  // the top bar, and its own toggle, are hidden.
  useEffect(() => subscribeNativeFocusModeToggle(() => setFocusModeEnabled(!focusModeEnabled())), [])
  return {
    focusMode, toggleFocusMode, focusActive, topPeek, railPeek, topPeekTrigger, topPeekSurface,
    railPeekTrigger, railPeekSurface, topChromeShown, localHeaderDragGaps,
  }
}
