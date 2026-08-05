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
    repos: [],
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

  it('applies the width it is given, defaulting to the former fixed width', () => {
    const { container, unmount } = render(<LeftRail />)
    expect((container.querySelector('aside') as HTMLElement).style.width).toBe('288px')
    unmount()
    const second = render(<LeftRail width={340} />)
    expect((second.container.querySelector('aside') as HTMLElement).style.width).toBe('340px')
  })
})

describe('LeftRail — collapsed strip', () => {
  const ACTIVE = { owner: 'kirodotdev', repo: 'KiroCrew' }
  const entry = (push: boolean) => ({ ...ACTIVE, permissions: { push, triage: false } })

  beforeEach(() => {
    ctx.value = { ...ctx.value, active: ACTIVE, repos: [entry(true)] }
  })

  it('shows the full owner/repo turned on its side and drops the accordion', () => {
    const { container } = render(<LeftRail width={48} collapsed />)
    // The org matters: two repos can share a name across owners.
    const name = screen.getByText('kirodotdev/KiroCrew')
    expect(name.style.writingMode).toBe('vertical-rl')
    // The nav sections have no room in a 48px strip.
    expect(screen.queryByText('Dashboards')).toBeNull()
    expect(container.querySelector('[data-testid="repo-switcher"]')).toBeNull()
    expect((container.querySelector('aside') as HTMLElement).style.width).toBe('48px')
    // The strip reads as one rounded card, matching the open rail's switcher pill.
    expect(container.querySelector('aside > div')!.className).toContain('rounded-xl')
  })

  it('repeats the owner/repo in the tooltip for a name too long to fit', async () => {
    render(<LeftRail width={48} collapsed />)
    const btn = screen.getByRole('button', { name: 'Expand sidebar' })
    expect(btn.getAttribute('title')).toContain('kirodotdev/KiroCrew')
  })

  it('keeps the app mark but not its name — no room at 48px', () => {
    const { container } = render(<LeftRail width={48} collapsed />)
    expect(screen.queryByText('Issue Radar')).toBeNull()
    // Name and version stay reachable through the mark's tooltip.
    expect(container.querySelector('[title="Issue Radar v0.1.0"]')).toBeTruthy()
  })

  it('carries the read-only flag through the collapse, trailing the repo name', () => {
    // Losing the flag would hide a real constraint on what the workspace can do,
    // and it belongs with the repo it describes — not parked at the card's foot.
    ctx.value = { ...ctx.value, repos: [entry(false)] }
    render(<LeftRail width={48} collapsed />)
    const tag = screen.getByText('Read Only')
    const name = screen.getByText('kirodotdev/KiroCrew')
    expect(tag.style.writingMode).toBe('vertical-rl')
    expect(name.parentElement).toBe(tag.parentElement)
    // DOCUMENT_POSITION_FOLLOWING — the tag comes after the name.
    expect(name.compareDocumentPosition(tag) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('does not flag a writable repo', () => {
    render(<LeftRail width={48} collapsed />)
    expect(screen.queryByText('Read Only')).toBeNull()
  })

  it('clicking the strip asks to expand', async () => {
    const onExpand = vi.fn()
    render(<LeftRail width={48} collapsed onExpand={onExpand} />)
    await userEvent.click(screen.getByRole('button', { name: 'Expand sidebar' }))
    expect(onExpand).toHaveBeenCalledTimes(1)
  })
})
