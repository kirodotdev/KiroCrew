/**
 * Telemetry Spend: what each session delivered, beside what it cost.
 *
 * The gateway reads a session's own transcript for the commits and pull requests
 * it can prove and attaches them as `outcome`. The table shows five columns for
 * it, and only once some row has one, so a window with nothing delivered keeps
 * the table it always had. A session with no outcome reads `—`, never 0: the
 * transcript proves a delivery and cannot prove its absence.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { MemoryRouter } from 'react-router-dom'

import TelemetryPanel from '../pages/TelemetryPanel'

const convo = (over: Record<string, unknown> = {}) => ({
  slot: 'chat-1-1700000000',
  category: 'dashboard',
  channel: 'dashboard',
  title: 'Shipped the export',
  credits: 120,
  turns: 10,
  peak_pct: 50,
  span_days: 1,
  first_ts: 1700000000,
  growth_pct_per_turn: 2,
  turns_to_compaction: 20,
  ...over,
})

const outcome = (over: Record<string, unknown> = {}) => ({
  commits: 3,
  lines_added: 1240,
  lines_removed: 310,
  prs: 2,
  merged: 1,
  unchecked: 1,
  per_merged: 120,
  ...over,
})

const row = (name: string, credits: number) => ({
  name,
  credits,
  turns: 10,
  per_turn: credits / 10,
  share_pct: credits / 10,
  delta_pct: null,
})

const cost = (over: Record<string, unknown> = {}) => ({
  window_days: 7,
  credits: 200,
  turns: 20,
  per_turn: 10,
  prior_credits: 100,
  prior_turns: 10,
  prior_per_turn: 10,
  delta_pct: 100,
  priciest: { credits: 40, slot: 'chat-1-1700000000', ts: '2026-08-05' },
  by_model: [row('opus-5', 200)],
  by_channel: [row('dashboard', 200)],
  by_category: [row('dashboard', 200)],
  context_bands: [],
  conversations: [convo({ outcome: outcome() }), convo({ slot: 'chat-2-1', title: 'Only talked', credits: 80 })],
  outcome_scan: 2,
  conversation_count: 2,
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
  localStorage.setItem('telemetry:spend-table-open', '1')
  render(<TelemetryPanel />, { wrapper: Wrapper })
  await waitFor(() => expect(document.querySelector('table')).not.toBeNull())
}

const rowOf = (title: string) =>
  within(document.querySelector('table') as HTMLElement).getByText(title).closest('tr') as HTMLElement

beforeEach(() => {
  vi.clearAllMocks()
  qc.clear()
  localStorage.clear()
})

describe('TelemetryPanel — what a session delivered', () => {
  it('shows the outcome columns once a row has an outcome', async () => {
    await mount()
    for (const name of ['Commits', 'Lines', 'PRs', 'Merged', 'Per merged']) {
      expect(screen.getByRole('columnheader', { name: new RegExp(`^${name}`) })).toBeInTheDocument()
    }
  })

  it('keeps today\'s table when no row has an outcome', async () => {
    await mount({
      conversations: [convo(), convo({ slot: 'chat-2-1', title: 'Only talked' })],
      outcome_scan: undefined,
    })
    expect(screen.queryByRole('columnheader', { name: /^Commits/ })).toBeNull()
    expect(screen.queryByRole('columnheader', { name: /^Per merged/ })).toBeNull()
  })

  it('reads the figures off the row that delivered them', async () => {
    await mount()
    const cells = within(rowOf('Shipped the export'))
    expect(cells.getByText('3')).toBeInTheDocument()
    expect(cells.getByText('+1,240')).toBeInTheDocument()
    expect(cells.getByText('−310')).toBeInTheDocument()
    expect(cells.getByText('2')).toBeInTheDocument()
    // One merged and one pull request not yet looked up: the count says it may be higher.
    expect(cells.getByText('≥1')).toBeInTheDocument()
  })

  it('prices the session per merged pull request', async () => {
    await mount({
      conversations: [convo({ outcome: outcome({ per_merged: 118.5 }) })],
    })
    const cells = within(rowOf('Shipped the export')).getAllByRole('cell')
    expect(cells.some(c => c.textContent === '118.5')).toBe(true)
  })

  it('reads a session with no outcome as unknown, not zero', async () => {
    await mount()
    const cells = within(rowOf('Only talked')).getAllByRole('cell')
    const dashes = cells.filter(c => c.textContent === '—')
    expect(dashes.length).toBeGreaterThanOrEqual(5)
    expect(within(rowOf('Only talked')).queryByText('0')).toBeNull()
  })

  it('shows no merged count while no pull request has been looked up', async () => {
    await mount({
      conversations: [
        convo({ outcome: outcome({ merged: 0, unchecked: 2, per_merged: undefined }) }),
      ],
    })
    const cells = within(rowOf('Shipped the export')).getAllByRole('cell')
    expect(cells.filter(c => c.textContent === '—').length).toBeGreaterThanOrEqual(2)
    expect(within(rowOf('Shipped the export')).queryByText('0')).toBeNull()
  })

  it('shows no merged figures to a caller the gateway sent none to', async () => {
    await mount({
      conversations: [
        convo({
          outcome: { commits: 1, lines_added: 5, lines_removed: 0, prs: 1 },
        }),
      ],
    })
    const cells = within(rowOf('Shipped the export')).getAllByRole('cell')
    expect(cells.filter(c => c.textContent === '—').length).toBeGreaterThanOrEqual(2)
  })

  it('names the scope of the reading on the Commits header', async () => {
    await mount()
    const head = screen.getByRole('columnheader', { name: /^Commits/ })
    await userEvent.hover(head)
    // The tip is the hover text; it carries the count of sessions that were read.
    expect(head.innerHTML).toContain('2 costliest')
  })
})
