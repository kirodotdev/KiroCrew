/**
 * OverviewView: the triage command center. Its own logic is which issues land in
 * "needs attention" (no triage yet or no reply), the newest-first and most-discussed
 * orderings, and that every row or tile opens the issue or the filtered list. The issue
 * context is the only mock; every number and row comes from the fixture below.
 */
import { fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Issue } from '../api'
import OverviewView from './OverviewView'

const ctx = vi.hoisted(() => ({ value: null as unknown }))
vi.mock('../context', () => ({ useIssueRadar: () => ctx.value }))

const DAY = 86400000
// The view ages every issue against the current time, so the clock is pinned to this
// instant and the fixture dates are built from it.
const NOW = Date.parse('2026-03-01T12:00:00Z')
const ago = (days: number) => new Date(NOW - days * DAY).toISOString()
const issue = (o: Partial<Issue> & { number: number; title: string }) =>
  ({ author: 'someone', comments: 0, thumbs_up: 0, assignees: [], author_association: 'MEMBER',
    created_at: ago(10), updated_at: ago(10), ...o }) as unknown as Issue

// A panel is the <section> that holds its title; a query inside it cannot match the same
// issue shown in another panel.
const panel = (title: string) => within(screen.getByText(title).closest('section') as HTMLElement)

let openIssues: ReturnType<typeof vi.fn>
let setSelectedIssue: ReturnType<typeof vi.fn>
let toggleLabel: ReturnType<typeof vi.fn>
let toggleAssignedToMe: ReturnType<typeof vi.fn>
let refresh: ReturnType<typeof vi.fn>

function setContext(over: Record<string, unknown> = {}) {
  ctx.value = {
    issues: [
      issue({ number: 11, title: 'Crash on save', author: 'ana', created_at: ago(2), updated_at: ago(2),
        comments: 0, thumbs_up: 3, assignees: ['octocat'], author_association: 'FIRST_TIME_CONTRIBUTOR' }),
      issue({ number: 12, title: 'Add dark theme', author: 'bo', created_at: ago(40), updated_at: ago(40), comments: 5 }),
      issue({ number: 13, title: 'Docs typo', author: 'cy', created_at: ago(200), updated_at: ago(1),
        comments: 2, thumbs_up: 1, assignees: ['octocat'] }),
      issue({ number: 14, title: 'Flaky upload', author: 'di', created_at: ago(20), updated_at: ago(20),
        comments: 0, thumbs_up: 5 }),
    ],
    sortedRepoLabels: [{ name: 'bug', color: 'd73a4a' }, { name: 'docs', color: '0075ca' }],
    countByLabel: new Map([['bug', 2], ['docs', 1]]),
    me: 'octocat',
    openIssues, setSelectedIssue, toggleLabel, toggleAssignedToMe, refresh,
    assignedToMe: false, refreshing: false, issuesLoading: false,
    needsTriage: (i: Issue) => i.number !== 13 && i.number !== 14,
    isGoodFirstIssue: (i: Issue) => i.number === 11,
    active: { provider: 'github' },
    ...over,
  }
}

beforeEach(() => {
  vi.setSystemTime(NOW)
  openIssues = vi.fn()
  setSelectedIssue = vi.fn()
  toggleLabel = vi.fn()
  toggleAssignedToMe = vi.fn()
  refresh = vi.fn()
  setContext()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('OverviewView', () => {
  it('filters the list to your issues from the Assigned to you tile', () => {
    render(<OverviewView />)
    fireEvent.click(screen.getByText('Assigned to you'))
    expect(toggleAssignedToMe).toHaveBeenCalledTimes(1)
  })

  it('refreshes the issue list from the refresh button', () => {
    render(<OverviewView />)
    fireEvent.click(screen.getByRole('button', { name: 'Refresh issues' }))
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('shows a failed refresh in an error notice, above the cached numbers', () => {
    const { rerender } = render(<OverviewView />)
    expect(screen.queryByRole('alert')).toBeNull()

    setContext({ issuesError: new Error('GitHub API returned 502') })
    rerender(<OverviewView />)
    expect(screen.getByRole('alert')).toHaveTextContent('GitHub API returned 502')
    // The cached tiles stay, so the notice is what says they may be old.
    expect(screen.getByText('Open issues')).toBeInTheDocument()
  })

  it('names the disabled refresh button for the refresh in flight, then restores it', () => {
    // A disabled "Refresh issues" reads as unavailable; while a refresh runs the
    // name and tooltip say it is busy instead.
    setContext({ refreshing: true })
    const { rerender } = render(<OverviewView />)

    const busy = screen.getByRole('button', { name: 'Refreshing issues…' })
    expect(busy).toHaveAttribute('aria-disabled', 'true')
    expect(busy).toHaveAttribute('title', 'Refreshing issues…')
    expect(screen.queryByRole('button', { name: 'Refresh issues' })).toBeNull()

    setContext({ refreshing: false })
    rerender(<OverviewView />)

    const idle = screen.getByRole('button', { name: 'Refresh issues' })
    expect(idle).not.toHaveAttribute('aria-disabled')
    expect(idle).toHaveAttribute('title', 'Re-fetch issues + labels from GitHub')
  })

  it('keeps keyboard focus on Refresh while it runs, and ignores a press then', () => {
    // A native `disabled` takes the focused button out of the tab order, so a
    // keyboard press dropped focus to the page for the whole refresh.
    const { rerender } = render(<OverviewView />)
    const button = screen.getByRole('button', { name: 'Refresh issues' })
    button.focus()

    setContext({ refreshing: true })
    rerender(<OverviewView />)
    expect(button).not.toBeDisabled()
    expect(button).toHaveFocus()
    fireEvent.click(button)
    expect(refresh).not.toHaveBeenCalled()

    setContext({ refreshing: false })
    rerender(<OverviewView />)
    expect(button).toHaveFocus()
  })

  it('shows no Assigned to you tile when nobody is signed in', () => {
    setContext({ me: null })
    render(<OverviewView />)
    expect(screen.queryByText('Assigned to you')).not.toBeInTheDocument()
  })

  it('lists issues with no triage yet or no reply, newest first, and opens the one clicked', () => {
    render(<OverviewView />)
    const needsAttention = panel('Needs attention')
    // 11 lacks triage and a reply, 12 only triage, 14 only a reply; 13 is triaged and
    // answered, so it is left out.
    expect(needsAttention.getAllByText(/^#\d+$/).map(el => el.textContent)).toEqual(['#11', '#14', '#12'])
    expect(needsAttention.queryByText('Docs typo')).not.toBeInTheDocument()
    fireEvent.click(needsAttention.getByText('Crash on save'))
    expect(setSelectedIssue).toHaveBeenCalledWith(11)
    expect(openIssues).toHaveBeenCalledTimes(1)
  })

  it('filters the list by a label from the label mix', () => {
    render(<OverviewView />)
    fireEvent.click(screen.getByText('bug'))
    expect(toggleLabel).toHaveBeenCalledWith('bug')
  })

  it('opens a discussed issue from the hotspots list', () => {
    render(<OverviewView />)
    fireEvent.click(panel('Discussion hotspots').getByText('Add dark theme'))
    expect(setSelectedIssue).toHaveBeenCalledWith(12)
  })

  it('ranks the discussion hotspots by comments, then by upvotes', () => {
    render(<OverviewView />)
    // 12 has 5 comments and 13 has 2; 11 and 14 have none, and 14 has more upvotes than 11.
    expect(panel('Discussion hotspots').getAllByText(/^#\d+$/).map(el => el.textContent))
      .toEqual(['#12', '#13', '#14', '#11'])
  })

  it('opens an issue from the recently updated list', () => {
    render(<OverviewView />)
    fireEvent.click(panel('Recently updated').getByText('Docs typo'))
    expect(setSelectedIssue).toHaveBeenCalledWith(13)
    expect(openIssues).toHaveBeenCalledTimes(1)
  })

  it('opens the issue list from every KPI tile', () => {
    render(<OverviewView />)
    const strip = within(screen.getByText('Open issues').closest('.grid') as HTMLElement)
    const labels = ['Open issues', 'Untriaged', 'Unanswered', 'Unassigned', 'Stale', 'New this week']
    for (const label of labels) fireEvent.click(strip.getByText(label))
    expect(openIssues).toHaveBeenCalledTimes(labels.length)
  })

  it('shows the loader while a first load has no issues yet', () => {
    setContext({ issues: [], issuesLoading: true })
    render(<OverviewView />)
    expect(screen.getByText('Loading overview…')).toBeInTheDocument()
  })

  it('says each activity panel is empty when the repo has no issues', () => {
    setContext({ issues: [], sortedRepoLabels: [], countByLabel: new Map() })
    render(<OverviewView />)
    expect(screen.getAllByText('No activity.')).toHaveLength(1)
    expect(screen.queryByText('Crash on save')).not.toBeInTheDocument()
    expect(panel('Needs attention').getByText('Nothing needs attention')).toBeInTheDocument()
    expect(panel('Label distribution').getByText('No labels in use.')).toBeInTheDocument()
    expect(panel('Recently updated').getByText('No activity.')).toBeInTheDocument()
    expect(panel('Discussion hotspots').getByText('No discussion yet.')).toBeInTheDocument()
  })
})
