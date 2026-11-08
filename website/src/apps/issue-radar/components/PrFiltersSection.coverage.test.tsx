/**
 * PrFiltersSection: the "Pull requests" accordion body in the Issue Radar rail.
 *
 * It must drive the PR-scoped context on every control: a sort row cycles that field, a lifecycle row
 * replaces the state filter and navigates away from the open detail, a toggle flips its own flag, the
 * "me" rows are disabled without a signed-in user, the member row is disabled when no member authored a
 * listed PR, and Clear appears only while a filter is active. The context is mocked; FilterRow,
 * LabelPalette and the sort icons are real, so what is asserted is what the user sees and clicks.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const ctx = vi.hoisted(() => ({
  value: {} as Record<string, unknown>,
}))

vi.mock('../context', () => ({ useIssueRadar: () => ctx.value }))

import PrFiltersSection from './PrFiltersSection'

function fns() {
  return {
    cyclePrSort: vi.fn(),
    setPrStateFilter: vi.fn(),
    setSelectedPull: vi.fn(),
    openPulls: vi.fn(),
    togglePrDraftOnly: vi.fn(),
    togglePrAuthoredByMe: vi.fn(),
    togglePrAssignedToMe: vi.fn(),
    togglePrReviewRequestedByMe: vi.fn(),
    togglePrCreatedByMember: vi.fn(),
    clearPrFilters: vi.fn(),
    togglePrLabel: vi.fn(),
  }
}

function setContext(overrides: Record<string, unknown> = {}) {
  ctx.value = {
    prSortKey: 'number',
    prSortDir: 'asc',
    prStateFilter: 'open',
    prDraftOnly: false,
    prAuthoredByMe: false,
    prAssignedToMe: false,
    prReviewRequestedByMe: false,
    prCreatedByMember: false,
    hasMemberPulls: true,
    me: 'octocat',
    anyPrFilterActive: false,
    repoLabels: [{ name: 'bug', color: 'ff0000', description: '' }],
    countByPrLabel: new Map([['bug', 3]]),
    prSelectedLabels: new Set<string>(),
    labelsLoading: false,
    labelsError: null,
    active: { owner: 'zzq', repo: 'fabric', provider: 'github', host: 'github.com' },
    ...fns(),
    ...overrides,
  }
  return ctx.value as ReturnType<typeof fns> & Record<string, unknown>
}

beforeEach(() => {
  ctx.value = {}
})

describe('PrFiltersSection', () => {
  it('cycles the sort field of the row that is clicked', () => {
    const value = setContext()
    render(<PrFiltersSection />)
    fireEvent.click(screen.getByRole('button', { name: 'Last update' }))
    expect(value.cyclePrSort).toHaveBeenCalledWith('updated')
  })

  it('switches the lifecycle filter, clears the open detail and navigates once per click', () => {
    const value = setContext()
    const onNavigate = vi.fn()
    render(<PrFiltersSection onNavigate={onNavigate} />)
    fireEvent.click(screen.getByRole('button', { name: 'Merged' }))
    expect(value.setPrStateFilter).toHaveBeenCalledWith('merged')
    expect(value.setSelectedPull).toHaveBeenCalledWith(null)
    expect(value.openPulls).toHaveBeenCalledTimes(1)
    expect(onNavigate).toHaveBeenCalledTimes(1)
  })

  it('sets the closed-unmerged state from its own row', () => {
    const value = setContext()
    render(<PrFiltersSection />)
    fireEvent.click(screen.getByRole('button', { name: 'Closed (unmerged)' }))
    expect(value.setPrStateFilter).toHaveBeenCalledWith('closed')
  })

  it('marks only the filter rows that are on as pressed', () => {
    setContext({ prStateFilter: 'open', prDraftOnly: true })
    render(<PrFiltersSection />)
    const row = (name: string) => screen.getByRole('button', { name })
    expect(row('Open')).toHaveAttribute('aria-pressed', 'true')
    expect(row('Merged')).toHaveAttribute('aria-pressed', 'false')
    expect(row('Draft')).toHaveAttribute('aria-pressed', 'true')
    expect(row('Authored by me')).toHaveAttribute('aria-pressed', 'false')
  })

  it('toggles the draft flag without touching the lifecycle state', () => {
    const value = setContext()
    render(<PrFiltersSection />)
    fireEvent.click(screen.getByRole('button', { name: 'Draft' }))
    expect(value.togglePrDraftOnly).toHaveBeenCalledTimes(1)
    expect(value.setPrStateFilter).not.toHaveBeenCalled()
  })

  it('wires the authored, assigned and review-requested toggles when a user is signed in', () => {
    const value = setContext()
    render(<PrFiltersSection />)
    fireEvent.click(screen.getByRole('button', { name: 'Authored by me' }))
    fireEvent.click(screen.getByRole('button', { name: 'Assigned to me' }))
    fireEvent.click(screen.getByRole('button', { name: 'Review requested' }))
    expect(value.togglePrAuthoredByMe).toHaveBeenCalledTimes(1)
    expect(value.togglePrAssignedToMe).toHaveBeenCalledTimes(1)
    expect(value.togglePrReviewRequestedByMe).toHaveBeenCalledTimes(1)
  })

  it('disables the three "me" rows without a signed-in user and explains why', () => {
    const value = setContext({ me: undefined })
    render(<PrFiltersSection />)
    const review = screen.getByRole('button', { name: 'Review requested' })
    expect(screen.getByRole('button', { name: 'Authored by me' })).toBeDisabled()
    expect(screen.getByRole('button', { name: 'Assigned to me' })).toBeDisabled()
    expect(review).toBeDisabled()
    expect(review.title).toMatch(/gh/i)
    fireEvent.click(review)
    expect(value.togglePrReviewRequestedByMe).not.toHaveBeenCalled()
  })

  it('disables the member row when no listed PR was authored by a repo member', () => {
    const value = setContext({ hasMemberPulls: false })
    render(<PrFiltersSection />)
    const member = screen.getByRole('button', { name: 'Created by member' })
    expect(member).toBeDisabled()
    expect(member.title).toMatch(/no repo members found/i)
    fireEvent.click(member)
    expect(value.togglePrCreatedByMember).not.toHaveBeenCalled()
  })

  it('uses the change-request vocabulary in the member hint for a GitLab workspace', () => {
    setContext({
      hasMemberPulls: false,
      active: { owner: 'g', repo: 'p', provider: 'gitlab', host: 'gitlab.com' },
    })
    render(<PrFiltersSection />)
    expect(screen.getByRole('button', { name: 'Created by member' }).title).toMatch(/merge request/i)
  })

  it('shows Clear only while a PR filter is active and calls clearPrFilters', () => {
    const value = setContext()
    const { unmount } = render(<PrFiltersSection />)
    expect(screen.queryByRole('button', { name: /clear/i })).toBeNull()
    unmount()

    setContext({ anyPrFilterActive: true, clearPrFilters: value.clearPrFilters })
    render(<PrFiltersSection />)
    fireEvent.click(screen.getByRole('button', { name: /clear/i }))
    expect(value.clearPrFilters).toHaveBeenCalledTimes(1)
  })

  it('passes a label click through to togglePrLabel with the label name', () => {
    const value = setContext()
    render(<PrFiltersSection />)
    fireEvent.click(screen.getByRole('button', { name: /bug$/ }))
    expect(value.togglePrLabel).toHaveBeenCalledWith('bug')
  })
})
