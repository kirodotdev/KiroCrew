import React, { useContext } from 'react'

import { keepPreviousData, useQuery } from '@tanstack/react-query'
import { Activity, Coins, Gauge, Rocket } from 'lucide-react'
import { Trans } from 'react-i18next'

import { api } from '../api/client'
import ErrorNotice from '../components/ErrorNotice'
import InfoTip from '../components/InfoTip'
import SegmentedControl from '../components/SegmentedControl'
import { SettingRef } from '../components/settingRef/SettingRef'
import { Card, CardTitle, EmptyState, Input } from '../components/ui'
import { usePersistedString } from '../hooks/usePersistedString'
import { fmtBytes, fmtNumber, fmtPercent } from '../i18n/format'
import { i18nT } from '../i18n/t'
import {
  DEFAULT_RANGE,
  MAX_RANGE_DAYS,
  RANGE_CHOICES,
  type RangeChoice,
  customDays,
  earliestDay,
  localDay,
  rangeQuery,
  rangeSearch,
} from './telemetryRange'
import {
  type Stat,
  type Startup,
  type Turn,
  type Other,
  type Resp,
  PanelWindowCtx,
  panelWindow,
  otelWindow,
  spendWindow,
  usePersistedChoice,
} from './telemetryPanelModel'
import { type Col, DataTable, Sums, Histogram } from './TelemetryPanelBlocks'
import { SpendTab, ContextTab } from './TelemetryPanelConversations'

/**
 * Outcomes that are NOT faults, mirroring the API's `_TERMINAL_FAULT_OUTCOMES`
 * complement. Kept as one named set so the fault count below and any future
 * reader cannot drift apart, and so adding an outcome is one edit rather than a
 * search for `k === 'ok'` comparisons.
 */
const TURN_NON_FAULT_OUTCOMES = new Set([
  'ok',
  // Recovered-in-place watchdog stalls, tracked separately under
  // kirocrew.watchdog.recovery.outcome.
  'tool_stall',
  'stale_recover',
  // The operator pressed Stop. Counting it would report a deliberate user
  // action as the system failing.
  'cancelled',
  // No stop reason was available for this turn; excluded from both sides of
  // fault_rate by the API (see the comment in HealthBar).
  'unclassified',
])

const fmtMs = (ms?: number | null): string =>
  ms == null ? '—' : ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms'

function Notice({ children }: { children: React.ReactNode }) {
  return <div className="text-muted text-sm py-12 text-center leading-relaxed">{children}</div>
}

// "these numbers cover only part of the window" caveat.
//
// Rendered next to every histogram-derived figure because the dropped
// generation is otherwise invisible: the API reports ONE generation's count and
// percentiles (merging incompatible boundaries would fabricate values), so a
// window that straddles a boundary change shows a subset styled exactly like a
// full-window total.
//
// It states the SHOWN/TOTAL pair rather than a generation count. A generation
// count is an internal unit a reader cannot convert into missing data, which is
// exactly the gap that made `n=1134` unreconcilable against a `2837 hit`
// counter beside it.
function GenNote({ shown, total, compact }: { shown?: number; total?: number; compact?: boolean }) {
  if (shown == null || total == null || total <= shown) return null
  const text = i18nT('pages.telemetryPanel.showing_partial_window', { shown, total })
  // A fixed-width cell cannot hold the sentence: rendered inline it wraps to a
  // nine-line sliver and pushes the row to ten times its height. The marker keeps
  // the caveat visible and discoverable while the sentence moves to the tooltip.
  if (compact) {
    return (
      <span
        // Non-actionable metadata, so it does not spend the alarm colour.
        className="cursor-help text-[11px] leading-none text-muted"
        title={text}
        aria-label={text}
      >
        *
      </span>
    )
  }
  return (
    <div className="text-[10px] mt-1" style={{ color: 'var(--warn)' }}>
      {text}
    </div>
  )
}

/**
 * A latency profile: where p50 and p90 sit between this instrument's own min and
 * max.
 *
 * LOGARITHMIC, because the measured data makes a linear axis useless. The gateway
 * request histogram runs min 0ms, p50 2ms, p90 228ms, max 213.7s — a max a
 * thousand times p90 — so on a linear scale both percentiles land inside the
 * leftmost 0.1% of the bar and every row renders as one indistinguishable sliver.
 * A log axis is how a latency distribution is read for exactly this reason.
 *
 * Normalised PER ROW as well: these instruments span four orders of magnitude
 * between them, so one shared axis would flatten the fast ones against the
 * slowest. Each bar therefore answers "how long is THIS one's tail"; the numbers
 * beside it stay comparable across rows.
 *
 * A zero minimum has no logarithm, so the axis starts at a floor below the
 * smallest reading worth plotting rather than at zero. That is a floor on the
 * AXIS, not a claim about the data — the real min is printed next to the bar.
 * The floor arrives with the row because it belongs to the row's own scale: 0.5
 * sits below any millisecond worth plotting and ABOVE most of a CPU share, where
 * it would clamp every point onto the left edge and flatten the bar.
 */
const _LOG_FLOOR_MS = 0.5

function RangeBar({
  min,
  p50,
  p90,
  max,
  fmt = fmtMs,
  floor = _LOG_FLOOR_MS,
}: {
  min: number
  p50: number
  p90: number
  max: number
  // The row's own formatter and axis floor. Defaulted to the millisecond family
  // because that is what every duration caller means, and a byte or ratio row
  // reading out as milliseconds in its accessible label is the same unit lie the
  // visible cells avoid -- a screen reader would be the only surface still told
  // that a resident set is a duration.
  fmt?: (v: number) => string
  floor?: number
}) {
  const lo = Math.max(min, floor)
  const hi = Math.max(max, lo * 1.001)
  const span = Math.log10(hi) - Math.log10(lo)
  const at = (v: number) =>
    span <= 0 ? 0 : Math.min(100, Math.max(0, ((Math.log10(Math.max(v, lo)) - Math.log10(lo)) / span) * 100))
  const left = at(p50)
  const right = at(p90)
  return (
    <div
      className="relative h-1.5 w-full rounded-full bg-[var(--bg)]"
      role="img"
      aria-label={i18nT('pages.telemetryPanel.range_bar_label', {
        min: fmt(min),
        p50: fmt(p50),
        p90: fmt(p90),
        max: fmt(max),
      })}
    >
      {/* p50→p90: where the bulk of the samples land. */}
      <span
        className="absolute top-0 h-full rounded-full"
        style={{
          left: `${left}%`,
          width: `${Math.max(right - left, 1.5)}%`,
          background: 'var(--muted-strong)',
        }}
      />
      {/* p50 itself, so the bar reads as a position and not only a width. */}
      <span
        className="absolute top-[-2px] h-[10px] w-[2px] rounded-sm"
        style={{ left: `${left}%`, background: 'var(--accent)' }}
      />
    </div>
  )
}

/**
 * A daily trend as columns. `startup.daily` has shipped in the payload since the
 * startup block existed and was rendered nowhere, so the one time series this
 * page has was invisible. Columns rather than a line: a line needs a path, and a
 * path needs SVG.
 *
 * A day with no cold start reports 0, which is an absence rather than a fast
 * start — those columns are drawn empty instead of as a floor value.
 */
function DailyTrend({ rows }: { rows: { date: string; count: number; cold_p50_ms: number; warm_p50_ms: number }[] }) {
  const peak = Math.max(1, ...rows.flatMap(r => [r.cold_p50_ms, r.warm_p50_ms]))
  return (
    <div className="flex items-end gap-[3px]" style={{ height: '52px' }}>
      {rows.map(r => {
        const cold = r.cold_p50_ms > 0 ? (r.cold_p50_ms / peak) * 100 : 0
        const warm = r.warm_p50_ms > 0 ? (r.warm_p50_ms / peak) * 100 : 0
        return (
          <div
            key={r.date}
            className="flex h-full min-w-0 flex-1 flex-col justify-end gap-[1px]"
            // The numbers exist nowhere else, so the column has to carry them for
            // anyone not using a pointer.
            role="img"
            aria-label={i18nT('pages.telemetryPanel.daily_trend_point', {
              date: r.date,
              cold: fmtMs(r.cold_p50_ms),
              warm: fmtMs(r.warm_p50_ms),
              count: fmtNumber(r.count),
            })}
            title={i18nT('pages.telemetryPanel.daily_trend_point', {
              date: r.date,
              cold: fmtMs(r.cold_p50_ms),
              warm: fmtMs(r.warm_p50_ms),
              count: fmtNumber(r.count),
            })}
          >
            <span className="block w-full rounded-t-sm" style={{ height: `${cold}%`, background: 'var(--accent)' }} />
            <span className="block w-full rounded-t-sm" style={{ height: `${warm}%`, background: 'var(--muted-strong)' }} />
          </div>
        )
      })}
    </div>
  )
}

// ── Latency ────────────────────────────────────────────────────

/**
 * Rows are instruments, not conversations: an OTEL histogram carries no slot
 * attribute (an unbounded session id is not a metric label), so per-turn
 * latency genuinely cannot be attributed to a conversation from this store.
 * Saying so with a different first column is more honest than an empty
 * per-conversation latency column would have been.
 */

function LatencyTab({ other }: { other: Other[] }) {
  const { period } = useContext(PanelWindowCtx)
  // Split on the KIND the API states, not on whether `p50_ms` is present. A
  // histogram in a unit other than milliseconds carries `p50`/`max` instead, so
  // the presence test sent it to the counter list, which renders its count and
  // throws the distribution away. Reading the kind means a future non-ms
  // instrument lands here without anyone remembering to add a field test.
  const isHist = (o: Other) => o.kind === 'histogram'
  // Histograms first and by volume: a counter has no percentiles to shape, so it
  // cannot carry a profile bar and belongs after the things that can.
  const hist = other.filter(isHist).sort((a, b) => (b.count ?? 0) - (a.count ?? 0))
  // Gauges are point-in-time readings (thread count, RSS): their number is
  // `latest`, and folding them under Counters would render the one field a
  // gauge never carries.
  // A raw 4,402,341,888 forces the reader to count digits to learn it is
  // ~4.4 GB; byte-unit gauges get human units (exact value stays in `title`).
  const fmtGaugeValue = (name: string, v: number): string =>
    name.endsWith('_bytes') ? fmtBytes(v) : fmtNumber(v)
  const gauges = other.filter(o => !isHist(o) && o.kind === 'gauge')
  // The residual, so a kind this panel does not know still appears somewhere
  // rather than vanishing from the page.
  const counters = other.filter(o => !isHist(o) && o.kind !== 'gauge')

  // One row shape for both families. The unit picks the formatter and the axis
  // floor, because the number alone cannot say whether 0.0625 is a ratio or 62
  // milliseconds: a resident set reads in GiB and a CPU share as a percentage,
  // and a row with no unit is the millisecond family this table was built for.
  const scaleFor = (unit?: string): { fmt: (v: number) => string; floor: number } => {
    if (unit === 'By') return { fmt: fmtBytes, floor: 1 }
    // A share of one machine's cores, shown as a percentage: 0.0625 reads as a
    // rounding artefact where 6.3% reads as a share. The floor is a tenth of a
    // percent of one core, under the smallest share worth plotting.
    if (unit === '1') return { fmt: v => fmtPercent(v, { maximumFractionDigits: 1 }), floor: 0.001 }
    return { fmt: fmtMs, floor: _LOG_FLOOR_MS }
  }
  const histRow = (o: Other) => {
    // The UNIT decides the family, not whether a percentile arrived. A non-ms
    // histogram with no samples in the window reports only count and unit, so a
    // presence test on `p50` routes that row to fmtMs and prints a byte count as
    // "0ms" -- the unit lie this split exists to prevent.
    const neutral = o.p50_ms == null && o.unit != null
    return {
      min: (neutral ? o.min : o.min_ms) ?? 0,
      p50: (neutral ? o.p50 : o.p50_ms) ?? 0,
      p90: (neutral ? o.p90 : o.p90_ms) ?? 0,
      max: (neutral ? o.max : o.max_ms) ?? 0,
      ...scaleFor(neutral ? o.unit : undefined),
    }
  }

  return (
    <Card className="mb-4">
      <CardTitle>
        {i18nT('pages.telemetryPanel.instruments')}
        <InfoTip text={i18nT('pages.telemetryPanel.otel_has_no_per_conversation_split')} />
        <span className="ml-auto text-[12px] text-muted font-mono tabular-nums font-normal">{period}</span>
      </CardTitle>

      {hist.length === 0 && counters.length === 0 && gauges.length === 0 ? (
        <EmptyState
          icon={<Activity className="lucide-inline" />}
          title={i18nT('pages.telemetryPanel.no_instruments_recorded')}
        />
      ) : (
        <>
          <div className="text-[10px] text-muted mb-2">
            {i18nT('pages.telemetryPanel.profile_scaled_per_row')}
          </div>
          <div className="overflow-x-auto">
          <div className="mb-1 flex items-center gap-3 text-[10px] text-muted">
            <span className="w-[260px] shrink-0" />
            <span className="w-14 shrink-0 text-right">{i18nT('pages.telemetryPanel.min_col')}</span>
            <span className="min-w-0 flex-1" />
            <span className="w-14 shrink-0">{i18nT('pages.telemetryPanel.max_col')}</span>
            <span className="w-16 shrink-0 text-right">{i18nT('pages.telemetryPanel.p50_col')}</span>
            <span className="w-16 shrink-0 text-right">{i18nT('pages.telemetryPanel.p90_col')}</span>
            <span className="w-16 shrink-0 text-right">{i18nT('pages.telemetryPanel.samples_col')}</span>
          </div>
          <div className="flex min-w-max flex-col gap-2">
            {hist.map(o => {
              const r = histRow(o)
              return (
              <div key={o.name} className="flex items-center gap-3 text-[11.5px]">
                <span className="w-[260px] shrink-0 truncate font-mono text-[11px]" title={o.name}>
                  {o.name}
                </span>
                {/* The endpoints flank the bar so each row is visibly its own
                    scale, rather than a position on a shared axis it never had. */}
                <span className="w-14 shrink-0 text-right font-mono tabular-nums text-muted">
                  {r.fmt(r.min)}
                </span>
                <div className="min-w-0 flex-1">
                  <RangeBar min={r.min} p50={r.p50} p90={r.p90} max={r.max} fmt={r.fmt} floor={r.floor} />
                </div>
                <span className="w-14 shrink-0 font-mono tabular-nums text-muted">
                  {r.fmt(r.max)}
                </span>
                <span className="w-16 shrink-0 text-right font-mono tabular-nums">{r.fmt(r.p50)}</span>
                <span className="w-16 shrink-0 text-right font-mono tabular-nums">{r.fmt(r.p90)}</span>
                <span className="flex w-16 shrink-0 items-center justify-end gap-1 text-right font-mono tabular-nums text-muted">
                  {fmtNumber(o.count ?? 0)}
                  {/* The window can span a boundary change, and stats() then describe
                      only the newest generation — say so rather than labelling a
                      subset as the total. */}
                  <GenNote shown={o.count} total={o.total_count} compact />
                </span>
              </div>
              )
            })}
          </div>
          </div>
          {/* The marker says WHICH rows are partial; this says what the marker
              means, in text, so it needs neither a hover nor a tab stop. */}
          {hist.some(o => o.total_count != null && o.count != null && o.total_count > o.count) && (
            <div className="mt-2 text-[10px] text-muted">
              {i18nT('pages.telemetryPanel.partial_window_legend')}
            </div>
          )}

          {counters.length > 0 && (
            <div className="mt-4 border-t border-border pt-3">
              <div className="text-[10px] text-muted uppercase tracking-wide mb-1.5">
                {i18nT('pages.telemetryPanel.counters')}
              </div>
              <div className="flex flex-col gap-1">
                {counters.map(o => (
                  <div key={o.name} className="flex items-center gap-3 text-[11.5px]">
                    <span className="min-w-0 flex-1 truncate font-mono text-[11px]" title={o.name}>
                      {o.name}
                    </span>
                    <span className="shrink-0 font-mono tabular-nums">
                      {fmtNumber(o.count ?? o.total ?? 0)}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
          {gauges.length > 0 && (
            <div className="mt-4 border-t border-border pt-3">
              <div className="flex items-baseline justify-between mb-1.5">
                <div className="text-[10px] text-muted uppercase tracking-wide">
                  {i18nT('pages.telemetryPanel.gauges')}
                </div>
                {/* Gauges are instantaneous samples; without this cue they read
                    as 14-day figures under the card's window label. */}
                <div className="text-[10px] text-muted">
                  {i18nT('pages.telemetryPanel.gauges_latest')}
                </div>
              </div>
              <div className="flex flex-col gap-1">
                {gauges.map(o => (
                  <div key={o.name}>
                    <div className="flex items-center gap-3 text-[11.5px]">
                      <span className="min-w-0 flex-1 truncate font-mono text-[11px]" title={o.name}>
                        {o.name}
                      </span>
                      <span
                        className="shrink-0 font-mono tabular-nums"
                        title={String(o.latest ?? 0)}
                      >
                        {fmtGaugeValue(o.name, o.latest ?? 0)}
                      </span>
                    </div>
                    {/* Multi-process shards: the API keys samples per PID so no
                        process masquerades as another — surface that breakdown
                        instead of only the newest process's headline. */}
                    {o.by_attr && Object.keys(o.by_attr).length > 1 && (
                      <div className="ml-4 flex flex-col gap-0.5">
                        {Object.entries(o.by_attr).map(([sig, v]) => (
                          <div key={sig} className="flex items-center gap-3 text-[10.5px] text-muted">
                            <span className="min-w-0 flex-1 truncate font-mono text-[10px]" title={sig}>
                              {sig}
                            </span>
                            <span className="shrink-0 font-mono tabular-nums" title={String(v)}>
                              {fmtGaugeValue(o.name, v)}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}
    </Card>
  )
}

// ── Startup ────────────────────────────────────────────────────

type StartupGroup = 'phase' | 'channel' | 'distribution'

const STARTUP_GROUPS = ['phase', 'channel', 'distribution'] as const

function statCols(first: string): Col<Stat & { name: string }>[] {
  return [
    {
      key: 'name',
      label: first,
      left: true,
      sort: r => r.name,
      render: r => <span className="font-mono text-[11.5px]">{r.name}</span>,
    },
    {
      key: 'count',
      label: i18nT('pages.telemetryPanel.samples_col'),
      sort: r => r.count,
      render: r => fmtNumber(r.count),
    },
    {
      key: 'p50',
      label: i18nT('pages.telemetryPanel.p50_col'),
      sort: r => r.p50_ms,
      render: r => fmtMs(r.p50_ms),
    },
    {
      key: 'p90',
      label: i18nT('pages.telemetryPanel.p90_col'),
      sort: r => r.p90_ms,
      render: r => fmtMs(r.p90_ms),
    },
    {
      key: 'mean',
      label: i18nT('pages.telemetryPanel.mean_col'),
      hide: 'max-[720px]:hidden',
      sort: r => r.mean_ms,
      render: r => fmtMs(r.mean_ms),
    },
  ]
}

type Bucket = { label: string; count: number; idx: number }

function StartupTab({ s, faults, total }: { s: Startup; faults: number; total: number }) {
  const { period } = useContext(PanelWindowCtx)
  const [group, setGroup] = usePersistedChoice<StartupGroup>(
    'telemetry:startup-group',
    STARTUP_GROUPS,
    'phase',
  )

  const buckets: Bucket[] = []
  if (s.distribution?.buckets?.length) {
    const { buckets: bs, bounds } = s.distribution
    bs.forEach((n, i) => {
      if (n > 0) {
        const label =
          i >= bounds.length
            ? i18nT('pages.telemetryPanel.bucket_above', { value: fmtMs(bounds[bounds.length - 1]) })
            : i18nT('pages.telemetryPanel.bucket_upto', { value: fmtMs(bounds[i]) })
        buckets.push({ label, count: n, idx: i })
      }
    })
  }

  return (
    <Card className="mb-4">
      <CardTitle>
        {i18nT('pages.telemetryPanel.session_startup')}
        <InfoTip text={i18nT('pages.telemetryPanel.startup_is_per_process_not_per_conversation')} />
        <span className="ml-auto text-[12px] text-muted font-mono tabular-nums font-normal">{period}</span>
      </CardTitle>
      <div className="flex flex-wrap items-center gap-2 mb-2">
        <span className="text-[10px] text-muted uppercase tracking-wide">
          {i18nT('pages.telemetryPanel.group_by')}
        </span>
        <SegmentedControl<StartupGroup>
          collapse={false}
          value={group}
          onChange={setGroup}
          segments={[
            { key: 'phase', label: i18nT('pages.telemetryPanel.phase_col') },
            { key: 'channel', label: i18nT('pages.telemetryPanel.channel_col') },
            { key: 'distribution', label: i18nT('pages.telemetryPanel.distribution_col') },
          ]}
        />
      </div>
      {group === 'distribution' ? (
        buckets.length === 0 ? (
          <EmptyState
            icon={<Activity className="lucide-inline" />}
            title={i18nT('pages.telemetryPanel.no_startups_recorded')}
          />
        ) : (
          <Histogram rows={buckets.map(b => ({ key: String(b.idx), label: b.label, count: b.count }))} />
        )
      ) : (
        <DataTable<Stat & { name: string }>
          rows={(group === 'phase' ? s.phases : s.by_channel) ?? []}
          key="telemetry-startup-stats"
          tableId="telemetry-startup-stats"
          cols={statCols(
            group === 'phase'
              ? i18nT('pages.telemetryPanel.phase_col')
              : i18nT('pages.telemetryPanel.channel_col'),
          )}
          rowKey={r => r.name}
          defaultSort="p50"
          emptyTitle={i18nT('pages.telemetryPanel.no_startups_recorded')}
        />
      )}
      <Sums
        items={[
          {
            label: i18nT('pages.telemetryPanel.cold_start_p50'),
            value: fmtMs(s.cold.p50_ms),
            color: 'var(--accent)',
            sub: i18nT('pages.telemetryPanel.cold_count', { n: fmtNumber(s.cold.count) }),
          },
          { label: i18nT('pages.telemetryPanel.cold_start_p90'), value: fmtMs(s.cold.p90_ms) },
          {
            label: i18nT('pages.telemetryPanel.warm_start_p50'),
            value: fmtMs(s.warm.p50_ms),
            sub: i18nT('pages.telemetryPanel.warm_count', { n: fmtNumber(s.warm.count) }),
          },
          {
            label: i18nT('pages.telemetryPanel.overall_mean'),
            value: fmtMs(s.overall.mean_ms),
            sub: i18nT('pages.telemetryPanel.min_max', {
              min: fmtMs(s.overall.min_ms),
              max: fmtMs(s.overall.max_ms),
            }),
          },
          {
            label: i18nT('pages.telemetryPanel.startup_faults'),
            value: fmtNumber(faults),
            color: faults === 0 ? 'var(--ok)' : 'var(--danger)',
            sub: i18nT('pages.telemetryPanel.startups_recorded', { count: total, n: fmtNumber(total) }),
            note: <GenNote shown={s.overall.count} total={s.overall.total_count} />,
          },
        ]}
      />
      {(s.daily?.length ?? 0) > 1 && (
        <div className="mt-4 border-t border-border pt-3">
          <div className="mb-2 flex items-center gap-3 text-[10px] text-muted uppercase tracking-wide">
            <span>{i18nT('pages.telemetryPanel.daily_p50_trend')}</span>
            {/* "cold above, warm below" only teaches the encoding on a day that
                has both; a day with no cold start shows one unlabelled bar. */}
            <span className="flex items-center gap-1 normal-case">
              <span className="h-2 w-2 rounded-sm" style={{ background: 'var(--accent)' }} />
              {i18nT('pages.telemetryPanel.cold_label')}
            </span>
            <span className="flex items-center gap-1 normal-case">
              <span className="h-2 w-2 rounded-sm" style={{ background: 'var(--muted-strong)' }} />
              {i18nT('pages.telemetryPanel.warm_label')}
            </span>
          </div>
          <DailyTrend rows={s.daily} />
          <div className="mt-1 flex justify-between text-[10px] text-muted font-mono">
            <span>{s.daily[0]?.date}</span>
            <span>{s.daily[s.daily.length - 1]?.date}</span>
          </div>
        </div>
      )}
    </Card>
  )
}

/**
 * Turn health, rendered outside the tabs.
 *
 * Everything else on this page is something you go looking for; the fault rate
 * is something that has to find you. A tab would hide the one number whose
 * whole job is to be noticed, so it sits in a persistent strip — the same role
 * Activity Monitor's bottom bar plays for load.
 */
function HealthBar({ t }: { t: Turn | null }) {
  const { period } = useContext(PanelWindowCtx)
  const turnFaults = t
    ? // Count faults the way the API computes fault_rate: everything that is
      // neither "ok" nor a named non-fault outcome. Naming the failure outcomes
      // explicitly (error + timeout) dropped any other value — including the
      // "unknown" that shards predating the attribute aggregate under — so the
      // tile could show a rate over one population beside a count over another,
      // and a fault in a third outcome read as zero faults. Hence a complement
      // rule with an exemption list, not a list of failures.
      //
      // "unclassified" is exempt because it is not an outcome at all: it marks a
      // turn whose surface had no stop reason to give (a helper call site
      // passing a bare TurnUsage). The API excludes it from BOTH sides of
      // fault_rate for that reason, so counting it here would put every clean
      // cron/heartbeat/workflow turn in this tile's fault count while the
      // percentage beside it excluded them — the two-populations bug this
      // complement rule exists to prevent, in a new place.
      //
      // "cancelled" is exempt because the operator pressing Stop is not the
      // system failing. It IS in fault_rate's denominator (the turn ran), so
      // unlike "unclassified" it stays in `turnClassified` below — only the
      // numerator excludes it, on both sides.
      Object.entries(t.outcome).reduce(
        (n, [k, v]) => (TURN_NON_FAULT_OUTCOMES.has(k) ? n : n + v),
        0,
      )
    : 0
  // The population the API's fault_rate divides by: everything except the turns
  // whose outcome could not be determined. Derived here so the tile's rate, its
  // fault count and its printed denominator all describe the same set of turns.
  const turnClassified = t ? t.count - (t.outcome.unclassified ?? 0) : 0
  const faultPct = t ? Math.round(t.fault_rate * 100) : null
  // A real failure must never render as a clean zero. One error in 499 turns is
  // 0.2%, which Math.round takes to 0 and the old `< 2 → --ok` branch painted
  // in the success colour: the tile read a green "0%" directly above the sub
  // line reporting 1 fault. Sub-threshold is shown as "<1", and the success
  // colour is reserved for a genuinely empty fault set.
  const faultLabel =
    faultPct == null
      ? '—'
      : faultPct === 0 && turnFaults > 0
        ? `<${fmtNumber(1)}`
        : fmtNumber(faultPct)
  const faultColor =
    faultPct == null
      ? undefined
      : turnFaults === 0
        ? 'var(--ok)'
        : faultPct < 10
          ? 'var(--warn)'
          : 'var(--danger)'
  const noTurns = i18nT('pages.telemetryPanel.no_turns_yet')

  return (
    <Card className="mb-4">
      <Sums
        items={[
          {
            label: i18nT('pages.telemetryPanel.turn_latency_p50'),
            value: t ? fmtMs(t.p50_ms) : '—',
            color: 'var(--accent)',
            sub: t ? i18nT('pages.telemetryPanel.p90_value', { value: fmtMs(t.p90_ms) }) : noTurns,
          },
          {
            label: i18nT('pages.telemetryPanel.fault_rate'),
            value: faultLabel,
            unit: faultPct == null ? undefined : '%',
            color: faultColor,
            sub: t
              ? i18nT('pages.telemetryPanel.turn_faults', {
                  count: turnFaults,
                  n: fmtNumber(turnFaults),
                  // The CLASSIFIED count, matching the denominator the API's
                  // fault_rate divides by. `t.count` is the whole histogram
                  // including `unclassified`, so printing it here put a rate over
                  // one population directly above a count over another — the same
                  // two-populations bug the fault count above exempts
                  // `unclassified` to avoid, one line down. The divergence is not
                  // static: it grows with background traffic, which is exactly
                  // what this change starts sampling.
                  turns: fmtNumber(turnClassified),
                })
              : noTurns,
            note: t ? <GenNote shown={t.count} total={t.total_count} /> : undefined,
          },
          {
            label: i18nT('pages.telemetryPanel.throughput'),
            value: t ? fmtNumber(t.count) : '—',
            sub: t ? period : noTurns,
          },
        ]}
      />
      {!t && (
        <div className="text-muted text-[11px] mt-2">
          {i18nT('pages.telemetryPanel.agent_turn_latency_fault_rate_populate_after_the')}
        </div>
      )}
    </Card>
  )
}

type Tab = 'spend' | 'context' | 'latency' | 'startup'

const TABS = ['spend', 'context', 'latency', 'startup'] as const

/**
 * The one window control for the whole panel: presets ending now, or a custom
 * pair of days. Every card below reads the payload it drives, and labels itself
 * from the window the gateway reports back, so this control never has to say
 * what was clamped.
 */
function WindowControl({
  choice,
  onChoice,
  since,
  until,
  onSince,
  onUntil,
  pending,
}: {
  choice: RangeChoice
  onChoice: (next: RangeChoice) => void
  since: string
  until: string
  /** A blank value resets only that end: start to a week before the end, end to today. */
  onSince: (next: string) => void
  onUntil: (next: string) => void
  /** The figures on screen still belong to the previous pick. */
  pending: boolean
}) {
  const now = new Date()
  const today = localDay(now)
  // Start through today spans at most MAX_RANGE_DAYS dates, end day included.
  const earliest = localDay(earliestDay(MAX_RANGE_DAYS, now))
  const preset = (key: RangeChoice, n: number, unit: 'h' | 'd') => ({
    key,
    label: i18nT(unit === 'h' ? 'pages.telemetryPanel.range_hours' : 'pages.telemetryPanel.range_days', {
      n: fmtNumber(n),
    }),
  })
  return (
    <div className="flex flex-wrap items-center gap-2 mb-1" data-testid="telemetry-range">
      <span className="text-[10px] text-muted uppercase tracking-wide">{i18nT('pages.telemetryPanel.range_label')}</span>
      <SegmentedControl<RangeChoice>
        layoutId="telemetry-range"
        collapse={false}
        // Content-sized segments: one row at desktop widths. Seven segments in
        // the longer locales outgrow a phone-width row; wrap rather than
        // overflow there, since this is the panel's only window control.
        wrap="hug"
        ariaLabel={i18nT('pages.telemetryPanel.range_label')}
        value={choice}
        onChange={onChoice}
        segments={[
          { key: 'default', label: i18nT('pages.telemetryPanel.range_default') },
          preset('24h', 24, 'h'),
          preset('7d', 7, 'd'),
          preset('14d', 14, 'd'),
          preset('30d', 30, 'd'),
          preset('90d', 90, 'd'),
          { key: 'custom', label: i18nT('pages.telemetryPanel.range_custom') },
        ]}
      />
      {choice === 'custom' && (
        <span className="flex items-center gap-1.5">
          <Input
            type="date"
            className="flex-none px-2 py-1 text-[12px]"
            aria-label={i18nT('pages.telemetryPanel.range_from')}
            value={since}
            min={earliest}
            max={until}
            onChange={e => onSince(e.target.value)}
          />
          <span className="text-muted text-[12px]" aria-hidden="true">
            –
          </span>
          <Input
            type="date"
            className="flex-none px-2 py-1 text-[12px]"
            aria-label={i18nT('pages.telemetryPanel.range_to')}
            value={until}
            min={since}
            max={today}
            onChange={e => onUntil(e.target.value)}
          />
        </span>
      )}
      {/* Always rendered, a full row of fixed height: an inline status wrapped
          onto a row of its own at phone widths only while a pick loaded, so
          every card below jumped down and back on each pick. The text alone
          comes and goes, which the live region announces. */}
      <span
        role="status"
        className="basis-full h-4 leading-4 truncate text-muted text-[11px]"
        data-testid="telemetry-range-pending"
      >
        {pending ? i18nT('pages.telemetryPanel.loading_telemetry') : null}
      </span>
    </div>
  )
}

export default function TelemetryPanel() {
  const [range, setRange] = usePersistedChoice<RangeChoice>('telemetry:range', RANGE_CHOICES, DEFAULT_RANGE)
  const [sinceRaw, setSince] = usePersistedString('telemetry:range-since', '')
  const [untilRaw, setUntil] = usePersistedString('telemetry:range-until', '')
  const custom = customDays(sinceRaw, untilRaw)
  const query = rangeSearch(rangeQuery(range, custom.since, custom.until))
  const { data, isLoading, isError, error, isPlaceholderData } = useQuery<Resp>({
    // Keyed on the range: two windows are two answers, and a cached week must
    // never be served as a month.
    queryKey: ['telemetry-startup', query],
    queryFn: () => api.telemetryStartup(query),
    refetchInterval: 5000,
    // A new pick keeps the current figures up until its own arrive, instead of
    // blanking the panel to "Loading" on every click of the control.
    placeholderData: keepPreviousData,
  })
  const [tab, setTab] = usePersistedChoice<Tab>('telemetry:tab', TABS, 'spend')
  // Every card, and the turn drill-down's own fetch, reads the window the
  // payload ON SCREEN reports it measured. While a new pick loads that is still
  // the previous payload, so one view never mixes two windows; and a fixed range
  // reuses the measured bounds, not the request's future `until`.
  const win = panelWindow(data, query)
  const windowControl = (
    <WindowControl
      choice={range}
      onChoice={setRange}
      since={custom.since}
      until={custom.until}
      onSince={setSince}
      onUntil={setUntil}
      pending={isPlaceholderData}
    />
  )

  if (isLoading && !data) return <Notice>{i18nT('pages.telemetryPanel.loading_telemetry')}</Notice>
  // A fetch that never produced data is a failure, not "nothing recorded": the
  // no-data notice below would otherwise state a fact the request never
  // established. askAgent on: a read-only telemetry panel with nothing editable.
  if (isError && !data) {
    return (
      <div className="py-12 px-4">
        <ErrorNotice message={error?.message} askAgent testId="telemetry-startup-error" />
      </div>
    )
  }
  // Once data exists, a failed refetch keeps the last figures on screen
  // (react-query retains `data`) — but the failure is still SAID above them, so
  // stale numbers never pass for live ones while the 5s poll retries.
  const refetchNotice = isError ? (
    <div className="px-4 pt-3">
      <ErrorNotice message={error?.message} askAgent testId="telemetry-startup-error" />
    </div>
  ) : null

  const offBody = data ? (
    <Trans
      i18nKey="pages.telemetryPanel.off_body"
      components={{
        settingRef: <SettingRef configKey="telemetry.enabled" />,
        metricsDir: <code className="text-accent">{data.metrics_dir}</code>,
      }}
    />
  ) : null

  if (data && !data.enabled) {
    // Context occupancy AND credit spend both come from the token row store,
    // which is written regardless of the OTEL switch — so show them rather than
    // an empty page. This matters more than it looks: `telemetry.enabled`
    // defaults to false, so without this branch the whole spend surface ships
    // invisible to anyone who never turned the switch on.
    // With real data on screen the off-state is a compact banner, not the
    // centered empty-state block: a full-page "nothing here" under live
    // numbers makes the page contradict itself.
    const offCost = data.cost && data.cost.turns ? data.cost : null
    if (!data.context && !offCost) {
      return (
        <PanelWindowCtx.Provider value={win}>
          {refetchNotice}
          {windowControl}
          <Notice>
            <div className="text-text font-medium mb-1">{i18nT('pages.telemetryPanel.telemetry_is_off')}</div>
            {offBody}
          </Notice>
        </PanelWindowCtx.Provider>
      )
    }
    return (
      <PanelWindowCtx.Provider value={win}>
        <div className="overflow-y-auto flex-1 min-h-0 pb-8">
          {refetchNotice}
          {windowControl}
          {offCost && (
            <PanelWindowCtx.Provider value={spendWindow(data, win)}>
              <SpendTab c={offCost} />
            </PanelWindowCtx.Provider>
          )}
          {data.context && <ContextTab c={data.context} convos={offCost?.conversations} navigable={offCost?.navigable_category} />}
          <div className="border border-border bg-card rounded-xl p-3 text-[11px] leading-relaxed">
            <span className="text-text font-medium">{i18nT('pages.telemetryPanel.telemetry_is_off')}</span>{' '}
            <span className="text-muted">{offBody}</span>
          </div>
        </div>
      </PanelWindowCtx.Provider>
    )
  }

  const s = data?.startup ?? null
  const t = data?.turn ?? null
  const ctx = data?.context ?? null
  const other = data?.other ?? []
  // `cost` counts as data in its own right. It is derived from the per-turn
  // usage rows rather than the OTEL shards, so a machine with spend recorded but
  // no shard yet (or rows carrying credits without an occupancy sample) would
  // otherwise be told "no telemetry recorded" while the whole spend surface sat
  // ready to render.
  const hasData =
    !!(s && s.overall.count) || !!(t && t.count) || !!ctx || other.length > 0 || !!(data?.cost && data.cost.turns)
  if (!data || !hasData) {
    // The control stays on screen here too: a reader who picked a window with
    // nothing in it must be able to pick a wider one.
    return (
      <PanelWindowCtx.Provider value={win}>
        {refetchNotice}
        {windowControl}
        <Notice>{i18nT('pages.telemetryPanel.no_telemetry_in_window', { period: win.period })}</Notice>
      </PanelWindowCtx.Provider>
    )
  }

  // Startup health as an absolute count, not a rate. A rate over this
  // denominator cannot report a failure: the window measured 1411 startups, so
  // one failed startup is 99.93% — which Math.round takes straight back to a
  // perfect "100%". That is the same rounding erasure fixed on the fault-rate
  // figure above, made worse by a denominator ~3x larger, and it saturated: a
  // ready rate had no reachable value between "100%" and a visible problem. A
  // count has no such ceiling — the first real failure moves 0 to 1.
  const startupTotal = s ? Object.values(s.outcome).reduce((a, b) => a + b, 0) : 0
  const startupFaults = s
    ? Object.entries(s.outcome).reduce((n, [k, v]) => (k === 'ready' ? n : n + v), 0)
    : 0

  const hasStartup = !!(s && s.overall.count > 0)
  // Every tab is a table of rows, but the ROW ENTITY differs — conversations for
  // spend and occupancy, instruments for latency, startups for startup — because
  // the underlying stores differ in what they can attribute. A tab whose store
  // recorded nothing is not offered at all rather than opening onto an empty
  // table.
  const segments = [
    ...(data.cost ? [{ key: 'spend' as Tab, label: i18nT('pages.telemetryPanel.tab_spend'), icon: <Coins size={13} /> }] : []),
    ...(ctx ? [{ key: 'context' as Tab, label: i18nT('pages.telemetryPanel.tab_context'), icon: <Gauge size={13} /> }] : []),
    ...(other.length > 0
      ? [{ key: 'latency' as Tab, label: i18nT('pages.telemetryPanel.tab_latency'), icon: <Activity size={13} /> }]
      : []),
    ...(hasStartup
      ? [
          {
            key: 'startup' as Tab,
            label: i18nT('pages.telemetryPanel.tab_startup'),
            icon: <Rocket size={13} />,
            // The one alarm allowed onto a tab label: a failed startup is
            // otherwise invisible until someone opens the tab.
            count: startupFaults > 0 ? startupFaults : undefined,
          },
        ]
      : []),
  ]
  const active = segments.some(g => g.key === tab) ? tab : (segments[0]?.key ?? 'spend')
  // The exporter prunes metric shards after `telemetry.retention_days`, so the
  // OTEL figures (health strip, latency, startup) cannot reach as far back as a
  // wider window. Said once under the control, and each of those cards titles
  // itself with the span it does cover rather than the panel's window.
  const retention = data.metrics_retention_days ?? 0
  const otelWin = otelWindow(data, win, Date.now())

  return (
    <PanelWindowCtx.Provider value={win}>
      <div className="overflow-y-auto flex-1 min-h-0 pb-8">
        {refetchNotice}
        {windowControl}
        {otelWin && (
          <div className="text-muted text-[11px] -mt-1 mb-3" data-testid="telemetry-otel-retention">
            <Trans
              i18nKey="pages.telemetryPanel.otel_retention_cap"
              values={{ days: fmtNumber(retention) }}
              // The chip draws its own border, so give it room from the
              // surrounding brackets instead of letting it sit on them.
              components={{ settingRef: <span className="mx-1 inline-block" data-testid="retention-setting-ref"><SettingRef configKey="telemetry.retention_days" /></span> }}
            />
          </div>
        )}
        <PanelWindowCtx.Provider value={otelWin ?? win}>
          {/* Above the tabs, not after the active tab's table.
              The justification for this strip is that the fault rate is the one
              number that has to find the reader rather than be looked for — and
              placed at the bottom of a scroll container it was exactly as
              findable as the section it replaced, i.e. only by scrolling. An
              Activity Monitor bar sits at an EDGE; this is the edge that costs no
              sticky positioning inside an already-scrolling panel. */}
          <HealthBar t={t} />
        </PanelWindowCtx.Provider>

        {segments.length > 1 && (
          <div className="mb-3">
            <SegmentedControl<Tab> segments={segments} value={active} onChange={setTab} />
          </div>
        )}

        {active === 'spend' && data.cost && (
          <PanelWindowCtx.Provider value={spendWindow(data, win)}>
            <SpendTab c={data.cost} />
          </PanelWindowCtx.Provider>
        )}
        {active === 'context' && ctx && <ContextTab c={ctx} convos={data?.cost?.conversations} navigable={data?.cost?.navigable_category} />}
        <PanelWindowCtx.Provider value={otelWin ?? win}>
          {active === 'latency' && <LatencyTab other={other} />}
          {active === 'startup' && s && <StartupTab s={s} faults={startupFaults} total={startupTotal} />}
        </PanelWindowCtx.Provider>

        <div className="text-muted text-[11px] mt-2">
          {/* The footer speaks for the OTEL sections, so it names the period they
              cover: the retention-capped one their card titles use, when capped. */}
          {i18nT('pages.telemetryPanel.window_footer', { period: (otelWin ?? win).period })} {data.shard_count}{' '}
          {i18nT('pages.telemetryPanel.shard_s_source')} <code>{data.metrics_dir}</code>{' '}
          {i18nT('pages.telemetryPanel.local_only_no_egress')}
        </div>
      </div>
    </PanelWindowCtx.Provider>
  )
}
