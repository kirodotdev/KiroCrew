import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { render, screen, fireEvent, within, act } from '@testing-library/react'
import type { Key, ReactNode } from 'react'

import type { PullRequest } from '../apps/issue-radar/api'

// Behaviour pins for the pull-request LIST column (PrList.tsx) — the middle
// column of Issue Radar's PR surface.
//
// What these cover, and why each one is load-bearing:
//
//  * The row's state icon is derived from three independent fields, and a merged
//    PR is `state: 'closed'` WITH `merged_at` — so the precedence between them is
//    the thing that can silently invert and paint a merged PR as closed.
//  * A per-bucket check tally is only shown when it is COMPLETE. A truncated
//    tally (more checks than one API page) omits a page that could hold the only
//    failure, so "34 passing" there would be a confident lie — the aggregate
//    rollup dot covers every check and is what must appear instead.
//  * The diff bar rounds proportionally, and both ends are clamped: a one-line
//    removal must still claim a red block, and a one-line addition a green one,
//    or a lopsided PR reads as entirely one-sided.
//  * A missing metric is never rendered as a zero — the whole bottom row is
//    omitted when neither the diff shape nor the checks arrived.
//  * The select checkbox renders only on a writable repo (every bulk action would
//    403 otherwise) and sits BESIDE the card, not inside its <button>.
//  * The footer count carries the caveat that explains it: a capped closed/merged
//    page, or a repo-wide search result, each get their own tooltip.
//  * The provider vocabulary is not decoration — calling a merge request a pull
//    request in a GitLab workspace is simply wrong copy.

/** Framer's `useReducedMotion` is read at render; flipping this drives the
 * animated-vs-virtualized branch without a second mock factory. */
const reduce = { value: false }

// Surface Framer's `layout` prop as an attribute so a test can assert which
// layout mode the cards mount with (same pattern as IssueRadarListResize).
vi.mock('framer-motion', () => ({
  useReducedMotion: () => reduce.value,
  AnimatePresence: ({ children }: { children: ReactNode }) => <>{children}</>,
  motion: {
    div: ({
      children, layout, initial, animate, exit, transition, ...rest
    }: Record<string, unknown> & { children?: ReactNode }) => {
      void initial; void animate; void exit; void transition
      return <div data-layout={String(layout)} {...rest}>{children}</div>
    },
  },
}))

// Virtuoso measures 0 height under happy-dom and would render no rows, so mock it
// to a plain flow that renders every item. Tagged so a test can assert the large
// list took the virtualized branch. `computeItemKey` is honoured rather than
// substituted with the index: keying virtualized rows by position is what makes a
// reordered list reuse the wrong row's DOM, so the real key function has to run.
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent, computeItemKey }: {
    data: unknown[]
    itemContent: (i: number, d: unknown) => ReactNode
    computeItemKey: (i: number, d: unknown) => Key
  }) => (
    <div data-testid="virtuoso">
      {data.map((d, i) => <div key={computeItemKey(i, d)}>{itemContent(i, d)}</div>)}
    </div>
  ),
}))

// The bulk bar owns its own mutations, confirmation tokens and react-query hooks,
// and is pinned by its own tests. This file is about the list, so it becomes a
// marker.
vi.mock('../apps/issue-radar/components/PrBulkBar', () => ({
  default: () => <div>pr-bulk-bar</div>,
}))

const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({ useIssueRadar: () => ctx.value }))

const PrList = (await import('../apps/issue-radar/components/PrList')).default

const spies = {
  setSelectedPull: vi.fn(),
  refreshPulls: vi.fn(),
  setPrQuery: vi.fn(),
  togglePullChecked: vi.fn(),
  clearCheckedPulls: vi.fn(),
}

function pr(over: Partial<PullRequest> = {}): PullRequest {
  return {
    number: 7,
    title: 'Row title',
    url: 'https://github.com/kirodotdev/Kiro/pull/7',
    state: 'open',
    draft: false,
    labels: [],
    author: 'alice',
    updated_at: new Date().toISOString(),
    merged_at: null,
    ...over,
  }
}

function setCtx(rows: PullRequest[], over: Record<string, unknown> = {}) {
  ctx.value = {
    filteredPulls: rows,
    sortedPulls: rows,
    pullsLoading: false,
    pullsError: null,
    pullsPartial: false,
    prStateFilter: 'open',
    colorByName: new Map<string, string>([['bug', 'ff0000']]),
    selectedPull: null,
    pullsRefreshing: false,
    prQuery: '',
    pullsUpdatedAt: Date.now(),
    prPersonFilterActive: false,
    prSearchTruncatedAt: null,
    active: { owner: 'kirodotdev', repo: 'Kiro' },
    canWrite: false,
    checkedPulls: new Set<number>(),
    ...spies,
    ...over,
  }
}

/** The card <button> for PR `n` — located by the `#n` it prints, so the lookup
 * survives a title change and never matches the footer or the toolbar. */
function cardFor(n: number): HTMLElement {
  const card = screen.getAllByRole('button').find((b) => b.textContent?.includes(`#${n}`))
  if (!card) throw new Error(`no card rendered for #${n}`)
  return card
}

function manyPulls(n: number): PullRequest[] {
  return Array.from({ length: n }, (_, i) => pr({ number: i + 1, title: `PR ${i + 1}` }))
}

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.clearAllMocks()
  reduce.value = false
  setCtx([pr()])
})

afterEach(() => {
  vi.clearAllTimers()
  vi.useRealTimers()
})

describe('PrList — card composition', () => {
  it('paints the number, author, age, title and label chips', () => {
    setCtx([pr({ labels: ['bug', 'perf'] })])
    render(<PrList />)

    const card = cardFor(7)
    expect(card.textContent).toContain('#7')
    expect(card.textContent).toContain('alice')
    expect(within(card).getByText('Row title')).toBeTruthy()
    // The known label takes its repo colour; an unknown one falls back to grey
    // rather than rendering an invalid `#undefined`.
    expect(within(card).getByText('bug').getAttribute('style')).toContain('#ff0000')
    expect(within(card).getByText('perf').getAttribute('style')).toContain('#888888')
  })

  it('omits the author fragment when the row has no author', () => {
    setCtx([pr({ author: null })])
    render(<PrList />)
    expect(cardFor(7).textContent).not.toContain('·')
  })

  it('selects the PR when its card is clicked', () => {
    render(<PrList />)
    fireEvent.click(cardFor(7))
    expect(spies.setSelectedPull).toHaveBeenCalledWith(7)
  })

  it('marks the selected card with the accent border', () => {
    setCtx([pr()], { selectedPull: 7 })
    render(<PrList />)
    expect(cardFor(7).className).toContain('border-accent')
  })
})

describe('PrList — lifecycle icon precedence', () => {
  it('paints merged over closed, then draft, then open', () => {
    // A merged PR is ALSO `state: 'closed'`, so merged_at has to win or every
    // merged row reads as closed-unmerged.
    setCtx([
      pr({ number: 1, title: 'Merged one', state: 'closed', merged_at: '2026-07-01T00:00:00Z' }),
      pr({ number: 2, title: 'Closed one', state: 'closed' }),
      // draft is only reachable while open, and must not outrank a merge.
      pr({ number: 3, title: 'Draft one', draft: true }),
      pr({ number: 4, title: 'Open one' }),
    ])
    render(<PrList />)

    expect(cardFor(1).querySelector('svg.text-aim')).not.toBeNull()
    expect(cardFor(2).querySelector('svg.text-danger')).not.toBeNull()
    expect(cardFor(3).querySelector('svg.text-muted')).not.toBeNull()
    expect(cardFor(4).querySelector('svg.text-ok')).not.toBeNull()
  })

  it('treats a draft that was merged as merged', () => {
    setCtx([pr({ draft: true, state: 'closed', merged_at: '2026-07-01T00:00:00Z' })])
    render(<PrList />)
    const card = cardFor(7)
    expect(card.querySelector('svg.text-aim')).not.toBeNull()
    expect(card.querySelector('svg.text-muted')).toBeNull()
  })
})

describe('PrList — diff shape', () => {
  it('omits the whole bottom row when neither the diff nor the checks arrived', () => {
    render(<PrList />)
    // The metrics row is the only tabular-nums block inside a card; no row at all
    // is the point — a "0 files, +0 −0" would claim the PR changes nothing.
    expect(cardFor(7).querySelector('.tabular-nums')).toBeNull()
  })

  it('pluralizes the changed-file count', () => {
    setCtx([
      pr({ number: 1, title: 'One file', changed_files: 1 }),
      pr({ number: 2, title: 'Three files', changed_files: 3 }),
    ])
    render(<PrList />)
    expect(within(cardFor(1)).getByTitle('1 file changed')).toBeTruthy()
    expect(within(cardFor(2)).getByTitle('3 files changed')).toBeTruthy()
  })

  it('fills the bar proportionally and never rounds a removal away', () => {
    setCtx([pr({ additions: 100, deletions: 1 })])
    render(<PrList />)
    const card = cardFor(7)
    // 100/101 rounds to 5 blocks, which would erase the removal entirely — the
    // non-zero side always keeps one.
    expect(card.querySelectorAll('.bg-ok')).toHaveLength(4)
    expect(card.querySelectorAll('.bg-danger')).toHaveLength(1)
    expect(within(card).getByText('+100')).toBeTruthy()
    expect(within(card).getByText('\u22121')).toBeTruthy()
  })

  it('never rounds a tiny addition away either', () => {
    setCtx([pr({ additions: 1, deletions: 100 })])
    render(<PrList />)
    expect(cardFor(7).querySelectorAll('.bg-ok')).toHaveLength(1)
  })

  it('fills every block for a one-sided change', () => {
    setCtx([
      pr({ number: 1, title: 'Additions only', additions: 4, deletions: 0 }),
      pr({ number: 2, title: 'Removals only', additions: 0, deletions: 4 }),
    ])
    render(<PrList />)

    const added = cardFor(1)
    expect(added.querySelectorAll('.bg-ok')).toHaveLength(5)
    expect(within(added).queryByText(/^\u2212/)).toBeNull()

    const removed = cardFor(2)
    expect(removed.querySelectorAll('.bg-ok')).toHaveLength(0)
    expect(removed.querySelectorAll('.bg-danger')).toHaveLength(5)
    expect(within(removed).queryByText(/^\+/)).toBeNull()
  })

  it('renders no bar when only the file count is known', () => {
    setCtx([pr({ changed_files: 2, additions: 0, deletions: 0 })])
    render(<PrList />)
    const card = cardFor(7)
    expect(within(card).getByTitle('2 files changed')).toBeTruthy()
    expect(card.querySelectorAll('.bg-ok')).toHaveLength(0)
    expect(card.querySelectorAll('.bg-danger')).toHaveLength(0)
  })
})

describe('PrList — check reporting', () => {
  const counts = (over: Partial<Record<'failure' | 'running' | 'success' | 'other', number>> = {}) => ({
    failure: 0, running: 0, success: 0, other: 0, ...over,
  })

  it('tallies each non-empty bucket and omits the empty ones', () => {
    setCtx([pr({ checks_counts: counts({ failure: 2, running: 1, success: 34 }) })])
    render(<PrList />)

    const card = cardFor(7)
    expect(within(card).getByTitle('2 failing')).toBeTruthy()
    expect(within(card).getByTitle('34 passing')).toBeTruthy()
    // A green PR stays quiet: a zero bucket is absent, not rendered as "0".
    expect(within(card).queryByTitle('0 skipped / neutral')).toBeNull()
    // Only the running badge spins.
    expect(within(card).getByTitle('1 running').querySelector('.animate-spin')).not.toBeNull()
    expect(within(card).getByTitle('34 passing').querySelector('.animate-spin')).toBeNull()
  })

  it('falls back to the aggregate dot when the tally is truncated', () => {
    // The omitted page could hold the only failure, so "34 passing" would be a
    // confident lie; the rollup covers every check.
    setCtx([pr({
      checks_counts: counts({ success: 34 }), checks_truncated: true, checks_state: 'failure',
    })])
    render(<PrList />)

    const card = cardFor(7)
    expect(within(card).queryByTitle('34 passing')).toBeNull()
    expect(within(card).getByTitle('checks failing')).toBeTruthy()
  })

  it('falls back to the aggregate dot when every bucket is zero', () => {
    setCtx([pr({ checks_counts: counts(), checks_state: 'other' })])
    render(<PrList />)
    expect(within(cardFor(7)).getByTitle('checks skipped / neutral')).toBeTruthy()
  })

  it('paints the aggregate dot for each rollup state', () => {
    setCtx([
      pr({ number: 1, title: 'Failing', checks_state: 'failure' }),
      pr({ number: 2, title: 'Running', checks_state: 'running' }),
      pr({ number: 3, title: 'Passing', checks_state: 'success' }),
      pr({ number: 4, title: 'Neutral', checks_state: 'other' }),
    ])
    render(<PrList />)

    expect(within(cardFor(1)).getByLabelText('checks failing')).toBeTruthy()
    expect(within(cardFor(3)).getByLabelText('checks passing')).toBeTruthy()
    expect(within(cardFor(4)).getByLabelText('checks skipped / neutral')).toBeTruthy()
    // Only the in-flight dot animates.
    const running = within(cardFor(2)).getByLabelText('checks running')
    expect(running.querySelector('.animate-spin')).not.toBeNull()
    expect(within(cardFor(3)).getByLabelText('checks passing').querySelector('.animate-spin'))
      .toBeNull()
  })

  it('shows no check element at all when the enrichment did not run', () => {
    setCtx([pr({ checks_state: null, checks_counts: null, changed_files: 1 })])
    render(<PrList />)
    const card = cardFor(7)
    // The metrics row is present for the diff, but carries no rollup dot.
    expect(within(card).getByTitle('1 file changed')).toBeTruthy()
    expect(within(card).queryByLabelText(/^checks /)).toBeNull()
  })
})

describe('PrList — search box', () => {
  it('reports every keystroke to the shared query state', () => {
    render(<PrList />)
    fireEvent.change(screen.getByLabelText('Search Pull Requests'), { target: { value: 'flake' } })
    expect(spies.setPrQuery).toHaveBeenCalledWith('flake')
  })

  it('offers the clear affordance only while a query is set', () => {
    render(<PrList />)
    expect(screen.queryByRole('button', { name: 'Clear search' })).toBeNull()
  })

  it('clears the query from the clear button', () => {
    setCtx([pr()], { prQuery: 'flake' })
    render(<PrList />)
    fireEvent.click(screen.getByRole('button', { name: 'Clear search' }))
    expect(spies.setPrQuery).toHaveBeenCalledWith('')
  })
})

describe('PrList — list states', () => {
  it('shows skeleton cards instead of rows while the first page loads', () => {
    setCtx([pr()], { pullsLoading: true })
    render(<PrList />)
    expect(screen.getByRole('status')).toBeTruthy()
    expect(screen.queryByText('Row title')).toBeNull()
  })

  it('surfaces the fetch error message', () => {
    setCtx([], { pullsError: new Error('gh: rate limit exceeded') })
    render(<PrList />)
    expect(screen.getByText('gh: rate limit exceeded')).toBeTruthy()
  })

  it('blames the SEARCH when a query is what emptied the list', () => {
    setCtx([], { prQuery: '  zzz  ' })
    render(<PrList />)
    expect(screen.getByText('No Pull Requests match your search.')).toBeTruthy()
  })

  it('blames the FILTERS when nothing is being searched', () => {
    setCtx([])
    render(<PrList />)
    expect(screen.getByText('No matching Pull Requests.')).toBeTruthy()
    expect(screen.getByText('Try clearing a filter in the sidebar.')).toBeTruthy()
  })
})

describe('PrList — animation and virtualization', () => {
  it('animates position only, and drops layout animation entirely mid-resize', () => {
    // The default layout mode animates a width change with a scale transform,
    // which stretches the card text every time the column rewraps.
    const idle = render(<PrList />)
    expect(idle.container.querySelector('[data-layout]')!.getAttribute('data-layout'))
      .toBe('position')
    idle.unmount()

    const dragging = render(<PrList resizing />)
    expect(dragging.container.querySelector('[data-layout]')!.getAttribute('data-layout'))
      .toBe('false')
  })

  it('switches to the virtualized scroller above the animation cap', () => {
    setCtx(manyPulls(201))
    const { container } = render(<PrList />)
    expect(screen.getByTestId('virtuoso')).toBeTruthy()
    expect(container.querySelector('[data-layout]')).toBeNull()
    // The virtualized rows are still selectable.
    fireEvent.click(cardFor(1))
    expect(spies.setSelectedPull).toHaveBeenCalledWith(1)
  })

  it('virtualizes a small list too when the user asked for reduced motion', () => {
    reduce.value = true
    const { container } = render(<PrList />)
    expect(screen.getByTestId('virtuoso')).toBeTruthy()
    expect(container.querySelector('[data-layout]')).toBeNull()
  })
})

describe('PrList — bulk selection', () => {
  it('offers no checkbox on a read-only repo', () => {
    render(<PrList />)
    expect(screen.queryByRole('checkbox')).toBeNull()
  })

  it('ticks and unticks a row through the shared selection', () => {
    setCtx([pr()], { canWrite: true })
    render(<PrList />)

    const box = screen.getByRole('checkbox', { name: 'Select PR #7 for a bulk action' })
    expect((box as HTMLInputElement).checked).toBe(false)
    fireEvent.click(box)
    expect(spies.togglePullChecked).toHaveBeenCalledWith(7)
  })

  it('reflects an already-ticked row as checked', () => {
    setCtx([pr()], { canWrite: true, checkedPulls: new Set([7]) })
    render(<PrList />)
    const box = screen.getByRole('checkbox', { name: 'Select PR #7 for a bulk action' })
    expect((box as HTMLInputElement).checked).toBe(true)
  })

  it('disarms a selection on Escape', () => {
    setCtx([pr()], { canWrite: true, checkedPulls: new Set([7]) })
    render(<PrList />)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(spies.clearCheckedPulls).toHaveBeenCalledTimes(1)
    // Another key is not an escape hatch.
    fireEvent.keyDown(window, { key: 'Enter' })
    expect(spies.clearCheckedPulls).toHaveBeenCalledTimes(1)
  })

  it('binds no Escape handler when nothing is ticked', () => {
    setCtx([pr()], { canWrite: true })
    render(<PrList />)
    fireEvent.keyDown(window, { key: 'Escape' })
    expect(spies.clearCheckedPulls).not.toHaveBeenCalled()
  })
})

describe('PrList — footer', () => {
  it('pluralizes the count and carries no caveat for an unfiltered open list', () => {
    setCtx([pr({ number: 1, title: 'First' }), pr({ number: 2, title: 'Second' })])
    render(<PrList />)
    const count = screen.getByText('2 PRs')
    expect(count.hasAttribute('title')).toBe(false)
  })

  it('renders the singular count for one row', () => {
    render(<PrList />)
    expect(screen.getByText('1 PR')).toBeTruthy()
  })

  it('marks a capped repo-wide search with a + and says where the cap came from', () => {
    setCtx([pr()], { prPersonFilterActive: true, prSearchTruncatedAt: 50 })
    render(<PrList />)
    const count = screen.getByText('1 PR+')
    expect(count.getAttribute('title'))
      .toBe('Resolved by GitHub search across the whole repo, capped at the 50 most recently updated matches')
  })

  it('says an uncapped repo-wide search is complete', () => {
    setCtx([pr()], { prPersonFilterActive: true })
    render(<PrList />)
    expect(screen.getByText('1 PR').getAttribute('title'))
      .toMatch(/Resolved by GitHub search across the whole repo/)
  })

  it('warns that a closed/merged list is only the newest page', () => {
    setCtx([pr()], { prStateFilter: 'closed' })
    render(<PrList />)
    expect(screen.getByText('1 PR').getAttribute('title'))
      .toMatch(/capped at the 100 most recently updated/)
  })

  it('says the rest is still loading on a cold first paint', () => {
    setCtx([pr()], { pullsPartial: true })
    render(<PrList />)
    expect(screen.getByText('Loading the rest…')).toBeTruthy()
  })

  it('shows the age of the list once it has been fetched', () => {
    render(<PrList />)
    expect(screen.getByTitle('Time since the PR list was last fetched from GitHub')).toBeTruthy()
  })

  it('omits the age before the first fetch lands', () => {
    setCtx([pr()], { pullsUpdatedAt: 0 })
    render(<PrList />)
    expect(screen.queryByTitle('Time since the PR list was last fetched from GitHub')).toBeNull()
  })

  it('refetches from the refresh button', () => {
    render(<PrList />)
    const button = screen.getByRole('button', { name: 'Refresh pull requests' })
    expect(button.getAttribute('title')).toBe('Re-fetch Pull Requests from GitHub')
    fireEvent.click(button)
    expect(spies.refreshPulls).toHaveBeenCalledTimes(1)
  })

  it('locks the refresh button and spins it while a refetch is in flight', () => {
    setCtx([pr()], { pullsRefreshing: true })
    render(<PrList />)
    const button = screen.getByRole('button', { name: 'Refresh pull requests' })
    expect((button as HTMLButtonElement).disabled).toBe(true)
    expect(button.querySelector('.animate-spin')).not.toBeNull()
  })
})

describe('PrList — provider vocabulary', () => {
  it('calls them merge requests in a GitLab workspace', () => {
    setCtx([pr()], {
      active: { owner: 'acme', repo: 'widget', provider: 'gitlab', host: 'gitlab.com' },
      canWrite: true,
    })
    render(<PrList />)

    expect(screen.getByLabelText('Search Merge Requests')).toHaveProperty(
      'placeholder', 'Search Merge Requests…',
    )
    expect(screen.getByRole('checkbox', { name: 'Select MR #7 for a bulk action' })).toBeTruthy()
    const button = screen.getByRole('button', { name: 'Refresh merge requests' })
    expect(button.getAttribute('title')).toBe('Re-fetch Merge Requests from GitLab')
  })
})

describe('PrList — relative-time ticker', () => {
  it('keeps re-rendering the ages on its own interval', () => {
    render(<PrList />)
    // The "3m ago" labels would otherwise freeze at their first paint for as long
    // as the column stays open.
    act(() => { vi.advanceTimersByTime(90_000) })
    expect(cardFor(7).textContent).toContain('Row title')
  })
})
