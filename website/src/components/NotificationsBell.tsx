import { useEffect, useState, useCallback, useRef, type HTMLAttributes } from 'react'
import { createPortal } from 'react-dom'
import { useLocation, useNavigate } from 'react-router-dom'
import { useAppSelector, useAppDispatch } from '../store'
import { ackNotification } from '../store/notificationsSlice'
import { useIsMobile } from '../hooks/useIsMobile'
import { recordEvent } from '../rum'
import { useGuardedLeave } from './NavigationLeaveGuard'
import { animateDrawer, registerDrawerTargets, takeOverDrawer } from '../hooks/useDrawerSwipe'
import { useMotionValue } from 'framer-motion'

import { Bell, AlertTriangle } from 'lucide-react'
import ErrorBoundary from './ErrorBoundary'
import NotificationDetailPanel from './notifications/NotificationDetailPanel'
import NotificationFeed from './notifications/NotificationFeed'
import { i18nT } from '../i18n/t'
const NC_SHEET_DESKTOP_W = 400
/** Extra travel past the sheet's own width so its shadow clears the edge too —
 *  what `translateX(calc(100% + 20px))` used to spell. */
const NC_SHEET_CLEARANCE = 20
/**
 * Backstop for the exit phase ONLY.
 *
 * `animateDrawer` reports arrival on every path it has — finish, browser-cancel,
 * and the main-thread fallback it takes when there is no element or no
 * `Element.animate` — so the unmount is normally driven by that callback and
 * this timer never fires. It exists because a stuck `closing` phase would leave
 * the bell inert (a tap during the exit is deliberately a no-op, see the
 * `onClick` below), and it is deliberately far longer than the 240ms exit
 * settle: a tight value would race the animation it is meant to outlive.
 */
const NC_CLOSE_BACKSTOP_MS = 1000

/**
 * Topbar Notifications bell. The Notifications surface is `hiddenFromNav`, so
 * this is its entry point. Click opens an Activity Feed popover
 * (portaled to <body> to escape the topbar's backdrop-filter containing
 * block); clicking an item slides out a detail panel. The full page is
 * preserved at /notifications via the popover's "Open inbox" link.
 */
export function NotificationsBellButton() {
  const navigate = useNavigate()
  // Both jumps out of this popover run inside the gate: the bell is reachable
  // from every page, including one holding an unsaved draft, and each handler
  // also CLOSES the popover — so asking around the `navigate` alone would leave
  // the user's "keep my draft" answer with the panel shut behind it.
  const leave = useGuardedLeave()
  const location = useLocation()
  const dispatch = useAppDispatch()
  const items = useAppSelector(s => s.notifications.items)
  const isMobile = useIsMobile()
  /**
   * ONE phase value, not an `open` + `closing` pair (mirrors the mobile nav
   * drawer above and ChatPage's sessions drawer).
   *
   * The pair was the defect: dismissal set `closing = true` AND `open = false`
   * in the same commit, while the sheet stayed on screen for the whole exit
   * animation. For those 240ms the logical state said closed and the pixels said
   * open, so the bell's `if (open) close() else open()` toggle read a tap as
   * "it's closed, open it" and re-entered the sheet — the reported "tapped to
   * dismiss and it opened again". A phase cannot disagree with itself: anything
   * other than `closed` means the sheet is on screen.
   */
  const [phase, setPhase] = useState<'closed' | 'open' | 'closing'>('closed')
  // Read by the handlers, which must see the phase this tap produced rather than
  // the one their closure was rendered with.
  const phaseRef = useRef(phase)
  phaseRef.current = phase
  const open = phase === 'open'
  const closing = phase === 'closing'
  const [selectedTs, setSelectedTs] = useState<string | null>(null)
  const containerRef = useRef<HTMLDivElement>(null)
  const popoverRef = useRef<HTMLDivElement>(null)
  const bellRef = useRef<HTMLButtonElement>(null)
  const sheetRef = useRef<HTMLDivElement | null>(null)
  /** Sheet offset in px: 0 at rest, +parked offscreen to the right. */
  const sheetX = useMotionValue(0)
  /**
   * Where the sheet sits when parked offscreen.
   *
   * Measured off the mounted sheet when there is one. Before the first mount
   * there is nothing to measure, so it is derived from the same rule the layout
   * uses. On mobile that overshoots by the safe-area insets (0 in portrait), and
   * overshooting is invisible — the sheet is offscreen either way and the settle
   * still lands exactly on 0. Deriving it from `innerWidth` on DESKTOP would
   * not be: the sheet is 400px there, so it would enter from far beyond its own
   * edge and the 420ms would be spent crossing empty space.
   */
  const parkedOffset = useCallback(() => {
    const measured = sheetRef.current?.offsetWidth
    if (measured && measured > 0) return measured + NC_SHEET_CLEARANCE
    const w = isMobile ? (typeof window !== 'undefined' ? window.innerWidth : 0) : NC_SHEET_DESKTOP_W
    return w + NC_SHEET_CLEARANCE
  }, [isMobile])
  /**
   * Point the settle at the real sheet so it runs on the COMPOSITOR, and — the
   * reason this replaced the CSS keyframe pair — so a REVERSAL is continuous.
   *
   * `animate-nc-slide-in` / `animate-nc-slide-out` each began at a hardcoded
   * endpoint, so swapping the class mid-flight teleported the sheet to the new
   * animation's `from` instead of continuing from where it was. Measured on a
   * 390px sheet: dismissing 100ms into the entrance jumped it the remaining
   * ~100px to fully-open before sliding out (~325px at 30ms), and re-opening
   * 50ms into the exit flung it the full 410px offscreen and replayed the entire
   * 420ms entrance. `animateDrawer` keyframes from the offset the outgoing
   * animation is PRESENTING, which is exactly the discontinuity those two
   * measurements are.
   *
   * `scrim: null` because the sheet's column scrim is its own CHILD and travels
   * with it; there is no separate backdrop to fade in lockstep. Safe against
   * registerDrawerTargets' projection precondition because nothing under
   * `components/notifications/` imports framer-motion at all.
   */
  useEffect(() => registerDrawerTargets(sheetX, {
    panel: () => sheetRef.current,
    scrim: () => null,
    travel: parkedOffset,
  }), [sheetX, parkedOffset])
  // Badge counts attention-worthy rows only (RFC Phase 3): passive and
  // muted-channel (silenced) rows are excluded, mirroring the backend's
  // _unread_count semantics.
  const unacked = items.filter(n => !n.acked && n.priority !== 'passive' && !n.silenced)

  // RFC Phase 4: mirror the unread count onto the desktop dock/taskbar badge.
  useEffect(() => {
    const api = (window as Window & { electronAPI?: { setBadgeCount?: (n: number) => void } }).electronAPI
    api?.setBadgeCount?.(unacked.length)
  }, [unacked.length])
  const selected = selectedTs ? items.find(n => n.ts === selectedTs) || null : null

  // Single dismissal path: every close (bell toggle, outside click, Escape,
  // navigation, error fallback) goes through here so the sheet always gets its
  // slide-out instead of being torn down instantly. Re-entrant by design — a
  // second dismissal while one is already running must not restart the settle.
  const closePanel = useCallback(() => {
    if (phaseRef.current !== 'open') return
    phaseRef.current = 'closing'
    setPhase('closing')
    setSelectedTs(null)
    takeOverDrawer(sheetX)
    animateDrawer(sheetX, parkedOffset(), () => {
      phaseRef.current = 'closed'
      setPhase('closed')
    })
  }, [sheetX, parkedOffset])

  const openPanel = useCallback(() => {
    if (phaseRef.current === 'open') return
    // Seat the parked offset BEFORE the phase flips: the render below serializes
    // `sheetX.get()` into the sheet's inline transform, so writing the value
    // first is what makes the FIRST painted frame offscreen instead of a flash
    // at rest followed by an entrance from nowhere.
    if (phaseRef.current === 'closed') sheetX.set(parkedOffset())
    phaseRef.current = 'open'
    setPhase('open')
    setSelectedTs(null)
    takeOverDrawer(sheetX)
    animateDrawer(sheetX, 0)
    recordEvent('notifications_open', { source: 'topbar' })
  }, [sheetX, parkedOffset])

  // See NC_CLOSE_BACKSTOP_MS: `animateDrawer`'s arrival callback owns the
  // unmount, and this only rescues a phase that never heard back at all.
  useEffect(() => {
    if (phase !== 'closing') return
    const t = window.setTimeout(() => {
      phaseRef.current = 'closed'
      setPhase('closed')
    }, NC_CLOSE_BACKSTOP_MS)
    return () => window.clearTimeout(t)
  }, [phase])

  // While the sheet plays its exit animation it is STILL in the DOM, so it must
  // stop being interactive in every modality — not just the pointer. `inert`
  // removes it from the tab order and the accessibility tree too, which is what
  // keeps a leaving panel from stealing a Tab stop or being announced. React 18
  // has no `inert` prop, so it rides through as a plain string attribute;
  // pointer-events-none stays as the floor for browsers without `inert`.
  const leavingProps = (closing
    ? { inert: '', 'aria-hidden': true }
    : {}) as HTMLAttributes<HTMLDivElement>

  // Close popover when navigating (e.g. detail panel's "Go to Chat" buttons)
  const lastPathRef = useRef(location.pathname)
  useEffect(() => {
    if (location.pathname !== lastPathRef.current) {
      lastPathRef.current = location.pathname
      if (open) closePanel()
    }
  }, [location.pathname, open, closePanel])

  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Node | null
      if (!target) return
      const inButton = containerRef.current?.contains(target) ?? false
      const inPopover = popoverRef.current?.contains(target) ?? false
      if (!inButton && !inPopover) {
        closePanel()
      }
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        if (selectedTs) setSelectedTs(null)
        // Escape is the keyboard dismissal, so return focus to the trigger.
        // The pointer paths deliberately do NOT do this: at pointerdown the
        // click's own focus move hasn't happened yet, so forcing focus here
        // would steal it from whatever the user just clicked.
        else { closePanel(); bellRef.current?.focus() }
      }
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    return () => { document.removeEventListener('pointerdown', onPointerDown); document.removeEventListener('keydown', onKey) }
  }, [open, selectedTs, closePanel])

  // Auto-mark-read when opening a notification's detail
  useEffect(() => {
    if (selected && !selected.acked) dispatch(ackNotification(selected.ts))
  }, [selected, dispatch])

  return (
    <div ref={containerRef} className="relative">
      <button
        ref={bellRef}
        className={`flex items-center justify-center w-7 h-7 rounded-md hover:bg-bg-hover transition-colors bg-transparent border-none cursor-pointer shrink-0 relative ${open ? 'text-accent' : 'text-muted hover:text-text'}`}
        onClick={() => { if (phaseRef.current === 'closed') openPanel(); else closePanel() }}
        title={unacked.length > 0 ? i18nT('app.notification_count', { count: unacked.length }) : i18nT('app.notifications')}
        aria-label={i18nT('app.notifications')}
        aria-haspopup="dialog"
        aria-expanded={open}
      >
        <Bell size={15} />
        {unacked.length > 0 && (
          <span className="absolute -top-1 -right-1 min-w-[16px] h-[16px] px-1 rounded-full bg-accent text-accent-fg text-[10px] font-bold flex items-center justify-center shadow-[0_0_2px_var(--accent-glow)]" aria-hidden="true">
            {unacked.length > 99 ? '99+' : unacked.length}
          </span>
        )}
      </button>
      {(open || closing) && createPortal(
        <div
          ref={popoverRef}
          // Anchored 48px below the viewport top, which the shell has pushed
          // down by the top inset — top-safe-offset-[48px] adds both.
          //
          // Both branches inset horizontally too, because a landscape iPhone is
          // ~852px wide and so takes the NON-mobile branch (isMobile is
          // max-width:767px) — that is where the sensor housing sits beside the
          // sheet's right edge. left-safe-or-3 keeps the desktop 12px gutter
          // and widens to the inset only when there is one.
          className={`fixed z-[60] pointer-events-none top-safe-offset-[48px] bottom-safe ${isMobile ? 'left-safe right-safe' : 'right-safe left-safe-or-3'}`}
        >
          <ErrorBoundary
            scope="notifications-bell"
            fallback={
              <div {...leavingProps} className={`absolute top-0 right-0 ${closing ? 'pointer-events-none' : 'pointer-events-auto'} ${isMobile ? 'w-full' : 'w-[400px]'} glass-surface glass-static rounded-xl shadow-xl flex flex-col items-center justify-center gap-2 p-6 text-center`} style={{ maxHeight: 240 }}>
                <AlertTriangle size={20} className="text-warn" />
                <div className="text-[13px] font-semibold text-text-strong">{i18nT('app.notifications_failed_to_load')}</div>
                <button className="text-[12px] text-accent hover:text-accent-hover bg-transparent border-none cursor-pointer" onClick={() => leave(() => { closePanel(); navigate('/notifications') }, '/notifications')}>{i18nT('app.open_the_full_inbox')}</button>
              </div>
            }
          >
          {/* Sheet — macOS Notification Center style: the panel itself is fully
              transparent (a tinted/blurred panel paints a hard edge at its left
              boundary — exactly what NC doesn't have). Every readable element
              (header, controls, notification rows) is its own floating
              material card instead. */}
          <div
            ref={sheetRef}
            {...leavingProps}
            data-nc-phase={phase}
            className={`absolute top-0 bottom-0 right-0 ${closing ? 'pointer-events-none' : 'pointer-events-auto'} ${isMobile ? 'w-full' : 'w-[400px]'} flex flex-col isolate`}
            // Serialized from the MotionValue rather than bound through framer:
            // this element is not framer-bound, and `animateDrawer` writes the
            // arrival into the element's own inline style for exactly that
            // reason. A re-render mid-settle re-serializes a stale offset here,
            // which is harmless — a running animation on `transform` wins over
            // the inline style, and the settle publishes the final value itself.
            style={{ transform: `translate3d(${sheetX.get()}px, 0, 0)` }}
          >
            {/* Column scrim — macOS NC dims/blurs only the strip behind the
                cards and it travels WITH the sheet. The layer extends 80px
                past the sheet's left edge and a mask fades both the dim and
                the blur to nothing there, so there is no hard boundary.
                -z-10 + isolate on the sheet keeps it behind the cards without
                forming a backdrop root (isolation is not a root trigger, so
                the cards' own backdrop-blur still samples the page). */}
            <div
              aria-hidden="true"
              className="absolute inset-y-0 -left-20 right-0 -z-10 pointer-events-none bg-black/[.12] backdrop-blur-sm [mask-image:linear-gradient(to_right,transparent,black_80px)] [-webkit-mask-image:linear-gradient(to_right,transparent,black_80px)]"
            />
            <div className="flex-1 min-h-0 px-3 py-2 flex flex-col">
              <NotificationFeed
                variant="mac"
                header={
                  <div className="flex items-center px-1 pb-1.5">
                    <span className="text-[14px] font-bold text-text-strong">{i18nT('app.notifications')}</span>
                  </div>
                }
                footer={
                  <div className="flex justify-end px-1 pb-1">
                    <button
                      className="text-[12px] text-accent hover:text-accent-hover bg-transparent border-none cursor-pointer"
                      onClick={() => leave(() => { closePanel(); navigate('/notifications') }, '/notifications')}
                    >
                      {i18nT('app.open_inbox')}
                    </button>
                  </div>
                }
                selectedTs={selectedTs}
                onSelect={n => setSelectedTs(n.ts)}
              />
            </div>
          </div>
          {/* Detail panel — overlays feed on mobile, sits beside it on desktop.
              Rendered plainly (no AnimatePresence): an exit animation here races
              the portal teardown when the popover closes and throws removeChild. */}
          {selected && (
            <div
              className={`absolute top-0 bottom-0 pointer-events-auto ${isMobile ? 'left-0 right-0' : 'left-0 right-[408px]'} bg-card border border-border rounded-xl shadow-xl overflow-hidden`}
            >
              <NotificationDetailPanel
                n={selected}
                onClose={() => setSelectedTs(null)}
              />
            </div>
          )}
          </ErrorBoundary>
        </div>,
        document.body
      )}
    </div>
  )
}
