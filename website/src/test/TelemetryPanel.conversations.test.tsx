/**
 * Telemetry panel: a conversation row may only be a link when it can be opened.
 *
 * `ChatPage` resolves `?sid=` against the LIVE slot list
 * (`filteredSlots.some(s => s.key === urlSid)`) and, when the slot is absent,
 * reports `Session "…" not found` from a 5s timer. A row is unnamed precisely
 * BECAUSE its slot is gone — titles live on the in-memory slot and are not
 * persisted — so rendering those rows as links pointed the user's click at a
 * guaranteed error that arrives five seconds later. Roughly half a real ranking
 * is unnamed, which made the failure the common case rather than an edge.
 *
 * The open rows navigate through the router rather than a bare `href`: the app
 * switches slots in place everywhere else, and a plain anchor reloads the whole
 * SPA to reach the same destination.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import TelemetryPanel from '../pages/TelemetryPanel'

const convo = (over: Record<string, unknown> = {}) => ({
  slot: 'chat-1-1700000000',
  channel: 'dashboard',
  category: 'dashboard',
  credits: 100,
  turns: 10,
  peak_pct: 42,
  span_days: 1,
  first_ts: 1700000000,
  growth_pct_per_turn: null,
  turns_to_compaction: null,
  ...over,
})

const resp = (conversations: Record<string, unknown>[]) => ({
  enabled: true,
  window_days: 14,
  shard_count: 3,
  metrics_dir: '/tmp/metrics',
  startup: null,
  turn: null,
  context: null,
  other: [],
  cost: {
    window_days: 7,
    credits: 1000,
    turns: 100,
    per_turn: 10,
    prior_credits: 500,
    prior_turns: 50,
    prior_per_turn: 10,
    delta_pct: 100,
    by_category: [],
  priciest: { credits: 50, slot: 'chat-1-1700000000', ts: '2026-08-01' },
    by_model: [],
    by_channel: [],
    context_bands: [],
    conversations,

    navigable_category: 'dashboard',
    conversation_count: conversations.length,
  },
})

vi.mock('../api/client', () => ({
  api: { telemetryStartup: vi.fn() },
}))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>
    <MemoryRouter>{children}</MemoryRouter>
  </QueryClientProvider>
)

async function mount(conversations: Record<string, unknown>[]) {
  const { api } = await import('../api/client')
  vi.mocked(api.telemetryStartup).mockResolvedValue(resp(conversations) as never)
  render(<TelemetryPanel />, { wrapper: Wrapper })
}

describe('TelemetryPanel — conversations by spend', () => {
  beforeEach(() => {
    qc.clear()
    vi.clearAllMocks()
  })

  it('links a named conversation to its chat', async () => {
    await mount([convo({ title: 'Telemetry cost page' })])
    const link = await screen.findByRole('link', { name: 'Telemetry cost page' })
    expect(link.getAttribute('href')).toContain('sid=chat-1-1700000000')
  })

  it('renders an unnamed conversation as text, never as a link', async () => {
    await mount([convo()])
    // The row is present…
    await waitFor(() => expect(screen.getByTitle('chat-1-1700000000')).toBeTruthy())
    // …and carries no anchor: its slot is closed, so a click could only ever
    // land on ChatPage's delayed "Session not found".
    expect(screen.queryByRole('link')).toBeNull()
  })

  it('treats spend as data on its own, with no OTEL shard present', async () => {
    // The payload here carries no startup/turn/context — only cost. Those come
    // from the OTEL shards while cost comes from the per-turn usage rows, so a
    // machine with spend recorded but no shard yet must still get the spend
    // surface rather than an "empty" notice.
    await mount([convo({ title: 'Open one' })])
    await screen.findByRole('link', { name: 'Open one' })
  })

  it('shows the spend surface even while the OTEL switch is off', async () => {
    // `telemetry.enabled` defaults to false. Spend comes from the per-turn usage
    // rows, which are written regardless of that switch, so the off-state must
    // still render the ranking -- otherwise the whole surface ships invisible to
    // anyone who never turned telemetry on.
    const { api } = await import('../api/client')
    const payload = resp([convo({ title: 'Open one' })])
    vi.mocked(api.telemetryStartup).mockResolvedValue({ ...payload, enabled: false } as never)
    render(<TelemetryPanel />, { wrapper: Wrapper })
    await screen.findByRole('link', { name: 'Open one' })
  })

  it('keeps the two kinds independent in one ranking', async () => {
    await mount([convo({ title: 'Open one' }), convo({ slot: 'chat-2-1700000001' })])
    await screen.findByRole('link', { name: 'Open one' })
    // Exactly one link for two rows — the closed row stayed inert.
    expect(screen.getAllByRole('link')).toHaveLength(1)
  })

  it('does not link a conversation the dashboard cannot open', async () => {
    // The bug this rule exists for. A Telegram thread IS a conversation and it
    // usually HAS a title, so the old "titled -> link it" rule rendered it as a
    // link to /chat?sid=<key> — which ChatPage resolves against dashboard slots
    // only, so the click landed on `Session "…" not found` after a 5s timeout.
    // Linkability is a property of the route, not of having a name.
    await mount([
      convo({
        title: 'Asked over Telegram',
        channel: 'telegram',
        category: 'telegram',
        slot: 'telegram:kirocrew:direct:874',
      }),
    ])
    await waitFor(() => expect(screen.getByText('Asked over Telegram')).toBeInTheDocument())
    expect(screen.queryByRole('link')).not.toBeInTheDocument()
    // Still named, though — falling back to "Untitled" would lose the one piece
    // of identity the row has.
    expect(screen.getByTitle('telegram:kirocrew:direct:874')).toBeInTheDocument()
  })
})
