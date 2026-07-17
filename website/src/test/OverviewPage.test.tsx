import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders, createTestStore } from './helpers'
import OverviewPage from '../pages/OverviewPage'
import type { RootState } from '../store'

// Mock all tab components to isolate OverviewPage shell logic
vi.mock('../pages/overview', () => ({
  MemoryTab: () => <div data-testid="memory-tab">MemoryTab</div>,
  CronTab: () => <div data-testid="cron-tab">CronTab</div>,
  SkillsTab: () => <div data-testid="skills-tab">SkillsTab</div>,
  McpTab: () => <div data-testid="mcp-tab">McpTab</div>,
  AgentCfgTab: () => <div data-testid="agentcfg-tab">AgentCfgTab</div>,
  KiroCrewCfgTab: () => <div data-testid="kirocrewcfg-tab">KiroCrewCfgTab</div>,
  DisplayTab: () => <div data-testid="display-tab">DisplayTab</div>,
  KiroUsageTab: () => <div data-testid="usage-tab">KiroUsageTab</div>,
}))

vi.mock('../hooks/useUptime', () => ({
  useUptime: () => '2h 30m',
}))

vi.mock('../api/client', () => ({
  api: {
    restartSessions: vi.fn().mockResolvedValue({}),
  },
}))

describe('OverviewPage', () => {
  it('renders stat cards', () => {
    renderWithProviders(<OverviewPage />)
    expect(screen.getByText('Uptime')).toBeInTheDocument()
  })

  it('shows stat cards with data', () => {
    const store = createTestStore({
      dashboard: {
        status: { uptime: '2h', sessions: 3, messages: 42, cron_jobs: 1, subagents: 0, lessons: 5 },
        slots: [],
        refreshTrigger: 0,
      } as RootState['dashboard'],
    })
    renderWithProviders(<OverviewPage />, { store })
    expect(screen.getByText('Uptime')).toBeInTheDocument()
    expect(screen.getByText('Sessions')).toBeInTheDocument()
  })

  it('shows memory tab by default', () => {
    renderWithProviders(<OverviewPage />)
    expect(screen.getByTestId('memory-tab')).toBeInTheDocument()
  })

  it('switches to KiroCrew Config tab', () => {
    renderWithProviders(<OverviewPage />)
    fireEvent.click(screen.getByText('KiroCrew Config'))
    expect(screen.getByTestId('kirocrewcfg-tab')).toBeInTheDocument()
  })

  it('switches to Agent Config tab', () => {
    renderWithProviders(<OverviewPage />)
    fireEvent.click(screen.getByText('Agent Config'))
    expect(screen.getByTestId('agentcfg-tab')).toBeInTheDocument()
  })
})
