import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useQuery } from '@tanstack/react-query'
import { AudioWaveform, Monitor } from 'lucide-react'
import { api } from '../../api/client'
import { useHoverIntent } from '../../hooks/useHoverIntent'
import { usePersistedBool } from '../../hooks/usePersistedBool'
import ErrorNotice from '../../components/ErrorNotice'
import { safeSetItem } from '../../utils/safeStorage'
import { metricColor } from '../../utils/metricColor'
import { isMetricNumber, metricNumber } from '../../utils/metrics'
import { machineIdentity, type MachineIdentity } from '../../utils/machineIdentity'
import { fmtNumber, fmtPercent, fmtUnit } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

/**
 * One `/api/system` metrics frame, as the topbar readout capsule consumes it.
 *
 * EVERY field is optional on purpose. `_collect_system_metrics` builds the
 * payload key-by-key with per-probe `try/except: pass`, so a probe that fails
 * (vm_stat timeout, unreadable /proc/meminfo, `system_memory()` returning None
 * on Windows) simply omits its keys — while `mem_total_gb` can still be served
 * from the cached STATIC system info the frame is seeded with. A frame with
 * `mem_total_gb` but no `mem_used_gb` is therefore normal, not corrupt, and any
 * readout must prove a value is a finite number before formatting it. Typing
 * these as required `number` is what let `undefined.toFixed(1)` crash the root
 * app-shell boundary; `api.system()` returns `any`, so only this annotation
 * makes the compiler check the guards.
 */
export type SysMetricsFrame = {
  memUsed?: number
  memTotal?: number
  cpuPct?: number
  diskTotal?: number
  diskFree?: number
  posture?: 'ample' | 'tight' | 'critical' | 'unknown'
  availableGb?: number
  subagentCap?: number
  /** Which machine the frame describes, proven from the untyped payload by
   *  `machineIdentity`; null when the frame names nothing. */
  identity?: MachineIdentity | null
}

/**
 * Validity flags + a sanitized frame for one metrics readout.
 *
 * Both readouts (the desktop button and the mobile passive row) derive their
 * flags here so the two cannot drift apart: a `memTotal > 0` check says nothing
 * about `memUsed`, and formatting a value the flag never proved is what crashed
 * the shell.
 */
export function readMetricsFrame(raw: SysMetricsFrame) {
  return {
    cpuValid: isMetricNumber(raw.cpuPct),
    memValid: isMetricNumber(raw.memUsed) && isMetricNumber(raw.memTotal) && raw.memTotal > 0,
    dskValid: isMetricNumber(raw.diskTotal) && isMetricNumber(raw.diskFree) && raw.diskTotal > 0,
    m: {
      cpuPct: metricNumber(raw.cpuPct),
      memUsed: metricNumber(raw.memUsed),
      memTotal: metricNumber(raw.memTotal),
      diskTotal: metricNumber(raw.diskTotal),
      diskFree: metricNumber(raw.diskFree),
    },
  }
}

/**
 * The readout capsule's own state: its collapse to the bare connection dot, and
 * the system-metrics control — the inline readings, the narrow-band popover, the
 * hover card and the metrics query they all read.
 */
export function useMetricsReadout(isMobile: boolean, updateAvailable: boolean, actionsLevel: number) {
  const [metricsOpen, setMetricsOpen] = useState(() => localStorage.getItem('mc-topbar-metrics') === '1')
  // The inline metric readings are dropped by a CSS container-query rung when
  // the actions group runs out of room (the ladder in index.css, whose rungs
  // shift while the update pill is mounted). In that band the open/closed
  // preference has nothing to render, so the click opens an anchored popover
  // instead of writing a setting that produces no visible change at all.
  // Whether the inline form fits is read FROM CSS through a zero-size probe
  // carrying the rung's own class -- never from a threshold copied out of
  // index.css, which would drift from the ladder the moment a rung moves.
  const [metricsInlineFits, setMetricsInlineFits] = useState(true)
  const [metricsPopoverAnchor, setMetricsPopoverAnchor] = useState<{ top: number; right: number } | null>(null)
  const metricsPopoverOpen = metricsPopoverAnchor !== null
  const metricsProbeRef = useRef<HTMLSpanElement>(null)
  const metricsGroupRef = useRef<HTMLDivElement>(null)
  const metricsBtnRef = useRef<HTMLButtonElement>(null)
  const metricsPopoverRef = useRef<HTMLDivElement>(null)
  // Readout capsule collapse: clicking the connection dot folds the capsule
  // down to just the dot; clicking again restores the full readout.
  const [capsuleCollapsed, setCapsuleCollapsed] = usePersistedBool('mc-topbar-capsule-collapsed', false)
  const [capsuleLayoutPulse, setCapsuleLayoutPulse] = useState(false)
  const capsulePulseTimer = useRef<ReturnType<typeof setTimeout>>(undefined)
  const pulseCapsuleLayout = useCallback(() => {
    setCapsuleLayoutPulse(true)
    clearTimeout(capsulePulseTimer.current)
    capsulePulseTimer.current = setTimeout(() => setCapsuleLayoutPulse(false), 350)
  }, [])
  useEffect(() => () => clearTimeout(capsulePulseTimer.current), [])
  // Hovering the metrics control previews the same card the click-pinned
  // popover shows, in every desktop form of the control: the bare icon, the
  // narrow-band popover trigger, and the expanded inline readout (which shows
  // percentages only, so the absolute GB figures live in the card). A pinned
  // popover owns the card while it is open, so the hover path is disabled then.
  const metricsHover = useHoverIntent({
    enabled: !isMobile && !capsuleCollapsed && !metricsPopoverOpen,
    triggerRef: metricsBtnRef,
    surfaceRef: metricsPopoverRef,
  })
  const [metricsHoverAnchor, setMetricsHoverAnchor] = useState<{ top: number; right: number } | null>(null)
  useEffect(() => {
    if (!metricsHover.open) { setMetricsHoverAnchor(null); return }
    const r = metricsBtnRef.current?.getBoundingClientRect()
    setMetricsHoverAnchor(r ? { top: r.bottom + 6, right: Math.max(8, window.innerWidth - r.right) } : null)
    // The anchor is measured once, so a resize or zoom would strand the card
    // away from its trigger. Close it instead, as the pinned card does.
    const close = metricsHover.close
    window.addEventListener('resize', close)
    return () => window.removeEventListener('resize', close)
  }, [metricsHover.open, metricsHover.close])
  const metricsCardAnchor = metricsPopoverAnchor ?? metricsHoverAnchor
  const metricsCardOpen = metricsCardAnchor !== null
  const metricsCardId = 'topbar-metrics-card'
  const { data: sysMetrics, isError, errorUpdatedAt, dataUpdatedAt: sysMetricsUpdatedAt } = useQuery({ queryKey: ['system-metrics'], queryFn: () => api.system().then((d): SysMetricsFrame => ({ memUsed: d.mem_used_gb, memTotal: d.mem_total_gb, cpuPct: d.cpu_pct, diskTotal: d.disk_total_gb, diskFree: d.disk_free_gb, posture: d.resource_posture as 'ample' | 'tight' | 'critical' | 'unknown' | undefined, availableGb: d.resource_available_gb as number | undefined, subagentCap: d.subagent_cap as number | undefined, identity: machineIdentity(d.hostname, d.os, d.cpu_count) })), refetchInterval: metricsOpen || metricsCardOpen ? 30_000 : 60_000, enabled: true })
  // A failing query that never produced a frame refetches through `pending`,
  // which clears `isError` for the length of each retry: the readout and the
  // failure notice would blink on every poll, and the desktop bar re-collapse
  // with them. The last settled result is still the failure until a frame lands.
  const sysMetricsError = isError || (!sysMetrics && errorUpdatedAt > sysMetricsUpdatedAt)
  // Tick every 10s while widget is open so `sysMetricsStale` re-evaluates even when the query stops refetching (backgrounded tab, network drop).
  const [, setStaleTick] = useState(0)
  useEffect(() => {
    if (!metricsOpen && !metricsCardOpen) return
    const id = setInterval(() => setStaleTick(t => t + 1), 10_000)
    return () => clearInterval(id)
  }, [metricsOpen, metricsCardOpen])
  // Consider metrics stale if last successful fetch was > 90s ago (3x the 30s poll interval) while the widget is open.
  const sysMetricsStale = (metricsOpen || metricsCardOpen) && (sysMetricsError || (sysMetricsUpdatedAt > 0 && Date.now() - sysMetricsUpdatedAt > 90_000))
  // A pinned card is a dialog; a hover preview is a tooltip unless the stale
  // fetch notice makes it interactive, which promotes it to a dialog too.
  const metricsCardRole: 'dialog' | 'tooltip' = metricsPopoverOpen || (metricsCardOpen && sysMetricsError) ? 'dialog' : 'tooltip'
  // Only the tooltip form describes the trigger. The dialog form carries its
  // own aria-label, so describing the trigger with it would announce
  // "System metrics" twice.
  const metricsDescribedBy = metricsHoverAnchor && metricsCardRole === 'tooltip' ? metricsCardId : undefined
  // Re-read the rung's verdict on any resize of the group -- its width is what
  // the container query measures -- and whenever the update pill mounts or
  // unmounts, which moves the rung without resizing anything, or the actions
  // group's desktop ladder changes level.
  useEffect(() => {
    const probe = metricsProbeRef.current
    const group = metricsGroupRef.current
    if (!probe) return
    const read = () => setMetricsInlineFits(getComputedStyle(probe).display !== 'none')
    read()
    if (typeof ResizeObserver === 'undefined' || !group) return
    const ro = new ResizeObserver(read)
    ro.observe(group)
    return () => ro.disconnect()
  }, [updateAvailable, isMobile, actionsLevel])
  const closeMetricsPopover = useCallback(() => setMetricsPopoverAnchor(null), [])
  const toggleMetricsPopover = useCallback(() => {
    setMetricsPopoverAnchor(prev => {
      if (prev) return null
      const r = metricsBtnRef.current?.getBoundingClientRect()
      return r ? { top: r.bottom + 6, right: Math.max(8, window.innerWidth - r.right) } : null
    })
  }, [])
  // The anchor is a snapshot of the trigger's box, so anything that can move
  // the trigger dismisses the popover rather than leaving it pointing at empty
  // space. The group growing back to where the readings fit is one of those
  // moves: the trigger reverts to the inline readout in the same frame.
  useEffect(() => {
    if (!metricsPopoverOpen) return
    // Move focus INTO the dialog on open. Without this the caret stays on the
    // trigger, and a screen reader reaches the readings only by traversing to
    // the end of the document -- the portal renders at the body's end. Not a
    // focus trap: the popover is not modal, and Escape hands focus back.
    metricsPopoverRef.current?.focus()
    const onPointerDown = (e: PointerEvent) => {
      const t = e.target as Node
      if (metricsBtnRef.current?.contains(t) || metricsPopoverRef.current?.contains(t)) return
      closeMetricsPopover()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return
      closeMetricsPopover()
      metricsBtnRef.current?.focus()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKey)
    window.addEventListener('resize', closeMetricsPopover)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKey)
      window.removeEventListener('resize', closeMetricsPopover)
    }
  }, [metricsPopoverOpen, closeMetricsPopover])
  // Two ways the trigger stops existing under an open popover: the group widens
  // back to where the readings fit, and the capsule collapses to its bare
  // connection dot (which unmounts every readout, this trigger included).
  // Either would otherwise leave the portalled dialog on screen anchored to a
  // box that is gone.
  useEffect(() => {
    if (metricsInlineFits || capsuleCollapsed) closeMetricsPopover()
  }, [metricsInlineFits, capsuleCollapsed, closeMetricsPopover])
  // An actions-ladder level change re-lays out the group without a window
  // resize, so it can move the trigger or hide it (the capsule rung hides
  // everything after the connection dot). Both cards anchor to a snapshot of
  // the trigger's box, so the pinned popover and the hover card close rather
  // than pointing at empty space.
  const closeMetricsHover = metricsHover.close
  useEffect(() => {
    closeMetricsPopover()
    closeMetricsHover()
  }, [actionsLevel, closeMetricsPopover, closeMetricsHover])
  return {
    metricsOpen, setMetricsOpen, metricsInlineFits, metricsPopoverOpen, metricsProbeRef, metricsGroupRef, metricsBtnRef,
    metricsPopoverRef, capsuleCollapsed, setCapsuleCollapsed, capsuleLayoutPulse, pulseCapsuleLayout, metricsHover,
    metricsHoverAnchor, metricsCardAnchor, metricsCardOpen, metricsCardId, sysMetrics, sysMetricsError, sysMetricsStale,
    metricsCardRole, metricsDescribedBy, toggleMetricsPopover,
  }
}

type MetricsReadout = ReturnType<typeof useMetricsReadout>

/** The capsule's metrics segment in its current form; `seg` is the capsule's shared segment class. */
export function metricsSegment(metrics: MetricsReadout, seg: string): ReactNode {
  const {
    metricsOpen, setMetricsOpen, metricsInlineFits, metricsPopoverOpen, metricsBtnRef, metricsHover, sysMetrics,
    sysMetricsError, sysMetricsStale, metricsDescribedBy, toggleMetricsPopover,
  } = metrics
  // In the collapsed band (the ladder has dropped the readings) a
  // click opens the popover and the stored preference is left
  // untouched -- it still describes what to do once the readings fit
  // again. The band changes the control's behaviour and ARIA only,
  // never what it renders: the desktop top bar measures these
  // contents to pick the band (lib/useTopbarCollapse.ts), so contents
  // that changed with the band would feed back into that measurement
  // and the two would flip back and forth without settling. The open
  // forms show whether their card is pinned through CSS, not contents:
  // each open form's button is a Tailwind `group`, and its glyph
  // carries a `group-aria-*` variant that reads this `aria-expanded`,
  // which the inline band never sets.
  const popoverTrigger = metricsInlineFits ? null : {
    ref: metricsBtnRef,
    ...metricsHover.triggerProps,
    'aria-describedby': metricsDescribedBy,
    'aria-label': i18nT('app.system_metrics'),
    'aria-haspopup': 'dialog' as const,
    'aria-expanded': metricsPopoverOpen,
    onClick: toggleMetricsPopover,
  }
  if (!metricsOpen) {
    return (<button key="metrics" className={`${seg} ${popoverTrigger && metricsPopoverOpen ? 'text-accent' : 'text-muted hover:text-text'}`} {...(popoverTrigger ?? { ref: metricsBtnRef, ...metricsHover.triggerProps, 'aria-describedby': metricsDescribedBy, 'aria-label': i18nT('app.system_metrics'), 'aria-pressed': false, onClick: () => { metricsHover.close(); setMetricsOpen(true); safeSetItem('mc-topbar-metrics', '1') } })}><AudioWaveform size={12} /></button>)
  } else if (!sysMetrics) {
    // Every OPEN state pushes a toggle. This branch is reached
    // whenever the query has produced no frame, which is the whole
    // of the first fetch AND the retry window of a failing one
    // (`isError` is only set once react-query's retries are spent).
    // Pushing nothing there took the toggle off screen while the
    // readout was logically open, so the click that was aimed at it
    // landed on the capsule's background and did nothing — the
    // reported "the metrics doesn't open". The control has to
    // outlive the data it displays.
    if (sysMetricsError) {
      // The failure text lives in MetricsErrorNotice, while this control keeps
      // the same icon shape in both bands so the ladder's measurement settles.
      return (<button key="metrics" className={`${seg} group`} {...(popoverTrigger ?? { title: `${i18nT('app.system_metrics')} — ${i18nT('app.click_to_hide')}`, 'aria-pressed': true, onClick: () => { setMetricsOpen(false); safeSetItem('mc-topbar-metrics', '0') } })}>
        <span className="sr-only">{i18nT('app.system_metrics')}</span>
        <AudioWaveform size={12} className="text-accent group-aria-[expanded=false]:!text-muted" />
      </button>)
    } else {
      // Em dashes, not a spinner. The sibling usage segment draws the
      // same distinction for the same reason: a spinner asserts a
      // fetch is about to land, and on a host that never reports
      // metrics (the reporter's `kiro-cli: unavailable`) that claim
      // never comes true. The dashes reuse the loaded branch's own
      // "no valid reading" glyph, so the two open states differ in
      // opacity rather than in shape.
      //
      // Shape is the point, not width: the readings are narrower as
      // dashes and the loaded readout's own width moves anyway (9% to
      // 10% is a reflow). What this removes is the SEGMENT MOUNT — the
      // capsule used to gain a button and a divider when the frame
      // landed, and it now only re-renders text inside a button that
      // was already there. A child mounting inside a
      // `container-type`-contained group is what stranded the header's
      // backdrop (see .topbar-glass in index.css), so the two halves
      // of this fix meet here.
      return (<button key="metrics" className={`${seg} group gap-2 text-[11px] font-mono opacity-60`} {...(popoverTrigger ?? { title: `${i18nT('app.system_metrics')} — ${i18nT('app.click_to_hide')}`, 'aria-pressed': true, onClick: () => { setMetricsOpen(false); safeSetItem('mc-topbar-metrics', '0') } })}>
        {/* Same two-form structure as the loaded readout: the
            container query picks the icon on the narrow rung, and the
            name is sr-only so the icon-only form is still named. The
            glyph's tint follows the loaded readout's too. */}
        <span className="sr-only">{i18nT('app.system_metrics')}</span>
        <AudioWaveform size={12} className="tb-narrow-only text-accent group-aria-[expanded=false]:!text-muted" />
        <span className="tb-drop-metrics flex items-center gap-2 text-muted">
        <span>{i18nT('app.cpu')} —</span>
        <span>{i18nT('app.mem')} —</span>
        <span>{i18nT('app.dsk')} —</span>
        </span>
      </button>)
    }
  } else {
    // Validity is decided on the RAW frame; formatting happens on a
    // sanitized copy. A `memTotal > 0` check says nothing about
    // `memUsed`, and a frame carrying a total with no used is normal
    // (see SysMetricsFrame) — that mismatch is what crashed the root
    // app-shell boundary with `undefined.toFixed(1)`.
    const { cpuValid, memValid, dskValid, m } = readMetricsFrame(sysMetrics)
    const memPct = memValid ? m.memUsed / m.memTotal : 0
    const dskUsed = m.diskTotal - m.diskFree
    const dskPct = dskValid ? dskUsed / m.diskTotal : 0
    // No title tooltip on this button or its readings: the hover card
    // carries the absolute figures, the stale note and the
    // click-to-hide hint, and a native title would pop up on top of
    // it. The readings are visible text here, so they are already in
    // the button's accessible name.
    return (<button key="metrics" className={`${seg} group gap-2 text-[11px] font-mono ${sysMetricsStale ? 'opacity-60' : ''}`} {...(popoverTrigger ?? { ref: metricsBtnRef, ...metricsHover.triggerProps, 'aria-describedby': metricsDescribedBy, 'aria-pressed': true, onClick: () => { metricsHover.close(); setMetricsOpen(false); safeSetItem('mc-topbar-metrics', '0') } })}>
      {/* Both forms are rendered and the container query picks one:
          the rung has to fire on the GROUP's width, which no JS
          branch here can see. Collapsing to the icon (rather than
          hiding the button) keeps the toggle reachable. The label is
          sr-only rather than an aria-label so it NAMES the control in
          both forms without suppressing the readings themselves from
          the accessible name — on the narrow rung every visible text
          node is display:none, which would otherwise leave an
          unnamed icon-only button. */}
      <span className="sr-only">{i18nT('app.system_metrics')}</span>
      {/* Accent-tinted, unlike the off state's muted icon: collapsed,
          the two forms are otherwise the same glyph with the same
          name, so clicking the toggle would produce no perceivable
          change while still writing the preference. `aria-pressed`
          carries the same distinction to assistive tech. Where a click
          pins the card instead, the glyph is this control's only
          visible part, so it is muted until the card is pinned. That
          is keyed off the button's `aria-expanded` in CSS rather than
          by a class chosen here, so the band sees the same contents
          (see popoverTrigger). The muted variant is `!important`: a
          theme's own `.text-accent` override (kiro-dark, index.css)
          matches at the same specificity and later, so a plain variant
          loses to it and the unpinned glyph stays accent. */}
      <AudioWaveform size={12} className="tb-narrow-only text-accent group-aria-[expanded=false]:!text-muted" />
      <span className="tb-drop-metrics flex items-center gap-2">
      <span className={cpuValid ? metricColor(m.cpuPct / 100) : 'text-muted'}>{i18nT('app.cpu')} {cpuValid ? fmtPercent(m.cpuPct / 100) : '—'}</span>
      <span className={memValid ? metricColor(memPct) : 'text-muted'}>{i18nT('app.mem')} {memValid ? fmtPercent(memPct) : '—'}</span>
      <span className={dskValid ? metricColor(dskPct) : 'text-muted'}>{i18nT('app.dsk')} {dskValid ? fmtPercent(dskPct) : '—'}</span>
      </span>
    </button>)
  }
}

/** The read-failure hand-off that follows the desktop capsule. */
export function MetricsErrorNotice({ metrics }: { metrics: MetricsReadout }) {
  const { capsuleCollapsed, metricsOpen, sysMetrics, sysMetricsError } = metrics
  if (capsuleCollapsed || !metricsOpen || sysMetrics || !sysMetricsError) return null
  const message = i18nT('app.metrics_unavailable')
  return (
    <ErrorNotice
      variant="inline"
      askAgent
      message={message}
      messageClassName="tb-metrics-notice-msg"
      className="tb-metrics-notice shrink-0"
      testId="topbar-metrics-error"
    />
  )
}

/** The metrics card, pinned by a click in the narrow band or previewed on hover. */
export function MetricsCard({ metrics }: { metrics: MetricsReadout }) {
  const {
    metricsOpen, metricsInlineFits, metricsPopoverOpen, metricsPopoverRef, metricsHover, metricsCardAnchor, metricsCardOpen,
    metricsCardId, sysMetrics, sysMetricsError, sysMetricsStale, metricsCardRole,
  } = metrics
  if (!metricsCardAnchor) return null
  return createPortal(
    // One card, two ways in: a click pins it as a dialog (the narrow band),
    // and a hover previews it over any desktop form of the control. A clean
    // hover preview is a tooltip; a stale-fetch hand-off is interactive, so
    // that hover form is a non-modal dialog without moving focus into it.
    <div
      ref={metricsPopoverRef}
      id={metricsCardId}
      {...(metricsPopoverOpen ? {} : metricsHover.surfaceProps)}
      role={metricsCardRole}
      // Only the dialog form carries a name. The tooltip is what the
      // trigger's aria-describedby resolves to, and an aria-label there
      // would replace the card's text (the readout rows) with a second
      // copy of the trigger's own name.
      {...(metricsCardRole === 'dialog' ? { 'aria-label': i18nT('app.system_metrics') } : {})}
      // Programmatically focusable so the open effect above can move the
      // caret here; -1 keeps it out of the tab ring, which is right for a
      // transient readout.
      tabIndex={-1}
      className="fixed z-[70] min-w-[176px] rounded-xl bg-card border border-border shadow-xl px-3 py-2.5 flex flex-col gap-1.5"
      style={{ top: metricsCardAnchor.top, right: metricsCardAnchor.right }}
    >
      <div className="text-[11px] font-semibold text-text-strong">{i18nT('app.system_metrics')}</div>
      {(() => {
        // Same derivation as both readouts, from the one helper, so the
        // popover cannot disagree with the inline form about what a partial
        // frame means.
        if (!sysMetrics) return <div className="text-[11px] text-muted">{i18nT('app.metrics_unavailable')}</div>
        const { cpuValid, memValid, dskValid, m } = readMetricsFrame(sysMetrics)
        const dskUsed = m.diskTotal - m.diskFree
        // An invalid reading renders a dash for its percentage; the detail
        // slot then names the reason, so the row never reads as a bare dash.
        const rows = [
          { label: i18nT('app.cpu'), valid: cpuValid, pct: cpuValid ? m.cpuPct / 100 : NaN, detail: cpuValid ? '' : i18nT('app.cpu_unavailable') },
          // used/total carries the unit ONCE, on the total: fmtUnit localizes
          // the digits and the unit and glues them with a non-breaking space,
          // while the used side is a bare localized number so the pair reads as
          // one quantity instead of repeating the unit.
          { label: i18nT('app.mem'), valid: memValid, pct: memValid ? m.memUsed / m.memTotal : NaN, detail: memValid ? `${fmtNumber(m.memUsed, { maximumFractionDigits: 1 })}/${fmtUnit(m.memTotal, 'gigabyte', { maximumFractionDigits: 1 })}` : i18nT('app.memory_unavailable') },
          { label: i18nT('app.dsk'), valid: dskValid, pct: dskValid ? dskUsed / m.diskTotal : NaN, detail: dskValid ? `${fmtNumber(dskUsed, { maximumFractionDigits: 0 })}/${fmtUnit(m.diskTotal, 'gigabyte', { maximumFractionDigits: 0 })}` : i18nT('app.disk_unavailable') },
        ]
        const id = sysMetrics.identity
        // OS family and core count tell machines apart when the hostname
        // alone does not: a Mac commonly reports a bare serial-like name.
        const idDetail = id ? [id.os, id.cores > 0 ? i18nT('app.metrics_card_cores', { count: id.cores }) : ''].filter(Boolean).join(' \u00b7 ') : ''
        return (
          <>
            {rows.map(r => (
              <div key={r.label} className="flex items-baseline justify-between gap-4 text-[11px] font-mono tabular-nums">
                <span className="text-muted">{r.label}</span>
                <span className="flex items-baseline gap-1.5">
                  {r.detail && <span className="text-muted text-[10px]">{r.detail}</span>}
                  <span className={r.valid ? metricColor(r.pct) : 'text-muted'}>{r.valid ? fmtPercent(r.pct) : '\u2014'}</span>
                </span>
              </div>
            ))}
            {/* Which machine these readings belong to. Every instance tab
                renders this same card from its own gateway, so the numbers
                alone cannot say whose they are. The full name is the
                tooltip; the line shows its first label so a long FQDN does
                not widen the card. */}
            {id && (
              <div className="border-t border-border pt-1.5 flex flex-col gap-px">
                {id.host && (
                  <div className="flex items-center gap-1.5 min-w-0 text-[11px] text-text" title={id.fullHost !== id.host ? id.fullHost : undefined}>
                    <Monitor aria-hidden="true" size={11} className="shrink-0 text-muted" />
                    <span className="font-mono truncate max-w-[13rem]">{id.host}</span>
                  </div>
                )}
                {idDetail && <div className="text-[10px] text-muted">{idDetail}</div>}
              </div>
            )}
            {/* The expanded readout's click hides it; the hover card is where
                that affordance is announced now that the button carries no
                title tooltip. */}
            {!metricsPopoverOpen && metricsOpen && metricsInlineFits && <div className="text-[10px] text-muted">{i18nT('app.click_to_hide')}</div>}
            {/* Old data with no failed fetch is not an error, so it gets a
                muted note that explains the dimmed readout. */}
            {sysMetricsStale && !sysMetricsError && <div className="text-[10px] text-muted">{i18nT('app.metrics_card_old_data')}</div>}
          </>
        )
      })()}
      {metricsCardOpen && sysMetricsError && (
        <ErrorNotice
          variant="inline"
          askAgent
          message={i18nT('app.metrics_card_fetch_failed')}
        />
      )}
    </div>,
    document.body
  )
}
