//
// Contract under test: the provider's CLI records no token counts, so the Usage
// tab shows ESTIMATED token use for this month and last month in a card of its
// own, says in place how the figures are built, warns when some sessions could
// not be read, and adds each day's estimate to the Daily History table. A
// gateway that sends no estimate shows neither the card nor the column, and so
// does an all-zero estimate with every session read: a dashboard with no
// kiro-cli sessions, which must not show a permanent empty card.
//
import { describe, it, expect, vi, afterEach } from 'vitest'
import { render, screen, cleanup, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { NormalizedUsage } from '../providers'

type Day = NormalizedUsage['sessions']['dailyHistory'][number]
type Estimate = NonNullable<NormalizedUsage['sessions']['estimatedTokens']>

const estimate: Estimate = {
  thisMonth: { input: 1_200_000, output: 34_000, requests: 1_500 },
  lastMonth: { input: 9_300_000_000, output: 4_100_000, requests: 30_825 },
  incomplete: false,
}

/** What a dashboard with no kiro-cli sessions receives: every figure zero, every session read. */
const zeroPeriod = { input: 0, output: 0, requests: 0 }
const zeroEstimate: Estimate = { thisMonth: zeroPeriod, lastMonth: zeroPeriod, incomplete: false }

function usage(days: Day[], estimatedTokens?: Estimate, refreshing = false): NormalizedUsage {
  const period = { sessions: 0, messages: 0, toolCalls: 0 }
  return {
    refreshing,
    sessions: {
      total: days.length,
      today: period,
      thisWeek: period,
      thisMonth: period,
      avgMsgsPerSession: 0,
      refusedTranscripts: 0,
      estimatedTokens,
      dailyHistory: days,
    },
    billing: { plan: 'Pro', used: 5439.42, limit: 10000, unit: 'credits' },
  }
}

let current: NormalizedUsage = usage([])

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

/** The Estimated Tokens table as [row label, this month, last month] rows. */
async function estimateRows(): Promise<string[][]> {
  const header = await screen.findByRole('columnheader', { name: 'Last Month' })
  const table = header.closest('table')!
  return within(table)
    .getAllByRole('row')
    .slice(1)
    .map(r => [
      within(r).getByRole('rowheader').textContent ?? '',
      ...within(r).getAllByRole('cell').map(c => c.textContent ?? ''),
    ])
}

/** The Daily History table, found by its Date column (the estimate card is a table too). */
async function historyTable(): Promise<HTMLElement> {
  return (await screen.findByRole('columnheader', { name: 'Date' })).closest('table')!
}

/** Daily History day rows as cell text, top to bottom, without the phone-only lines. */
async function historyRows(): Promise<string[][]> {
  const body = within(await historyTable()).getAllByRole('row').slice(1).filter(r => !r.hasAttribute('data-phone-line'))
  return body.map(r => within(r).getAllByRole('cell').map(c => c.textContent ?? ''))
}

async function phoneLines(): Promise<string[]> {
  return within(await historyTable())
    .getAllByRole('row')
    .filter(r => r.hasAttribute('data-phone-line'))
    .map(r => r.textContent ?? '')
}

const days: Day[] = [
  { date: '2026-09-21', sessions: 2, messages: 10, toolCalls: 4, credits: 12.5, estTokens: 31_000_000 },
  { date: '2026-09-22', sessions: 1, messages: 3, toolCalls: 0, credits: 300, estTokens: 0 },
]

afterEach(() => cleanup())

describe('UsageTab Estimated Tokens card', () => {
  it('shows this month and last month in short form, with a total and the request count', async () => {
    current = usage([], estimate)
    mount()
    expect(await estimateRows()).toEqual([
      ['Input tokens (context sent)', '1.2M', '9.3B'],
      ['Output tokens', '34K', '4.1M'],
      ['Total tokens', '1.2M', '9.3B'],
      ['Model requests', '1,500', '30,825'],
    ])
    expect(screen.getByRole('columnheader', { name: 'This Month' })).toBeInTheDocument()
  })

  it('keeps a gutter before the Last Month column only, so its header cannot touch This Month', async () => {
    current = usage([], estimate)
    mount()
    const table = (await screen.findByRole('columnheader', { name: 'Last Month' })).closest('table')!
    const valueRows = within(table).getAllByRole('row').slice(1).map(r => within(r).getAllByRole('cell'))
    expect(valueRows).toHaveLength(4)
    for (const cells of valueRows) expect(cells).toHaveLength(2)
    const thisMonth = [within(table).getByRole('columnheader', { name: 'This Month' }), ...valueRows.map(cells => cells[0])]
    const lastMonth = [within(table).getByRole('columnheader', { name: 'Last Month' }), ...valueRows.map(cells => cells[1])]
    for (const cell of lastMonth) expect(cell).toHaveClass('pl-4', 'text-right')
    for (const cell of thisMonth) expect(cell).not.toHaveClass('pl-4')
  })

  it('says in place that the figures are estimates and what each one counts', async () => {
    current = usage([], estimate)
    mount()
    const note = await screen.findByText(/does not save token counts/)
    expect(note.textContent).toMatch(/^Kiro CLI does not save token counts/)
    expect(note.textContent).toMatch(/context sent with each model request/)
    expect(note.textContent).toMatch(/cached or not/)
    expect(note.textContent).toMatch(/leaves out tool calls and reasoning/)
    expect(note.textContent).not.toMatch(/at least/)
    expect(note.textContent).toMatch(/divided by 4/)
  })

  it('warns that unread sessions are left out when some sessions could not be read', async () => {
    current = usage([], { ...estimate, incomplete: true })
    mount()
    const warning = await screen.findByText(/Some sessions could not be read/)
    expect(warning.textContent).toMatch(/could not be read and are not counted/)
  })

  it('shows no warning when every session was read', async () => {
    current = usage([], estimate)
    mount()
    await estimateRows()
    expect(screen.queryByText(/Some sessions could not be read/)).not.toBeInTheDocument()
  })

  it('shows no card when the provider sends no estimate', async () => {
    current = usage(days)
    mount()
    await historyTable()
    expect(screen.queryByText('Estimated Tokens')).not.toBeInTheDocument()
  })

  it('shows neither card nor column for an all-zero estimate with every session read', async () => {
    current = usage(days.map(d => ({ ...d, estTokens: 0 })), zeroEstimate)
    mount()
    const headers = within(await historyTable()).getAllByRole('columnheader').map(h => h.textContent)
    expect(screen.queryByText('Estimated Tokens')).not.toBeInTheDocument()
    expect(headers).toEqual(['Date', 'Sessions', 'Messages', 'Tool Calls', 'Credits used', 'Credits used (%)'])
    expect(await phoneLines()).toEqual(['Credits used: 300.00 · 3.0%', 'Credits used: 12.50 · 0.1%'])
  })

  it('keeps the card for zero totals when some sessions could not be read', async () => {
    current = usage([], { ...zeroEstimate, incomplete: true })
    mount()
    expect(await screen.findByText(/Some sessions could not be read/)).toBeInTheDocument()
    expect((await estimateRows()).map(r => r.slice(1))).toEqual([['0', '0'], ['0', '0'], ['0', '0'], ['0', '0']])
  })

  it('shows the card when only last month has a figure', async () => {
    current = usage([], { ...zeroEstimate, lastMonth: estimate.lastMonth })
    mount()
    expect((await estimateRows())[0]).toEqual(['Input tokens (context sent)', '0', '9.3B'])
  })

  it('shows the card and the column when only one day has a figure', async () => {
    current = usage(days, zeroEstimate)
    mount()
    expect((await estimateRows())[2]).toEqual(['Total tokens', '0', '0'])
    expect((await historyRows()).map(r => r[6])).toEqual(['0', '31M'])
  })

  it('shows a placeholder, not the figures, while the session scan refreshes', async () => {
    current = usage([], estimate, true)
    mount()
    expect(await screen.findByTestId('usage-estimate-refreshing')).toBeInTheDocument()
    expect(screen.getByText('Estimated Tokens')).toBeInTheDocument()
    expect(screen.queryByRole('columnheader', { name: 'Last Month' })).not.toBeInTheDocument()
  })

  it('shows no card while a cold refresh still carries an all-zero estimate', async () => {
    current = usage([], zeroEstimate, true)
    mount()
    expect(await screen.findByTestId('usage-session-refreshing')).toBeInTheDocument()
    expect(screen.queryByText('Estimated Tokens')).not.toBeInTheDocument()
    expect(screen.queryByTestId('usage-estimate-refreshing')).not.toBeInTheDocument()
  })
})

describe('UsageTab Daily History estimated tokens column', () => {
  it('adds Est. tokens after the credit columns, newest day first', async () => {
    current = usage(days, estimate)
    mount()
    const headers = within(await historyTable()).getAllByRole('columnheader').map(h => h.textContent)
    expect(headers).toEqual(['Date', 'Sessions', 'Messages', 'Tool Calls', 'Credits used', 'Credits used (%)', 'Est. tokens'])
    expect(await historyRows()).toEqual([
      ['2026-09-22', '1', '3', '0', '300.00', '3.0%', '0'],
      ['2026-09-21', '2', '10', '4', '12.50', '0.1%', '31M'],
    ])
  })

  it('shows a dash for a day with no figure when other days have one', async () => {
    current = usage([{ ...days[0], estTokens: undefined }, days[1]], estimate)
    mount()
    const rows = await historyRows()
    expect(rows.map(r => r[6])).toEqual(['0', '\u2014'])
  })

  it('leaves the column out when no day carries an estimate', async () => {
    current = usage(days.map(d => ({ ...d, estTokens: undefined })))
    mount()
    const headers = within(await historyTable()).getAllByRole('columnheader').map(h => h.textContent)
    expect(headers).toEqual(['Date', 'Sessions', 'Messages', 'Tool Calls', 'Credits used', 'Credits used (%)'])
    expect(await phoneLines()).toEqual(['Credits used: 300.00 · 3.0%', 'Credits used: 12.50 · 0.1%'])
  })

  it('explains the estimate on the header tooltip', async () => {
    current = usage(days, estimate)
    mount()
    const header = within(await historyTable()).getByRole('columnheader', { name: 'Est. tokens' })
    expect(header).toHaveAttribute('title', expect.stringMatching(/context size/))
  })

  it('adds the estimate to the phone-only line under each day', async () => {
    current = usage(days, estimate)
    mount()
    expect(await phoneLines()).toEqual([
      'Credits used: 300.00 · 3.0% · Est. tokens: 0',
      'Credits used: 12.50 · 0.1% · Est. tokens: 31M',
    ])
  })
})
