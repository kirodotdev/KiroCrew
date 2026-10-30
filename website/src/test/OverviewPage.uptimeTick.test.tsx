import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { screen, act } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import OverviewPage from '../pages/OverviewPage'
import { __resetSecondClockForTests } from '../hooks/useSecondClock'
import type { RootState } from '../store'

vi.mock('../pages/overview', () => ({
  UsageTab: () => <div data-testid="usage-tab">UsageTab</div>,
  WakaTimeTab: () => <div data-testid="wakatime-tab">WakaTimeTab</div>,
}))
vi.mock('../pages/overview/MemoryTab', () => ({
  default: () => <div data-testid="memory-tab">MemoryTab</div>,
}))
vi.mock('../api/client', () => ({
  api: {
    memorySettings: vi.fn().mockResolvedValue({ history_idle_hours: 3, history_max_days: 90, migrated: false }),
    wakatimeStats: vi.fn().mockResolvedValue({ configured: false }),
    kasLoginStatus: vi.fn().mockResolvedValue({ authenticated: false }),
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { acp_backend: 'kiro' } }),
  },
}))
vi.mock('../providers', () => ({
  useProvider: () => ({ id: 'test', displayName: 'Test Provider', capabilities: {}, fetchUsage: vi.fn().mockResolvedValue(null) }),
}))

// TunnelStatus renders once per OverviewPage body render, so its render count
// is the page's.
const pageRenders = vi.hoisted(() => ({ n: 0 }))
vi.mock('../components/TunnelStatus', () => ({
  TunnelStatus: () => { pageRenders.n += 1; return null },
}))

const T0 = new Date('2026-01-01T00:00:00Z').getTime()

describe('OverviewPage uptime tick', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: false })
    vi.setSystemTime(T0)
    __resetSecondClockForTests()
    pageRenders.n = 0
  })
  afterEach(() => {
    vi.useRealTimers()
    __resetSecondClockForTests()
  })

  it('advances the uptime figures without re-rendering the page', () => {
    const store = createTestStore({
      dashboard: {
        status: { uptime: '30s', start_time: T0 / 1000 - 30, sessions: 3, messages: 42, cron_jobs: 1, subagents: 0, lessons: 5, version: '0.1.0' },
        connected: true,
        slots: [],
        refreshTrigger: 0,
      } as unknown as RootState['dashboard'],
    })
    renderWithProviders(<OverviewPage />, { store })
    const before = pageRenders.n
    const tile = screen.getByText('Uptime').closest('[data-testid="stat-card"]') as HTMLElement
    const first = tile.textContent
    for (let i = 0; i < 3; i++) act(() => { vi.advanceTimersByTime(1000) })
    expect(tile.textContent).not.toBe(first)
    expect(pageRenders.n).toBe(before)
  })
})
