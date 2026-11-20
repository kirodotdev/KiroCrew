import { useCallback, useEffect, useRef, useState } from 'react'
import { Gauge } from 'lucide-react'
import { Btn, Card, CardTitle } from '../../components/ui'
import { keyboardFocused } from '../../components/InstantTip'
import { OverlayPortal, useCloseWhenCrewHidden } from '../../components/overlayRoot'
import { useHoverIntent } from '../../hooks/useHoverIntent'
import { ESCAPE_PRIORITY, useEscapeLayer } from '../../hooks/useEscapeLayer'
import { acpBackendName } from '../../api/acpBackend'
import type { PlanLimitReading, PlanLimitWindow, PlanLimits } from '../../api/planLimits'
import { metricColor } from '../../utils/metricColor'
import { fmtDateTime, fmtPercent, fmtRelative } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

const EM_DASH = '\u2014'

type WindowKey = 'five_hour' | 'weekly'

/** One window as the row and the detail card show it, judged against one clock. */
export interface PlanLimitWindowView {
  key: WindowKey
  label: string
  window: PlanLimitWindow
  /** The reset passed after the reading, so its percentage describes an earlier window. */
  stale: boolean
}

/**
 * What the Codex row has to say. `none`: Codex has reported nothing on this
 * gateway. `no-windows`: it reported, but named no usage window (a sign-in
 * with no plan limits).
 */
export type PlanLimitRowState =
  | { kind: 'none' }
  | { kind: 'no-windows'; reading: PlanLimitReading }
  | { kind: 'windows'; reading: PlanLimitReading; windows: PlanLimitWindowView[] }

/**
 * The window's length as the provider states it. Codex sends `window_minutes`;
 * a window without it is named for its length: `five_hour` is five hours and
 * `weekly` is seven days.
 */
function windowMinutes(key: WindowKey, window: PlanLimitWindow): number {
  return window.windowMinutes ?? (key === 'five_hour' ? 300 : 7 * 1440)
}

export function planLimitWindowLabel(key: WindowKey, window: PlanLimitWindow): string {
  const minutes = windowMinutes(key, window)
  if (minutes % 1440 === 0) return i18nT('pages.overview.planLimits.window_days', { days: minutes / 1440 })
  if (minutes % 60 === 0) return i18nT('pages.overview.planLimits.window_hours', { hours: minutes / 60 })
  return i18nT('pages.overview.planLimits.window_minutes', { minutes })
}

export function planLimitRowState(reading: PlanLimitReading | null, now: number): PlanLimitRowState {
  if (!reading) return { kind: 'none' }
  const windows: PlanLimitWindowView[] = []
  for (const [key, window] of [['five_hour', reading.fiveHour], ['weekly', reading.weekly]] as const) {
    if (!window) continue
    // The server judged `stale` when it answered; the report can be minutes
    // old by the time it renders, so the reset time is judged again here.
    const stale = window.stale || (window.resetsAt !== null && window.resetsAt <= now)
    windows.push({ key, label: planLimitWindowLabel(key, window), window, stale })
  }
  return windows.length ? { kind: 'windows', reading, windows } : { kind: 'no-windows', reading }
}

function usedColor(view: PlanLimitWindowView): string {
  if (view.stale) return 'text-muted'
  return metricColor(view.window.usedPercent / 100)
}

function fmtShare(percent: number): string {
  return fmtPercent(percent / 100, { maximumFractionDigits: 0 })
}

/** The card's place: `top` when it opens below the row, `bottom` when it opens above. */
type Anchor = { top?: number; bottom?: number; right: number; maxHeight: number; maxWidth: number }

/** The card opens below the row when at least this much height is free there. */
const MIN_SPACE_BELOW = 120
const GAP = 6
const EDGE = 8

function anchorToRow(el: HTMLElement | null): Anchor | null {
  const r = el?.getBoundingClientRect()
  if (!r) return null
  const right = Math.max(EDGE, window.innerWidth - r.right)
  // The card's right edge sits on the row's, so its width is what is left of
  // the viewport from there, with the same 8px margin on the left.
  const maxWidth = Math.max(160, window.innerWidth - right - EDGE)
  // Below the row unless too little height is free there and more is free
  // above. Either way the card's height is capped to its side of the row, so
  // the card stays in the viewport and scrolls inside itself whenever that
  // side has room for the card's padding.
  const spaceBelow = window.innerHeight - r.bottom - GAP - EDGE
  const spaceAbove = r.top - GAP - EDGE
  if (spaceBelow >= MIN_SPACE_BELOW || spaceBelow >= spaceAbove) {
    return { top: r.bottom + GAP, right, maxHeight: Math.max(0, spaceBelow), maxWidth }
  }
  return { bottom: window.innerHeight - r.top + GAP, right, maxHeight: Math.max(0, spaceAbove), maxWidth }
}

function near(a: number | undefined, b: number | undefined): boolean {
  return a === undefined || b === undefined ? a === b : Math.abs(a - b) < 1
}

/** Whether the row still sits where the card was anchored to it. */
function rowStillAt(el: HTMLElement | null, anchor: Anchor | null): boolean {
  const now = anchorToRow(el)
  return !!now && !!anchor && near(now.top, anchor.top) && near(now.bottom, anchor.bottom) && near(now.right, anchor.right)
}

function PlanLimitRow({ reading }: { reading: PlanLimitReading | null }) {
  const name = acpBackendName({ id: 'codex' })
  const triggerRef = useRef<HTMLButtonElement>(null)
  const cardRef = useRef<HTMLDivElement>(null)
  const cardId = 'plan-limits-card-codex'
  // A click or tap pins the card as a dialog; a hover or keyboard focus shows
  // the same card as a tooltip without moving focus.
  const [pinned, setPinned] = useState<Anchor | null>(null)
  const [focusAnchor, setFocusAnchor] = useState<Anchor | null>(null)
  // Hover stays off while the focus preview shows the card: an open hover
  // pushes a newer Escape layer, so one Escape would close only the hover and
  // leave the focus card on screen.
  const hover = useHoverIntent({ enabled: !pinned && focusAnchor === null, triggerRef, surfaceRef: cardRef })
  const closeHover = hover.close
  const [hoverAnchor, setHoverAnchor] = useState<Anchor | null>(null)
  // Set while focus is handed back to the row, so that focus does not reopen
  // the card the user just dismissed.
  const refocusing = useRef(false)
  // A close that unmounts a card holding focus hands focus back to the row
  // first, so focus does not drop to the page body. A close with focus
  // elsewhere leaves it where the user put it.
  const returnFocusFromCard = useCallback(() => {
    if (!cardRef.current?.contains(document.activeElement)) return
    refocusing.current = true
    // No scroll: a scroll that moved the row away closes the card, and focus
    // must not drag the page back to the row.
    triggerRef.current?.focus({ preventScroll: true })
    refocusing.current = false
  }, [])
  useEffect(() => {
    if (hover.open) {
      setHoverAnchor(anchorToRow(triggerRef.current))
      return
    }
    // The hover closed, on its timer or for any other reason. A hover card the
    // user clicked into still holds focus here, one render before it unmounts.
    returnFocusFromCard()
    setHoverAnchor(null)
  }, [hover.open, returnFocusFromCard])
  const anchor = pinned ?? hoverAnchor ?? focusAnchor
  const open = anchor !== null
  const closeAll = useCallback(() => {
    setPinned(null)
    setFocusAnchor(null)
    closeHover()
  }, [closeHover])
  const dismiss = useCallback(() => {
    returnFocusFromCard()
    closeAll()
  }, [returnFocusFromCard, closeAll])
  // The card is placed from a snapshot of the row's box, so a resize, or a
  // scroll that moves the row, closes it rather than leaving it adrift. A
  // scroll inside the card, or of a pane the row is not in, keeps it open.
  const anchorRef = useRef<Anchor | null>(null)
  useEffect(() => { anchorRef.current = anchor }, [anchor])
  useEffect(() => {
    if (!open) return
    const onScroll = (e: Event) => {
      if (cardRef.current?.contains(e.target as Node | null)) return
      if (rowStillAt(triggerRef.current, anchorRef.current)) return
      dismiss()
    }
    window.addEventListener('resize', dismiss)
    window.addEventListener('scroll', onScroll, true)
    return () => {
      window.removeEventListener('resize', dismiss)
      window.removeEventListener('scroll', onScroll, true)
    }
  }, [open, dismiss])
  // A pinned card is a non-modal dialog: focus moves in, and a press outside
  // or Escape closes it. Escape hands focus back to the row only while the card
  // holds it; after a Tab out, focus stays on the control the user moved to.
  useEffect(() => {
    if (!pinned) return
    cardRef.current?.focus()
    const onPointerDown = (e: PointerEvent) => {
      const t = e.target as Node
      if (triggerRef.current?.contains(t) || cardRef.current?.contains(t)) return
      setPinned(null)
    }
    document.addEventListener('pointerdown', onPointerDown)
    return () => document.removeEventListener('pointerdown', onPointerDown)
  }, [pinned])
  // Active whenever the card shows, a hover-open card included, so every
  // Escape goes through dismiss() and the `refocusing` guard: the hover's own
  // layer would hand focus back to the row with no guard, and that focus would
  // reopen the card as a focus preview. This layer is pushed after the hover's
  // (the card opens one render after hover.open, and in a shared commit this
  // effect runs second), so the newest-first order asks it first.
  useEscapeLayer(open, e => {
    if (e.isComposing) return 'yield'
    dismiss()
    return 'take'
  }, ESCAPE_PRIORITY.flyout)
  useCloseWhenCrewHidden(open, closeAll)
  // The clock every stale verdict and the reading's age are judged against. It
  // moves when a new report arrives and every 30 s while the card is open;
  // nothing ticks while it is closed.
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => { setNow(Date.now()) }, [reading])
  useEffect(() => {
    if (!open) return
    setNow(Date.now())
    const id = setInterval(() => setNow(Date.now()), 30_000)
    return () => clearInterval(id)
  }, [open])

  const state = planLimitRowState(reading, now)
  // A pinned card is a dialog. A hover or focus preview is a tooltip.
  const role: 'dialog' | 'tooltip' = pinned ? 'dialog' : 'tooltip'

  return (
    <>
      <Btn
        ref={triggerRef}
        data-testid="plan-limits-row-codex"
        className="w-full min-h-11 justify-between gap-3 px-3 py-2 text-left"
        {...hover.triggerProps}
        aria-haspopup="dialog"
        aria-expanded={pinned !== null}
        aria-describedby={open && role === 'tooltip' ? cardId : undefined}
        onFocus={e => {
          // Keyboard focus only: a mouse click focuses the row too, and pins it.
          if (!refocusing.current && keyboardFocused(e.currentTarget)) setFocusAnchor(anchorToRow(e.currentTarget))
        }}
        onBlur={e => {
          hover.triggerProps.onBlur(e)
          if (!cardRef.current?.contains(e.relatedTarget as Node | null)) setFocusAnchor(null)
        }}
        onClick={() => {
          hover.close()
          setFocusAnchor(null)
          setPinned(prev => (prev ? null : anchorToRow(triggerRef.current)))
        }}
      >
        <span className="font-medium text-text">{name}</span>
        <RowSummary state={state} />
      </Btn>
      {anchor && (
        <OverlayPortal>
          {/* eslint-disable-next-line jsx-a11y/no-static-element-interactions -- role is tooltip or dialog, set from a variable the rule cannot read; onBlur only closes the card when focus leaves it */}
          <div
            ref={cardRef}
            id={cardId}
            role={role}
            {...(pinned ? {} : hover.surfaceProps)}
            // Focus that leaves the card for anything but the row closes a focus
            // card, which a click on it focused.
            onBlur={e => {
              if (!pinned) hover.surfaceProps.onBlur(e)
              const to = e.relatedTarget as Node | null
              if (!triggerRef.current?.contains(to) && !cardRef.current?.contains(to)) setFocusAnchor(null)
            }}
            // Only the dialog form carries a name. The tooltip is what the row's
            // aria-describedby resolves to, so a label there would replace its text.
            {...(role === 'dialog' ? { 'aria-label': i18nT('pages.overview.planLimits.details_label', { provider: name }) } : {})}
            tabIndex={-1}
            data-testid="plan-limits-detail"
            className="fixed z-[70] w-80 overflow-y-auto rounded-xl bg-card border border-border shadow-xl px-3 py-2.5 flex flex-col gap-2 text-[12px] text-text"
            style={{ top: anchor.top, bottom: anchor.bottom, right: anchor.right, maxHeight: anchor.maxHeight, maxWidth: anchor.maxWidth }}
          >
            <div className="text-[12px] font-semibold text-text-strong">
              {i18nT('pages.overview.planLimits.details_label', { provider: name })}
            </div>
            <PlanLimitDetail state={state} now={now} />
          </div>
        </OverlayPortal>
      )}
    </>
  )
}

function RowSummary({ state }: { state: PlanLimitRowState }) {
  if (state.kind === 'none') {
    return <span className="text-muted text-[12px]">{i18nT('pages.overview.planLimits.summary_no_reading')}</span>
  }
  if (state.kind === 'no-windows') {
    // Codex writes a window-less block for a sign-in with no plan limits.
    return <span className="text-muted text-[12px]">{i18nT('pages.overview.planLimits.summary_no_limits')}</span>
  }
  return (
    <span className="flex flex-wrap items-baseline justify-end gap-x-3 gap-y-0.5 font-mono text-[12px] tabular-nums">
      {state.windows.map(view => (
        <span key={view.key} className={usedColor(view)}>
          {i18nT('pages.overview.planLimits.summary_window', { window: view.label, used: view.stale ? EM_DASH : fmtShare(view.window.usedPercent) })}
        </span>
      ))}
    </span>
  )
}

function WindowDetail({ view }: { view: PlanLimitWindowView }) {
  const { window: w, stale } = view
  const left = Math.max(0, 100 - w.usedPercent)
  const reset = w.resetsAt === null
    ? i18nT('pages.overview.planLimits.reset_unknown')
    : stale
      ? i18nT('pages.overview.planLimits.reset_passed', { time: fmtDateTime(w.resetsAt) })
      : i18nT('pages.overview.planLimits.resets_at', { time: fmtDateTime(w.resetsAt) })
  return (
    <div className="flex flex-col gap-1" data-testid={`plan-limits-window-${view.key}`}>
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-muted">{i18nT('pages.overview.planLimits.window_label', { window: view.label })}</span>
        <span className={`font-mono tabular-nums ${usedColor(view)}`}>
          {stale ? EM_DASH : i18nT('pages.overview.planLimits.used_left', { used: fmtShare(w.usedPercent), left: fmtShare(left) })}
        </span>
      </div>
      <div className="text-[11px] text-muted">{reset}</div>
    </div>
  )
}

function PlanLimitDetail({ state, now }: { state: PlanLimitRowState; now: number }) {
  if (state.kind === 'none') {
    return <p className="text-muted">{i18nT('pages.overview.planLimits.no_reading_codex')}</p>
  }
  const { reading } = state
  return (
    <>
      {state.kind === 'windows'
        ? state.windows.map(view => <WindowDetail key={view.key} view={view} />)
        : <p className="text-muted">{i18nT('pages.overview.planLimits.no_windows_codex')}</p>}
      <div className="border-t border-border pt-1.5 flex flex-col gap-0.5 text-[11px] text-muted">
        <span>
          {reading.capturedAt === null
            ? i18nT('pages.overview.planLimits.reported_unknown')
            : i18nT('pages.overview.planLimits.reported_at', { relative: fmtRelative(reading.capturedAt, { now }) })}
        </span>
        <span>{i18nT('pages.overview.planLimits.source_codex')}</span>
      </div>
    </>
  )
}

/**
 * The Usage view's plan-limits card: one Codex row with the usage windows
 * Codex reports for its account, opening a detail card on hover, keyboard
 * focus, or a click or tap. The row shows when Codex has a reading, and on a
 * Codex install even before it has one, so that install sees why the row is
 * empty; an install that runs only Kiro gets no card. The numbers are Codex's,
 * for the account; the token and cost cards below count only this install's turns.
 */
export function PlanLimitsCard({ limits, defaultBackend }: {
  limits: PlanLimits | undefined
  /** `agent.acp_backend` of a LOADED config; undefined while it is unread. */
  defaultBackend: string | undefined
}) {
  const reading = limits?.codex ?? null
  if (reading === null && defaultBackend !== 'codex') return null
  return (
    <Card data-testid="plan-limits-card">
      <CardTitle><Gauge className="lucide-inline" /> {i18nT('pages.overview.planLimits.title')}</CardTitle>
      <p className="text-[12px] text-muted mb-3">{i18nT('pages.overview.planLimits.intro')}</p>
      <div className="flex flex-col gap-2">
        <PlanLimitRow reading={reading} />
      </div>
    </Card>
  )
}
