import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import OverviewPage from '../pages/OverviewPage'
import type { RootState } from '../store'

// Mock the two drill-in surfaces to isolate the mission-control shell.
vi.mock('../pages/overview', () => ({
  MemoryTab: () => <div data-testid="memory-tab">MemoryTab</div>,
  UsageTab: () => <div data-testid="usage-tab">UsageTab</div>,
}))

vi.mock('../hooks/useUptime', () => ({
  useUptime: () => '2h 30m',
}))

vi.mock('../api/client', () => ({
  api: {
    restartSessions: vi.fn().mockResolvedValue({}),
    memorySettings: vi.fn().mockResolvedValue({ history_idle_hours: 3, history_max_days: 90, migrated: false }),
  },
}))

// Usage summary card goes through the provider seam.
vi.mock('../providers', () => ({
  useProvider: () => ({
    id: 'test',
    displayName: 'Test Provider',
    capabilities: { usageBilling: true },
    fetchUsage: vi.fn().mockResolvedValue({
      sessions: {
        total: 10,
        today: { sessions: 3, messages: 42, toolCalls: 5 },
        thisWeek: { sessions: 8, messages: 100, toolCalls: 12 },
        thisMonth: { sessions: 10, messages: 120, toolCalls: 15 },
        avgMsgsPerSession: 12,
        dailyHistory: [],
      },
      billing: { plan: 'Pro', percentUsed: 42, unit: 'tokens' },
      tokens: { total: 63_300 },
      costUsd: 4.12,
    }),
  }),
}))

function statusStore(connected = true) {
  return createTestStore({
    dashboard: {
      status: { uptime: '2h', sessions: 3, messages: 42, cron_jobs: 1, subagents: 0, lessons: 5, version: '0.1.0' },
      connected,
      slots: [],
      refreshTrigger: 0,
    } as RootState['dashboard'],
  })
}

describe('OverviewPage — mission control', () => {
  it('renders the health hero and stat tiles, with no nested tab bar', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.getByText('All systems running')).toBeInTheDocument()
    expect(screen.getByText('Uptime')).toBeInTheDocument()
    expect(screen.getByText('Sessions')).toBeInTheDocument()
    // The old sub-tab bar is gone.
    expect(screen.queryByText('KiroCrew Config')).not.toBeInTheDocument()
    expect(screen.queryByText('Agent Config')).not.toBeInTheDocument()
    expect(screen.queryByText('Import/Export')).not.toBeInTheDocument()
    // Neither drill-in surface is mounted on the landing view.
    expect(screen.queryByTestId('memory-tab')).not.toBeInTheDocument()
    expect(screen.queryByTestId('usage-tab')).not.toBeInTheDocument()
  })

  it('shows the connecting state without status', () => {
    renderWithProviders(<OverviewPage />)
    expect(screen.getByText('Connecting…')).toBeInTheDocument()
  })

  it('drops the healthy claim when the socket disconnects (stale status kept)', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore(false) })
    expect(screen.getByText('Reconnecting…')).toBeInTheDocument()
    expect(screen.queryByText('All systems running')).not.toBeInTheDocument()
  })

  it('renders usage and memory summary cards', async () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.getByText('Usage')).toBeInTheDocument()
    expect(screen.getByText('Memory')).toBeInTheDocument()
    expect(await screen.findByText(/Summarizes chats into memory after 3h idle/)).toBeInTheDocument()
    expect(await screen.findByText(/Today:/)).toBeInTheDocument()
  })

  it('drills into Memory and back', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    // Both summary cards use the same verb; Usage renders first, Memory second.
    fireEvent.click(screen.getAllByRole('button', { name: /View details/ })[1])
    expect(screen.getByTestId('memory-tab')).toBeInTheDocument()
    expect(screen.queryByText('All systems running')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Overview/ }))
    expect(screen.getByText('All systems running')).toBeInTheDocument()
    expect(screen.queryByTestId('memory-tab')).not.toBeInTheDocument()
  })

  it('drills into Usage and back', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    fireEvent.click(screen.getAllByRole('button', { name: /View details/ })[0])
    expect(screen.getByTestId('usage-tab')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Overview/ }))
    expect(screen.queryByTestId('usage-tab')).not.toBeInTheDocument()
  })

  it('keeps the Apply & Restart action in the hero', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.getByRole('button', { name: /Restart/ })).toBeInTheDocument()
  })
})
