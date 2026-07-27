import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

// The rail only needs the navigation slice of the context.
const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({
  useIssueRadar: () => ctx.value,
}))
// The repo switcher pulls in the connect flow; the rail's nav is what's under test.
vi.mock('../apps/issue-radar/components/RepoSwitcher', () => ({
  default: () => <div data-testid="repo-switcher" />,
}))
vi.mock('../apps/issue-radar/components/DashboardsSection', () => ({ default: () => null }))
vi.mock('../apps/issue-radar/components/FiltersSection', () => ({ default: () => null }))
vi.mock('../apps/issue-radar/components/PrFiltersSection', () => ({ default: () => null }))
vi.mock('../apps/issue-radar/components/SettingsSection', () => ({ default: () => null }))

const LeftRail = (await import('../apps/issue-radar/components/LeftRail')).default

const openDashboard = vi.fn()

beforeEach(() => {
  vi.clearAllMocks()
  ctx.value = {
    expanded: 'filters',
    dashboardTab: 'tagging',
    openDashboard,
    openIssues: vi.fn(),
    openPulls: vi.fn(),
    openSettings: vi.fn(),
  }
})

describe('LeftRail', () => {
  it('returns to the dashboard you were last on, not Overview', async () => {
    // `dashboardTab` is persisted, so hardcoding 'overview' here threw away the
    // one piece of state the Dashboards section exists to remember: leaving for
    // Issues and coming back dumped you on Overview.
    render(<LeftRail />)
    await userEvent.click(screen.getByText('Dashboards'))
    expect(openDashboard).toHaveBeenCalledWith('tagging')
    expect(openDashboard).not.toHaveBeenCalledWith('overview')
  })

  it('still honours a persisted Overview tab', async () => {
    ctx.value = { ...ctx.value, dashboardTab: 'overview' }
    render(<LeftRail />)
    await userEvent.click(screen.getByText('Dashboards'))
    expect(openDashboard).toHaveBeenCalledWith('overview')
  })
})
