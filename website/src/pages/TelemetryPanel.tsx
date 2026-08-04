import { useQuery } from '@tanstack/react-query'
import { Activity, Coins, Gauge, MessageSquare, Rocket } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api } from '../api/client'

import { fmtDateNumeric, fmtNumber, fmtPercent, fmtUnit } from '../i18n/format'
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
  context_bands: CostBand[]
  conversations: CostConvo[]
  conversation_count: number
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

function Notice({ children }: { children: React.ReactNode }) {
  return <div className="text-muted text-sm py-12 text-center leading-relaxed">{children}</div>
}

function Section({ title, icon, children }: { title: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="mb-7">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-muted mb-3 flex items-center gap-1.5">
        {icon}
        {title}
      </h3>
      {children}
    </div>
  )
}

// One KPI tile: label + big value + optional sub + optional accent color.
function Tile({
  label,
  value,
  unit,
  sub,
  note,
  color,
}: {
  label: string
  value: string
  unit?: string
  sub?: string
  note?: React.ReactNode
  color?: string
}) {
  return (
    <div className="card-glow border border-border bg-card rounded-xl p-3.5">
      <div className="text-[10px] text-muted mb-1">{label}</div>
      <div className="text-[22px] font-bold leading-none" style={color ? { color } : undefined}>
        {value}
        {unit && <span className="text-[11px] text-muted font-normal ml-0.5">{unit}</span>}
      </div>
      {sub && <div className="text-[10px] text-muted mt-1.5">{sub}</div>}
      {note}
    </div>
  )
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
function GenNote({ shown, total }: { shown?: number; total?: number }) {
  if (shown == null || total == null || total <= shown) return null
  return (
    <div className="text-[10px] mt-1" style={{ color: 'var(--warn)' }}>
      {i18nT('pages.telemetryPanel.showing_partial_window', { shown, total })}
    </div>
  )
}


/**
 * Occupancy colour thresholds. Compaction triggers at 90% of the window, so
 * that is the danger line rather than an arbitrary "nearly full" guess; 70% is
 * the point where a long session still has room but is worth watching. Below
 * 70% stays on the neutral accent (matching the sibling distribution bars) —
 * painting the healthy majority green would make a wall of green in which
 * amber and red no longer read as signal.
 */
const occColor = (p: number): string =>
  p >= 90 ? 'var(--danger)' : p >= 70 ? 'var(--warn)' : 'var(--accent)'


/**
 * Per-turn context-window occupancy, read from the token row store rather than
 * the OTEL shards (see api_telemetry_startup): a per-session ratio keyed by an
 * unbounded slot id is not a metric label. Rendered whether or not OTEL export
 * is on, because these rows are always written.
 */
/** Bars carry magnitude by length; hue is reserved for the one alarm on the page. */
const BAR = 'var(--muted-strong)'

/**
 * One grid template shared by the model and channel tables AND their headers.
 *
 * Both tables previously laid out with flex and omitted the delta cell for
 * channels, so the flexible bar absorbed that width and the two tables' columns
 * landed in different places. A fixed template makes misalignment impossible,
 * and every cell is always rendered — empty where a column does not apply —
 * so a missing value cannot reflow the row.
 *
 * The track list is an inline `gridTemplateColumns` rather than a
 * `grid-cols-[...]` arbitrary value: Tailwind's arbitrary syntax needs `_` as its
 * space substitute, and `_` is outside the class-string shape the i18n lint
 * exempts, so the literal reads as untranslated copy to a gate whose added-line
 * tolerance is zero. `1fr` plus `min-w-0` on the bar cell gives what
 * `minmax(0,1fr)` did — the comma would fall outside that shape too.
 */
const SHARE_CLS = 'grid items-center gap-2.5'
const SHARE_COLS = '168px 1fr 52px 76px 60px 104px'

// The spend ranking carries different columns from the share tables, so it gets
// its own template — but the same rule: every cell is a grid track, so a value
// that is absent for one row cannot shift the columns of the others. The two
// `hidden` breakpoints are repeated on the header cells so a column and its
// label always disappear together.
const CONVO_CLS = 'grid items-center gap-2.5'
const CONVO_COLS = '224px 1fr 64px 40px 62px 48px 160px'

function ConvoHeader() {
  return (
    <div
      className={`${CONVO_CLS} text-[10px] text-muted uppercase tracking-wide `
        + 'pb-1.5 mb-1 border-b border-border'}
      style={{ gridTemplateColumns: CONVO_COLS }}
    >
      <span>{i18nT('pages.telemetryPanel.conversation_col')}</span>
      <span />
      <span className="text-right">{i18nT('pages.telemetryPanel.credits_col')}</span>
      <span className="text-right">{i18nT('pages.telemetryPanel.turns_col')}</span>
      <span className="text-right">{i18nT('pages.telemetryPanel.peak_ctx_col')}</span>
      <span className="text-right max-[720px]:hidden">
        {i18nT('pages.telemetryPanel.span_col')}
      </span>
      <span className="text-right max-[900px]:hidden">
        {i18nT('pages.telemetryPanel.growth_col')}
      </span>
    </div>
  )
}

function ShareHeader({ first }: { first: string }) {
  return (
    <div
      className={`${SHARE_CLS} text-[10px] text-muted uppercase tracking-wide `
        + 'pb-1.5 mb-1 border-b border-border'}
      style={{ gridTemplateColumns: SHARE_COLS }}
    >
      <span>{first}</span>
      <span />
      <span className="text-right">{i18nT('pages.telemetryPanel.share_col')}</span>
      <span className="text-right">{i18nT('pages.telemetryPanel.credits_col')}</span>
      <span className="text-right">{i18nT('pages.telemetryPanel.per_turn_col')}</span>
      <span className="text-right">{i18nT('pages.telemetryPanel.vs_last_col')}</span>
    </div>
  )
}

function ShareRows({ rows, showDelta }: { rows: CostRow[]; showDelta?: boolean }) {
  const peak = Math.max(1, ...rows.map(r => r.share_pct))
  return (
    <>
      {rows.map(r => (
        <div key={r.name} className={`${SHARE_CLS} py-[3px] text-[11px]`}
             style={{ gridTemplateColumns: SHARE_COLS }}>
          <span className="min-w-0 truncate" title={r.name}>{r.name}</span>
          <div className="h-1.5 min-w-0 bg-[var(--bg)] rounded-full overflow-hidden">
            <span className="block h-full rounded-full"
                  style={{ width: `${(r.share_pct / peak) * 100}%`, background: BAR }} />
          </div>
          <span className="text-right tabular-nums">
            {/* Composed, not a catalog string: "<1%" carries no letter in any
                language, so it is built from the locale-formatted 1% instead. */}
            {r.share_pct > 0 && r.share_pct < 0.5
              ? `<${fmtPercent(0.01)}`
              : fmtPercent(r.share_pct / 100)}
          </span>
          <span className="text-right tabular-nums">{fmtNumber(r.credits)}</span>
          <span className="text-right tabular-nums">{fmtNumber(r.per_turn)}</span>
          <span className="text-right tabular-nums text-muted">
            {!showDelta
              ? ''
              : r.delta_pct == null
                ? i18nT('pages.telemetryPanel.cost_new')
                : (r.delta_pct > 0 ? '+' : '') + fmtPercent(r.delta_pct / 100)}
          </span>
        </div>
      ))}
    </>
  )
}

function CostSections({ c }: { c: Cost }) {
  const bandPeak = Math.max(1, ...c.context_bands.map(b => b.mean_credits))
  const convoPeak = Math.max(1, ...c.conversations.map(v => v.credits))
  return (
    <>
      <Section title={i18nT('pages.telemetryPanel.credits')} icon={<Coins size={13} />}>
        {/* Names this section's source AND window. The OTEL-fed sections below
            run on a different store over a different window, so without this the
            two turn counts read as one number contradicting itself. */}
        <div className="text-[10px] text-muted -mt-1.5 mb-2.5">
          {i18nT('pages.telemetryPanel.measured_from_token_records', { days: fmtNumber(c.window_days) })}
        </div>
        <div className="grid gap-2.5 grid-cols-[repeat(auto-fit,minmax(140px,1fr))] mb-3">
          <Tile
            label={i18nT('pages.telemetryPanel.credits_this_period', { days: fmtNumber(c.window_days) })}
            value={fmtNumber(c.credits)}
            color="var(--accent)"
            sub={i18nT('pages.telemetryPanel.turns_measured', { count: c.turns, n: fmtNumber(c.turns) })}
          />
          <Tile
            label={i18nT('pages.telemetryPanel.vs_previous_period')}
            value={c.delta_pct == null
              ? '—'
              : (c.delta_pct > 0 ? '+' : '') + fmtPercent(c.delta_pct / 100)}
            color="var(--accent)"
            sub={`${fmtNumber(c.prior_credits)} · ${fmtNumber(c.prior_turns)}`}
          />
          <Tile
            label={i18nT('pages.telemetryPanel.credits_per_turn')}
            value={fmtNumber(c.per_turn)}
            sub={i18nT('pages.telemetryPanel.was_value', { value: fmtNumber(c.prior_per_turn) })}
          />
          <Tile
            label={i18nT('pages.telemetryPanel.priciest_turn')}
            value={fmtNumber(c.priciest.credits)}
          />
        </div>
        <div className="card-glow border border-border bg-card rounded-xl p-3.5">
          <ShareHeader first={i18nT('pages.telemetryPanel.by_model')} />
          <ShareRows rows={c.by_model} showDelta />
          <div className="mt-3">
            <ShareHeader first={i18nT('pages.telemetryPanel.by_channel')} />
          </div>
          <ShareRows rows={c.by_channel} showDelta />
        </div>
      </Section>

      {c.context_bands.length > 0 && (
        <Section title={i18nT('pages.telemetryPanel.cost_by_context_size')} icon={<Activity size={13} />}>
          <div className="card-glow border border-border bg-card rounded-xl p-3.5">
            {c.context_bands.map(b => (
              <div key={b.label} className="flex items-center gap-2.5 py-[3px] text-[11px]">
                <span className="w-24 shrink-0 text-muted">{b.label}</span>
                <div className="flex-1 h-1.5 bg-[var(--bg)] rounded-full overflow-hidden">
                  <span className="block h-full rounded-full"
                        style={{ width: `${(b.mean_credits / bandPeak) * 100}%`, background: BAR }} />
                </div>
                <span className="w-14 text-right shrink-0 tabular-nums">{fmtNumber(b.mean_credits)}</span>
                <span className="w-14 text-right shrink-0 text-muted tabular-nums">{i18nT('pages.telemetryPanel.sample_count', { count: fmtNumber(b.turns) })}</span>
              </div>
            ))}
            <div className="text-[10px] text-muted mt-2">{i18nT('pages.telemetryPanel.mean_credits_by_occupancy')}</div>
          </div>
        </Section>
      )}

      {c.conversations.length > 0 && (
        <Section
          title={i18nT('pages.telemetryPanel.conversations_by_spend')}
          icon={<MessageSquare size={13} />}
        >
          <div className="card-glow border border-border bg-card rounded-xl p-3.5">
            <ConvoHeader />
            <div className="flex flex-col gap-1.5">
              {c.conversations.map(v => (
                <div key={v.slot} className={`${CONVO_CLS} text-[11px]`}
                     style={{ gridTemplateColumns: CONVO_COLS }}>
                  <span className="min-w-0">
                    {/* Only a conversation that is still open can be navigated
                        to: ChatPage resolves ?sid against the live slot list and
                        otherwise reports `Session "…" not found` after a 5s
                        timeout. An unnamed row is unnamed BECAUSE the slot is
                        gone, so linking it would buy the user a delayed error --
                        it renders as plain text, which is also what the caption
                        below promises. Open rows use a router link so the click
                        switches slots instead of reloading the whole app. */}
                    {v.title ? (
                      <Link to={`/chat?sid=${encodeURIComponent(v.slot)}`}
                            className="block truncate text-[var(--accent)] hover:underline"
                            title={v.title}>{v.title}</Link>
                    ) : (
                      <span className="block truncate text-muted" title={v.slot}>
                        {i18nT('pages.telemetryPanel.untitled_conversation_on', { date: fmtDateNumeric(v.first_ts * 1000) })}
                      </span>
                    )}
                  </span>
                  <div className="h-1.5 min-w-0 bg-[var(--bg)] rounded-full overflow-hidden">
                    <span className="block h-full rounded-full"
                          style={{ width: `${(v.credits / convoPeak) * 100}%`, background: BAR }} />
                  </div>
                  <span className="text-right tabular-nums">{fmtNumber(v.credits)}</span>
                  <span className="text-right tabular-nums text-muted">{fmtNumber(v.turns)}</span>
                  <span className="text-right tabular-nums"
                        style={{ color: occColor(v.peak_pct) }}>{fmtPercent(v.peak_pct / 100)}</span>
                  <span className="text-right tabular-nums text-muted max-[720px]:hidden">
                    {fmtUnit(v.span_days, 'day')}
                  </span>
                  <span className="text-right text-muted max-[900px]:hidden">
                    {v.growth_pct_per_turn == null || v.turns_to_compaction == null
                      ? '—'
                      : i18nT('pages.telemetryPanel.context_growth_forecast', {
                          rate: fmtNumber(v.growth_pct_per_turn),
                          turns: fmtNumber(v.turns_to_compaction),
                        })}
                  </span>
                </div>
              ))}
            </div>
            <div className="text-[10px] text-muted mt-2">
              {i18nT('pages.telemetryPanel.conversations_caption', { count: fmtNumber(c.conversation_count) })}
            </div>
          </div>
        </Section>
      )}
    </>
  )
}

function ContextSection({ c }: { c: Context }) {
  return (
    <Section title={i18nT('pages.telemetryPanel.context_window')} icon={<Gauge size={13} />}>
      {/* Names the source explicitly AND bounds it in time: this section is the
          one part of the page that survives telemetry.enabled=false, where the
          "Window: last Nd" footer below is not rendered — so without the window
          here the turn count reads as unbounded, and the page looks
          self-contradictory next to the "telemetry is off" banner. */}
      <div className="text-[10px] text-muted -mt-1.5 mb-2.5">{i18nT('pages.telemetryPanel.measured_from_token_records', { days: fmtNumber(c.window_days) })}</div>
      <div className="grid gap-2.5 grid-cols-[repeat(auto-fit,minmax(140px,1fr))] mb-3">
        <Tile
          label={i18nT('pages.telemetryPanel.occupancy_p50')}
          value={String(c.p50_pct)}
          unit="%"
          color={occColor(c.p50_pct)}
          sub={i18nT('pages.telemetryPanel.turns_measured', { count: c.turns, n: fmtNumber(c.turns) })}
        />
        <Tile
          label={i18nT('pages.telemetryPanel.occupancy_p90')}
          value={String(c.p90_pct)}
          unit="%"
          color={occColor(c.p90_pct)}
        />
        <Tile
          label={i18nT('pages.telemetryPanel.peak_occupancy')}
          value={String(c.max_pct)}
          unit="%"
          color={occColor(c.max_pct)}
        />
      </div>
    </Section>
  )
}

export default function TelemetryPanel() {
  const { data, isLoading } = useQuery<Resp>({
    queryKey: ['telemetry-startup'],
    queryFn: () => api.telemetryStartup(),
    refetchInterval: 5000,
  })

  if (isLoading && !data) return <Notice>{i18nT('pages.telemetryPanel.loading_telemetry')}</Notice>
  if (data && !data.enabled) {
    // Context occupancy AND credit spend both come from the token row store,
    // which is written regardless of the OTEL switch — so show them rather than
    // an empty page. This matters more than it looks: `telemetry.enabled`
    // defaults to false, so without this branch the whole spend surface ships
    // invisible to anyone who never turned the switch on.
    // With real data on screen the off-state is a compact banner, not the
    // centered empty-state block: a full-page "nothing here" under live
    // numbers makes the page contradict itself.
    const offBody = (
      <>
        {i18nT('pages.telemetryPanel.enable_with')} <code className="text-accent">{i18nT('pages.telemetryPanel.telemetry_enabled_true')}</code>{i18nT('pages.telemetryPanel.metrics_stay_local')}
        <code className="text-accent">{data.metrics_dir}</code>{i18nT('pages.telemetryPanel.nothing_leaves_this_machine')}
      </>
    )
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
        {offCost && <CostSections c={offCost} />}
        {data.context && <ContextSection c={data.context} />}
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
  const hasData = !!(s && s.overall.count) || !!(t && t.count) || !!ctx
    || other.length > 0 || !!(data?.cost && data.cost.turns)
  if (!data || !hasData) {
    return <Notice>{i18nT('pages.telemetryPanel.no_telemetry_recorded_yet_in_the_last')} {data?.window_days ?? 14} {i18nT('pages.telemetryPanel.days')}</Notice>
  }


  // Startup health as an absolute count, not a rate. A rate over this
  // denominator cannot report a failure: the window measured 1411 startups, so
  // one failed startup is 99.93% — which Math.round takes straight back to a
  // perfect "100%". That is the same rounding erasure fixed on the fault-rate
  // tile below, made worse by a denominator ~3x larger, and it saturated: a
  // ready rate had no reachable value between "100%" and a visible problem. A
  // count has no such ceiling — the first real failure moves 0 to 1.
  const startupTotal = s ? Object.values(s.outcome).reduce((a, b) => a + b, 0) : 0
  const startupFaults = s
    ? Object.entries(s.outcome).reduce((n, [k, v]) => (k === 'ready' ? n : n + v), 0)
    : 0
  // Count faults the way the API computes fault_rate: everything that is not
  // "ok". Naming the failure outcomes explicitly (error + timeout) dropped any
  // other value — including the "unknown" that shards predating the attribute
  // aggregate under — so the tile could show a rate over one population beside
  // a count over another, and a fault in a third outcome read as zero faults.
  const turnFaults = t
    ? Object.entries(t.outcome).reduce((n, [k, v]) => (k === 'ok' ? n : n + v), 0)
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

  // Startup latency distribution: only render non-empty buckets.
  const distRows: { label: string; count: number }[] = []
  if (s?.distribution?.buckets?.length) {
    const { buckets, bounds } = s.distribution
    buckets.forEach((c, i) => {
      if (c > 0) {
        const label = i >= bounds.length ? `> ${fmtMs(bounds[bounds.length - 1])}` : `≤ ${fmtMs(bounds[i])}`
        distRows.push({ label, count: c })
      }
    })
  }
  const distMax = Math.max(1, ...distRows.map(r => r.count))

  return (
    <div className="overflow-y-auto flex-1 min-h-0 pb-8">
      {data.cost && <CostSections c={data.cost} />}

      {/* ── Section 1: Key metrics ─────────────────────────────── */}
      <Section title={i18nT('pages.telemetryPanel.key_metrics')} icon={<Activity size={13} />}>
        <div className="grid gap-2.5 grid-cols-[repeat(auto-fit,minmax(150px,1fr))]">
          <Tile
            label={i18nT('pages.telemetryPanel.turn_latency_p50')}
            value={t ? fmtMs(t.p50_ms) : '—'}
            sub={t ? `p90 ${fmtMs(t.p90_ms)}` : 'no turns yet'}
            color="var(--accent)"
          />
          <Tile
            label={i18nT('pages.telemetryPanel.fault_rate')}
            value={faultLabel}
            unit={faultPct == null ? undefined : '%'}
            sub={t
              ? i18nT('pages.telemetryPanel.turn_faults', {
                  count: turnFaults,
                  n: fmtNumber(turnFaults),
                  turns: fmtNumber(t.count),
                })
              : 'no turns yet'}
            color={faultColor}
          />
          <Tile label={i18nT('pages.telemetryPanel.throughput')} value={t ? String(t.count) : '—'} unit={t ? 'turns' : undefined} sub={`last ${data.window_days}d`} />
          <Tile
            label={i18nT('pages.telemetryPanel.startup_faults')}
            value={s ? fmtNumber(startupFaults) : '—'}
            sub={s ? i18nT('pages.telemetryPanel.startups_recorded', { count: startupTotal, n: fmtNumber(startupTotal) }) : undefined}
            color={s ? (startupFaults === 0 ? 'var(--ok)' : 'var(--danger)') : undefined}
          />
        </div>
        {t && (t.outcome.ok ?? 0) + turnFaults > 0 && (
          <div className="mt-2.5">
            <div className="flex h-2 rounded-md overflow-hidden border border-border">
              <span style={{ flex: t.outcome.ok ?? 0, background: 'var(--ok)' }} />
              <span style={{ flex: t.outcome.error ?? 0, background: 'var(--danger)' }} />
              <span style={{ flex: t.outcome.timeout ?? 0, background: 'var(--warn)' }} />
            </div>
            <div className="flex gap-4 mt-1 text-[10px] text-muted">
              <span>{i18nT('pages.telemetryPanel.ok')} {t.outcome.ok ?? 0}</span>
              <span>{i18nT('pages.telemetryPanel.error')} {t.outcome.error ?? 0}</span>
              <span>{i18nT('pages.telemetryPanel.timeout')} {t.outcome.timeout ?? 0}</span>
            </div>
          </div>
        )}
        {t && <GenNote shown={t.count} total={t.total_count} />}
        {!t && (
          <div className="text-muted text-[11px] mt-2">
            {i18nT('pages.telemetryPanel.agent_turn_latency_fault_rate_populate_after_the')}
          </div>
        )}
      </Section>

      {/* ── Section 2: Session startup ─────────────────────────── */}
      {s && s.overall.count > 0 && (
        <Section title={i18nT('pages.telemetryPanel.session_startup')} icon={<Rocket size={13} />}>
          <div className="grid gap-2.5 grid-cols-[repeat(auto-fit,minmax(140px,1fr))] mb-3">
            <Tile label={i18nT('pages.telemetryPanel.cold_start_p50')} value={fmtMs(s.cold.p50_ms)} color="var(--accent)" sub={`${s.cold.count} cold`} />
            <Tile label={i18nT('pages.telemetryPanel.cold_start_p90')} value={fmtMs(s.cold.p90_ms)} color="var(--warn)" />
            <Tile label={i18nT('pages.telemetryPanel.warm_start_p50')} value={fmtMs(s.warm.p50_ms)} color="var(--ok)" sub={`${s.warm.count} warm`} />
            <Tile label={i18nT('pages.telemetryPanel.overall_mean')} value={fmtMs(s.overall.mean_ms)} sub={`min ${fmtMs(s.overall.min_ms)} · max ${fmtMs(s.overall.max_ms)}`} />
          </div>
          {/* Directly under the tiles it qualifies, and OUTSIDE the phases /
              distribution conditionals below: nesting the caveat inside either
              of those meant the truncated tiles rendered caveat-free whenever
              that unrelated card was absent (no phase points on the claude
              startup path, or empty distribution buckets). */}
          <div className="-mt-1 mb-3">
            <GenNote shown={s.overall.count} total={s.overall.total_count} />
          </div>
          {s.phases?.length > 0 && (
            <div className="card-glow border border-border bg-card rounded-xl p-3.5 mb-3">
              <div className="text-[10px] text-muted mb-2">{i18nT('pages.telemetryPanel.internal_phase_breakdown')}</div>
              <div className="grid gap-2.5 grid-cols-[repeat(auto-fit,minmax(120px,1fr))]">
                {s.phases.map(p => (
                  <div key={p.name}>
                    <div className="text-[10px] text-muted font-mono">{p.name}</div>
                    <div className="text-[15px] font-bold">{fmtMs(p.p50_ms)}</div>
                    <div className="text-[10px] text-muted">p90 {fmtMs(p.p90_ms)}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
          {s.by_channel?.length > 0 && (
            <div className="card-glow border border-border bg-card rounded-xl p-3.5 mb-3">
              <div className="text-[10px] text-muted mb-2">{i18nT('pages.telemetryPanel.startup_by_channel')}</div>
              <div className="grid gap-2.5 grid-cols-[repeat(auto-fit,minmax(120px,1fr))]">
                {s.by_channel.map(c => (
                  <div key={c.name}>
                    <div className="text-[10px] text-muted font-mono">{c.name}</div>
                    <div className="text-[15px] font-bold">{fmtMs(c.p50_ms)}</div>
                    <div className="text-[10px] text-muted">{i18nT('pages.telemetryPanel.p50_p90_with_sample_count', { p90: fmtMs(c.p90_ms), count: fmtNumber(c.count) })}</div>
                  </div>
                ))}
              </div>
            </div>
          )}
          {distRows.length > 0 && (
            <div className="card-glow border border-border bg-card rounded-xl p-3.5">
              <div className="text-[10px] text-muted mb-2">{i18nT('pages.telemetryPanel.startup_latency_distribution_from_otel_histogram')}</div>
              <div className="flex flex-col gap-1.5">
                {distRows.map(r => (
                  <div key={r.label} className="flex items-center gap-2 text-[10px]">
                    <span className="text-muted w-16 shrink-0 text-right font-mono">{r.label}</span>
                    <div className="flex-1 h-3 bg-[var(--bg)] rounded overflow-hidden">
                      <span className="block h-full rounded" style={{ width: `${(r.count / distMax) * 100}%`, background: 'var(--accent)' }} />
                    </div>
                    <span className="text-text font-mono w-6 text-right shrink-0">{r.count}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Section>
      )}

      {/* ── Section 3: Context window ──────────────────────────── */}
      {ctx && <ContextSection c={ctx} />}


      <div className="text-muted text-[11px] mt-2">
        {i18nT('pages.telemetryPanel.window_last')} {data.window_days}{i18nT('pages.telemetryPanel.d')} {data.shard_count} {i18nT('pages.telemetryPanel.shard_s_source')} <code>{data.metrics_dir}</code> {i18nT('pages.telemetryPanel.local_only_no_egress')}
      </div>
    </div>
  )
}
