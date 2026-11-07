/**
 * IssueList footer refresh button: an icon-only control that is disabled while a
 * refresh runs. Its accessible name and tooltip have to say it is busy then, because
 * a disabled "Refresh issues" reads as unavailable with no reason. The issue context
 * is the only mock; `refreshing` is the one field the tests move.
 */
import { fireEvent, render, screen } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import IssueList from './IssueList'

const ctx = vi.hoisted(() => ({ value: null as unknown }))
vi.mock('../context', () => ({ useIssueRadar: () => ctx.value }))

function setContext(over: Record<string, unknown> = {}) {
  ctx.value = {
    filteredIssues: [], sortedIssues: [], issues: [],
    issuesLoading: false, issuesError: null, issuesPartial: false,
    stateFilter: 'open', colorByName: new Map(),
    selectedIssue: null, setSelectedIssue: vi.fn(),
    refresh: vi.fn(), refreshing: false, listDetail: 'compact',
    query: '', setQuery: vi.fn(), issuesUpdatedAt: null,
    active: { provider: 'github' },
    ...over,
  }
}

beforeEach(() => setContext())

describe('IssueList refresh button', () => {
  it('names the disabled button for the refresh in flight, then restores it', () => {
    setContext({ refreshing: true })
    const { rerender } = render(<IssueList />)

    const busy = screen.getByRole('button', { name: 'Refreshing issues…' })
    expect(busy).toHaveAttribute('aria-disabled', 'true')
    expect(busy).toHaveAttribute('title', 'Refreshing issues…')
    expect(screen.queryByRole('button', { name: 'Refresh issues' })).toBeNull()

    setContext({ refreshing: false })
    rerender(<IssueList />)

    const idle = screen.getByRole('button', { name: 'Refresh issues' })
    expect(idle).not.toHaveAttribute('aria-disabled')
    expect(idle).toHaveAttribute('title', 'Re-fetch issues + labels from GitHub')
    expect(screen.queryByRole('button', { name: 'Refreshing issues…' })).toBeNull()
  })

  it('keeps keyboard focus on Refresh while it runs, and ignores a press then', () => {
    // A native `disabled` takes the focused button out of the tab order, so a
    // keyboard press dropped focus to the page for the whole refresh.
    const refresh = vi.fn()
    setContext({ refresh })
    const { rerender } = render(<IssueList />)
    const button = screen.getByRole('button', { name: 'Refresh issues' })
    button.focus()

    setContext({ refresh, refreshing: true })
    rerender(<IssueList />)
    expect(button).not.toBeDisabled()
    expect(button).toHaveFocus()
    fireEvent.click(button)
    expect(refresh).not.toHaveBeenCalled()

    setContext({ refresh, refreshing: false })
    rerender(<IssueList />)
    expect(button).toHaveFocus()
    fireEvent.click(button)
    expect(refresh).toHaveBeenCalledTimes(1)
  })
})
