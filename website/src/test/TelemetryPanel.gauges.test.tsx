/**
 * Telemetry panel: gauge instruments render their `latest` sample.
 *
 * Defect pinned here: the API reports process gauges (threads, FDs, RSS) as
 * `{kind: "gauge", latest: N}` — a gauge has no meaningful `total`, because
 * summing point-in-time samples across export cycles fabricates a number that
 * grows with collection count. The panel used to fold every non-histogram row
 * under Counters and render `count ?? total ?? 0`, which displayed every gauge
 * as a hard zero while the API carried the correct value.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
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

describe('gauge instruments', () => {
  it('renders a gauge row under Gauges with its latest value, not zero', async () => {
    vi.mocked(api.telemetryStartup).mockResolvedValue(resp({
      other: [
        {
          name: 'kirocrew.process.threads.os',
          kind: 'gauge',
          latest: 72,
          by_attr: {},
        },
        {
          name: 'kirocrew.mcp.warm_pool.acquire',
          kind: 'counter',
          total: 4,
          by_attr: { 'result=hit': 3, 'result=miss': 1 },
        },
      ],
    }) as never)

    render(<TelemetryPanel />, { wrapper: Wrapper })

    await waitFor(() => {
      expect(screen.getByText('kirocrew.process.threads.os')).toBeInTheDocument()
    })
    const gaugeRow = screen.getByText('kirocrew.process.threads.os').closest('div')!
    // The one number a gauge carries is `latest` — 72 here. A zero would mean
    // the panel read count/total, the exact regression this test pins.
    expect(gaugeRow.textContent).toContain('72')
    expect(gaugeRow.textContent).not.toMatch(/\b0\b/)

    // The counter keeps its summed rendering untouched.
    const counterRow = screen.getByText('kirocrew.mcp.warm_pool.acquire').closest('div')!
    expect(counterRow.textContent).toContain('4')

    // Both section headings are present and distinct.
    expect(screen.getByText(/gauges/i)).toBeInTheDocument()
    expect(screen.getByText(/counters/i)).toBeInTheDocument()
  })

  it('formats byte gauges with units and renders the per-PID breakdown', async () => {
    vi.mocked(api.telemetryStartup).mockResolvedValue(resp({
      other: [
        {
          name: 'kirocrew.process.memory.rss_bytes',
          kind: 'gauge',
          latest: 4402341888,
          by_attr: { 'pid=5346': 4402341888, 'pid=9121': 287309824 },
        },
      ],
    }) as never)

    render(<TelemetryPanel />, { wrapper: Wrapper })

    await waitFor(() => {
      expect(screen.getByText('kirocrew.process.memory.rss_bytes')).toBeInTheDocument()
    })
    // A raw 4,402,341,888 forces digit-counting; byte gauges render in GB/GiB
    // units with the exact value preserved in the title attribute.
    expect(screen.queryByText(/4,402,341,888/)).not.toBeInTheDocument()
    // Headline and the pid=5346 row legitimately share the exact value.
    expect(screen.getAllByTitle('4402341888').length).toBeGreaterThan(0)
    // Multi-PID window: the API's per-process breakdown must be visible, not
    // silently collapsed into whichever process exported last.
    expect(screen.getByText('pid=5346')).toBeInTheDocument()
    expect(screen.getByText('pid=9121')).toBeInTheDocument()
  })
})
