/**
 * Telemetry panel: a histogram whose values are not milliseconds renders in its
 * own unit.
 *
 * Defect pinned here: the generic instrument table picks its histogram rows by
 * testing for `p50_ms` and formats every cell with `fmtMs`. A sampled resident
 * set arrives as `{kind: "histogram", unit: "By", p50: N}` and a CPU share as
 * `{unit: "1"}`, so a presence test on `p50_ms` sorts both into the counter
 * list, where a distribution shows as a bare sample count, and any cell that
 * does render puts a millisecond suffix on a byte count.
 *
 * The zero-sample case is pinned separately because it is the one the unit
 * decides and a percentile cannot: `_amount_stats` reports only `count` and
 * `unit` for a window with no samples, so a row keyed on `p50` being present
 * falls through to the millisecond formatter and prints a byte figure as "0ms".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import TelemetryPanel from '../pages/TelemetryPanel'

const stat = (over: Record<string, number> = {}) => ({
  count: 10, mean_ms: 100, p50_ms: 90, p90_ms: 200, min_ms: 10, max_ms: 300,
  other_generations: 0, total_count: 10, ...over,
})

const startup = () => ({
  overall: stat(), cold: stat(), warm: stat(),
  outcome: { ready: 10 },
  daily: [],
  distribution: { buckets: [0, 7, 3], bounds: [3000, 5000] },
  phases: [],
})

const resp = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  window_days: 14,
  shard_count: 3,
  metrics_dir: '/tmp/metrics',
  startup: startup(),
  turn: { ...stat({ count: 80 }), outcome: { ok: 80 }, fault_rate: 0 },
  context: null,
  other: [],
  ...over,
})

vi.mock('../api/client', () => ({
  api: { telemetryStartup: vi.fn() },
}))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>{children}</QueryClientProvider>
)

import { api } from '../api/client'

beforeEach(() => {
  qc.clear()
  vi.mocked(api.telemetryStartup).mockReset()
})

/** The row div that carries a named instrument's cells. */
const rowFor = (name: string) => screen.getByText(name).closest('div')!

describe('non-millisecond histogram rows', () => {
  it('renders a byte histogram in byte units, in its cells and its bar label', async () => {
    vi.mocked(api.telemetryStartup).mockResolvedValue(resp({
      other: [
        {
          name: 'kirocrew.process.memory.rss_sampled',
          kind: 'histogram',
          unit: 'By',
          count: 120,
          total: 180000000000,
          mean: 1500000000,
          p50: 1500000000,
          p90: 3200000000,
          min: 800000000,
          max: 4200000000,
          other_generations: 0,
          total_count: 120,
        },
      ],
    }) as never)

    render(<TelemetryPanel />, { wrapper: Wrapper })

    await waitFor(() => {
      expect(screen.getByText('kirocrew.process.memory.rss_sampled')).toBeInTheDocument()
    })
    const row = rowFor('kirocrew.process.memory.rss_sampled')
    // A byte reading in byte units. A millisecond suffix anywhere in this row is
    // the unit lie, whichever cell carries it.
    expect(row.textContent).toContain('GB')
    expect(row.textContent).not.toMatch(/\dms/)
    // The bar's accessible label is the fourth copy of the same four numbers, and
    // a screen reader is the only surface that reads it.
    const bar = within(row).getByRole('img')
    expect(bar.getAttribute('aria-label')).toContain('GB')
    expect(bar.getAttribute('aria-label')).not.toMatch(/\dms/)
  })

  it('renders a dimensionless share as a percentage', async () => {
    vi.mocked(api.telemetryStartup).mockResolvedValue(resp({
      other: [
        {
          name: 'kirocrew.process.cpu.utilization',
          kind: 'histogram',
          unit: '1',
          count: 300,
          total: 21,
          mean: 0.07,
          p50: 0.0625,
          p90: 0.42,
          min: 0.01,
          max: 0.94,
          other_generations: 0,
          total_count: 300,
        },
      ],
    }) as never)

    render(<TelemetryPanel />, { wrapper: Wrapper })

    await waitFor(() => {
      expect(screen.getByText('kirocrew.process.cpu.utilization')).toBeInTheDocument()
    })
    const row = rowFor('kirocrew.process.cpu.utilization')
    // A share of one machine's cores. 0.0625 of the cores is 6.3% of them; the
    // raw ratio reads as a rounding artefact. A whole share keeps no trailing
    // zero, so the maximum reads 94%.
    expect(row.textContent).toContain('6.3%')
    expect(row.textContent).toContain('94%')
    expect(row.textContent).not.toMatch(/\dms/)
  })

  it('renders a byte histogram with no samples in bytes, not as 0ms', async () => {
    vi.mocked(api.telemetryStartup).mockResolvedValue(resp({
      other: [
        // Exactly the shape `_amount_stats` reports for an empty window: a count,
        // a unit, and no percentile of any name.
        {
          name: 'kirocrew.process.memory.rss_sampled',
          kind: 'histogram',
          unit: 'By',
          count: 0,
        },
      ],
    }) as never)

    render(<TelemetryPanel />, { wrapper: Wrapper })

    await waitFor(() => {
      expect(screen.getByText('kirocrew.process.memory.rss_sampled')).toBeInTheDocument()
    })
    const row = rowFor('kirocrew.process.memory.rss_sampled')
    // The unit is the only thing that says which family this row belongs to, so
    // an empty window still reads in bytes.
    expect(row.textContent).not.toContain('0ms')
    expect(row.textContent).not.toMatch(/\dms/)
    expect(row.textContent).toContain('0B')
  })

  it('keeps the millisecond family on millisecond keys and formatting', async () => {
    vi.mocked(api.telemetryStartup).mockResolvedValue(resp({
      other: [
        {
          name: 'kirocrew.tool.call.duration',
          kind: 'histogram',
          count: 45,
          mean_ms: 120,
          p50_ms: 95,
          p90_ms: 240,
          min_ms: 12,
          max_ms: 610,
          other_generations: 0,
          total_count: 45,
        },
      ],
    }) as never)

    render(<TelemetryPanel />, { wrapper: Wrapper })

    await waitFor(() => {
      expect(screen.getByText('kirocrew.tool.call.duration')).toBeInTheDocument()
    })
    const row = rowFor('kirocrew.tool.call.duration')
    // A duration row carries no unit, and the millisecond formatter is what it
    // has always meant. Selecting rows by kind must not move it.
    expect(row.textContent).toContain('95ms')
    expect(row.textContent).toContain('240ms')
    const bar = within(row).getByRole('img')
    expect(bar.getAttribute('aria-label')).toMatch(/\dms/)
  })
})
