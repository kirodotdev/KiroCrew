import { useEffect, useState, useCallback, useRef, type HTMLAttributes } from 'react'
import { createPortal } from 'react-dom'
import { useLocation, useNavigate } from 'react-router-dom'
import { useMotionValue } from 'framer-motion'
import { AlertTriangle } from 'lucide-react'
import { useAppSelector, useAppDispatch } from '../../store'
import { ackNotification } from '../../store/notificationsSlice'
import { useIsMobile } from '../../hooks/useIsMobile'
import { animateDrawer, registerDrawerTargets, takeOverDrawer } from '../../hooks/useDrawerSwipe'
import { useGuardedLeave } from '../../components/NavigationLeaveGuard'
import ErrorBoundary from '../../components/ErrorBoundary'
import AskAgentButton from '../../components/AskAgentButton'
import NotificationDetailPanel from '../../components/notifications/NotificationDetailPanel'
import NotificationFeed from '../../components/notifications/NotificationFeed'
import NotificationBanner from '../../components/notifications/NotificationBanner'
import { recordEvent } from '../../rum'
import { i18nT } from '../../i18n/t'

/**
 * Desktop width of the notification sheet, in px.
 *
 * Stated as a constant because the PARKED offset is derived from it, and a
 * parked offset that disagrees with the rendered width is not a cosmetic
 * mismatch: too small leaves a strip of the sheet on screen before the
 * entrance starts, too large stretches the entrance over travel the sheet
 * never occupies. Tailwind cannot take an interpolated class, so the `w-[400px]`
 * literal below is the second spelling — `App.notificationSheetExit.test.tsx`
 * pins the two together.
 */
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
 * `toggle` below), and it is deliberately far longer than the 240ms exit
 * settle: a tight value would race the animation it is meant to outlive.
 */
const NC_CLOSE_BACKSTOP_MS = 1000

/**
 * True when the press landed on `el`'s own classic scrollbar.
 *
 * The one thing a material selector cannot express: a scrollbar hit-tests to
 * the element it scrolls, so a press on the list's 6px thumb has the SAME
 * target as a press on the empty strip below the last card. Only the pointer
 * position tells them apart — the client box excludes the bar, so a pointer
 * outside it (past the right edge, or the left edge under RTL where
 * `clientLeft` already counts the bar) is on the bar. Overlay scrollbars take
 * no layout space and cannot be told apart this way, but this dashboard styles
 * `::-webkit-scrollbar`, which makes every Chromium and WebKit bar a classic
 * one. Nothing to detect while the content does not overflow — which also
 * covers a DOM with no layout at all, where every box measures zero.
 */
function onOwnScrollbar(el: Element, e: MouseEvent): boolean {
  const r = el.getBoundingClientRect()
  const x0 = r.left + el.clientLeft
  const y0 = r.top + el.clientTop
  const onVerticalBar = el.scrollHeight > el.clientHeight && (e.clientX < x0 || e.clientX >= x0 + el.clientWidth)
  const onHorizontalBar = el.scrollWidth > el.clientWidth && (e.clientY < y0 || e.clientY >= y0 + el.clientHeight)
  return onVerticalBar || onHorizontalBar
}

/**
 * A press inside the popover that hit the sheet's own background rather than
 * something on it.
 *
 * The sheet is transparent by design: the panel paints nothing and every
 * readable element is a floating card, so the popover's box says nothing about
 * what the user pressed. On a phone that box is the whole viewport under the
 * top bar, on desktop it is the 400px column — so judging a press by the box
 * left the strip below the last card inert while the identical-looking strip
 * left of the column dismissed, and on a phone left nothing but the bell to
 * dismiss with. A press is judged by what it landed on instead. "On it" means
 * a card (`notif-material`, the index.css hook every card already carries), a
 * row, the detail panel (`data-nc-material`) or any control — those keep the
 * sheet; anything else inside the popover is its background and dismisses
 * exactly like a press outside would. Labels that float directly on the
 * background (group headings, the empty inbox) are background too — they are
 * not cards and hold nothing to press. Judged for the pointerdown and again
 * for the click that completes it.
 */
function isSheetBackgroundPress(target: Element, e: MouseEvent): boolean {
  if (target.closest('.notif-material, [data-notif-row], [data-nc-material], button, a, input, textarea, select, [role="button"]')) return false
  return !onOwnScrollbar(target, e)
}

/**
 * The notification sheet the top-bar bell opens: its open/closing/closed phase,
 * the compositor slide, every dismissal path (the bell, an outside or background
 * press, Escape, a route change) and the once-per-selection auto-ack. The bell
 * button itself, and the unread badge on it, stay with the bell in `App.tsx`.
 */
export function useNotificationSheet() {
  const location = useLocation()
  const dispatch = useAppDispatch()
  const items = useAppSelector(s => s.notifications.items)
  const isMobile = useIsMobile()
  /**
   * ONE phase value, not an `open` + `closing` pair (mirrors the shell's mobile nav
   * drawer in `App.tsx` and ChatPage's sessions drawer).
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
   * registerDrawerTargets' projection precondition because nothing rendered
   * INSIDE the sheet uses framer-motion (`NotificationBanner` does, but it is
   * portalled beside the sheet, never within it).
   */
  useEffect(() => registerDrawerTargets(sheetX, {
    panel: () => sheetRef.current,
    scrim: () => null,
    travel: parkedOffset,
  }), [sheetX, parkedOffset])
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

  // The banner's "open on this note" path: the same open as the bell, then
  // the selection — one state owner, so the popover's auto-ack effect and its
  // detail panel work for a banner tap exactly as for a row tap.
  const openPanelOn = useCallback((ts: string) => {
    openPanel()
    setSelectedTs(ts)
  }, [openPanel])

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
    // Where the pointer gesture in flight began and ended: on the sheet's own
    // background (inside the popover, on no material — see
    // isSheetBackgroundPress) or not. Set by every pointerdown and pointerup,
    // consumed by the click that completes the same gesture.
    let pressedBackground = false
    let releasedBackground = false
    const onBackground = (target: Node, e: MouseEvent) =>
      // A pointer never targets a text node, so a node inside the popover is
      // an Element.
      (popoverRef.current?.contains(target) ?? false) && isSheetBackgroundPress(target as Element, e)
    const onPointerDown = (e: PointerEvent) => {
      const target = e.target as Node | null
      if (!target) return
      const inButton = containerRef.current?.contains(target) ?? false
      const inPopover = popoverRef.current?.contains(target) ?? false
      pressedBackground = onBackground(target, e)
      releasedBackground = false
      if (!inButton && !inPopover) {
        closePanel()
      }
    }
    const onPointerUp = (e: PointerEvent) => {
      const target = e.target as Node | null
      releasedBackground = !!target && onBackground(target, e)
    }
    // A press on the sheet's background dismisses too, because on a phone the
    // popover's box is the whole viewport under the top bar and nothing else
    // could. It is dismissed at CLICK, not at pointerdown, and only when the
    // gesture both began AND ended there with nothing selected on the way:
    // - at click the sheet is still hit-testable, so the gesture ends on the
    //   sheet and never reaches the page under the transparent strip.
    //   Dismissing at pointerdown made the leaving sheet pointer-transparent
    //   and the same tap's click landed on whatever sat beneath — a
    //   suggestion chip, a link;
    // - a touch drag that starts in the gap between two cards to scroll the
    //   list produces no click, so scrolling still works;
    // - a drag that crosses a card's edge in EITHER direction (selecting its
    //   text) clicks the common ancestor of its two ends, which is background
    //   — requiring both ends to be background is what keeps that from
    //   dismissing, whichever end was the card;
    // - a drag between two background points sweeps the cards between them
    //   into a selection; the selection is the intent, so a click that left
    //   one is not a dismissal either.
    const onClick = (e: MouseEvent) => {
      const backgroundGesture = pressedBackground && releasedBackground
      pressedBackground = false
      releasedBackground = false
      if (!backgroundGesture) return
      const target = e.target as Node | null
      if (!target || !popoverRef.current?.contains(target)) return
      if (!(window.getSelection()?.isCollapsed ?? true)) return
      if (isSheetBackgroundPress(target as Element, e)) closePanel()
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
    document.addEventListener('pointerup', onPointerUp)
    document.addEventListener('click', onClick)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('pointerup', onPointerUp)
      document.removeEventListener('click', onClick)
      document.removeEventListener('keydown', onKey)
    }
  }, [open, selectedTs, closePanel])

  // Auto-mark-read when opening a notification's detail -- ONCE per
  // selection. A rejected ack flips the row back to unread
  // (`ackNotification.rejected`), and re-asking on that flip would loop the
  // request forever; the detail panel's own "Mark read" is the retry.
  const autoAckedTsRef = useRef<string | null>(null)
  useEffect(() => {
    if (!selected) { autoAckedTsRef.current = null; return }
    if (selected.acked || autoAckedTsRef.current === selected.ts) return
    autoAckedTsRef.current = selected.ts
    dispatch(ackNotification(selected.ts))
  }, [selected, dispatch])

  const toggle = () => { if (phaseRef.current === 'closed') openPanel(); else closePanel() }
  return {
    items, isMobile, phase, open, closing, selectedTs, setSelectedTs, selected,
    containerRef, popoverRef, bellRef, sheetRef, sheetX, leavingProps,
    closePanel, openPanelOn, toggle,
  }
}

/** The sheet and the live-arrival banner, both portalled to `<body>`. */
export function NotificationSheet({ sheet }: { sheet: ReturnType<typeof useNotificationSheet> }) {
  const navigate = useNavigate()
  // Every in-app jump out of this popover runs inside the gate — the inbox
  // link, and the crash fallback's agent hand-off (through the button's own
  // `gate`): the bell is reachable from every page, including one holding an
  // unsaved draft, and each handler also CLOSES the popover — so asking around
  // the `navigate` alone would leave the user's "keep my draft" answer with
  // the panel shut behind it.
  const leave = useGuardedLeave()
  const {
    isMobile, phase, open, closing, selectedTs, setSelectedTs, selected,
    popoverRef, bellRef, sheetRef, sheetX, leavingProps, closePanel, openPanelOn,
  } = sheet
  return (
    <>
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
          fallback={error => (
            <div {...leavingProps} data-nc-material className={`absolute top-0 right-0 ${closing ? 'pointer-events-none' : 'pointer-events-auto'} ${isMobile ? 'w-full' : 'w-[400px]'} glass-surface glass-static rounded-xl shadow-xl flex flex-col items-center justify-center gap-2 p-6 text-center`} style={{ maxHeight: 240 }}>
              <AlertTriangle size={20} className="text-warn" />
              <div className="text-[13px] font-semibold text-text-strong">{i18nT('app.notifications_failed_to_load')}</div>
              {/* The same hand-off as the boundary's default card this panel
                  replaces. The crash's own message is what lets the button
                  recover the journaled report at click time — `|| name` is
                  the value the boundary journals for a message-less throw,
                  and the button renders nothing for an empty string. SOFT,
                  through the same gate as the inbox link below: the crash is
                  contained to the sheet, so the router and store under it
                  are sound, and a full load would rebuild the store and drop
                  every draft it holds — a Remote Crew form under edit lives
                  in `instances.crewForms` precisely so an in-app navigation
                  keeps it, and `beforeunload` never sees a store-held draft.
                  The gate is the button's own, so a veto stages nothing.
                  `onHandoff` dismisses the sheet: a jump to another page
                  closes it through the route change, but a hand-off raised
                  ON the chat changes no route and would leave this panel
                  sitting over the composer it just filled. */}
              <AskAgentButton
                message={error.message || error.name}
                variant="solid"
                gate={proceed => leave(proceed, '/chat')}
                onHandoff={closePanel}
              />
              <div className="text-[12px] text-muted">{i18nT('app.notifications_ask_agent_help')}</div>
              <button className="text-[12px] text-accent hover:text-accent-hover bg-transparent border-none cursor-pointer" onClick={() => leave(() => { closePanel(); navigate('/notifications') }, '/notifications')}>{i18nT('app.open_the_full_inbox')}</button>
            </div>
          )}
        >
        {/* Sheet — macOS Notification Center style: the panel itself is fully
            transparent (a tinted/blurred panel paints a hard edge at its left
            boundary — exactly what NC doesn't have). Every readable element
            (header, controls, notification rows) is its own floating
            material card instead.
            Invariant: everything composed into the sheet is material (a
            `notif-material` card, a `data-notif-row`, `data-nc-material`, a
            control) or background BY DECISION — an unmarked child dismisses
            the sheet on press (`isSheetBackgroundPress`); the structural test
            in App.notificationSheetBackgroundDismiss.test.tsx enforces it. */}
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
              the cards' own backdrop-blur still samples the page).
              Strength: 2% black, 2px blur. The 12% / 4px it wore before the
              rows became liquid glass (#3029, for text contrast on flat
              cards) now stacks under every row's own glass tint and
              `glass-shadow`, and read as a heavy shadow down the sheet's
              left edge (measured 248 -> 217 on a white page, 12% darker).
              At 2% the strip still separates the column from the page
              (248 -> 243) without reading as a shadow; the rows carry the
              contrast themselves now. */}
          <div
            aria-hidden="true"
            className="absolute inset-y-0 -left-20 right-0 -z-10 pointer-events-none bg-black/[.02] backdrop-blur-[2px] [mask-image:linear-gradient(to_right,transparent,black_80px)] [-webkit-mask-image:linear-gradient(to_right,transparent,black_80px)]"
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
            the portal teardown when the popover closes and throws removeChild.
            Material, not background: it is an opaque card, so a press on it
            keeps the sheet like a press on a row does. */}
        {selected && (
          <div
            data-nc-material
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
    {/* Live-arrival banner. Portalled like the sheet so a transformed
        ancestor in the top bar cannot capture its `fixed` positioning; it
        borrows the bell for its exit vector and this component's open/select
        mechanics rather than holding any selection of its own. */}
    {createPortal(
      <NotificationBanner
        bellRef={bellRef}
        popoverOpen={phase !== 'closed'}
        onOpenNote={openPanelOn}
      />,
      document.body
    )}
    </>
  )
}
