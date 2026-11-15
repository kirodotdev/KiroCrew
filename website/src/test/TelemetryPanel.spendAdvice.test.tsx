/**
 * Telemetry Spend: the recommendations list.
 *
 * The gateway sends ids and measured numbers; this tab words them. The list is
 * pinned on three things: each line carries its share of the window, a payload
 * with nothing to recommend (or from an older gateway) shows no list at all, and
 * an id this build does not know is skipped instead of rendering a blank line.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import TelemetryPanel from '../pages/TelemetryPanel'

const cost = (over: Record<string, unknown> = {}) => ({
  window_days: 7,
  credits: 1000,
  turns: 100,
  per_turn: 10,
  prior_credits: 500,
  prior_turns: 50,
  prior_per_turn: 8,
  delta_pct: 100,
  priciest: { credits: 90, slot: 'chat-1-1700000000', ts: '2026-08-05' },
  by_model: [],
  by_channel: [],
  by_category: [],
  context_bands: [],
  conversations: [],
  conversation_count: 0,
  navigable_category: 'dashboard',
  ...over,
})

vi.mock('../api/client', () => ({ api: { telemetryStartup: vi.fn(), usageTurns: vi.fn() } }))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>
    <MemoryRouter>{children}</MemoryRouter>
  </QueryClientProvider>
)

async function mount(over: Record<string, unknown> = {}) {
  const { api } = await import('../api/client')
  vi.mocked(api.telemetryStartup).mockResolvedValue({
    enabled: true,
    window_days: 7,
    shard_count: 1,
    metrics_dir: '/metrics',
    startup: null,
    turn: null,
    context: null,
    other: [],
    cost: cost(over),
  } as never)
  return render(<TelemetryPanel />, { wrapper: Wrapper })
}

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  localStorage.clear()
})

describe('TelemetryPanel — spend recommendations', () => {
  it('words each measured recommendation with its share of the window', async () => {
    await mount({
      advice: [
        { id: 'high_context', share_pct: 31, tokens: 600000 },
        { id: 'costly_model', share_pct: 22.4, model: 'big', cheaper_model: 'small', ratio: 4 },
      ],
    })
    expect(await screen.findByText('Where to save')).toBeInTheDocument()
    expect(screen.getByText('31% of spend is on turns above 600K of context: compact earlier')).toBeInTheDocument()
    expect(
      screen.getByText(
        '22% of spend is on big, which costs 4× as much per turn as small: use the cheaper model for routine work',
      ),
    ).toBeInTheDocument()
  })

  it('shows no list when nothing is measured', async () => {
    await mount({ advice: [] })
    await waitFor(() => expect(screen.getByText('Priciest turn')).toBeInTheDocument())
    expect(screen.queryByText('Where to save')).toBeNull()
  })

  it('shows no list for a gateway that sends no advice field', async () => {
    await mount()
    await waitFor(() => expect(screen.getByText('Priciest turn')).toBeInTheDocument())
    expect(screen.queryByText('Where to save')).toBeNull()
  })

  it('skips an id this build cannot word', async () => {
    await mount({ advice: [{ id: 'from_a_newer_gateway', share_pct: 40 }] })
    await waitFor(() => expect(screen.getByText('Priciest turn')).toBeInTheDocument())
    expect(screen.queryByText('Where to save')).toBeNull()
  })

  it('gives the dollar card of a mixed window its own list', async () => {
    await mount({
      advice: [],
      usd: cost({ unit: 'usd', advice: [{ id: 'high_context', share_pct: 45, tokens: 600000 }] }),
    })
    expect(await screen.findByText('45% of spend is on turns above 600K of context: compact earlier')).toBeInTheDocument()
    expect(screen.getAllByText('Where to save')).toHaveLength(1)
  })

  it('keeps a model name verbatim even when it looks like a placeholder', async () => {
    await mount({
      advice: [{ id: 'costly_model', share_pct: 50, model: '{{share}}', cheaper_model: '$t(x.y)', ratio: 2 }],
    })
    expect(await screen.findByText(/50% of spend is on \{\{share\}\}, which costs 2× as much per turn as \$t\(x\.y\)/)).toBeInTheDocument()
  })
})
