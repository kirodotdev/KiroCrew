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
const openCrews = vi.fn()
const setCrewFilter = vi.fn()
const cycleCrewSort = vi.fn()

/** The rail's resting crew tallies. */
const CALM_COUNTS = { on_duty: 2, working: 1, paused: 0 }

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
    // Crews slice: the rail reads the tallies for its filter rows.
    crews: [],
    crewsLoading: false,
    crewCounts: CALM_COUNTS,
    crewView: { kind: 'crew', id: 'c1' },
    mainView: 'issues',
    openCrews,
    // The roster's filter and sort controls live here, mirroring how the issue and
    // PR sections hold theirs while their list column holds the list.
    crewFilter: 'all',
    setCrewFilter,
    crewSortKey: 'status',
    crewSortDir: 'asc',
    cycleCrewSort,
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

describe('LeftRail — crews section', () => {
  // Queried by test id, not by copy: the `apps.issueRadar.views.crews.*` catalog
  // keys are populated separately, so asserting on rendered English here would
  // couple the rail's behaviour to the state of the translation files.
  it('navigates with NO argument, so the crews page you were last on is restored', async () => {
    render(<LeftRail />)
    await userEvent.click(screen.getByText('Crews'))
    expect(openCrews).toHaveBeenCalledWith()
  })

  it('carries no count badge — nothing on this surface queues for a human', () => {
    // A crew never holds an issue waiting on a person: the one that needs a
    // decision says so on the issue, labels it and moves on. So there is no
    // per-repo number that has to survive the section being collapsed, and a badge
    // here would be a queue the product does not have.
    ctx.value = { ...ctx.value, crewCounts: { on_duty: 6, working: 3, paused: 1 } }
    const { container } = render(<LeftRail />)
    const header = screen.getByText('Crews').closest('button') as HTMLButtonElement
    expect(header).not.toBeNull()
    expect(container.querySelector('[data-testid="crews-needs-you"]')).toBeNull()
    // No stray tally in the header either — the counts belong to the filter rows.
    expect(header.textContent).toBe('Crews')
  })

  it('does not repeat the roster — that list, with its status, is column 2', () => {
    ctx.value = {
      ...ctx.value,
      expanded: 'crews',
      mainView: 'crews',
      crews: [
        { id: 'c1', name: 'Andromeda', enabled: true, retired_at: null, labels: [], status: 'working' },
        { id: 'c2', name: 'Whirlpool', enabled: true, retired_at: null, labels: [], status: 'idle' },
      ],
      crewCounts: { on_duty: 2, working: 1, paused: 0 },
    }
    render(<LeftRail />)
    // The section is a DESTINATION, not a second copy of the roster: column 2
    // already lists every crew AND carries each one's status dot and current
    // work item, so repeating the names here would duplicate one list and the
    // duplicate would be the copy without the state. The issues view sets the
    // precedent — its rail holds filters, its list column holds the issues.
    expect(screen.queryByText('Andromeda')).toBeNull()
    expect(screen.queryByText('Whirlpool')).toBeNull()
    // And no status word in column 1 either.
    expect(screen.queryByText('idle')).toBeNull()
    expect(screen.queryByText('working')).toBeNull()
  })


  it('carries the roster filters, with the server tally on each', async () => {
    // These moved out of column 2 so the three list columns are consistent: the
    // rail narrows a list, the column shows it. The counts are the SERVER's — the
    // roster payload has no per-crew work items to derive them from.
    ctx.value = {
      ...ctx.value,
      expanded: 'crews',
      crewCounts: { on_duty: 6, working: 3, paused: 1 },
    }
    render(<LeftRail />)
    expect(screen.getByTestId('crew-filter-all').getAttribute('aria-pressed')).toBe('true')
    expect(screen.getByTestId('crew-filter-working').textContent).toContain('3')

    await userEvent.click(screen.getByTestId('crew-filter-paused'))
    expect(setCrewFilter).toHaveBeenCalledWith('paused')
    // Navigates too, so picking a filter from a rail left open on another view is
    // not silent.
    expect(openCrews).toHaveBeenCalled()
  })

  it('cycles the roster sort from the rail', async () => {
    ctx.value = { ...ctx.value, expanded: 'crews' }
    render(<LeftRail />)
    expect(screen.getByTestId('crew-sort-status').getAttribute('aria-pressed')).toBe('true')
    await userEvent.click(screen.getByTestId('crew-sort-name'))
    expect(cycleCrewSort).toHaveBeenCalledWith('name')
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
