import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { RepoLabel } from '../apps/issue-radar/api'

// FiltersSection is pure context wiring, so the context is the fixture. It renders
// the REAL FilterRow / LabelPalette / LabelRow so the wiring is asserted end to
// end rather than against stubs of the rows it drives.
const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({ useIssueRadar: () => ctx.value }))

const FiltersSection = (await import('../apps/issue-radar/components/FiltersSection')).default
const LabelPalette = (await import('../apps/issue-radar/components/LabelPalette')).default

const cycleSort = vi.fn()
const setStateFilter = vi.fn()
const setSelectedIssue = vi.fn()
const openIssues = vi.fn()
const toggleRequestedByMe = vi.fn()
const toggleAssignedToMe = vi.fn()
const toggleCreatedByMember = vi.fn()
const clearFilters = vi.fn()
const toggleLabel = vi.fn()

const LABELS: RepoLabel[] = [
  { name: 'zzq-bug', color: 'ff0000', description: 'crashes and wrongness' },
  { name: 'zzq-docs', color: '00ff00', description: '' },
  { name: 'zzq-perf', color: '0000ff', description: 'slow paths' },
]

beforeEach(() => {
  vi.clearAllMocks()
  ctx.value = {
    sortKey: 'number', sortDir: 'asc', cycleSort,
    stateFilter: 'open', setStateFilter, setSelectedIssue, openIssues,
    requestedByMe: false, toggleRequestedByMe,
    assignedToMe: false, toggleAssignedToMe,
    createdByMember: false, toggleCreatedByMember, hasMemberIssues: true,
    me: 'zzq-login',
    anyFilterActive: false, clearFilters,
    sortedRepoLabels: LABELS,
    countByLabel: new Map([['zzq-bug', 7]]),
    selectedLabels: new Set<string>(),
    toggleLabel,
    labelsLoading: false,
    labelsError: null,
  }
})

describe('FiltersSection', () => {
  it('cycles the sort field the row names', async () => {
    render(<FiltersSection />)
    await userEvent.click(screen.getByText('Last update'))
    expect(cycleSort).toHaveBeenCalledWith('updated')
  })

  it('clears the issue selection when the state filter changes', async () => {
    // The detail pane is resolved from the filtered list, so leaving a selection
    // behind across a state switch renders a pane for a row the list dropped.
    render(<FiltersSection />)
    await userEvent.click(screen.getByText('Closed'))
    expect(setStateFilter).toHaveBeenCalledWith('closed')
    expect(setSelectedIssue).toHaveBeenCalledWith(null)
    expect(openIssues).toHaveBeenCalled()
  })

  it('disables the per-person rows when there is no resolved login', async () => {
    ctx.value = { ...ctx.value, me: null }
    render(<FiltersSection />)
    const requested = screen.getByText('Requested by me')
    expect(requested).toHaveProperty('disabled', true)
    await userEvent.click(requested)
    expect(toggleRequestedByMe).not.toHaveBeenCalled()
  })

  it('enables the per-person rows once a login is known', async () => {
    render(<FiltersSection />)
    await userEvent.click(screen.getByText('Assigned to me'))
    expect(toggleAssignedToMe).toHaveBeenCalledTimes(1)
  })

  it('disables the member row when no loaded issue was opened by a member', async () => {
    ctx.value = { ...ctx.value, hasMemberIssues: false }
    render(<FiltersSection />)
    const row = screen.getByText('Created by member')
    expect(row).toHaveProperty('disabled', true)
    await userEvent.click(row)
    expect(toggleCreatedByMember).not.toHaveBeenCalled()
  })

  it('offers the clear affordance only while a filter is on', async () => {
    render(<FiltersSection />)
    expect(screen.queryByText('clear')).toBeNull()

    ctx.value = { ...ctx.value, anyFilterActive: true }
    render(<FiltersSection />)
    await userEvent.click(screen.getByText('clear'))
    expect(clearFilters).toHaveBeenCalledTimes(1)
  })
})

describe('LabelPalette', () => {
  function renderPalette(over: Partial<React.ComponentProps<typeof LabelPalette>> = {}) {
    return render(
      <LabelPalette
        labels={LABELS}
        countByLabel={new Map([['zzq-bug', 7]])}
        selected={new Set<string>()}
        onToggle={toggleLabel}
        loading={false}
        error={null}
        {...over}
      />,
    )
  }

  it('toggles the label a pill names', async () => {
    renderPalette()
    await userEvent.click(screen.getByText('zzq-perf'))
    expect(toggleLabel).toHaveBeenCalledWith('zzq-perf')
  })

  it('keeps non-matching rows mounted but hidden, so the palette morphs', async () => {
    renderPalette()
    await userEvent.click(screen.getByRole('button', { name: 'Search labels' }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Search labels' }), 'perf')

    // Still in the DOM (it animates closed), but marked hidden.
    const perfRow = screen.getByText('zzq-perf').closest('[aria-hidden]') as HTMLElement
    const docsRow = screen.getByText('zzq-docs').closest('[aria-hidden]') as HTMLElement
    expect(perfRow.getAttribute('aria-hidden')).toBe('false')
    expect(docsRow.getAttribute('aria-hidden')).toBe('true')
  })

  it('matches on the description as well as the name', async () => {
    renderPalette()
    await userEvent.click(screen.getByRole('button', { name: 'Search labels' }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Search labels' }), 'wrongness')
    const bugRow = screen.getByText('zzq-bug').closest('[aria-hidden]') as HTMLElement
    expect(bugRow.getAttribute('aria-hidden')).toBe('false')
  })

  it('clears the query when the search box is closed, so collapsed means unfiltered', async () => {
    renderPalette()
    await userEvent.click(screen.getByRole('button', { name: 'Search labels' }))
    const box = screen.getByRole('textbox', { name: 'Search labels' })
    await userEvent.type(box, 'perf')
    // The header toggle closes it — it is the first of the two dismiss controls
    // (the in-box X is the other path, exercised below).
    await userEvent.click(screen.getAllByRole('button', { name: 'Close label search' })[0])
    expect(screen.queryByRole('textbox', { name: 'Search labels' })).toBeNull()

    await userEvent.click(screen.getByRole('button', { name: 'Search labels' }))
    expect((screen.getByRole('textbox', { name: 'Search labels' }) as HTMLInputElement).value)
      .toBe('')
  })

  it('closes and clears from the in-box dismiss button', async () => {
    renderPalette()
    await userEvent.click(screen.getByRole('button', { name: 'Search labels' }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Search labels' }), 'zzq')
    const dismiss = screen.getAllByRole('button', { name: 'Close label search' })
    await userEvent.click(dismiss[dismiss.length - 1])
    expect(screen.queryByRole('textbox', { name: 'Search labels' })).toBeNull()
  })

  it('says so when a query matches nothing', async () => {
    renderPalette()
    await userEvent.click(screen.getByRole('button', { name: 'Search labels' }))
    await userEvent.type(screen.getByRole('textbox', { name: 'Search labels' }), 'nomatchxyz')
    expect(screen.getByText(/nomatchxyz/)).toBeInTheDocument()
  })

  it('reports loading, error and empty states instead of an empty block', () => {
    const loading = renderPalette({ labels: [], loading: true })
    expect(screen.getByText('Loading labels…')).toBeInTheDocument()
    loading.unmount()

    const failed = renderPalette({ labels: [], error: new Error('zzq-labels-broke') })
    expect(screen.getByText('zzq-labels-broke')).toBeInTheDocument()
    failed.unmount()

    renderPalette({ labels: [] })
    expect(screen.getByText('No labels on this repo.')).toBeInTheDocument()
    // With no labels there is nothing to search, so the toggle is not offered.
    expect(screen.queryByRole('button', { name: 'Search labels' })).toBeNull()
  })

  it('shows the selected pill as selected', () => {
    renderPalette({ selected: new Set(['zzq-bug']) })
    const pill = screen.getByText('zzq-bug').closest('button') as HTMLButtonElement
    expect(pill.className).toContain('font-bold')
  })
})
