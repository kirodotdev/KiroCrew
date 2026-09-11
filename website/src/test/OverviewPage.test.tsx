import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import OverviewPage from '../pages/OverviewPage'
import { KIRO_SIGN_IN_BACKEND, KIRO_SIGN_IN_PATH } from '../pages/developer/kiroSignInLink'
import type { RootState } from '../store'

// Mock the two drill-in surfaces to isolate the mission-control shell.
vi.mock('../pages/overview', () => ({
  UsageTab: () => <div data-testid="usage-tab">UsageTab</div>,
  WakaTimeTab: () => <div data-testid="wakatime-tab">WakaTimeTab</div>,
}))
vi.mock('../pages/overview/MemoryTab', () => ({
  default: () => <div data-testid="memory-tab">MemoryTab</div>,
}))

vi.mock('../hooks/useUptime', () => ({
  useUptime: () => '2h 30m',
}))

vi.mock('../api/client', () => ({
  api: {
    memorySettings: vi.fn().mockResolvedValue({ history_idle_hours: 3, history_max_days: 90, migrated: false }),
    // Present only to be asserted NEVER called: the landing page must not read
    // the Kiro sign-in status, let alone render its chooser.
    kasLoginStatus: vi.fn().mockResolvedValue({ authenticated: false }),
    // The selected backend decides whether the sign-in signpost renders.
    kirocrewConfig: vi.fn().mockResolvedValue({ agent: { acp_backend: '' } }),
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
    // The landing view has no sub-tab bar.
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

  it('drills into Memory and back', async () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    // Summary cards share the verb, in render order: Usage (0), WakaTime (1),
    // Memory (2).
    fireEvent.click(screen.getAllByRole('button', { name: /View details/ })[2])
    expect(await screen.findByTestId('memory-tab')).toBeInTheDocument()
    expect(screen.queryByText('All systems running')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Overview/ }))
    expect(screen.getByText('All systems running')).toBeInTheDocument()
    expect(screen.queryByTestId('memory-tab')).not.toBeInTheDocument()
  })

  it('lets a member drill-in own its title while keeping one back action', async () => {
    renderWithProviders(<OverviewPage />, { store: statusStore(), route: '/settings/overview?view=memory&store=member-alice' })
    expect(await screen.findByTestId('memory-tab')).toBeInTheDocument()
    expect(screen.queryByText('Memory')).toBeNull()
    expect(screen.getAllByRole('button', { name: /Overview/ })).toHaveLength(1)
  })

  it('drills into Usage and back', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    fireEvent.click(screen.getAllByRole('button', { name: /View details/ })[0])
    expect(screen.getByTestId('usage-tab')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Overview/ }))
    expect(screen.queryByTestId('usage-tab')).not.toBeInTheDocument()
  })

  it('drills into WakaTime and back', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    fireEvent.click(screen.getAllByRole('button', { name: /View details/ })[1])
    expect(screen.getByTestId('wakatime-tab')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: /Overview/ }))
    expect(screen.queryByTestId('wakatime-tab')).not.toBeInTheDocument()
  })

  // Overview reads state and edits nothing, so it offers no apply/restart
  // action: the button that used to sit here had no change to apply, and its
  // one effect (dropping live agent sessions) belongs with the surfaces that
  // edit the config those sessions load. Asserted so it is not re-added.
  it('offers no restart action in the hero', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.queryByRole('button', { name: /Restart/i })).not.toBeInTheDocument()
  })

  // The region below the summary cards is empty in the stock build, and the
  // panel seam is only worth anything if the page actually READS it — a
  // registry nothing renders is the failure this asserts against.
  it('renders nothing in the lower panel region until a panel is registered', () => {
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.queryByTestId('edition-panel')).not.toBeInTheDocument()
  })

  it('renders a registered lower panel', async () => {
    const { registerOverviewPanel } = await import('../pages/overviewPanel')
    registerOverviewPanel({
      id: 'test:lower-panel',
      component: () => <div data-testid="edition-panel">registered</div>,
    })
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(screen.getByTestId('edition-panel')).toBeInTheDocument()
  })

  // The Kiro sign-in card serves the KAS backend only, a Developer Mode
  // preview, so it lives under Developer > Agent Backend. On the landing page
  // its provider chooser read as a required step to every user, first-run
  // installs included. Asserted so it is not re-added: neither the card nor a
  // read of its status belongs here.
  it('hosts no Kiro sign-in card and never reads the sign-in status', async () => {
    const { api } = await import('../api/client')
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(await screen.findByText('Memory')).toBeInTheDocument()
    expect(screen.queryByTestId('kiro-sign-in-card')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Continue with/ })).not.toBeInTheDocument()
    expect(api.kasLoginStatus).not.toHaveBeenCalled()
  })

  it('signposts the sign-in card only while KAS is the selected backend', async () => {
    const { api } = await import('../api/client')
    // Kiro CLI (the default, spelled as the empty string): no pointer, and no
    // wrapper left behind where the card used to sit.
    const first = renderWithProviders(<OverviewPage />, { store: statusStore() })
    expect(await screen.findByText('Memory')).toBeInTheDocument()
    expect(screen.queryByTestId('kiro-sign-in-moved')).not.toBeInTheDocument()
    first.unmount()

    vi.mocked(api.kirocrewConfig).mockResolvedValueOnce({ agent: { acp_backend: KIRO_SIGN_IN_BACKEND } })
    renderWithProviders(<OverviewPage />, { store: statusStore() })
    const pointer = await screen.findByTestId('kiro-sign-in-moved')
    expect(pointer).toHaveTextContent('Kiro sign-in moved to Developer > Agent Backend')
    // Same destination as the chat error row, so the two doors cannot drift.
    expect(pointer).toHaveAttribute('href', KIRO_SIGN_IN_PATH)
    expect(api.kasLoginStatus).not.toHaveBeenCalled()
  })
})
