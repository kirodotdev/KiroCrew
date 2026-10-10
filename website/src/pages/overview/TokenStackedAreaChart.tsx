import { useMemo, useRef, useState } from 'react'
import type { KeyboardEvent } from 'react'
import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import { area, curveMonotoneX, scaleLinear, stack } from 'd3'
import type { Series, SeriesPoint } from 'd3'
import { api } from '../../api/client'
import type {
  UsageSeriesDimension,
  UsageSeriesLayer,
  UsageSeriesPayload,
} from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import SimpleSelect from '../../components/SimpleSelect'
import { Btn, Toggle } from '../../components/ui'
import { useSessionPalette } from '../../hooks/useSessionPalette'
import { fmtCompact, fmtCredits, fmtDate, fmtDateFields, fmtNumber } from '../../i18n/format'
import { i18nT } from '../../i18n/t'
import { safeGetItem, safeSetItem } from '../../utils/safeStorage'

export const DIMENSIONS: UsageSeriesDimension[] = ['channel', 'agent', 'model', 'cohort']

// Flat tables indexed inline at the call, the one shape the static key checker
// can verify (see MemoryGraphTab's GROUP_LABEL_KEY).
const DIMENSION_LABEL_KEY: Record<UsageSeriesDimension, string> = {
  channel: 'pages.overview.usageSeriesChart.by_channel',
  agent: 'pages.overview.usageSeriesChart.by_agent',
  model: 'pages.overview.usageSeriesChart.by_model',
  cohort: 'pages.overview.usageSeriesChart.by_cohort',
}

/** Persisted view choices. Versioned so a shape change invalidates. */
const PREFS_KEY = 'kc.usageSeries.v1'
type Prefs = { by: UsageSeriesDimension; cumulative: boolean }
const DEFAULT_PREFS: Prefs = { by: 'channel', cumulative: true }

function readPrefs(): Prefs {
  try {
    const raw = safeGetItem(PREFS_KEY)
    if (!raw) return DEFAULT_PREFS
    const parsed = JSON.parse(raw) as Partial<Prefs>
    return {
      by: DIMENSIONS.includes(parsed.by as UsageSeriesDimension) ? (parsed.by as UsageSeriesDimension) : DEFAULT_PREFS.by,
      cumulative: typeof parsed.cumulative === 'boolean' ? parsed.cumulative : DEFAULT_PREFS.cumulative,
    }
  } catch {
    return DEFAULT_PREFS
  }
}

/** Running total per layer, so a layer that stopped spending stays level
 *  rather than vanishing: credits are never un-spent, which is what makes the
 *  cumulative stack the faithful reading of "what today's total is made of". */
export function toCumulative(layers: UsageSeriesLayer[]): UsageSeriesLayer[] {
  return layers.map(layer => {
    let running = 0
    return { ...layer, values: layer.values.map(v => (running += v)) }
  })
}

/** The day index under a pointer at `fraction` (0..1) of the chart width. */
export function nearestIndex(fraction: number, count: number): number {
  if (count <= 1) return 0
  const clamped = Math.min(1, Math.max(0, fraction))
  return Math.round(clamped * (count - 1))
}

/** `YYYY-MM-DD` as a LOCAL calendar day. `new Date('2026-09-14')` would read
 *  the string as UTC midnight and render the previous evening west of it. */
function localDay(iso: string): Date {
  const [y, m, d] = iso.split('-').map(Number)
  return new Date(y, m - 1, d)
}

/** `YYYY-MM-DD` of a local calendar day, the form the server keys weeks by. */
function isoDay(day: Date): string {
  return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`
}

/** The Monday of the ISO week holding `iso`, as the server keys a cohort. */
export function weekMondayOf(iso: string): string {
  const day = localDay(iso)
  day.setDate(day.getDate() - ((day.getDay() + 6) % 7))
  return isoDay(day)
}

/** The last day a session first seen in the cohort week keyed `monday` could
 *  have started by: that week's Sunday, or the axis's last day when the week
 *  is still running. */
export function startedByDay(monday: string, lastAxisDay: string): string {
  const sunday = localDay(monday)
  sunday.setDate(sunday.getDate() + 6)
  const bound = isoDay(sunday)
  return bound < lastAxisDay ? bound : lastAxisDay
}

type Row = Record<string, number>

/** Normalised chart space: paths are drawn in a 0..1000 box the SVG stretches. */
const BOX = 1000

/** The axis top for a stack whose highest point is `max`: the next round number
 *  up, so the labels read `2K · 1K · 0` rather than `1.6K · 801.1 · 0`. */
export function niceTop(max: number): number {
  const [, top] = scaleLinear().domain([0, Math.max(1e-9, max)]).nice(4).domain()
  return top
}

export function stackPaths(layers: UsageSeriesLayer[], days: number): { paths: string[]; top: number } {
  const rows: Row[] = Array.from({ length: days }, (_, i) =>
    Object.fromEntries(layers.map(layer => [layer.key, layer.values[i] ?? 0])),
  )
  const stacked: Series<Row, string>[] = stack<Row>().keys(layers.map(layer => layer.key))(rows)
  const top = niceTop(Math.max(...stacked.flatMap(series => series.map(point => point[1]))))
  const x = (i: number) => (days <= 1 ? BOX / 2 : (i / (days - 1)) * BOX)
  const y = (v: number) => BOX - (v / top) * BOX
  const shape = area<SeriesPoint<Row>>()
    .x((_, i) => x(i))
    .y0(point => y(point[0]))
    .y1(point => y(point[1]))
    .curve(curveMonotoneX)
  return { paths: stacked.map(series => shape(series) ?? ''), top }
}

/** Channel ids are the closed lowercase vocabulary `telemetry_channel_of`
 *  returns (`dashboard`, `cron`, `background`, `telegram` …), shown capitalised
 *  so the legend reads as places spend went rather than as code. `other` is the
 *  classifier's "key shape not recognised" and gets its own label, since shown
 *  as-is it would be mistaken for the fold layer's "Other (n more)". */
export function channelLabel(id: string): string {
  if (id === 'other') return i18nT('pages.overview.usageSeriesChart.channel_unrecognized')
  const words = id.replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** A series answer plus the dimension it was requested for. */
type SeriesView = UsageSeriesPayload & { by: UsageSeriesDimension }

/** `oldest` is the key of the oldest cohort layer drawn, `lastDay` the axis's
 *  last day. A cohort is the week a session was first seen INSIDE the window,
 *  so the oldest layer also holds sessions that began before the window; it is
 *  named for the last day they could have started by, not as a week of starts. */
function layerLabel(layer: UsageSeriesLayer, by: UsageSeriesDimension, oldest: string | undefined, lastDay: string): string {
  if (layer.kind === 'other') return i18nT('pages.overview.usageSeriesChart.other', { n: fmtNumber(layer.members ?? 0) })
  if (layer.kind === 'unattributed') return i18nT('pages.overview.usageSeriesChart.unattributed')
  if (by === 'cohort' && layer.key === oldest) return i18nT('pages.overview.usageSeriesChart.cohort_started_by', { date: fmtDateFields(localDay(startedByDay(layer.key, lastDay)), { month: 'short', day: 'numeric' }) })
  if (by === 'cohort') return i18nT('pages.overview.usageSeriesChart.cohort_week', { date: fmtDateFields(localDay(layer.key), { month: 'short', day: 'numeric' }) })
  if (by === 'channel') return channelLabel(layer.key)
  return layer.key
}

/**
 * Stacked area chart of spend over the shard retention window: one layer per
 * bucket of the chosen dimension, cumulative by default so the stack's top
 * edge is the window's running total and each band's height is that bucket's
 * share of it. The per-day view is the same stack without the prefix sum.
 * Layers come from the backend already in stack order and already folded to
 * top-N + other + unattributed; the browser only sums, stacks and draws.
 */
export function TokenStackedAreaChart() {
  const [prefs, setPrefs] = useState<Prefs>(readPrefs)
  const [hover, setHover] = useState<number | null>(null)
  const plotRef = useRef<HTMLDivElement>(null)
  const reducedMotion = useReducedMotion()
  const { paletteColors } = useSessionPalette()

  const update = (patch: Partial<Prefs>) => {
    setPrefs(prev => {
      const next = { ...prev, ...patch }
      safeSetItem(PREFS_KEY, JSON.stringify(next))
      return next
    })
  }

  // Switching Layer by keeps the previous dimension's stack on screen, dimmed,
  // until the new one answers; without `keepPreviousData` every first switch
  // collapses the card to the skeleton. The answer carries the dimension it was
  // split by, so a stand-in stack keeps its own labels and caption.
  const { data, error: queryErr, isPlaceholderData, isFetching, refetch } = useQuery<SeriesView>({
    queryKey: ['usage-series', prefs.by],
    queryFn: async () => ({ ...(await api.usageSeries(prefs.by)), by: prefs.by }),
    staleTime: 60_000,
    placeholderData: keepPreviousData,
  })
  const shownBy = data?.by ?? prefs.by
  const err = queryErr ? (queryErr instanceof Error ? queryErr.message : String(queryErr)) : ''

  const layers = useMemo(() => {
    if (!data) return []
    return prefs.cumulative ? toCumulative(data.series) : data.series
  }, [data, prefs.cumulative])
  const days = data?.dates.length ?? 0
  const { paths, top } = useMemo(() => stackPaths(layers, days), [layers, days])

  const colorOf = (layer: UsageSeriesLayer, index: number): string => {
    if (layer.kind === 'other') return 'var(--muted)'
    if (layer.kind === 'unattributed') return 'var(--muted-strong)'
    return paletteColors[index % paletteColors.length] || 'var(--accent)'
  }

  const pointTo = (clientX: number) => {
    const el = plotRef.current
    if (!el || days === 0) return
    const rect = el.getBoundingClientRect()
    if (rect.width <= 0) return
    setHover(nearestIndex((clientX - rect.left) / rect.width, days))
  }

  // The keyboard path to per-day values: the plot takes focus, arrows step the
  // day, Home/End jump, and the slider's value text reads each day out.
  const stepTo = (e: KeyboardEvent) => {
    if (days === 0) return
    const current = hover ?? days - 1
    const next =
      e.key === 'ArrowLeft' ? Math.max(0, current - 1)
      : e.key === 'ArrowRight' ? Math.min(days - 1, current + 1)
      : e.key === 'Home' ? 0
      : e.key === 'End' ? days - 1
      : e.key === 'Escape' ? null
      : undefined
    if (next === undefined) return
    e.preventDefault()
    setHover(next)
  }

  const hasUnattributed = layers.some(layer => layer.kind === 'unattributed')
  const controls = (
    <>
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2 mb-3 text-[12px] text-muted">
      <div className="flex items-center gap-2">
        <span>{i18nT('pages.overview.usageSeriesChart.layer_by')}</span>
        <SimpleSelect
          aria-label={i18nT('pages.overview.usageSeriesChart.layer_by')}
          options={DIMENSIONS}
          optionLabels={DIMENSIONS.map(d => i18nT(DIMENSION_LABEL_KEY[d]))}
          value={prefs.by}
          onChange={v => update({ by: v as UsageSeriesDimension })}
        />
      </div>
      <div className="flex items-center gap-2">
        <span>{i18nT('pages.overview.usageSeriesChart.cumulative')}</span>
        <Toggle
          checked={prefs.cumulative}
          onChange={v => update({ cumulative: v })}
          label={i18nT('pages.overview.usageSeriesChart.cumulative')}
        />
      </div>
      {/* The dimmed stand-in stack below still carries the previous dimension's
          legend, so the controls say in words that the new one is on its way.
          Always mounted as a live region: a status that appears from nothing is
          not reliably announced, one whose text changes is. */}
      <span role="status" className={isPlaceholderData ? undefined : 'sr-only'} data-testid="usage-series-loading">
        {isPlaceholderData ? i18nT('pages.overview.usageSeriesChart.loading') : ''}
      </span>
      </div>
      {shownBy === 'cohort' && (
        <p className="text-[12px] text-muted -mt-1 mb-3" data-testid="usage-series-cohort-caption">
          {i18nT('pages.overview.usageSeriesChart.cohort_caption')}
        </p>
      )}
      {hasUnattributed && (
        <p className="text-[12px] text-muted -mt-1 mb-3" data-testid="usage-series-unattributed-caption">
          {i18nT('pages.overview.usageSeriesChart.unattributed_hint')}
        </p>
      )}
      {data?.truncated && (
        <p className="text-[12px] text-warn -mt-1 mb-3" data-testid="usage-series-truncated">
          {data.complete_from
            ? i18nT('pages.overview.usageSeriesChart.truncated_from', {
              n: fmtNumber(data.dropped_rows),
              date: fmtDate(localDay(data.complete_from)),
            })
            : i18nT('pages.overview.usageSeriesChart.truncated_notice', { n: fmtNumber(data.dropped_rows) })}
        </p>
      )}
    </>
  )

  // With nothing drawn there are no "last values" to keep showing, so a failed
  // refresh over an empty window reads like a failed first load.
  const nothingDrawn = !data || data.total <= 0 || days === 0
  if (err && nothingDrawn) {
    return (
      <div>
        {controls}
        <ErrorNotice title={i18nT('pages.overview.usageSeriesChart.load_failed')} message={err} messagePlacement="below" askAgent />
        <Btn className="mt-3 min-h-11" disabled={isFetching} onClick={() => void refetch()}>
          {i18nT('pages.overview.usageSeriesChart.retry')}
        </Btn>
      </div>
    )
  }
  if (!data) return <div>{controls}<div className="skeleton h-44 rounded" /></div>
  if (nothingDrawn) {
    return (
      <div>
        {controls}
        <div className="text-[13px] text-muted py-6 text-center" data-testid="usage-series-empty">
          {i18nT('pages.overview.usageSeriesChart.empty')}
        </div>
      </div>
    )
  }

  const hoverX = hover == null ? null : days <= 1 ? 50 : (hover / (days - 1)) * 100
  // Bottom of the stack first, matching the legend: the largest bucket (or the
  // oldest cohort) leads, and the reserved layers close the list.
  const rowsAt = (day: number) =>
    layers.map((layer, i) => ({ layer, i, value: layer.values[day] ?? 0 })).filter(r => r.value > 0)
  const hoverRows = hover == null ? [] : rowsAt(hover)
  const hoverTotal = hoverRows.reduce((sum, r) => sum + r.value, 0)
  const totalLine = (total: number) =>
    i18nT(prefs.cumulative ? 'pages.overview.usageSeriesChart.total_to_date' : 'pages.overview.usageSeriesChart.total_that_day', { value: fmtCredits(total) })
  // What a screen reader hears for the focused day: the slider's value text,
  // the same facts the pointer tooltip shows.
  const focusedDay = hover ?? days - 1
  const focusedRows = rowsAt(focusedDay)
  // Cohorts arrive oldest-first and a 30-day window holds at most six of them
  // against the seven kept, so the oldest cohort drawn is never in the fold.
  const oldestCohort = shownBy === 'cohort' ? layers.find(layer => layer.kind === 'bucket')?.key : undefined
  const lastDay = data.dates[data.dates.length - 1]
  const valueText = [
    fmtDate(localDay(data.dates[focusedDay])),
    totalLine(focusedRows.reduce((sum, r) => sum + r.value, 0)),
    ...focusedRows.map(({ layer, value }) => `${layerLabel(layer, shownBy, oldestCohort, lastDay)} ${fmtCredits(value)}`),
  ].join('. ')
  const transition = reducedMotion ? { duration: 0 } : { duration: 0.35, ease: 'easeOut' as const }

  return (
    <div data-testid="usage-series-chart">
      {err && <ErrorNotice title={i18nT('pages.sessionsTab.could_not_refresh')} message={err} messagePlacement="below" askAgent className="mb-3" />}
      {controls}
      <div
        data-testid="usage-series-body"
        className={isPlaceholderData ? 'opacity-60 transition-opacity' : undefined}
        aria-busy={isPlaceholderData || undefined}
      >
        <div className="flex gap-2">
          {/* Y axis: HTML labels so the stretched SVG never distorts text. */}
          <div className="relative w-10 shrink-0 h-44 text-[10px] text-muted tabular-nums text-right">
            <span className="absolute right-0 top-0 leading-none">{fmtCompact(top)}</span>
            <span className="absolute right-0 top-1/2 -translate-y-1/2 leading-none">{fmtCompact(top / 2)}</span>
            <span className="absolute right-0 bottom-0 leading-none">{fmtNumber(0)}</span>
          </div>
          <div
            ref={plotRef}
            data-testid="usage-series-plot"
            // `touch-pan-y`: a vertical swipe that starts on the plot still scrolls
            // the page; only horizontal drags are kept for scrubbing.
            className="relative flex-1 h-44 touch-pan-y rounded outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] focus-visible:ring-offset-1 focus-visible:ring-offset-[var(--bg)]"
            // A slider over the day axis: arrows move the highlighted day and the
            // value text reads that day out, so the tooltip needs no live region.
            role="slider"
            tabIndex={0}
            aria-label={`${i18nT('pages.overview.usageSeriesChart.aria_label')}. ${i18nT('pages.overview.usageSeriesChart.keyboard_hint')}`}
            aria-orientation="horizontal"
            aria-valuemin={0}
            aria-valuemax={days - 1}
            aria-valuenow={focusedDay}
            aria-valuetext={valueText}
            onPointerMove={e => pointTo(e.clientX)}
            onPointerDown={e => pointTo(e.clientX)}
            onPointerLeave={() => setHover(null)}
            onKeyDown={stepTo}
            onFocus={() => setHover(h => h ?? days - 1)}
            onBlur={() => setHover(null)}
          >
            <svg
              className="absolute inset-0 w-full h-full overflow-visible"
              viewBox={`0 0 ${BOX} ${BOX}`}
              preserveAspectRatio="none"
              role="img"
              aria-label={i18nT('pages.overview.usageSeriesChart.aria_label')}
            >
              {[0.25, 0.5, 0.75].map(f => (
                <line key={f} x1={0} x2={BOX} y1={BOX * f} y2={BOX * f} stroke="var(--border)" strokeDasharray="4 6" vectorEffect="non-scaling-stroke" />
              ))}
              <AnimatePresence initial={false}>
                {layers.map((layer, i) => (
                  <motion.path
                    key={layer.key}
                    data-layer={layer.key}
                    // A layer that mounts mid-session (the dimension changed) must start at its
                    // real path: with no initial `d`, Framer animates from the absent attribute and
                    // writes d="undefined" for a frame, which the browser logs as an invalid path.
                    initial={{ opacity: 0, d: paths[i] }}
                    animate={{ opacity: 0.9, d: paths[i] }}
                    exit={{ opacity: 0 }}
                    transition={transition}
                    fill={colorOf(layer, i)}
                    stroke="var(--card)"
                    strokeWidth={1}
                    vectorEffect="non-scaling-stroke"
                  />
                ))}
              </AnimatePresence>
              {hoverX != null && (
                <line x1={(hoverX / 100) * BOX} x2={(hoverX / 100) * BOX} y1={0} y2={BOX} stroke="var(--text)" strokeOpacity={0.5} vectorEffect="non-scaling-stroke" />
              )}
            </svg>
            {/* Tooltip sits INSIDE the plot (same choice as TokenDailyChart): hung
                above it, it would overflow the card for the top of the window. */}
            {hover != null && hoverX != null && (
              <div
                aria-hidden="true"
                data-testid="usage-series-tooltip"
                className="absolute top-1 -translate-x-1/2 bg-bg-elevated border border-border rounded px-2 py-1 text-[11px] whitespace-nowrap z-50 shadow-lg pointer-events-none"
                style={{ left: `clamp(5rem, ${hoverX}%, calc(100% - 5rem))` }}
              >
                <div className="font-medium">{fmtDate(localDay(data.dates[hover]))}</div>
                <div className="text-muted">{totalLine(hoverTotal)}</div>
                {hoverRows.map(({ layer, i, value }) => (
                  <div key={layer.key} className="flex items-center gap-1.5">
                    <span className="w-2 h-2 rounded-sm inline-block shrink-0" style={{ background: colorOf(layer, i) }} />
                    <span className="truncate max-w-56">{layerLabel(layer, shownBy, oldestCohort, lastDay)}</span>
                    <span className="ml-auto pl-3 tabular-nums">{fmtCredits(value)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
        <div className="flex justify-between pl-12 mt-1 text-[10px] text-muted">
          {[...new Set([0, Math.floor((days - 1) / 2), days - 1])].map(i => (
            <span key={i}>{fmtDateFields(localDay(data.dates[i]), { month: 'short', day: 'numeric' })}</span>
          ))}
        </div>
        <div data-testid="usage-series-legend" className="flex gap-x-4 gap-y-1 mt-3 text-[12px] text-muted justify-center flex-wrap">
          {layers.map((layer, i) => (
            <span key={layer.key} className="flex items-center gap-1.5">
              <span className="w-3 h-3 rounded-sm inline-block shrink-0" style={{ background: colorOf(layer, i) }} />
              <span className="truncate max-w-64">{layerLabel(layer, shownBy, oldestCohort, lastDay)}</span>
              <span className="tabular-nums">{fmtCredits(layer.total)}</span>
            </span>
          ))}
        </div>
      </div>
    </div>
  )
}
