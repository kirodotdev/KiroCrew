//
// Contract under test: the Daily History table on the Usage tab shows how many
// credits each day cost and what share of the plan allowance that was (#3371).
// The share is that day's credits over the CURRENT billing period's allowance,
// the same `limit` the Billing card divides by; it is not clamped at 100%; and
// it is a dash, never "0%", when there is no allowance to divide by.
//
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, waitFor, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { NormalizedUsage } from '../providers'

type Day = NormalizedUsage['sessions']['dailyHistory'][number]

function usage(days: Day[], billing: NormalizedUsage['billing']): NormalizedUsage {
  const period = { sessions: 0, messages: 0, toolCalls: 0 }
  return {
    sessions: {
      total: days.length,
      today: period,
      thisWeek: period,
      thisMonth: period,
      avgMsgsPerSession: 0,
      refusedTranscripts: 0,
      dailyHistory: days,
    },
    billing,
  }
}

const plan: NormalizedUsage['billing'] = { plan: 'Pro', used: 5439.42, limit: 10000, unit: 'credits' }

let current: NormalizedUsage = usage([], null)

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp',
    displayName: 'Kiro',
    capabilities: { usageBilling: true },
    fetchUsage: () => Promise.resolve(current),
  }),
}))

import UsageTab from '../pages/overview/UsageTab'

function mount() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <QueryClientProvider client={qc}>
      <UsageTab />
    </QueryClientProvider>,
  )
}

/**
 * The Daily History table's per-day rows as arrays of cell text, top to bottom.
 * The phone-only second line under each day is a separate `<tr>` that CSS hides
 * above 600px; jsdom applies no media queries, so it is filtered out by marker.
 */
async function rows(): Promise<string[][]> {
  const table = await screen.findByRole('table')
  const body = within(table).getAllByRole('row').slice(1) // drop the header row
    .filter(r => !r.hasAttribute('data-phone-line'))
  return body.map(r => within(r).getAllByRole('cell').map(c => c.textContent ?? ''))
}

/** The phone-only second lines, top to bottom. */
async function phoneLines(): Promise<string[]> {
  const table = await screen.findByRole('table')
  return within(table).getAllByRole('row').filter(r => r.hasAttribute('data-phone-line')).map(r => r.textContent ?? '')
}

afterEach(() => cleanup())

describe('UsageTab Daily History credits columns (#3371)', () => {
  it('adds Credits used and Credits used (%) after Tool Calls, newest day first', async () => {
    current = usage(
      [
        { date: '2026-09-21', sessions: 2, messages: 10, toolCalls: 4, credits: 12.5 },
        { date: '2026-09-22', sessions: 1, messages: 3, toolCalls: 0, credits: 300 },
      ],
      plan,
    )
    mount()
    const table = await screen.findByRole('table')
    const headers = within(table).getAllByRole('columnheader').map(h => h.textContent)
    expect(headers).toEqual(['Date', 'Sessions', 'Messages', 'Tool Calls', 'Credits used', 'Credits used (%)'])
    expect(await rows()).toEqual([
      ['2026-09-22', '1', '3', '0', '300.00', '3.0%'],
      ['2026-09-21', '2', '10', '4', '12.50', '0.1%'],
    ])
  })

  it('divides by the Billing card allowance and does not clamp a day above 100%', async () => {
    current = usage([{ date: '2026-09-22', sessions: 1, messages: 1, toolCalls: 0, credits: 12500 }], plan)
    mount()
    expect(await rows()).toEqual([['2026-09-22', '1', '1', '0', '12,500.00', '125.0%']])
  })

  it('shows a dash, not 0%, when there is no billing plan at all', async () => {
    current = usage([{ date: '2026-09-22', sessions: 1, messages: 1, toolCalls: 0, credits: 12.5 }], null)
    mount()
    expect(await rows()).toEqual([['2026-09-22', '1', '1', '0', '12.50', '\u2014']])
  })

  it.each([
    ['zero', 0],
    ['absent', undefined],
  ])('shows a dash for the share when the allowance is %s', async (_label, limit) => {
    current = usage(
      [{ date: '2026-09-22', sessions: 1, messages: 1, toolCalls: 0, credits: 12.5 }],
      { ...plan, limit },
    )
    mount()
    const [row] = await rows()
    expect(row[4]).toBe('12.50')
    expect(row[5]).toBe('\u2014')
    expect(row[5]).not.toMatch(/%/)
  })

  it('shows a dash in both columns for a day with no credits figure', async () => {
    current = usage([{ date: '2026-09-22', sessions: 1, messages: 1, toolCalls: 0 }], plan)
    mount()
    expect(await rows()).toEqual([['2026-09-22', '1', '1', '0', '\u2014', '\u2014']])
  })

  it('renders a day that cost nothing as 0.00 and 0.0%, not as a dash', async () => {
    current = usage([{ date: '2026-09-22', sessions: 1, messages: 1, toolCalls: 0, credits: 0 }], plan)
    mount()
    expect(await rows()).toEqual([['2026-09-22', '1', '1', '0', '0.00', '0.0%']])
  })

  it('explains the percentage on the header tooltip', async () => {
    current = usage([{ date: '2026-09-22', sessions: 1, messages: 1, toolCalls: 0, credits: 1 }], plan)
    mount()
    const header = await screen.findByRole('columnheader', { name: 'Credits used (%)' })
    expect(header).toHaveAttribute('title', expect.stringMatching(/billing period/i))
    await waitFor(() => expect(header).toBeInTheDocument())
  })

  it('folds both figures onto one phone-only line per day, spanning the four base columns', async () => {
    current = usage(
      [
        { date: '2026-09-21', sessions: 2, messages: 10, toolCalls: 4, credits: 12.5 },
        { date: '2026-09-22', sessions: 1, messages: 3, toolCalls: 0, credits: 12500 },
      ],
      plan,
    )
    mount()
    expect(await phoneLines()).toEqual(['Credits used: 12,500.00 · 125.0%', 'Credits used: 12.50 · 0.1%'])
    const table = await screen.findByRole('table')
    const line = within(table).getAllByRole('row').find(r => r.hasAttribute('data-phone-line'))!
    expect(within(line).getByRole('cell')).toHaveAttribute('colspan', '4')
  })

  it('keeps the dash on the phone line when there is no plan', async () => {
    current = usage([{ date: '2026-09-22', sessions: 1, messages: 1, toolCalls: 0, credits: 12.5 }], null)
    mount()
    expect(await phoneLines()).toEqual(['Credits used: 12.50 · \u2014'])
  })
})
