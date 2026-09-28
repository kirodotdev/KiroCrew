import { useCallback, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'
import { watchCursorAway } from '../lib/cursorAway'

/** Delay before a hover opens the surface. Sweeping the pointer ACROSS a
 *  trigger on the way somewhere else must not fire it — the surface only
 *  belongs on screen if the pointer settles. Same value as the issue-radar
 *  RefLink hover card, which solved the same problem. */
export const HOVER_OPEN_MS = 320
/** Grace period after the pointer leaves. The pointer has to travel from the
 *  trigger into the surface, and any gap between them is a moment where it is
 *  over neither — without this the surface vanishes underneath it. Same value
 *  as McpInfoButton. */
export const HOVER_CLOSE_MS = 250

/** Minimum time a surface stays on screen once it opens before an off-window
 *  dismissal may retract it. A fast swipe past the edge both reveals the
 *  surface AND crosses the away distance within a few frames, so without this
 *  the slide-in is interrupted halfway and reads as a jitter. Long enough to
 *  cover the reveal animation plus a short beat of it fully shown. Coming back
 *  inside during the hold cancels the dismissal as usual. */
export const HOVER_MIN_VISIBLE_MS = 450

/** Width of the band an exit sample must fall in for the event to count as the
 *  pointer crossing the window boundary. Matches the edge-slam threshold in
 *  App.tsx: the same coarse gesture that reveals the surface dismisses it. */
const EDGE_BAND_PX = 20

/** A mid-window region that stops answering hover reports the same
 *  relatedTarget-null mouseout as a real exit; only the edge band is an exit. */
function leftThroughWindowEdge(e: MouseEvent): boolean {
  return e.clientX <= EDGE_BAND_PX || e.clientY <= EDGE_BAND_PX
    || e.clientX >= window.innerWidth - EDGE_BAND_PX
    || e.clientY >= window.innerHeight - EDGE_BAND_PX
}

interface Options {
  /** When false every handler is inert and the surface force-closes. */
  enabled?: boolean
  openMs?: number
  closeMs?: number
  /** Trigger element. Outside-pointerdown treats it as inside, and Escape
   *  hands focus back to it when focus is inside the pair. */
   triggerRef?: RefObject<HTMLElement | null>
  /** Surface element — outside-pointerdown treats it as inside. */
  surfaceRef?: RefObject<HTMLElement | null>
  /** Positional close: while OPEN, closing is decided EXCLUSIVELY by observed
   *  pointer position — a document-level mousemove for which this returns true
   *  starts the close grace, one for which it returns false cancels it, and
   *  anchor mouseleave is ignored for closing entirely.
   *
   *  For a surface that doubles as a window-drag area, event-based closing is
   *  structurally unreliable: a `-webkit-app-region: drag` rect is resolved by
   *  the compositor BEFORE hit-testing, so a pointer resting on (or dragging
   *  from) the surface's own empty region produces silence that is
   *  indistinguishable from departure. Closing on that silence oscillates —
   *  close → the drag region unmounts with the surface → events revive → the
   *  peek trigger reopens it → repeat, pointer never moving. Requiring positive
   *  evidence of departure (a mousemove genuinely outside the surface's band)
   *  is what makes "the bar stays visible while the cursor is in its area" a
   *  guarantee instead of a heuristic. The pointer leaving the WINDOW is not
   *  positional evidence either (there are no off-window samples) — that case
   *  belongs to `dismissOnWindowExit`. */
  departWhen?: (e: MouseEvent) => boolean
  /** Dismiss when the user stops looking at this window: the pointer leaves it
   *  through an edge and travels FAR ENOUGH AWAY, the window loses focus, or the
   *  tab is hidden.
   *
   *  Required for an edge-revealed surface. Its reveal gesture ends with the
   *  pointer OUTSIDE the window, so no in-window event is ever coming: a
   *  positional close has nobody left to hear from and the surface stays on
   *  screen indefinitely, over the content the user walked away to glance at.
   *
   *  "Far enough away" is a DISTANCE, not a delay, and it is measured in the
   *  Electron main process because the renderer receives no mouse events past a
   *  window edge (see `src/lib/cursorAway.ts`). So parking the pointer just
   *  outside the surface keeps it, and heading for another window dismisses it at
   *  once, however long each takes — the Arc/Zen sidebar contract. Coming back
   *  inside before that distance is crossed cancels the dismissal. An embedded
   *  pane gets the same measurement relayed through its host frame. Where none
   *  exists (a browser tab, a pane whose host cannot measure) the surface is
   *  dismissed by blur, visibility and outside-click only.
   *
   *  Focus resting inside the surface (a focused input, an open menu) suppresses
   *  the pointer path entirely — blur and visibility still cover leaving for
   *  real. */
  dismissOnWindowExit?: boolean
}

type PointerHandlers = {
  onMouseEnter: () => void
  onMouseLeave: () => void
}

export interface HoverIntent {
  open: boolean
  /** How the surface was opened. `keyboard` means the user asked for it with a
   *  keypress, so the caller SHOULD move focus into the surface; `hover` must
   *  NOT, or it steals focus from whatever the user is typing in. */
  openedBy: 'hover' | 'keyboard' | null
  /** Bind to the trigger. `onKeyDown` opens on ArrowDown (the ARIA
   *  menu-button opener) — deliberately not on focus; see the note at the
   *  return site. */
  triggerProps: PointerHandlers & {
    onKeyDown: (e: React.KeyboardEvent) => void
    onBlur: (e: React.FocusEvent) => void
  }
  /** Bind to the surface, so the pointer entering it cancels the close, and
   *  focus leaving it for good closes it. */
  surfaceProps: PointerHandlers & { onFocus: () => void; onBlur: (e: React.FocusEvent) => void }
  /** Close now, skipping the grace period. */
  close: () => void
  /** Open now, skipping the intent delay — for a caller-detected gesture that is
   *  already unambiguous (e.g. the pointer slamming through the window edge the
   *  trigger sits on: the overshoot fires mouseleave on its way OUT, which the
   *  ordinary handlers read as a cancel). Reports hover intent, so focus is not
   *  moved into the surface. */
  openNow: () => void
}

/**
 * Hover-intent for a floating surface: delayed open, graced close, keyboard
 * parity, Escape, and outside-pointerdown.
 *
 * Both delays are load-bearing and asymmetric on purpose. The open delay is
 * about *intent* (did the user mean to summon this?), the close delay is about
 * *reachability* (can the pointer get there?). Implementations with only one
 * of the two either flash open on every pass or become unreachable across a
 * gap; this repo previously had one of each, in different files.
 */
export function useHoverIntent(options: Options = {}): HoverIntent {
  const {
    enabled = true,
    openMs = HOVER_OPEN_MS,
    closeMs = HOVER_CLOSE_MS,
    triggerRef,
    surfaceRef,
    departWhen,
    dismissOnWindowExit = false,
  } = options

  const [open, setOpen] = useState(false)
  const [openedBy, setOpenedBy] = useState<'hover' | 'keyboard' | null>(null)
  const openTimer = useRef<number | null>(null)
  const closeTimer = useRef<number | null>(null)
  // Latest predicate without re-subscribing the mousemove listener per render.
  const departWhenRef = useRef(departWhen)
  departWhenRef.current = departWhen
  // Is the pointer currently out of the window? Only meaningful with
  // `dismissOnWindowExit`, which is what keeps it fed.
  const pointerOutside = useRef(false)
  // Stop function for an in-flight off-window distance watch, or null. Holding
  // it is what makes the watch cancellable AND what marks a dismissal as already
  // pending, the way a live `closeTimer` does for the timed paths.
  const cursorWatch = useRef<(() => void) | null>(null)
  // When the surface last opened, for the minimum-visible hold. Set during
  // render on the closed->open edge so an away answer in the same frame sees it.
  const openedAt = useRef(0)
  const wasOpen = useRef(false)
  if (open && !wasOpen.current) openedAt.current = Date.now()
  wasOpen.current = open

  /** Release a pending distance answer. Idempotent; also disarms the
   *  main-process poll, so nothing polls while no surface is waiting. */
  const dropCursorWatch = useCallback(() => {
    if (!cursorWatch.current) return
    cursorWatch.current()
    cursorWatch.current = null
  }, [])

  const clearTimers = useCallback(() => {
    if (openTimer.current !== null) { window.clearTimeout(openTimer.current); openTimer.current = null }
    if (closeTimer.current !== null) { window.clearTimeout(closeTimer.current); closeTimer.current = null }
  }, [])

  /** Cancel every pending transition: the open/close timers and any
   *  off-window distance watch. */
  const cancelPending = useCallback(() => {
    clearTimers()
    dropCursorWatch()
  }, [clearTimers, dropCursorWatch])

  const close = useCallback(() => {
    cancelPending()
    setOpen(false)
    setOpenedBy(null)
  }, [cancelPending])

  const openNow = useCallback(() => {
    if (!enabled) return
    // Timers only, NOT a live distance watch. The gesture this exists for shoves
    // the pointer OUT of the window, and the hook's own exit handler arms the
    // watch from that same native event one phase earlier — so cancelling it here
    // would leave the re-revealed surface with nothing watching it, permanently
    // on screen. The pointer really is outside; that watch is still the right
    // question.
    clearTimers()
    setOpen(true)
    setOpenedBy('hover')
  }, [enabled, clearTimers])

  // Timers outlive a fast unmount (navigating away mid-delay) unless cleared.
  useEffect(() => cancelPending, [cancelPending])

  // Disabling mid-hover must retract the surface, not freeze it on screen.
  useEffect(() => { if (!enabled) close() }, [enabled, close])

  const scheduleOpen = useCallback((by: 'hover' | 'keyboard') => {
    if (!enabled) return
    // A hover cannot be intended while the pointer is off-window; this is also
    // what stops a just-dismissed surface reopening under a stationary pointer.
    if (by === 'hover' && pointerOutside.current) return
    cancelPending()
    // A keypress is an explicit request — no intent delay to second-guess.
    if (by === 'keyboard') { setOpen(true); setOpenedBy('keyboard'); return }
    openTimer.current = window.setTimeout(() => {
      openTimer.current = null
      setOpen(true)
      setOpenedBy('hover')
    }, openMs)
  }, [enabled, openMs, cancelPending])

  const scheduleClose = useCallback((ms: number = closeMs) => {
    if (!enabled) return
    cancelPending()
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null
      setOpen(false)
      setOpenedBy(null)
    }, ms)
  }, [enabled, closeMs, cancelPending])

  const cancelClose = useCallback(() => {
    if (closeTimer.current !== null) { window.clearTimeout(closeTimer.current); closeTimer.current = null }
    // Every caller of this is positive evidence the pointer is back IN the window
    // (it entered the surface, or a mousemove landed inside the band), so an
    // off-window distance watch has nothing left to answer.
    dropCursorWatch()
  }, [dropCursorWatch])

  // Escape + outside pointerdown, bound only while open so a closed surface
  // costs no listeners.
  const insideAnchors = useCallback((target: EventTarget | null) => {
    const node = target as Node | null
    if (!node) return false
    return !!triggerRef?.current?.contains(node) || !!surfaceRef?.current?.contains(node)
  }, [triggerRef, surfaceRef])

  // Positional close (see `departWhen`): while open, every observed pointer
  // position votes. Outside the surface's territory → start the close grace
  // (idempotent: an armed timer is left running so the grace measures time
  // since the FIRST outside sighting); back inside → cancel it. Capture phase
  // so a stopPropagation in whatever the pointer crosses cannot eat the signal.
  const positional = !!departWhen
  useEffect(() => {
    if (!open || !positional) return
    const onMove = (e: MouseEvent) => {
      const fn = departWhenRef.current
      if (!fn) return
      if (fn(e)) {
        if (closeTimer.current === null) scheduleClose()
      } else {
        cancelClose()
      }
    }
    document.addEventListener('mousemove', onMove, true)
    return () => document.removeEventListener('mousemove', onMove, true)
  }, [open, positional, scheduleClose, cancelClose])

  // Window-exit dismissal (see `dismissOnWindowExit`). Bound while merely
  // ENABLED, not while open, because the reveal gesture that needs it fires the
  // exit BEFORE the surface opens: the flag is what lets the open arm the watch.
  useEffect(() => {
    if (!enabled || !dismissOnWindowExit) return
    const armExit = () => {
      // A dismissal already pending — timed or distance-based — is left alone, so
      // the wait measures from the FIRST exit rather than restarting.
      if (!open || closeTimer.current !== null || cursorWatch.current) return
      // Focus inside means the user is still working in the surface.
      if (insideAnchors(document.activeElement)) return
      // Ask how far the cursor actually goes. The answer arrives at most once:
      // away → the user left, dismiss; back inside → they never did, and the
      // in-window positional logic owns the surface again from here.
      // An embedded pane asks its host frame instead; a host that never answers
      // leaves the surface up, same as a browser tab.
      const stop = watchCursorAway(away => {
        cursorWatch.current = null
        if (!away) { pointerOutside.current = false; return }
        // Let a just-opened surface finish revealing before it retracts: a fast
        // swipe crosses the away distance mid slide-in. The remainder runs as an
        // ordinary close timer, so re-entry (cancelClose) still keeps it open.
        const hold = HOVER_MIN_VISIBLE_MS - (Date.now() - openedAt.current)
        if (hold > 0) scheduleClose(hold)
        else close()
      })
      // Null: no distance available (a browser tab). Blur, visibility and
      // outside-click are the dismissals there.
      if (stop) cursorWatch.current = stop
    }
    if (pointerOutside.current) armExit()
    const onOut = (e: MouseEvent) => {
      if (e.relatedTarget !== null || !leftThroughWindowEdge(e)) return
      pointerOutside.current = true
      armExit()
    }
    const onIn = () => {
      if (!pointerOutside.current) return
      pointerOutside.current = false
      cancelClose()
    }
    const onVisibility = () => { if (document.visibilityState === 'hidden') close() }
    // Capture phase: the flag must be current before React's own root listener
    // turns the same native event into the trigger's synthetic enter/leave.
    document.addEventListener('mouseout', onOut, true)
    document.addEventListener('mouseover', onIn, true)
    window.addEventListener('blur', close)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      document.removeEventListener('mouseout', onOut, true)
      document.removeEventListener('mouseover', onIn, true)
      window.removeEventListener('blur', close)
      document.removeEventListener('visibilitychange', onVisibility)
      // Never leave a poll armed against a torn-down effect. Re-running this
      // effect with the pointer still outside re-arms above, so a dep change
      // mid-watch heals itself rather than stranding the surface.
      dropCursorWatch()
    }
  }, [
    enabled, dismissOnWindowExit, open,
    insideAnchors, scheduleClose, cancelClose, close, dropCursorWatch,
  ])

  const onAnchorLeave = useCallback(() => {
    // Positional mode owns closing while open: a mouseleave can be silence from
    // a drag region or off-window travel, neither of which is departure. While
    // still CLOSED it keeps its usual job — cancelling a pending open when the
    // pointer sweeps off the trigger.
    if (positional && open) return
    scheduleClose()
  }, [positional, open, scheduleClose])

  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: PointerEvent) => { if (!insideAnchors(e.target)) close() }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key !== 'Escape' || e.isComposing) return
      // CAPTURE phase, and both preventDefault and stopPropagation.
      //
      // Escape belongs to the topmost dismissible surface, and while this
      // surface is up that is this one. Other document-level Escape handlers
      // exist — ChatInput cancels an in-progress dictation, and it defers only
      // to `[role="dialog"]`, which this menu is not. On the BUBBLE phase the
      // winner would be whichever listener happened to register first, so
      // opening the flyout mid-dictation and pressing Escape would discard the
      // captured audio instead of closing the flyout. Capture runs before every
      // bubble listener regardless of registration order, `stopPropagation`
      // then keeps them from seeing it at all, and `preventDefault` covers the
      // ones (ChatInput included) that gate on `defaultPrevented`.
      e.preventDefault()
      e.stopPropagation()
      close()
      // Hand focus back. The surface's rows are focusable, so if the keyboard
      // opened it, focus is INSIDE a subtree that is about to unmount —
      // leaving `document.activeElement` on <body>, which restarts the next Tab
      // from the top of the page and never re-announces the trigger's state.
      // Only when focus is actually in there: an Escape during a hover-open
      // must not yank focus away from wherever the user was typing.
      if (insideAnchors(document.activeElement)) triggerRef?.current?.focus()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown, true)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      // Same `true` as the add: a capture listener removed without it stays
      // registered, so every open would leak one and old closures would keep
      // firing against a closed surface.
      document.removeEventListener('keydown', onKeyDown, true)
    }
  }, [open, close, insideAnchors, triggerRef])

  // A blur only means "leaving" when focus lands OUTSIDE both anchors. Moving
  // focus from the trigger INTO the surface necessarily blurs the trigger, and
  // treating that as a departure closed the surface the moment the keyboard
  // opened it — the exact path this hook exists to support. `relatedTarget` is
  // the reliable signal here because the trigger and surface are siblings in
  // one tree, not split across a portal.
  const onFocusOut = useCallback((e: React.FocusEvent) => {
    if (!enabled) return
    if (insideAnchors(e.relatedTarget)) return
    scheduleClose()
  }, [enabled, insideAnchors, scheduleClose])

  // The mirror of onFocusOut: a surface that hides off-screen keeps its controls
  // in the tab order, so Tab can land INSIDE it while it is closed — an invisible
  // focus ring, and an invisible control on Enter. Focused chrome must be visible
  // chrome, so focus entering the surface opens it the way hover does. This is
  // not the WCAG 3.2.1 trigger-focus trap documented on triggerProps: focus is
  // already inside the surface, so revealing it discloses the focused control's
  // own container rather than moving the user anywhere.
  const onFocusIn = useCallback(() => {
    if (!enabled || open) return
    cancelPending()
    setOpen(true)
    setOpenedBy('keyboard')
  }, [enabled, open, cancelPending])

  return {
    open,
    openedBy,
    triggerProps: {
      onMouseEnter: () => scheduleOpen('hover'),
      onMouseLeave: onAnchorLeave,
      // Focus deliberately does NOT open. Opening on focus and then moving
      // focus into the surface is a WCAG 3.2.1 (On Focus) change of context,
      // and it makes the trigger impossible to Tab PAST — a keyboard user
      // sweeping through the header would be dropped into a menu they did not
      // ask for, and would have to Shift+Tab back out to reach the button.
      // ArrowDown is the ARIA menu-button opener; Enter/Space stay with the
      // trigger's own click action.
      onKeyDown: (e: React.KeyboardEvent) => {
        if (e.key !== 'ArrowDown') return
        e.preventDefault()
        scheduleOpen('keyboard')
      },
      onBlur: onFocusOut,
    },
    surfaceProps: {
      onMouseEnter: cancelClose,
      onMouseLeave: onAnchorLeave,
      onFocus: onFocusIn,
      onBlur: onFocusOut,
    },
    close,
    openNow,
  }
}
