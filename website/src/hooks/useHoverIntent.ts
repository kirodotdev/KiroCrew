import { useCallback, useEffect, useRef, useState } from 'react'
import type { RefObject } from 'react'

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
  surfaceProps: PointerHandlers & { onBlur: (e: React.FocusEvent) => void }
  /** Close now, skipping the grace period. */
  close: () => void
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
  } = options

  const [open, setOpen] = useState(false)
  const [openedBy, setOpenedBy] = useState<'hover' | 'keyboard' | null>(null)
  const openTimer = useRef<number | null>(null)
  const closeTimer = useRef<number | null>(null)

  const clearTimers = useCallback(() => {
    if (openTimer.current !== null) { window.clearTimeout(openTimer.current); openTimer.current = null }
    if (closeTimer.current !== null) { window.clearTimeout(closeTimer.current); closeTimer.current = null }
  }, [])

  const close = useCallback(() => {
    clearTimers()
    setOpen(false)
    setOpenedBy(null)
  }, [clearTimers])

  // Timers outlive a fast unmount (navigating away mid-delay) unless cleared.
  useEffect(() => clearTimers, [clearTimers])

  // Disabling mid-hover must retract the surface, not freeze it on screen.
  useEffect(() => { if (!enabled) close() }, [enabled, close])

  const scheduleOpen = useCallback((by: 'hover' | 'keyboard') => {
    if (!enabled) return
    clearTimers()
    // A keypress is an explicit request — no intent delay to second-guess.
    if (by === 'keyboard') { setOpen(true); setOpenedBy('keyboard'); return }
    openTimer.current = window.setTimeout(() => {
      openTimer.current = null
      setOpen(true)
      setOpenedBy('hover')
    }, openMs)
  }, [enabled, openMs, clearTimers])

  const scheduleClose = useCallback(() => {
    if (!enabled) return
    clearTimers()
    closeTimer.current = window.setTimeout(() => {
      closeTimer.current = null
      setOpen(false)
      setOpenedBy(null)
    }, closeMs)
  }, [enabled, closeMs, clearTimers])

  const cancelClose = useCallback(() => {
    if (closeTimer.current !== null) { window.clearTimeout(closeTimer.current); closeTimer.current = null }
  }, [])

  // Escape + outside pointerdown, bound only while open so a closed surface
  // costs no listeners.
  const insideAnchors = useCallback((target: EventTarget | null) => {
    const node = target as Node | null
    if (!node) return false
    return !!triggerRef?.current?.contains(node) || !!surfaceRef?.current?.contains(node)
  }, [triggerRef, surfaceRef])

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

  return {
    open,
    openedBy,
    triggerProps: {
      onMouseEnter: () => scheduleOpen('hover'),
      onMouseLeave: scheduleClose,
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
      onMouseLeave: scheduleClose,
      onBlur: onFocusOut,
    },
    close,
  }
}
