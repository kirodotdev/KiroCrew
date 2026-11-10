import { describe, it, expect, vi, afterEach } from 'vitest'
import { screen, within } from '@testing-library/react'
import { i18next } from '../i18n/index'
import '../i18n/all'
import { fmtUnit } from '../i18n/format'
import { renderWithProviders } from './helpers'
import { namedCeiling } from './namedCeiling'
import McpGatewayCard, { formatKb } from '../pages/McpGatewayCard'

vi.mock('../api/client', () => ({
  api: {
    // Running but not answering its ping: the "unhealthy" state.
    mcpGatewayStatus: vi.fn().mockResolvedValue({ enabled: true, running: true, ping_ok: false }),
    mcpGatewayMetrics: vi.fn().mockResolvedValue({
      running: true,
      max_backends: 8,
      backends: [{ server: 'slack-mcp', pid: 41, stubs: 2, idle_s: 12.3, rss_kb: 2912 * 1024 }],
      warm_pool_hits: 2,
      warm_pool_misses: 1,
      warm_pool_hit_rate_pct: 67,
    }),
  },
}))

const previous = i18next.language
afterEach(async () => {
  await i18next.changeLanguage(previous)
})

describe('McpGatewayCard in the active language', () => {
  it('formats the card in English', async () => {
    await i18next.changeLanguage('en')
    renderWithProviders(<McpGatewayCard />)
    expect(await screen.findByText('unhealthy')).toBeInTheDocument()
    expect(await screen.findByText('2.8 GB (50%)')).toBeInTheDocument()
    expect(screen.getByText('67% (2/3)')).toBeInTheDocument()
    const row = screen.getByRole('row', { name: /slack-mcp/ })
    expect(within(row).getAllByRole('cell').map(c => c.textContent)).toContain('12.3s')
  })

  it('names the state and formats sizes, percents and idle time in German', async () => {
    await i18next.changeLanguage('de')
    renderWithProviders(<McpGatewayCard />)
    expect(await screen.findByText('gestört')).toBeInTheDocument()
    expect(await screen.findByText(/^2,8 GB \(50[\u00A0\u202F ]%\)$/)).toBeInTheDocument()
    expect(screen.getByText(/^67[\u00A0\u202F ]% \(2\/3\)$/)).toBeInTheDocument()
    const row = screen.getByRole('row', { name: /slack-mcp/ })
    // German's own short unit for seconds, after a comma decimal.
    const idle = fmtUnit(12.3, 'second', { maximumFractionDigits: 1 })
    expect(idle).toMatch(/^12,3/)
    expect(within(row).getAllByRole('cell').map(c => c.textContent)).toContain(idle)
  })
})

describe('McpGatewayCard counts in a language with its own digits', () => {
  it('writes the backend and stub counts in Bengali digits, like the sizes beside them (#33230)', async () => {
    await i18next.changeLanguage('bn')
    renderWithProviders(<McpGatewayCard />)
    // The size tile is already Bengali (২.৮); the counts must match it. The
    // metrics query waits on the status query, so this wait spans both.
    const sizes = await screen.findAllByText(/^২\.৮ GB/, {}, namedCeiling('MCP_STATUS_THEN_METRICS', 5000))
    expect(sizes.length).toBeGreaterThan(0)
    const values = screen.getAllByTestId('stat-card-value').map(v => v.textContent)
    expect(values).toContain('১ / ৮')
    expect(values).toContain('২')
    const row = screen.getByRole('row', { name: /slack-mcp/ })
    const cells = within(row).getAllByRole('cell').map(c => c.textContent)
    expect(cells[2]).toBe('২')
    // The pid is an identifier and stays as the gateway reported it.
    expect(cells[1]).toBe('41')
  })
})

describe('formatKb in the active language', () => {
  it('uses the locale decimal separator and no digit grouping', async () => {
    await i18next.changeLanguage('de')
    expect(formatKb(2912 * 1024)).toBe('2,8 GB')
    expect(formatKb(1023)).toBe('1023 KB')
  })
})
