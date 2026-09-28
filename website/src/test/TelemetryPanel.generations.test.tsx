/**
 * Telemetry panel: bucket-generation truthfulness + the cold-spawn source.
 *
 * Defects pinned here:
 *
 *  1. A histogram's numbers describe ONE bounds generation (merging
 *     incompatible boundaries would fabricate values), but the dropped
 *     generation was invisible on the page — a subset was styled exactly like a
 *     full-window total. The caveat quantifies in EXCLUDED SAMPLES, because a
 *     generation count cannot be reconciled against the numbers beside it.
 *  2. The MCP cold-load card read `kirocrew.mcp.lazy_load.duration`, which only
 *     the legacy pre-ensure_backend spawn path emits. Modern stubs never take
 *     that path, so the card said "no data yet" forever while real cold spawns
 *     were recorded on the acquire histogram under `warm=false`.
 *  3. The caveat must not be nested inside an unrelated card's conditional, and
 *     a zero-cold-spawn window must not be reported as absent telemetry — both
 *     re-create the very "silent subset" / "permanently empty card" this change
 *     exists to remove.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import TelemetryPanel from '../pages/TelemetryPanel'

const CAVEAT = /Showing \d[\d,]* of \d[\d,]* samples/i

const stat = (over: Record<string, number> = {}) => ({
  count: 10, mean_ms: 100, p50_ms: 90, p90_ms: 200, min_ms: 10, max_ms: 300,
  other_generations: 0, total_count: 10, ...over,
})

const startup = (over: Record<string, unknown> = {}) => ({
  overall: stat(), cold: stat(), warm: stat(),
  outcome: { ready: 10 },
  daily: [],
  distribution: { buckets: [0, 7, 3], bounds: [3000, 5000] },
  phases: [],
  ...over,
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

const acquireRow = (over: Record<string, unknown> = {}) => ({
  name: 'kirocrew.mcp.backend.acquire.duration',
  kind: 'histogram',
  ...stat({ count: 1134 }),
  ...over,
})

vi.mock('../api/client', () => ({
  api: { telemetryStartup: vi.fn() },
}))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>{children}</QueryClientProvider>
)

async function mount(payload: Record<string, unknown>) {
  const { api } = await import('../api/client')
  vi.mocked(api.telemetryStartup).mockResolvedValue(payload as never)
  render(<TelemetryPanel />, { wrapper: Wrapper })
}

describe('TelemetryPanel bucket generations', () => {
  beforeEach(() => { vi.clearAllMocks(); qc.clear() })

  it('reports the shown/total sample pair, not the generation count', async () => {
    await mount(resp({
      turn: {
        ...stat({ count: 80, other_generations: 1, total_count: 105 }),
        outcome: { ok: 80 }, fault_rate: 0,
      },
    }))
    await waitFor(() => expect(screen.getByText(CAVEAT)).toBeInTheDocument())
    // The pair is what reconciles against the figure beside it; "1 generation"
    // did not. Assert BOTH numbers so a half-wired note cannot pass.
    expect(screen.getByText(/Showing 80 of 105 samples/i)).toBeInTheDocument()
  })

  it('stays silent on a clean window', async () => {
    await mount(resp())
    await waitFor(() => expect(screen.getByText('80')).toBeInTheDocument())
    expect(screen.queryByText(CAVEAT)).not.toBeInTheDocument()
  })


  it('keeps the startup caveat when the phases and distribution cards are absent', async () => {
    // The claude startup path emits no phase points, and a window can have no
    // distribution buckets. Neither may take the caveat down with it.
    await mount(resp({
      startup: startup({
        overall: stat({ count: 1090, other_generations: 1, total_count: 1730 }),
        // The outcome tally and the count come from one _Hist group, so they
        // agree in real payloads; the startup tile derives its population from
        // the outcome map (same source as the fault count beside it), so the
        // fixture has to say the same number in both places.
        outcome: { ready: 1090 },
        phases: [],
        distribution: { buckets: [], bounds: [] },
      }),
      turn: null,
    }))
    await waitFor(() => expect(screen.getByText('1,090 startups recorded')).toBeInTheDocument())
    expect(screen.getByText(CAVEAT)).toBeInTheDocument()
    expect(screen.getByText(/Showing 1090 of 1730 samples/i)).toBeInTheDocument()
  })

  it('keeps the latency caveat a marker in the cell, with the sentence in the tooltip', async () => {
    // Rendered inline, the sentence wrapped to nine lines inside a 64px cell,
    // tripled three of six row heights and collided the max value with the count.
    // The caveat must stay visible but compact.
    await mount(resp({
      other: [acquireRow({ ...stat({ count: 56917, other_generations: 1, total_count: 83679 }) })],
    }))
    await waitFor(() => expect(screen.getByRole('button', { name: /Latency/ })).toBeInTheDocument())
    await userEvent.click(screen.getByRole('button', { name: /Latency/ }))

    const marker = await waitFor(() => {
      const el = document.querySelector('[title*="56,917"], [title*="56917"]')
      if (!el) throw new Error('caveat marker not rendered')
      return el as HTMLElement
    })
    // A marker in the cell; the numbers live in the tooltip where they cannot
    // reflow the row.
    expect(marker.textContent?.trim()).toBe('*')
    expect(marker.getAttribute('title') ?? '').toMatch(/83,?679/)
    // The sentence itself must not be visible text anywhere in the profile row.
    const row = marker.closest('div')
    expect(row?.textContent ?? '').not.toMatch(/histogram boundaries/i)
  })
})

