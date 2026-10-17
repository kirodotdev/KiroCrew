/** The Telemetry panel's data model: the `GET /api/telemetry/startup` payload
 *  shape (dashboard/handlers/telemetry.py), the measured window every card reads
 *  (`PanelWindowCtx`), and `usePersistedChoice`, the remembered window, tab
 *  and group-by choices. `TelemetryPanel.tsx` composes the panel and keeps
 *  `fmtMs`. */
import { createContext } from 'react'
import { usePersistedString } from '../hooks/usePersistedString'
import { fmtDate, fmtNumber } from '../i18n/format'
import { i18nT } from '../i18n/t'
import {
  inclusiveEnd,
  measuredQuery,
  rangeSearch,
} from './telemetryRange'

// ── GET /api/telemetry/startup shape (dashboard/handlers/telemetry.py) ──
export type Stat = {
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

export type Startup = {
  overall: Stat
  cold: Stat
  warm: Stat
  outcome: Record<string, number>
  daily: { date: string; count: number; cold_p50_ms: number; cold_p90_ms: number; warm_p50_ms: number }[]
  distribution: { buckets: number[]; bounds: number[] }
  phases: (Stat & { name: string })[]
  by_channel: (Stat & { name: string })[]
}

export type Turn = Stat & { outcome: Record<string, number>; fault_rate: number }

export type ContextSession = {
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

export type Context = {
  turns: number
  p50_pct: number
  p90_pct: number
  max_pct: number
  sessions: ContextSession[]
  window_days: number
}

export type Other = {
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
  // A histogram whose values are NOT milliseconds carries this family instead of
  // the `*_ms` one, plus the `unit` its values are in ("By" for a byte count,
  // "1" for a dimensionless ratio). The backend keeps the two families apart
  // because a resident set reported as `p50_ms` is a unit lie; the panel must
  // therefore read WHICHEVER family a row carries, or the distribution is
  // collected and then dropped at the render.
  unit?: string
  p50?: number
  p90?: number
  min?: number
  max?: number
  mean?: number
  // Present on gauge instruments only: the newest point-in-time sample.
  // Summing a gauge across export cycles would misreport process state, so
  // the API keeps the latest value and the panel must read THIS field.
  latest?: number
  by_attr?: Record<string, number>
  // Per-attribute sub-histograms, present only for the attribute keys the
  // backend splits on (_OTHER_SPLIT_ATTRS). Keyed "attr=value", e.g. "warm=false".
  splits?: Record<string, Stat>
}

export type CostRow = {
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

export type CostConvo = {
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

export type Cost = {
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

export type Resp = {
  enabled: boolean
  /** The effective window every block covers, after the gateway's clamp. */
  window_days: number
  window_start?: string
  window_end?: string
  /** True when the window ends now (a preset); false for a fixed custom range. */
  window_rolling?: boolean
  /**
   * True when no window was asked for: every block keeps main's own window, so
   * the spend block covers `cost_window_days` and the rest `window_days`.
   */
  window_default?: boolean
  cost_window_days?: number
  /** `telemetry.retention_days`: metric shards older than this are pruned (0 = never). */
  metrics_retention_days?: number
  shard_count: number
  metrics_dir: string
  startup: Startup | null
  turn: Turn | null
  context: Context | null
  cost: Cost | null
  other: Other[]
}

/**
 * How every card names the window it shows, worked out once from the payload.
 *
 * `period` is the short label in each card title ("Last 7d", or "Sep 1, 2026 –
 * Sep 3, 2026" for a custom range); `measured` is the spend and context cards'
 * source line. Both are read from the gateway's EFFECTIVE window, not from the
 * control, so a clamped pick is labelled with what was measured. `range` is the
 * query string for that same measured window, which the per-session turn
 * drill-down fetches with so it lists the same turns its row summed.
 */
type PanelWindow = { period: string; measured: string; range: string }

export const PanelWindowCtx = createContext<PanelWindow>({ period: '', measured: '', range: '' })

/** The title label for a rolling window of `days`. */
function rollingPeriod(days: number): string {
  return days === 1
    ? i18nT('pages.telemetryPanel.period_last_hours', { n: fmtNumber(24) })
    : i18nT('pages.telemetryPanel.credits_this_period', { days: fmtNumber(days) })
}

export function panelWindow(data: Resp | undefined, fallbackRange: string): PanelWindow {
  const days = data?.window_days ?? 0
  // The bounds the payload reports, not the request: a custom range ending
  // today sends a future `until` that each reader would clamp to its own now.
  const measured = data ? measuredQuery(data) : null
  const range = measured ? rangeSearch(measured) : fallbackRange
  // Decided by the payload, not by the control: while a new pick is loading the
  // previous payload stays on screen, and its label has to describe it.
  if (data?.window_rolling === false && data.window_start && data.window_end) {
    const label = i18nT('pages.telemetryPanel.window_range', {
      start: fmtDate(data.window_start),
      end: fmtDate(inclusiveEnd(data.window_end)),
    })
    return {
      period: label,
      measured: i18nT('pages.telemetryPanel.measured_from_token_records_range', { range: label }),
      range,
    }
  }
  return {
    period: rollingPeriod(days),
    measured: i18nT('pages.telemetryPanel.measured_from_token_records', { days: fmtNumber(days) }),
    range,
  }
}

/**
 * The window the OTEL cards (health strip, latency, startup) actually cover.
 * The exporter prunes metric shards after `telemetry.retention_days`, so when
 * the panel's window starts before that cutoff those cards hold less than the
 * panel window, and their titles name what they hold: "Last 14d" under a 30d
 * pick, or a range whose start is the cutoff day. `null` when nothing is cut.
 */
export function otelWindow(data: Resp, win: PanelWindow, nowMs: number): PanelWindow | null {
  const retention = data.metrics_retention_days ?? 0
  // Compared against the window's START, not its length: a 3-day range a month
  // ago is short but already pruned.
  const start = Date.parse(data.window_start ?? '')
  const cutoff = nowMs - retention * 86_400_000
  if (!(retention > 0) || !Number.isFinite(start) || start >= cutoff) return null
  if (data.window_rolling === false && data.window_end) {
    const end = inclusiveEnd(data.window_end)
    // A range wholly before the cutoff covers nothing; its start then names
    // the end day rather than a start after the end.
    const coveredStart = new Date(Math.min(cutoff, end.getTime()))
    return {
      ...win,
      period: i18nT('pages.telemetryPanel.window_range', { start: fmtDate(coveredStart), end: fmtDate(end) }),
    }
  }
  return { ...win, period: rollingPeriod(Math.min(retention, data.window_days)) }
}

/**
 * The spend card's window. With no pick (`window_default`) the spend block keeps
 * main's week while the rest of the panel keeps main's fortnight, so the card
 * titles itself, and its drill-down reads, over `cost_window_days`. After a pick
 * it is simply the panel's window.
 */
export function spendWindow(data: Resp, win: PanelWindow): PanelWindow {
  const days = data.cost_window_days
  if (!data.window_default || !(typeof days === 'number' && days > 0)) return win
  return {
    period: rollingPeriod(days),
    measured: i18nT('pages.telemetryPanel.measured_from_token_records', { days: fmtNumber(days) }),
    range: rangeSearch({ days }),
  }
}

/**
 * A string-union choice remembered across reloads, the same way a chosen sort is.
 *
 * The tab and both group-by controls were plain `useState`, so every visit reopened
 * on Spend grouped by session no matter what the reader had been looking at - while
 * the sort inside those very tables was remembered. The storage half is
 * `usePersistedString`, so this shares the quota-defensive write and the read-once
 * mount behaviour with every other remembered preference rather than re-spelling it.
 *
 * What this ADDS is validation. A stored value outlives the choices it named: this
 * page's own sort persistence carries a comment about a sort saved by an older column
 * layout surviving in localStorage, and a tab set changes the same way - the segment
 * list is already filtered by which data exists, so `startup` can be stored on a
 * machine that has no startup shard today. An unrecognised value falls back rather
 * than selecting nothing, which is what a segmented control does with a value it has
 * no segment for.
 *
 * Local to this panel on purpose: there is one consumer today (its call sites are in the panel's files).
 * If a second page needs it, it belongs beside `usePersistedBool` and
 * `usePersistedString` in `hooks/` rather than copied.
 */
export function usePersistedChoice<T extends string>(
  key: string,
  allowed: readonly T[],
  fallback: T,
): readonly [T, (next: T) => void] {
  // Delegates the storage half to `usePersistedString` rather than re-spelling its
  // initializer and write effect. What this adds is validation, and validating on
  // READ rather than correcting the stored value matters: a choice written by a
  // NEWER build (a tab this build does not have yet) is unknown here, so it shows
  // the fallback -- but it stays in storage and comes back intact on the newer
  // build, instead of being clobbered by whichever version mounted last.
  const [raw, setRaw] = usePersistedString(key, fallback)
  const value = (allowed as readonly string[]).includes(raw) ? (raw as T) : fallback
  return [value, setRaw as (next: T) => void] as const
}
