import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider, focusManager } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { MemoryRouter } from 'react-router-dom'
import { createTestStore } from './helpers'
import type { NormalizedUsage } from '../providers'
import UsageTab from '../pages/overview/UsageTab'
import OverviewPage from '../pages/OverviewPage'

const { fetchUsage, capability } = vi.hoisted(() => ({
  fetchUsage: vi.fn(),
  capability: { usageBilling: true },
}))

vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'acp', displayName: 'Kiro', capabilities: capability, fetchUsage,
  }),
}))
vi.mock('../hooks/useUptime', () => ({ useUptime: () => '1h' }))
vi.mock('../components/TunnelStatus', () => ({ TunnelStatus: () => null }))
vi.mock('../components/TailnetMobileCard', () => ({ TailnetMobileCard: () => null }))
vi.mock('../pages/overview', () => ({ UsageTab: () => null, WakaTimeTab: () => null }))
vi.mock('../api/client', () => ({
  api: {
    memorySettings: vi.fn().mockResolvedValue({ migrated: true }),
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { acp_backend: '' } }),
    wakatimeStats: vi.fn().mockResolvedValue({ configured: false }),
  },
}))

function report(plan = 'Cached plan'): NormalizedUsage {
  const period = { sessions: 3, messages: 12, toolCalls: 2 }
  return {
    billing: { plan, unit: 'credits', used: 10, limit: 100, percentUsed: 10 },
    sessions: {
      total: 3, today: period, thisWeek: period, thisMonth: period,
      avgMsgsPerSession: 4, refusedTranscripts: 0, dailyHistory: [],
    },
  }
}

const FIVE_MINUTES = 300_000
let client: QueryClient

beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-09-13T06:00:00Z'))
  client = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } })
  fetchUsage.mockReset().mockResolvedValue(report())
  capability.usageBilling = true
})
afterEach(() => {
  cleanup()
  client.clear()
  focusManager.setFocused(undefined)
  vi.useRealTimers()
})

function mount(View: typeof UsageTab | typeof OverviewPage) {
  return render(
    <QueryClientProvider client={client}>
      <Provider store={createTestStore()}>
        <MemoryRouter><View /></MemoryRouter>
      </Provider>
    </QueryClientProvider>,
  )
}

async function tick(ms = 10) {
  await act(() => vi.advanceTimersByTimeAsync(ms))
}

describe.each([['Usage tab', UsageTab], ['Overview summary', OverviewPage]] as const)('%s cache', (_name, View) => {
  it('reopens from the cache without fetching again inside five minutes', async () => {
    const first = mount(View)
    await tick()
    expect(screen.getByText('Cached plan')).toBeInTheDocument()
    first.unmount()
    await tick(60_000)
    mount(View)
    expect(screen.getByText('Cached plan')).toBeInTheDocument()
    await tick()
    expect(fetchUsage).toHaveBeenCalledTimes(1)
  })

  it('keeps the cache while closed beyond five minutes and refreshes behind it', async () => {
    const first = mount(View)
    await tick()
    first.unmount()
    await tick(FIVE_MINUTES * 2)
    expect(client.getQueryData(['provider-usage', 'acp'])).toEqual(report())
    let finish!: (value: NormalizedUsage) => void
    fetchUsage.mockReturnValueOnce(new Promise<NormalizedUsage>(resolve => { finish = resolve }))
    mount(View)
    expect(screen.getByText('Cached plan')).toBeInTheDocument()
    expect(fetchUsage).toHaveBeenCalledTimes(2)
    await act(async () => { finish(report('Updated plan')) })
    await tick()
    expect(screen.getByText('Updated plan')).toBeInTheDocument()
  })

  it('refreshes every five minutes while the view is visible', async () => {
    mount(View)
    await tick()
    fetchUsage.mockResolvedValue(report('Updated plan'))
    await tick(FIVE_MINUTES - 11)
    expect(fetchUsage).toHaveBeenCalledTimes(1)
    await tick(11)
    expect(fetchUsage).toHaveBeenCalledTimes(2)
    expect(screen.getByText('Updated plan')).toBeInTheDocument()
    await tick(FIVE_MINUTES)
    expect(fetchUsage).toHaveBeenCalledTimes(3)
  })

  it('keeps refreshing a mounted report in a hidden browser tab', async () => {
    mount(View)
    await tick()
    focusManager.setFocused(false)
    fetchUsage.mockResolvedValue(report('Updated plan'))
    await tick(FIVE_MINUTES)
    expect(fetchUsage).toHaveBeenCalledTimes(2)
    expect(screen.getByText('Updated plan')).toBeInTheDocument()
    focusManager.setFocused(true)
    await tick()
    expect(fetchUsage).toHaveBeenCalledTimes(2)
  })

  it('shows refresh errors alongside cached data and recovers on the next tick', async () => {
    mount(View)
    await tick()
    fetchUsage.mockRejectedValueOnce(new Error('Usage refresh failed'))
    await tick(FIVE_MINUTES)
    expect(screen.getByText('Cached plan')).toBeInTheDocument()
    expect(screen.getByText('Usage refresh failed')).toBeInTheDocument()
    expect(screen.getByText(/showing the last values we read/i)).toBeInTheDocument()
    fetchUsage.mockResolvedValue(report('Recovered plan'))
    await tick(FIVE_MINUTES)
    expect(screen.getByText('Recovered plan')).toBeInTheDocument()
    expect(screen.queryByText('Usage refresh failed')).not.toBeInTheDocument()
  })

  it('shows the first fetch failure instead of a loading placeholder', async () => {
    fetchUsage.mockRejectedValue(new Error('Usage unavailable'))
    mount(View)
    await tick()
    const error = screen.getByText('Usage unavailable')
    expect(error).toBeInTheDocument()
    expect(screen.queryByText('Cached plan')).not.toBeInTheDocument()
    expect(screen.queryByText(/showing the last values we read/i)).not.toBeInTheDocument()
    const usageCard = error.closest('.card-glow')
    expect(usageCard).not.toBeNull()
    expect(usageCard?.querySelector('.skeleton')).toBeNull()
  })

  it('shows neutral status, not an error, when the provider has no usage support', async () => {
    capability.usageBilling = false
    mount(View)
    await tick(FIVE_MINUTES * 2)
    expect(fetchUsage).not.toHaveBeenCalled()
    const status = screen.getByText(/Usage tracking is not available for/)
    expect(status).toHaveClass('text-muted')
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText('Ask the agent')).not.toBeInTheDocument()
  })
})

it('shares one report when moving from Overview into Usage', async () => {
  const summary = mount(OverviewPage)
  await tick()
  summary.unmount()
  mount(UsageTab)
  expect(screen.getByText('Cached plan')).toBeInTheDocument()
  await tick()
  expect(fetchUsage).toHaveBeenCalledTimes(1)
})
