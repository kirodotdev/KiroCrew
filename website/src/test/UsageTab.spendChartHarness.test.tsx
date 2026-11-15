/**
 * The Spend over time card stacks CREDITS, which only the kiro-cli harness and
 * its KAS relay bill in. On any other harness the per-turn rows carry no
 * credits, so the card would call turns that did run "no spend"; it mounts only
 * once a loaded config names a credits-billing harness, read positively.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { NormalizedUsage } from '../providers'
import UsageTab from '../pages/overview/UsageTab'
import { namedCeiling } from './namedCeiling'

const { fetchUsage, kirocrewConfig, usageSeries } = vi.hoisted(() => ({
  fetchUsage: vi.fn(),
  kirocrewConfig: vi.fn(),
  usageSeries: vi.fn(),
}))

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp', displayName: 'Kiro', capabilities: { usageBilling: true }, fetchUsage,
  }),
}))
vi.mock('../api/client', () => ({ api: { kirocrewConfig, usageSeries } }))
vi.mock('../hooks/useSessionPalette', () => ({
  useSessionPalette: () => ({ paletteColors: ['#ff0000', '#00ff00', '#0000ff'] }),
}))

function report(): NormalizedUsage {
  const period = { sessions: 3, messages: 12, toolCalls: 2 }
  return {
    billing: { plan: 'Plan', unit: 'credits', used: 10, limit: 100, percentUsed: 10 },
    sessions: {
      total: 3, today: period, thisWeek: period, thisMonth: period,
      avgMsgsPerSession: 4, refusedTranscripts: 0, unreadableSessionDirs: 0, dailyHistory: [],
    },
  }
}

let client: QueryClient

beforeEach(() => {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  fetchUsage.mockReset().mockResolvedValue(report())
  usageSeries.mockReset().mockResolvedValue({
    dates: ['2026-10-02'],
    series: [{ key: 'dashboard', kind: 'bucket', values: [5], total: 5 }],
    total: 5,
    truncated: false,
    dropped_rows: 0,
    complete_from: null,
  })
  kirocrewConfig.mockReset()
})
afterEach(() => {
  cleanup()
  client.clear()
})

// Subscribes to the same `['kirocrewConfig']` entry the tab reads. Both
// observers are notified in one batch, so once this probe has rendered the
// answer, the tab has it too -- "the request was sent" is not that.
function ConfigProbe() {
  const { data } = useQuery({ queryKey: ['kirocrewConfig'], queryFn: () => kirocrewConfig() })
  return data === undefined ? null : <span data-testid="cfg-loaded" />
}

// The chart mounts only after the config and usage reads resolve, then its
// own series read resolves: a chain of three queries, longer than findBy's
// default ceiling on a busy coverage runner.
const chartChain = () => namedCeiling('CONFIG_USAGE_THEN_SERIES_CHART', 5000)

function mount() {
  return render(
    <QueryClientProvider client={client}>
      <Provider store={createTestStore()}>
        <MemoryRouter><UsageTab /><ConfigProbe /></MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
}

describe('UsageTab spend chart harness gate', () => {
  it.each([
    ['kiro-cli', { agent: { acp_backend: 'kiro' } }],
    ['an unset key, which the gateway reads as kiro-cli', {}],
    ['KAS', { agent: { acp_backend: 'kas' } }],
  ])('mounts the credits chart for %s', async (_name, cfg) => {
    kirocrewConfig.mockResolvedValue(cfg)

    mount()

    expect(await screen.findByText('Spend over time (credits)', {}, chartChain())).toBeInTheDocument()
    await screen.findByTestId('usage-series-chart', {}, chartChain())
    expect(usageSeries).toHaveBeenCalledWith('channel', 'credits')
  })

  it('mounts a dollar chart, and no credits chart, for a harness that bills in dollars (#35249)', async () => {
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'claude' } })

    mount()

    expect(await screen.findByText('Spend over time (USD)', {}, chartChain())).toBeInTheDocument()
    await screen.findByTestId('usage-series-chart', {}, chartChain())
    expect(screen.queryByText('Spend over time (credits)')).toBeNull()
    expect(usageSeries).toHaveBeenCalledWith('channel', 'cost')
    expect(usageSeries).not.toHaveBeenCalledWith('channel', 'credits')
    // The axis ticks read in dollars too, not as bare numbers.
    const chart = await screen.findByTestId('usage-series-chart', {}, chartChain())
    expect(within(chart).getByText('$0')).toBeInTheDocument()
  })

  it('keeps cents on the dollar axis, so a few cents of spend never reads as zero', async () => {
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'claude' } })
    usageSeries.mockResolvedValue({
      dates: ['2026-10-02'],
      metric: 'cost',
      series: [{ key: 'dashboard', kind: 'bucket', values: [0.0231], total: 0.0231 }],
      total: 0.0231,
      truncated: false,
      dropped_rows: 0,
      complete_from: null,
    })

    mount()

    const chart = await screen.findByTestId('usage-series-chart', {}, chartChain())
    expect(await within(chart).findByText('$0.02', {}, chartChain())).toBeInTheDocument()
  })

  it('never draws the credits series under dollar ticks when the harness switches to one billed in dollars', async () => {
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'kiro' } })
    usageSeries.mockResolvedValue({
      dates: ['2026-10-02'],
      series: [{ key: 'dashboard', kind: 'bucket', values: [100], total: 100 }],
      total: 100,
      truncated: false,
      dropped_rows: 0,
      complete_from: null,
    })
    mount()
    expect(await screen.findByText('Spend over time (credits)', {}, chartChain())).toBeInTheDocument()
    await within(await screen.findByTestId('usage-series-chart', {}, chartChain())).findByText('100', {}, chartChain())

    // The cost series stays in flight while the switch renders.
    let release!: (v: unknown) => void
    usageSeries.mockImplementation(() => new Promise(resolve => { release = resolve }))
    act(() => client.setQueryData(['kirocrewConfig'], { agent: { acp_backend: 'claude' } }))
    expect(await screen.findByText('Spend over time (USD)', {}, chartChain())).toBeInTheDocument()
    await waitFor(() => expect(usageSeries).toHaveBeenCalledWith('channel', 'cost'), chartChain())

    expect(screen.queryByText('$100')).toBeNull()
    expect(screen.queryByText('$100.00')).toBeNull()
    act(() => release({
      dates: ['2026-10-02'], metric: 'cost',
      series: [{ key: 'dashboard', kind: 'bucket', values: [2], total: 2 }],
      total: 2, truncated: false, dropped_rows: 0, complete_from: null,
    }))
  })

  it('says so where the card would stand when the config read fails, holds the notice through a retry, and mounts the card once a read succeeds', async () => {
    kirocrewConfig.mockRejectedValue(new Error('config unavailable'))
    mount()
    const notice = await screen.findByTestId('usage-series-harness-unread')
    expect(notice).toHaveTextContent("Couldn’t read the agent backend setting")
    expect(screen.queryByText('Spend over time (credits)')).toBeNull()
    expect(usageSeries).not.toHaveBeenCalled()

    // Try again re-reads the config. While that read is in flight the query is
    // pending with its error cleared, and the notice must not blink out.
    let release!: (cfg: unknown) => void
    kirocrewConfig.mockImplementation(() => new Promise(resolve => { release = resolve }))
    fireEvent.click(screen.getByRole('button', { name: 'Try again' }))
    await waitFor(() => expect(client.getQueryState(['kirocrewConfig'])?.fetchStatus).toBe('fetching'))
    expect(screen.getByTestId('usage-series-harness-unread')).toBeInTheDocument()

    act(() => release({ agent: { acp_backend: 'kiro' } }))

    expect(await screen.findByText('Spend over time (credits)', {}, chartChain())).toBeInTheDocument()
    expect(screen.queryByTestId('usage-series-harness-unread')).toBeNull()
    expect(kirocrewConfig).toHaveBeenCalledTimes(2)
  })

  it('mounts nothing on a guess while the config has not loaded', async () => {
    let answer!: (cfg: unknown) => void
    kirocrewConfig.mockImplementation(() => new Promise(resolve => { answer = resolve }))
    mount()
    await screen.findByText('Session Activity (30 days)')

    expect(screen.queryByText('Spend over time (credits)')).toBeNull()
    expect(screen.queryByTestId('usage-series-harness-unread')).toBeNull()
    expect(screen.queryByTestId('cfg-loaded')).toBeNull()

    answer({ agent: { acp_backend: 'kiro' } })
    await screen.findByTestId('cfg-loaded')
    expect(await screen.findByText('Spend over time (credits)', {}, chartChain())).toBeInTheDocument()
  })
})

describe('UsageTab harness-unread Try again', () => {
  it('keeps the pressed button in the tab order while the config re-reads (#32868)', async () => {
    kirocrewConfig.mockRejectedValue(new Error('config unavailable'))
    mount()
    const retry = await screen.findByRole('button', { name: 'Try again' })

    let release!: (cfg: unknown) => void
    kirocrewConfig.mockImplementation(() => new Promise(resolve => { release = resolve }))
    retry.focus()
    fireEvent.click(retry)
    await waitFor(() => expect(client.getQueryState(['kirocrewConfig'])?.fetchStatus).toBe('fetching'))

    expect(retry).toBeInTheDocument()
    expect(retry).toHaveFocus()
    expect(retry).not.toBeDisabled()
    expect(retry).toHaveAttribute('aria-disabled', 'true')
    // Sighted users see the retry start: the label says so while it reads.
    expect(retry).toHaveTextContent('Retrying…')
    fireEvent.click(retry)
    expect(kirocrewConfig).toHaveBeenCalledTimes(2)

    act(() => release({ agent: { acp_backend: 'kiro' } }))
    expect(await screen.findByText('Spend over time (credits)', {}, chartChain())).toBeInTheDocument()
  })
})
