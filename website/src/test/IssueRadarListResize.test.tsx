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

beforeEach(() => {
  ctx.value = {
    filteredIssues: [ISSUE], sortedIssues: [ISSUE], issues: [ISSUE],
    issuesLoading: false, issuesError: null, stateFilter: 'open',
    colorByName: new Map<string, string>(),
    selectedIssue: null, setSelectedIssue: vi.fn(),
    refresh: vi.fn(), refreshing: false,
    query: '', setQuery: vi.fn(), issuesUpdatedAt: Date.now(),
  }
})

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
