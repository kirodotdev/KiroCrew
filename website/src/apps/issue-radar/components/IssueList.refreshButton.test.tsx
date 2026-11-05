/**
 * IssueList footer refresh button: an icon-only control that is disabled while a
 * refresh runs. Its accessible name and tooltip have to say it is busy then, because
 * a disabled "Refresh issues" reads as unavailable with no reason. The issue context
 * is the only mock; `refreshing` is the one field the tests move.
 */
import { render, screen } from '@testing-library/react'
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
    expect(busy).toBeDisabled()
    expect(busy).toHaveAttribute('title', 'Refreshing issues…')
    expect(screen.queryByRole('button', { name: 'Refresh issues' })).toBeNull()

    setContext({ refreshing: false })
    rerender(<IssueList />)

    const idle = screen.getByRole('button', { name: 'Refresh issues' })
    expect(idle).toBeEnabled()
    expect(idle).toHaveAttribute('title', 'Re-fetch issues + labels from GitHub')
    expect(screen.queryByRole('button', { name: 'Refreshing issues…' })).toBeNull()
  })
})
