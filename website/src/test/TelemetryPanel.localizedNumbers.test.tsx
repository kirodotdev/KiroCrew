/**
 * Telemetry and Performance: numbers go through the shared formatters.
 *
 * Defects pinned here: the telemetry duration formatter built `1.5s` with
 * `toFixed`, so German and French showed a dot decimal and an English unit, and
 * a non-finite reading printed `NaNms`. The footer glued the shard count in
 * front of a `shard(s)` fragment, so one shard read "1 shard(s)" and no language
 * could pick its own plural. The Performance network caption printed
 * `max.toFixed(1)` with the unit written into the catalog text.
 */
import fs from 'node:fs'
import path from 'node:path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'

import { i18next } from '../i18n/index'
import '../i18n/all'
import { fmtUnit } from '../i18n/format'
import { i18nT } from '../i18n/t'
import TelemetryPanel from '../pages/TelemetryPanel'

const stat = (over: Record<string, number> = {}) => ({
  count: 10, mean_ms: 100, p50_ms: 90, p90_ms: 200, min_ms: 10, max_ms: 300,
  other_generations: 0, total_count: 10, ...over,
})

const resp = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  window_days: 14,
  shard_count: 1,
  metrics_dir: '/tmp/metrics',
  startup: {
    overall: stat(), cold: stat(), warm: stat(),
    outcome: { ready: 10 }, daily: [],
    distribution: { buckets: [0, 7, 3], bounds: [3000, 5000] }, phases: [],
  },
  turn: { ...stat({ count: 80 }), outcome: { ok: 80 }, fault_rate: 0 },
  context: null,
  other: [{
    name: 'kirocrew.tool.call.duration', kind: 'histogram', count: 45,
    mean_ms: 1500, p50_ms: 1500, p90_ms: 2400, min_ms: 12, max_ms: 3100,
    other_generations: 0, total_count: 45,
  }],
  ...over,
})

vi.mock('../api/client', () => ({ api: { telemetryStartup: vi.fn() } }))
import { api } from '../api/client'

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>{children}</QueryClientProvider>
)
const SRC = path.resolve(__dirname, '..')

beforeEach(() => {
  qc.clear()
  vi.mocked(api.telemetryStartup).mockReset()
})

afterEach(async () => {
  if (i18next.language !== 'en') await i18next.changeLanguage('en')
})

describe('telemetry numbers in the active language', () => {
  it('writes a German duration with a comma and a German unit', async () => {
    await i18next.changeLanguage('de')
    vi.mocked(api.telemetryStartup).mockResolvedValue(resp() as never)
    render(<TelemetryPanel />, { wrapper: Wrapper })
    await waitFor(() => expect(screen.getByText('kirocrew.tool.call.duration')).toBeInTheDocument())
    const row = screen.getByText('kirocrew.tool.call.duration').closest('div')!
    const expected = fmtUnit(1.5, 'second', { minimumFractionDigits: 1, maximumFractionDigits: 1 })
    expect(expected).toContain(',')
    expect(row.textContent).toContain(expected)
    expect(row.textContent).not.toContain('1.5s')
  })

  it('says one shard, not shard(s)', async () => {
    vi.mocked(api.telemetryStartup).mockResolvedValue(resp() as never)
    render(<TelemetryPanel />, { wrapper: Wrapper })
    await waitFor(() => expect(screen.getByText(/1 shard · source:/)).toBeInTheDocument())
    expect(screen.queryByText(/shard\(s\)/)).toBeNull()
  })

  it('shows a metrics path holding markup as plain text', async () => {
    const dir = '/tmp/</dir><dir style=color:red>x'
    vi.mocked(api.telemetryStartup).mockResolvedValue(resp({ metrics_dir: dir }) as never)
    render(<TelemetryPanel />, { wrapper: Wrapper })
    await waitFor(() => expect(screen.getByText(dir)).toBeInTheDocument())
    expect(screen.getByText(dir).tagName).toBe('CODE')
  })

  it('picks the Russian plural for the shard count', async () => {
    await i18next.changeLanguage('ru')
    const footer = (count: number) => i18nT('pages.telemetryPanel.window_footer_shards', { period: '14d', count, n: String(count) })
    expect(footer(1)).toContain('1 шард ·')
    expect(footer(3)).toContain('3 шарда ·')
    expect(footer(5)).toContain('5 шардов ·')
  })

  it('formats the Performance network scale and window through the unit formatter', async () => {
    await i18next.changeLanguage('de')
    const caption = i18nT('pages.performanceTab.graph_scale_network', {
      max: fmtUnit(1234.5, 'kilobyte-per-second', { maximumFractionDigits: 1 }),
      seconds: fmtUnit(60, 'second'),
    })
    expect(caption).toContain('1.234,5')
    expect(caption).not.toContain('KB/s')
    const src = fs.readFileSync(path.join(SRC, 'pages/system/PerformanceTab.tsx'), 'utf8')
    expect(src).not.toContain('max: max.toFixed(1)')
  })
})
