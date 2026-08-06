import { useState } from 'react'

import { useQuery } from '@tanstack/react-query'
import { Activity, ChevronDown, ChevronUp, Coins, Gauge, Rocket } from 'lucide-react'
import { Link } from 'react-router-dom'

import { api } from '../api/client'
import InfoTip from '../components/InfoTip'
import SegmentedControl from '../components/SegmentedControl'
import { Btn, Card, CardTitle, EmptyState } from '../components/ui'
import { useSortableTable } from '../hooks/useSortableTable'
import { compareText, fmtDateNumeric, fmtNumber, fmtPercent, fmtUnit } from '../i18n/format'
import { i18nT } from '../i18n/t'
// ── GET /api/telemetry/startup shape (dashboard/handlers/telemetry.py) ──
type Stat = {
  count: number
  mean_ms: number
  p50_ms: number
  p90_ms: number
  min_ms: number
  max_ms: number
  // >0 => the 14d window straddles a bucket-boundary change and these numbers
  // describe only the newest generation. Surfaced so a truncated sample is
  // never quoted as the whole window.
  other_generations?: number
  // Samples across EVERY generation. Paired with `count` this reads
  // "showing 1,134 of 2,926" — the reader-facing form of the disclosure.
  total_count?: number
}
type Startup = {
  overall: Stat
  cold: Stat
  warm: Stat
  outcome: Record<string, number>
  daily: { date: string; count: number; cold_p50_ms: number; cold_p90_ms: number; warm_p50_ms: number }[]
  distribution: { buckets: number[]; bounds: number[] }
  phases: (Stat & { name: string })[]
  by_channel: (Stat & { name: string })[]
}
type Turn = Stat & { outcome: Record<string, number>; fault_rate: number }
type ContextSession = {
  slot: string
  turns: number
  peak_pct: number
  used: number
  window: number
  agent: string
  model: string
  surface: string
  ts: string
}
type Context = {
  turns: number
  p50_pct: number
  p90_pct: number
  max_pct: number
  sessions: ContextSession[]
  window_days: number
}
type Other = {
  name: string
  kind: string
  count?: number
  p50_ms?: number
  p90_ms?: number
  // Present on every histogram instrument and absent on counters. Declared here
  // because a latency profile needs the extremes, not only the percentiles.
  min_ms?: number
  max_ms?: number
  mean_ms?: number
  other_generations?: number
  total_count?: number
  total?: number
  by_attr?: Record<string, number>
  // Per-attribute sub-histograms, present only for the attribute keys the
  // backend splits on (_OTHER_SPLIT_ATTRS). Keyed "attr=value", e.g. "warm=false".
  splits?: Record<string, Stat>
}
type CostRow = {
  name: string
  credits: number
  turns: number
  per_turn: number
  share_pct: number
  // Absent for a name with no spend in the preceding period: there is no
  // percentage change from zero, and rendering one would invent a number.
  delta_pct?: number | null
}
type CostBand = { label: string; turns: number; mean_credits: number }
type CostConvo = {
  slot: string
  /**
   * The session taxonomy: `dashboard`, `bg`, `telegram`, `slack`, … Classified
   * by the backend, because it is the only place that knows every session-key
   * form. Used for the category column and for linkability — only a `dashboard`
   * session has a route to open, while a Telegram thread is every bit a session
   * with nowhere for a dashboard link to go. Deriving either from the key shape
   * here would put a second, drifting copy of session-key knowledge in the
   * frontend.
   */
  category: string
  /** The unollapsed channel underneath the category (`cron`, `heartbeat`, …). */
  channel: string
  // Present only while the conversation is still open — titles are not persisted.
  title?: string
  credits: number
  turns: number
  peak_pct: number
  span_days: number
  first_ts: number
  growth_pct_per_turn?: number | null
  turns_to_compaction?: number | null
}
type Cost = {
  window_days: number
  credits: number
  turns: number
  per_turn: number
  prior_credits: number
  prior_turns: number
  prior_per_turn: number
  delta_pct?: number | null
  priciest: { credits: number; slot: string; ts: string }
  by_model: CostRow[]
  by_channel: CostRow[]
  /** Spend by the session taxonomy — the grouping the panel offers. */
  by_category: CostRow[]
  context_bands: CostBand[]
  conversations: CostConvo[]
  conversation_count: number
  navigable_category: string
}
type Resp = {
  enabled: boolean
  window_days: number
  shard_count: number
  metrics_dir: string
  startup: Startup | null
  turn: Turn | null
  context: Context | null
  cost: Cost | null
  other: Other[]
}

const fmtMs = (ms?: number | null): string =>
  ms == null ? '—' : ms >= 1000 ? (ms / 1000).toFixed(1) + 's' : Math.round(ms) + 'ms'

/**
 * A share that is real but rounds to zero must not read as zero.
 *
 * Computed from the credits and the window total rather than read from the
 * payload's `share_pct`, which the backend has already rounded: a model with 7
 * credits arrived as a flat `0` and rendered "0%", while its 9.5-credit
 * neighbour arrived as 0.05 and rendered "<1%". Same magnitude, two different
 * claims, and the smaller one was the lie. Deriving it here means the only
 * rounding is the one being described.
 */
const fmtSharePct = (credits: number, total: number): string => {
  if (!total) return '—'
  const ratio = credits / total
  return ratio > 0 && ratio < 0.005 ? `<${fmtPercent(0.01)}` : fmtPercent(ratio)
}

const fmtDelta = (d?: number | null): string =>
  d == null ? i18nT('pages.telemetryPanel.cost_new') : (d > 0 ? '+' : '') + fmtPercent(d / 100)

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
 * Occupancy colour thresholds, applied ONLY to the aggregate percentiles.
 *
 * Compaction triggers at 90% of the window, so that is the danger line rather
 * than an arbitrary "nearly full" guess; 70% is the point where a long session
 * still has room but is worth watching.
 *
 * Deliberately NOT applied to the per-row peak-occupancy column. A peak is a
 * high-water mark over the session's whole life, so a busy window puts nearly
 * every row above 90% — the measured page had 6 of 8 rows in red, at which
 * point the danger colour marks "this row exists" rather than "act on this
 * row". The actionable companion number is turns-to-compaction, which IS
 * threshold-coloured below, because it is the one that can still be acted on.
 */
const occColor = (p: number): string =>
  p >= 90 ? 'var(--danger)' : p >= 70 ? 'var(--warn)' : 'var(--accent)'

/**
 * Turns remaining before the next compaction. Unlike a peak, this is a forecast
 * a reader can act on, so it carries the page's only per-row alarm: under two
 * turns of headroom the next turn may compact, under five it is imminent.
 */
const headroomColor = (turns: number): string | undefined =>
  turns <= 2 ? 'var(--danger)' : turns <= 5 ? 'var(--warn)' : undefined

// ── Sortable table ─────────────────────────────────────────────
//
// The sort MODEL is the shared `useSortableTable` hook the other five tables on
// the dashboard use, so a chosen sort persists per table exactly as it does on
// Hooks, Schedule, Cron, MCP and Memory.
//
// The header markup is local rather than `SortableHeader` for two reasons the
// shared component cannot express without changing those five pages: this is
// the first numeric-heavy table, so most columns must right-align (SortableHeader
// hard-codes `text-left`, and its className is appended rather than merged, so
// a competing `text-right` would resolve by stylesheet order), and it carries
// the ▾/▴ caret plus accent-coloured active column from the Session & Task
// Memory card. Giving SortableHeader a caret is worth doing for all six tables,
// but as its own change.
//
// The comparison is also local, because the hook's direction model negates by
// swapping arguments — which cannot express "nulls last in BOTH directions".
// That property is load-bearing here: `unknown` is not a small value, and
// flipping a column must not promote unmeasured rows to the top.
const NUM_CELL = 'px-3 py-1.5 text-right font-mono text-[12.5px] tabular-nums whitespace-nowrap'
const TXT_CELL = 'px-3 py-1.5 text-left text-[12.5px]'
const HEAD_BASE = 'px-3 py-1.5 text-[11px] font-medium text-muted whitespace-nowrap'

type Col<R> = {
  key: string
  label: string
  /** Text columns render left-aligned and collate with compareText. */
  left?: boolean
  /** Sort value. `null` sorts last in both directions — unknown is not small. */
  sort: (r: R) => number | string | null
  render: (r: R) => React.ReactNode
  color?: (r: R) => string | undefined
  /** Responsive drop class, applied to the header and its cells together. */
  hide?: string
}

/**
 * Defined at module scope on purpose. As a function declared INSIDE DataTable it
 * was a new component type on every render, so React unmounted and re-mounted
 * every `<th>` whenever the sort changed — which detached the very button the
 * user had just clicked, and a detached node's click never reaches React's root
 * listener. The visible symptom was a header that sorted once and then went
 * dead.
 */
function HeadCell({
  label,
  left,
  hide,
  active,
  desc,
  onToggle,
}: {
  label: string
  left?: boolean
  hide?: string
  active: boolean
  desc: boolean
  onToggle: () => void
}) {
  return (
    <th
      className={`${HEAD_BASE} ${left ? 'text-left' : 'text-right'} ${hide ?? ''}`}
      aria-sort={active ? (desc ? 'descending' : 'ascending') : 'none'}
    >
      <Btn
        type="button"
        onClick={onToggle}
        // `Btn` rather than a raw <button>, and a Lucide chevron rather than a
        // ▾/▴ glyph: a text symbol ignores `currentColor` and the theme tokens,
        // so it would not follow the accent colour the active column is marked
        // with, and it renders differently on each platform.
        //
        // The overrides strip Btn's border, fill and 13px body type — a boxed
        // control in every column head would read as an action rather than a
        // label. twMerge resolves them over the defaults.
        className={`border-transparent bg-transparent px-0 py-0 gap-1 text-[11px] font-medium ${
          active ? 'text-accent' : 'text-muted'
        }`}
      >
        {label}
        {active &&
          (desc ? (
            <ChevronDown size={12} aria-hidden="true" className="lucide-inline" />
          ) : (
            <ChevronUp size={12} aria-hidden="true" className="lucide-inline" />
          ))}
      </Btn>
    </th>
  )
}

function DataTable<R>({
  rows,
  cols,
  rowKey,
  tableId,
  defaultSort,
  emptyTitle,
}: {
  rows: R[]
  cols: Col<R>[]
  rowKey: (r: R) => string
  /** Namespaces the persisted sort, so each tab and grouping remembers its own. */
  tableId: string
  defaultSort: string
  emptyTitle: string
}) {
  // A text column opens A→Z; a measurement opens largest-first, which is the
  // question being asked of it ("what cost the most", "what was slowest").
  //
  // Rebuilt each render rather than memoised: `cols` is a fresh array on every
  // parent render, so a `[cols]` memo never once hit its cache — it paid the
  // dependency check for nothing and read as if it were saving work.
  const initialDirs = Object.fromEntries(cols.map(c => [c.key, c.left ? 'asc' : 'desc'] as const))
  // Comparators are deliberately empty: this hook is used for the sort STATE
  // and its persistence, and the ordering is applied below so nulls can stay
  // last in both directions.
  const { sort, toggle } = useSortableTable<R>(
    [],
    tableId,
    {},
    // The opening direction is the column's own, not a blanket 'desc': a column
    // ordered by an index (the latency buckets) has to open ascending or the
    // distribution renders back to front.
    { key: defaultSort, dir: initialDirs[defaultSort] ?? 'desc' },
    { bidirectional: true, initialDirs },
  )
  const desc = sort.dir === 'desc'
  // The column that ACTUALLY orders the rows, which is not always the one the
  // persisted sort names: a sort saved by an older column layout survives in
  // localStorage, and the lookup then falls back to the first column. Resolving
  // it once and marking the header from THIS key means the caret and `aria-sort`
  // can never disagree with the order on screen — previously that case left the
  // table sorted by its first column with no header marked at all.
  const activeKey = (cols.find(c => c.key === sort.key) ?? cols[0])?.key

  // Not memoised, for the same reason `initialDirs` is not: `cols` is rebuilt
  // by the parent on every render, so a dependency array naming it could never
  // hit its cache. Hoisting the column builders to module scope WOULD stabilise
  // them and is the wrong fix — they call `i18nT` per render on purpose, and
  // freezing the labels at import time would leave the table in the old
  // language after a locale switch. Sorting a handful of rows is cheaper than
  // that bug.
  const shown = (() => {
    const col = cols.find(c => c.key === activeKey) ?? cols[0]
    return rows.slice().sort((a, b) => {
      const av = col.sort(a)
      const bv = col.sort(b)
      if (av == null && bv == null) return 0
      if (av == null) return 1
      if (bv == null) return -1
      const d =
        typeof av === 'string' && typeof bv === 'string' ? compareText(av, bv) : Number(av) - Number(bv)
      return desc ? -d : d
    })
  })()

  return (
    <>
      {shown.length === 0 ? (
        <EmptyState icon={<Activity className="lucide-inline" />} title={emptyTitle} />
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full border-collapse table-striped">
            <thead>
              <tr className="bg-bg-elevated border-b border-border">
                {cols.map(c => (
                  <HeadCell
                    key={c.key}
                    label={c.label}
                    left={c.left}
                    hide={c.hide}
                    active={c.key === activeKey}
                    desc={desc}
                    onToggle={() => toggle(c.key)}
                  />
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map(r => (
                <tr key={rowKey(r)} className="border-b border-border/60 last:border-b-0">
                  {cols.map(c => (
                    <td
                      key={c.key}
                      className={`${c.left ? TXT_CELL : NUM_CELL} ${c.hide ?? ''}`}
                      style={c.color?.(r) ? { color: c.color(r) } : undefined}
                    >
                      {c.render(r)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  )
}

/**
 * The per-tab summary. One borderless strip replaces what were fifteen
 * identically-shaped bordered tiles spread over four sections: with every
 * number in the same card as the rows it summarises, nothing has to be
 * cross-referenced between sections, and a headline total no longer competes
 * visually with a row of a table.
 */
type Sum = { label: string; value: string; unit?: string; sub?: string; note?: React.ReactNode; color?: string }

function Sums({ items }: { items: Sum[] }) {
  return (
    <div className="flex flex-wrap gap-x-9 gap-y-3 border-t border-border mt-3 pt-3">
      {items.map(i => (
        <div key={i.label}>
          <div className="text-[10px] text-muted uppercase tracking-wide">{i.label}</div>
          <div className="text-[19px] font-bold leading-tight" style={i.color ? { color: i.color } : undefined}>
            {i.value}
            {i.unit && <span className="text-[11px] text-muted font-normal ml-0.5">{i.unit}</span>}
          </div>
          {i.sub && <div className="text-[10px] text-muted mt-0.5">{i.sub}</div>}
          {i.note}
        </div>
      ))}
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
 * A zero minimum has no logarithm, so the axis starts at a sub-millisecond floor
 * rather than at zero. That is a floor on the AXIS, not a claim about the data —
 * the real min is printed next to the bar.
 */
const _LOG_FLOOR_MS = 0.5

function RangeBar({ min, p50, p90, max }: { min: number; p50: number; p90: number; max: number }) {
  const lo = Math.max(min, _LOG_FLOOR_MS)
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
        min: fmtMs(min),
        p50: fmtMs(p50),
        p90: fmtMs(p90),
        max: fmtMs(max),
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
 * A horizontal histogram. Bars carry magnitude by length and share one scale,
 * which is exactly what a distribution needs — unlike the sortable bucket table
 * this replaces, where sorting by count destroyed the bound order that IS the
 * shape.
 */
function Histogram({ rows }: { rows: { label: string; count: number }[] }) {
  const peak = Math.max(1, ...rows.map(r => r.count))
  return (
    <div className="flex flex-col gap-1">
      {rows.map(r => (
        <div key={r.label} className="flex items-center gap-2.5 text-[11px]">
          <span className="w-20 shrink-0 text-right font-mono text-muted">{r.label}</span>
          <div className="h-2 min-w-0 flex-1 overflow-hidden rounded-sm bg-[var(--bg)]">
            <span
              className="block h-full rounded-sm"
              style={{ width: `${(r.count / peak) * 100}%`, background: 'var(--muted-strong)' }}
            />
          </div>
          <span className="w-12 shrink-0 text-right font-mono tabular-nums">{fmtNumber(r.count)}</span>
        </div>
      ))}
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

// ── Spend ──────────────────────────────────────────────────────

type SpendGroup = 'session' | 'category' | 'model'

/**
 * Session / category / model are the same credits regrouped, so they are a
 * group-by over one table rather than three sections. As three sections they
 * were three separately-scrolled bar lists whose columns did not align and
 * whose empty rows — a model with a 0.4% share still drew a full-width rail —
 * made half the section blank.
 */
function convoCols(navigable: string): Col<CostConvo>[] {
  return [
    {
      key: 'name',
      label: i18nT('pages.telemetryPanel.session_col'),
      left: true,
      sort: v => v.title ?? v.slot,
      render: v =>
        // TWO conditions, not one. A title alone is not enough: ChatPage
        // resolves ?sid against the live DASHBOARD slot list, so a Telegram
        // thread — a real conversation, often titled — would render as a link
        // that lands on `Session "…" not found` after a 5s timeout. And a
        // dashboard row with no title is untitled BECAUSE its slot is gone, so
        // it has nothing to resolve either. Only a titled dashboard row gets a
        // router link; everything else is plain text.
        //
        // The `!!navigable` guard makes this fail CLOSED: without it, a payload
        // missing `navigable_category` would satisfy `v.category === navigable`
        // for every row that also has no category, and link all of them.
        v.title && !!navigable && v.category === navigable ? (
          <Link
            to={`/chat?sid=${encodeURIComponent(v.slot)}`}
            className="block max-w-[320px] truncate text-[var(--accent)] hover:underline"
            title={v.title}
          >
            {v.title}
          </Link>
        ) : (
          <span className="block max-w-[320px] truncate text-muted" title={v.slot}>
            {v.title ??
              i18nT('pages.telemetryPanel.untitled_conversation_on', {
                date: fmtDateNumeric(v.first_ts * 1000),
              })}
          </span>
        ),
    },
    {
      key: 'category',
      label: i18nT('pages.telemetryPanel.category_col'),
      left: true,
      // Rendered verbatim: these are backend enum values (`dashboard`, `bg`,
      // `telegram`, `slack`), i.e. data, not copy to translate — the same
      // treatment model names get one column over.
      sort: v => v.category,
      // Every sibling column has a breakpoint; without one this pushed Credits —
      // the column the page exists for — off a narrow viewport.
      hide: 'max-[900px]:hidden',
      render: v => (
        <span className="text-muted font-mono text-[11.5px]">{categoryLabel(v.category)}</span>
      ),
    },
    {
      key: 'credits',
      label: i18nT('pages.telemetryPanel.credits_col'),
      sort: v => v.credits,
      render: v => fmtNumber(v.credits),
    },
    {
      key: 'turns',
      label: i18nT('pages.telemetryPanel.turns_col'),
      sort: v => v.turns,
      render: v => fmtNumber(v.turns),
    },
    {
      key: 'per_turn',
      // Derived here rather than read from the payload, which carries a per-turn
      // figure only for the window as a whole. Rounded to the same single
      // decimal the backend uses for its own averages, so the column and the
      // total below it do not quote the same quantity at two precisions.
      label: i18nT('pages.telemetryPanel.per_turn_col'),
      sort: v => (v.turns ? v.credits / v.turns : null),
      render: v => (v.turns ? fmtNumber(v.credits / v.turns, { maximumFractionDigits: 1 }) : '—'),
    },
    {
      key: 'peak',
      label: i18nT('pages.telemetryPanel.peak_ctx_col'),
      sort: v => v.peak_pct,
      render: v => fmtPercent(v.peak_pct / 100),
    },
    {
      key: 'span',
      label: i18nT('pages.telemetryPanel.span_col'),
      hide: 'max-[720px]:hidden',
      sort: v => v.span_days,
      render: v => fmtUnit(v.span_days, 'day'),
    },
    {
      key: 'growth',
      label: i18nT('pages.telemetryPanel.growth_col'),
      hide: 'max-[900px]:hidden',
      sort: v => v.growth_pct_per_turn ?? null,
      render: v =>
        v.growth_pct_per_turn == null
          ? '—'
          : i18nT('pages.telemetryPanel.growth_per_turn', { rate: fmtNumber(v.growth_pct_per_turn) }),
    },
    {
      key: 'headroom',
      label: i18nT('pages.telemetryPanel.to_compaction_col'),
      hide: 'max-[900px]:hidden',
      sort: v => v.turns_to_compaction ?? null,
      color: v => (v.turns_to_compaction == null ? undefined : headroomColor(v.turns_to_compaction)),
      render: v => (v.turns_to_compaction == null ? '—' : fmtNumber(v.turns_to_compaction)),
    },
  ]
}

/**
 * `bg` is a coined abbreviation with no external referent, so it gets a translated
 * label; a transport name (`dashboard`, `telegram`, `slack`) is a proper noun the
 * reader already knows and is shown as the backend sent it.
 *
 * This lives in ONE place because the category is rendered by two different
 * surfaces — the Session table's column and the Group-by-Category table — and
 * mapping it in only one produced two labels for the same value.
 */
function categoryLabel(name: string): string {
  return name === 'bg' ? i18nT('pages.telemetryPanel.category_bg') : name
}

function shareCols(first: string, total: number): Col<CostRow>[] {
  return [
    { key: 'name', label: first, left: true, sort: r => r.name, render: r => categoryLabel(r.name) },
    {
      key: 'credits',
      label: i18nT('pages.telemetryPanel.credits_col'),
      sort: r => r.credits,
      render: r => fmtNumber(r.credits),
    },
    {
      key: 'turns',
      label: i18nT('pages.telemetryPanel.turns_col'),
      sort: r => r.turns,
      render: r => fmtNumber(r.turns),
    },
    {
      key: 'per_turn',
      label: i18nT('pages.telemetryPanel.per_turn_col'),
      sort: r => r.per_turn,
      render: r => fmtNumber(r.per_turn),
    },
    {
      key: 'share',
      label: i18nT('pages.telemetryPanel.share_col'),
      sort: r => r.credits,
      render: r => fmtSharePct(r.credits, total),
    },
  ]
}

function SpendTab({ c }: { c: Cost }) {
  const [group, setGroup] = useState<SpendGroup>('session')
  const bands = c.context_bands
  return (
    <Card className="mb-4">
      <CardTitle>
        {i18nT('pages.telemetryPanel.credits')}
        <InfoTip text={i18nT('pages.telemetryPanel.measured_from_token_records', { days: fmtNumber(c.window_days) })} />
        <span className="ml-auto text-[12px] text-muted font-mono tabular-nums font-normal">
          {i18nT('pages.telemetryPanel.credits_this_period', { days: fmtNumber(c.window_days) })}
        </span>
      </CardTitle>
      {/* In the open, not only in the title's InfoTip. The spend and OTEL
          numbers come from different stores over different windows, so a
          950-turn spend total sits beside a 685-turn throughput; without the
          source stated where both are visible, fewer turns over a longer
          window reads as broken data rather than as two measurements. */}
      <div className="text-[10px] text-muted -mt-2 mb-2.5">
        {i18nT('pages.telemetryPanel.measured_from_token_records', { days: fmtNumber(c.window_days) })}
      </div>
      <div className="flex flex-wrap items-center gap-2 mb-2">
        <span className="text-[10px] text-muted uppercase tracking-wide">
          {i18nT('pages.telemetryPanel.group_by')}
        </span>
        <SegmentedControl<SpendGroup>
          collapse={false}
          value={group}
          onChange={setGroup}
          segments={[
            { key: 'session', label: i18nT('pages.telemetryPanel.session_col') },
            { key: 'category', label: i18nT('pages.telemetryPanel.category_col') },
            { key: 'model', label: i18nT('pages.telemetryPanel.model_col') },
          ]}
        />
      </div>
      {group === 'session' ? (
        <DataTable<CostConvo>
          rows={c.conversations}
          key="telemetry-spend-conversation"
          tableId="telemetry-spend-conversation"
          cols={convoCols(c.navigable_category)}
          rowKey={v => v.slot}
          defaultSort="credits"
          emptyTitle={i18nT('pages.telemetryPanel.no_spend_recorded')}
        />
      ) : (
        <DataTable<CostRow>
          rows={group === 'model' ? c.by_model : c.by_category}
          key="telemetry-spend-share"
          tableId="telemetry-spend-share"
          cols={shareCols(
            group === 'model'
              ? i18nT('pages.telemetryPanel.model_col')
              : i18nT('pages.telemetryPanel.category_col'),
            c.credits,
          )}
          rowKey={r => r.name}
          defaultSort="credits"
          emptyTitle={i18nT('pages.telemetryPanel.no_spend_recorded')}
        />
      )}
      <Sums
        items={[
          {
            label: i18nT('pages.telemetryPanel.credits_col'),
            value: fmtNumber(c.credits),
            color: 'var(--accent)',
            sub: i18nT('pages.telemetryPanel.turns_measured', { count: c.turns, n: fmtNumber(c.turns) }),
          },
          {
            label: i18nT('pages.telemetryPanel.vs_previous_period'),
            value: fmtDelta(c.delta_pct),
            sub: `${fmtNumber(c.prior_credits)} · ${fmtNumber(c.prior_turns)}`,
          },
          {
            label: i18nT('pages.telemetryPanel.per_turn_col'),
            value: fmtNumber(c.per_turn),
            sub: i18nT('pages.telemetryPanel.was_value', { value: fmtNumber(c.prior_per_turn) }),
          },
          {
            label: i18nT('pages.telemetryPanel.priciest_turn'),
            value: fmtNumber(c.priciest.credits),
          },
          {
            // Shown as "8 / 252", not a bare 252: the table carries only the top
            // spenders, and with the count alone a reader who filtered for a
            // conversation outside that slice got "no rows match" and could
            // reasonably conclude it had spent nothing. The ratio states the
            // truncation without inventing new copy for it.
            label: i18nT('pages.telemetryPanel.sessions_col'),
            // The list is no longer truncated, so the pair is now equal on every
            // real payload and "45 / 45" spends a stat slot saying nothing. The
            // ratio still appears if the payload backstop ever does clamp.
            value: c.conversations.length === c.conversation_count
              ? fmtNumber(c.conversation_count)
              : `${fmtNumber(c.conversations.length)} / ${fmtNumber(c.conversation_count)}`,
            sub: i18nT('pages.telemetryPanel.top_spenders_link_to_chat'),
          },
        ]}
      />
      {bands.length > 0 && (
        // The former "cost by context size" section, which was five rows of bar
        // to carry five numbers. Occupancy is already a column on the rows
        // above; what the section actually added was the shape of the
        // relationship, and that fits on one line.
        <div className="text-[10px] text-muted mt-3 leading-relaxed">
          <span className="uppercase tracking-wide">
            {i18nT('pages.telemetryPanel.mean_credits_by_occupancy')}
          </span>{' '}
          {bands.map((b, i) => (
            <span key={b.label}>
              {i > 0 && ' · '}
              <span className="font-mono">{b.label}</span>{' '}
              <span className="text-text tabular-nums">{fmtNumber(b.mean_credits)}</span>{' '}
              <span className="tabular-nums">
                ({i18nT('pages.telemetryPanel.sample_count', { count: fmtNumber(b.turns) })})
              </span>
            </span>
          ))}
        </div>
      )}
    </Card>
  )
}

// ── Context ────────────────────────────────────────────────────

/**
 * Rows are the per-session occupancy samples the API has always returned and
 * the page never rendered: `context.sessions[]` was computed, serialised and
 * dropped on the floor, so the only way to see which conversation was near
 * compaction was to read the spend ranking and hope the two windows agreed.
 */
function sessionCols(): Col<ContextSession>[] {
  return [
    {
      key: 'slot',
      label: i18nT('pages.telemetryPanel.session_col'),
      left: true,
      sort: s => s.slot,
      render: s => (
        <span className="block max-w-[260px] truncate font-mono text-[11.5px]" title={s.slot}>
          {s.slot}
        </span>
      ),
    },
    {
      key: 'peak',
      label: i18nT('pages.telemetryPanel.peak_ctx_col'),
      sort: s => s.peak_pct,
      render: s => fmtPercent(s.peak_pct / 100),
    },
    {
      key: 'used',
      label: i18nT('pages.telemetryPanel.used_tokens_col'),
      sort: s => s.used,
      render: s => fmtNumber(s.used),
    },
    {
      key: 'window',
      label: i18nT('pages.telemetryPanel.window_tokens_col'),
      hide: 'max-[720px]:hidden',
      sort: s => s.window,
      render: s => fmtNumber(s.window),
    },
    {
      key: 'turns',
      label: i18nT('pages.telemetryPanel.turns_col'),
      sort: s => s.turns,
      render: s => fmtNumber(s.turns),
    },
    {
      key: 'model',
      label: i18nT('pages.telemetryPanel.model_col'),
      left: true,
      hide: 'max-[900px]:hidden',
      sort: s => s.model,
      render: s => <span className="font-mono text-[11.5px]">{s.model}</span>,
    },
    {
      key: 'agent',
      label: i18nT('pages.telemetryPanel.agent_col'),
      left: true,
      hide: 'max-[1100px]:hidden',
      sort: s => s.agent,
      render: s => <span className="font-mono text-[11.5px]">{s.agent}</span>,
    },
    {
      key: 'surface',
      label: i18nT('pages.telemetryPanel.surface_col'),
      left: true,
      hide: 'max-[1100px]:hidden',
      sort: s => s.surface,
      render: s => <span className="font-mono text-[11.5px]">{s.surface}</span>,
    },
  ]
}

function ContextTab({ c }: { c: Context }) {
  return (
    <Card className="mb-4">
      <CardTitle>
        {i18nT('pages.telemetryPanel.context_window')}
        <InfoTip text={i18nT('pages.telemetryPanel.measured_from_token_records', { days: fmtNumber(c.window_days) })} />
        <span className="ml-auto text-[12px] text-muted font-mono tabular-nums font-normal">
          {i18nT('pages.telemetryPanel.credits_this_period', { days: fmtNumber(c.window_days) })}
        </span>
      </CardTitle>
      <div className="text-[10px] text-muted -mt-2 mb-2.5">
        {i18nT('pages.telemetryPanel.measured_from_token_records', { days: fmtNumber(c.window_days) })}
      </div>
      <DataTable<ContextSession>
        rows={c.sessions ?? []}
          key="telemetry-context"
          tableId="telemetry-context"
        cols={sessionCols()}
        rowKey={s => s.slot}
        defaultSort="peak"
        emptyTitle={i18nT('pages.telemetryPanel.no_occupancy_samples')}
      />
      <Sums
        items={[
          {
            label: i18nT('pages.telemetryPanel.occupancy_p50'),
            value: fmtNumber(c.p50_pct),
            unit: '%',
            color: occColor(c.p50_pct),
            sub: i18nT('pages.telemetryPanel.turns_measured', { count: c.turns, n: fmtNumber(c.turns) }),
          },
          {
            label: i18nT('pages.telemetryPanel.occupancy_p90'),
            value: fmtNumber(c.p90_pct),
            unit: '%',
            color: occColor(c.p90_pct),
          },
          {
            // Uncoloured, unlike the two percentiles beside it. This is the
            // high-water mark over every session in the window, so on any busy
            // install it sits at or near 100% permanently — a red that never
            // changes is wallpaper, not an alarm. p50 and p90 keep the colour
            // because they can actually move.
            label: i18nT('pages.telemetryPanel.peak_occupancy'),
            value: fmtNumber(c.max_pct),
            unit: '%',
          },
        ]}
      />
    </Card>
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

function LatencyTab({ other, days }: { other: Other[]; days: number }) {
  // Histograms first and by volume: a counter has no percentiles to shape, so it
  // cannot carry a profile bar and belongs after the things that can.
  const hist = other
    .filter(o => o.p50_ms != null && o.max_ms != null)
    .sort((a, b) => (b.count ?? 0) - (a.count ?? 0))
  const counters = other.filter(o => o.p50_ms == null)

  return (
    <Card className="mb-4">
      <CardTitle>
        {i18nT('pages.telemetryPanel.instruments')}
        <InfoTip text={i18nT('pages.telemetryPanel.otel_has_no_per_conversation_split')} />
        <span className="ml-auto text-[12px] text-muted font-mono tabular-nums font-normal">
          {i18nT('pages.telemetryPanel.credits_this_period', { days: fmtNumber(days) })}
        </span>
      </CardTitle>

      {hist.length === 0 && counters.length === 0 ? (
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
            {hist.map(o => (
              <div key={o.name} className="flex items-center gap-3 text-[11.5px]">
                <span className="w-[260px] shrink-0 truncate font-mono text-[11px]" title={o.name}>
                  {o.name}
                </span>
                {/* The endpoints flank the bar so each row is visibly its own
                    scale, rather than a position on a shared axis it never had. */}
                <span className="w-14 shrink-0 text-right font-mono tabular-nums text-muted">
                  {fmtMs(o.min_ms)}
                </span>
                <div className="min-w-0 flex-1">
                  <RangeBar
                    min={o.min_ms ?? 0}
                    p50={o.p50_ms ?? 0}
                    p90={o.p90_ms ?? 0}
                    max={o.max_ms ?? 0}
                  />
                </div>
                <span className="w-14 shrink-0 font-mono tabular-nums text-muted">
                  {fmtMs(o.max_ms)}
                </span>
                <span className="w-16 shrink-0 text-right font-mono tabular-nums">{fmtMs(o.p50_ms)}</span>
                <span className="w-16 shrink-0 text-right font-mono tabular-nums">{fmtMs(o.p90_ms)}</span>
                <span className="flex w-16 shrink-0 items-center justify-end gap-1 text-right font-mono tabular-nums text-muted">
                  {fmtNumber(o.count ?? 0)}
                  {/* The window can span a boundary change, and stats() then describe
                      only the newest generation — say so rather than labelling a
                      subset as the total. */}
                  <GenNote shown={o.count} total={o.total_count} compact />
                </span>
              </div>
            ))}
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
        </>
      )}
    </Card>
  )
}

// ── Startup ────────────────────────────────────────────────────

type StartupGroup = 'phase' | 'channel' | 'distribution'

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


function StartupTab({ s, faults, total, days }: { s: Startup; faults: number; total: number; days: number }) {
  const [group, setGroup] = useState<StartupGroup>('phase')

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
        <span className="ml-auto text-[12px] text-muted font-mono tabular-nums font-normal">
          {i18nT('pages.telemetryPanel.credits_this_period', { days: fmtNumber(days) })}
        </span>
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
          <Histogram rows={buckets} />
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
function HealthBar({ t, days }: { t: Turn | null; days: number }) {
  const turnFaults = t
    ? // Count faults the way the API computes fault_rate: everything that is not
      // "ok". Naming the failure outcomes explicitly (error + timeout) dropped
      // any other value — including the "unknown" that shards predating the
      // attribute aggregate under — so the tile could show a rate over one
      // population beside a count over another, and a fault in a third outcome
      // read as zero faults.
      Object.entries(t.outcome).reduce((n, [k, v]) => (k === 'ok' ? n : n + v), 0)
    : 0
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
                  turns: fmtNumber(t.count),
                })
              : noTurns,
            note: t ? <GenNote shown={t.count} total={t.total_count} /> : undefined,
          },
          {
            label: i18nT('pages.telemetryPanel.throughput'),
            value: t ? fmtNumber(t.count) : '—',
            sub: t ? i18nT('pages.telemetryPanel.credits_this_period', { days: fmtNumber(days) }) : noTurns,
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

export default function TelemetryPanel() {
  const { data, isLoading } = useQuery<Resp>({
    queryKey: ['telemetry-startup'],
    queryFn: () => api.telemetryStartup(),
    refetchInterval: 5000,
  })
  const [tab, setTab] = useState<Tab>('spend')

  if (isLoading && !data) return <Notice>{i18nT('pages.telemetryPanel.loading_telemetry')}</Notice>

  const offBody = data ? (
    <>
      {i18nT('pages.telemetryPanel.enable_with')}{' '}
      <code className="text-accent">{i18nT('pages.telemetryPanel.telemetry_enabled_true')}</code>
      {i18nT('pages.telemetryPanel.metrics_stay_local')}
      <code className="text-accent">{data.metrics_dir}</code>
      {i18nT('pages.telemetryPanel.nothing_leaves_this_machine')}
    </>
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
        <Notice>
          <div className="text-text font-medium mb-1">{i18nT('pages.telemetryPanel.telemetry_is_off')}</div>
          {offBody}
        </Notice>
      )
    }
    return (
      <div className="overflow-y-auto flex-1 min-h-0 pb-8">
        {offCost && <SpendTab c={offCost} />}
        {data.context && <ContextTab c={data.context} />}
        <div className="border border-border bg-card rounded-xl p-3 text-[11px] leading-relaxed">
          <span className="text-text font-medium">{i18nT('pages.telemetryPanel.telemetry_is_off')}</span>{' '}
          <span className="text-muted">{offBody}</span>
        </div>
      </div>
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
    return (
      <Notice>
        {i18nT('pages.telemetryPanel.no_telemetry_recorded_yet_in_the_last')} {data?.window_days ?? 14}{' '}
        {i18nT('pages.telemetryPanel.days')}
      </Notice>
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

  return (
    <div className="overflow-y-auto flex-1 min-h-0 pb-8">
      {/* Above the tabs, not after the active tab's table.
          The justification for this strip is that the fault rate is the one
          number that has to find the reader rather than be looked for — and
          placed at the bottom of a scroll container it was exactly as
          findable as the section it replaced, i.e. only by scrolling. An
          Activity Monitor bar sits at an EDGE; this is the edge that costs no
          sticky positioning inside an already-scrolling panel. */}
      <HealthBar t={t} days={data.window_days} />

      {segments.length > 1 && (
        <div className="mb-3">
          <SegmentedControl<Tab> segments={segments} value={active} onChange={setTab} />
        </div>
      )}

      {active === 'spend' && data.cost && <SpendTab c={data.cost} />}
      {active === 'context' && ctx && <ContextTab c={ctx} />}
      {active === 'latency' && <LatencyTab other={other} days={data.window_days} />}
      {active === 'startup' && s && <StartupTab s={s} faults={startupFaults} total={startupTotal} days={data.window_days} />}

      <div className="text-muted text-[11px] mt-2">
        {i18nT('pages.telemetryPanel.window_last')} {data.window_days}
        {i18nT('pages.telemetryPanel.d')} {data.shard_count} {i18nT('pages.telemetryPanel.shard_s_source')}{' '}
        <code>{data.metrics_dir}</code> {i18nT('pages.telemetryPanel.local_only_no_egress')}
      </div>
    </div>
  )
}
