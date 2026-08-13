import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render } from '@testing-library/react'

// Surface Framer's `layout` prop as an attribute so the test can assert which
// layout mode each card is mounted with.
vi.mock('framer-motion', () => ({
  useReducedMotion: () => false,
  AnimatePresence: ({ children }: { children: React.ReactNode }) => <>{children}</>,
  motion: {
    button: ({
      children, layout, initial, animate, exit, transition, ...rest
    }: Record<string, unknown> & { children?: React.ReactNode }) => {
      void initial; void animate; void exit; void transition
      return <button data-layout={String(layout)} {...rest}>{children}</button>
    },
  },
}))

// Virtuoso measures 0 height in jsdom and would render no rows, so mock it to a
// plain flow that renders every item (same pattern as the ChatPage tests). Tag it
// so a test can assert the large-list path took the virtualized branch.
vi.mock('react-virtuoso', () => ({
  Virtuoso: ({ data, itemContent }: {
    data: unknown[]; itemContent: (i: number, d: unknown) => React.ReactNode
  }) => (
    <div data-testid="virtuoso">{data.map((d, i) => <div key={i}>{itemContent(i, d)}</div>)}</div>
  ),
}))

const ctx = { value: {} as Record<string, unknown> }
vi.mock('../apps/issue-radar/context', () => ({ useIssueRadar: () => ctx.value }))

const IssueList = (await import('../apps/issue-radar/components/IssueList')).default

const ISSUE = {
  number: 7,
  title: 'A title long enough to rewrap when the column narrows',
  author: 'octocat',
  updated_at: new Date().toISOString(),
  labels: [] as string[],
}

function manyIssues(n: number) {
  return Array.from({ length: n }, (_, i) => ({ ...ISSUE, number: i + 1 }))
}

function setCtx(rows: object[]) {
  ctx.value = {
    filteredIssues: rows, sortedIssues: rows, issues: rows,
    issuesLoading: false, issuesError: null, issuesPartial: false, stateFilter: 'open',
    colorByName: new Map<string, string>(),
    selectedIssue: null, setSelectedIssue: vi.fn(),
    refresh: vi.fn(), refreshing: false,
    query: '', setQuery: vi.fn(), issuesUpdatedAt: Date.now(),
  }
}

beforeEach(() => setCtx([ISSUE]))

describe('IssueList — layout animation during a column resize', () => {
  it('animates position only when idle', () => {
    // The default `layout` (size + position) animates a width change with a
    // scale transform, which visibly stretches the card text every time the
    // column rewraps. Position-only keeps reorder animation without the stretch.
    const { container } = render(<IssueList />)
    expect(container.querySelector('[data-layout]')!.getAttribute('data-layout')).toBe('position')
  })

  it('drops layout animation entirely while the handle is being dragged', () => {
    const { container } = render(<IssueList resizing />)
    expect(container.querySelector('[data-layout]')!.getAttribute('data-layout')).toBe('false')
  })
})

describe('IssueList — virtualization on large lists', () => {
  it('renders a small list as animated cards, NOT virtualized', () => {
    const { queryByTestId, container } = render(<IssueList />)
    expect(queryByTestId('virtuoso')).toBeNull()
    // Animated path: the motion.button carries the layout attribute.
    expect(container.querySelector('[data-layout]')).not.toBeNull()
  })

  it('switches to the virtualized scroller above the animation cap', () => {
    // A big repo would otherwise mount thousands of card nodes at once; over the
    // cap the list virtualizes and the per-card layout animation is dropped.
    setCtx(manyIssues(300))
    const { getByTestId, container } = render(<IssueList />)
    expect(getByTestId('virtuoso')).toBeTruthy()
    expect(container.querySelector('[data-layout]')).toBeNull()
  })
})
