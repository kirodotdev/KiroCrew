/**
 * The Spend over time card stacks CREDITS, which only the kiro-cli harness and
 * its KAS relay bill in. On any other harness the per-turn rows carry no
 * credits, so the card would call turns that did run "no spend"; it mounts only
 * once a loaded config names a credits-billing harness, read positively.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider, useQuery } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { NormalizedUsage } from '../providers'
import UsageTab from '../pages/overview/UsageTab'

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
      avgMsgsPerSession: 4, refusedTranscripts: 0, dailyHistory: [],
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
    ['kiro-cli', { agent: { acp_backend: '' } }],
    ['an unset key, which the gateway reads as kiro-cli', {}],
    ['KAS', { agent: { acp_backend: 'kas' } }],
  ])('mounts the credits chart for %s', async (_name, cfg) => {
    kirocrewConfig.mockResolvedValue(cfg)

    mount()

    expect(await screen.findByText('Spend over time (credits)')).toBeInTheDocument()
    await screen.findByTestId('usage-series-chart')
    expect(usageSeries).toHaveBeenCalledWith('channel')
  })

  it('mounts no credits chart for a harness that bills in tokens or dollars, and never asks for the series', async () => {
    kirocrewConfig.mockResolvedValue({ agent: { acp_backend: 'claude' } })

    mount()

    await screen.findByText('Session Activity (30 days)')
    expect(screen.queryByText('Spend over time (credits)')).toBeNull()
    await screen.findByTestId('cfg-loaded')
    expect(screen.queryByText('Spend over time (credits)')).toBeNull()
    expect(usageSeries).not.toHaveBeenCalled()
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

    act(() => release({ agent: { acp_backend: '' } }))

    expect(await screen.findByText('Spend over time (credits)')).toBeInTheDocument()
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

    answer({ agent: { acp_backend: '' } })
    await screen.findByTestId('cfg-loaded')
    expect(await screen.findByText('Spend over time (credits)')).toBeInTheDocument()
  })
})
