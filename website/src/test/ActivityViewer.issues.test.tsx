/**
 * Test: the Issues side-panel view, and the Resources list subtracting anything
 * that already has a rich panel.
 *
 * The Resources list exists to keep links reachable. A link with its own panel
 * (Changes tab for a PR, Issues tab for an issue) must NOT also appear there, or
 * the panel shows the same resource twice — but a link neither parser can render
 * (an issue on a non-allowlisted host) must still survive in Resources.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent } from '@testing-library/react'

// jsdom polyfill: SegmentedControl uses ResizeObserver
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { ExtractedLink } from '../utils/extractChatLinks'
import type { PullRequestLink } from '../utils/pullRequestLinks'

vi.mock('../api/client', () => ({
  api: {
    browseFiles: vi.fn().mockResolvedValue({ path: '/p', parent: '/', dirs: [], files: [] }),
    pullRequestSource: vi.fn().mockImplementation(() => new Promise(() => {})),
    // The Issues view is render-tested here; its payload never resolves so the
    // assertions stay on the panel shell rather than a mocked provider payload.
    fetchIssueSource: vi.fn().mockImplementation(() => new Promise(() => {})),
    fileDiff: vi.fn().mockResolvedValue({ diff: '' }),
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    artifactSessionDocs: vi.fn().mockResolvedValue({ docs: [] }),
  },
}))

import ActivityViewer from '../pages/chat/ActivityViewer'
import { __resetPanelTabs } from '../hooks/usePanelTabs'

const PR_URL = 'https://github.com/acme/widgets/pull/12'
const ISSUE_URL = 'https://github.com/acme/widgets/issues/9'
const FOREIGN_ISSUE_URL = 'https://git.notallowlisted.example/team/api/-/issues/3'

const sources: PullRequestLink[] = [
  { url: PR_URL, provider: 'github', number: 12, repo: 'widgets', kind: 'change' },
]
const issues: PullRequestLink[] = [
  { url: ISSUE_URL, provider: 'github', number: 9, repo: 'widgets', kind: 'issue' },
]

const navLinks: ExtractedLink[] = [
  { url: PR_URL, type: 'cr', label: '#12', msgIdx: 0 },
  // Same issue, mentioned with a comment fragment: collapses to the canonical
  // issue identity, so it must be subtracted as well.
  { url: `${ISSUE_URL}#issuecomment-77`, type: 'issue', label: '#9', msgIdx: 1 },
  { url: FOREIGN_ISSUE_URL, type: 'issue', label: '#3', msgIdx: 2 },
  { url: 'https://example.com/design-doc', type: 'other', label: 'Design Doc', msgIdx: 3 },
]

function wrapper({ children }: { children: React.ReactNode }) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  return (
    <Provider store={store}>
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    </Provider>
  )
}

const baseProps = {
  subagents: {},
  toolLog: [],
  open: true as const,
  onToggle: vi.fn(),
  slot: 'test-slot',
}

describe('ActivityViewer – issues view', () => {
  beforeEach(() => { localStorage.clear(); __resetPanelTabs() })

  it('renders the Issues view when the session has issues', () => {
    render(
      <ActivityViewer
        {...baseProps}
        view="issues"
        issues={issues}
        selectedIssueUrl={ISSUE_URL}
        onSelectIssue={vi.fn()}
      />,
      { wrapper },
    )
    expect(screen.getByRole('status', { name: 'Loading issue' })).toBeInTheDocument()
  })

  it('shows an empty state, not the Files view, when Issues is opened with no issues', () => {
    // `issues` is not a PINNED view, so a tab opened from the + menu is never
    // auto-removed. Falling back to Files here would render the file list under
    // an "Issues" label, so the view must own an empty state instead.
    render(
      <ActivityViewer {...baseProps} view="issues" issues={[]} files={[]} />,
      { wrapper },
    )
    expect(screen.queryByRole('status', { name: 'Loading issue' })).toBeNull()
    expect(screen.queryByText('No files changed yet')).toBeNull()
    expect(screen.getByText(/No issues in this session yet/)).toBeInTheDocument()
  })

  it('offers an Issues tab in the internal tab bar only when issues exist', () => {
    // SegmentedControl measures its parent's clientWidth, which is 0 in jsdom,
    // so it renders in 'dropdown' mode: only the active segment is visible until
    // the trigger is opened. Open it, then look for the segment.
    const { unmount } = render(
      <ActivityViewer {...baseProps} issues={issues} selectedIssueUrl={ISSUE_URL} files={[]} />,
      { wrapper },
    )
    fireEvent.click(screen.getByRole('button', { name: /Files/ }))
    expect(screen.getByText('Issues')).toBeInTheDocument()
    unmount()

    render(<ActivityViewer {...baseProps} issues={[]} files={[]} />, { wrapper })
    fireEvent.click(screen.getByRole('button', { name: /Files/ }))
    expect(screen.queryByText('Issues')).toBeNull()
  })

  it('subtracts both PRs and issues from the Resources list, keeping unrenderable links', () => {
    render(
      <ActivityViewer
        {...baseProps}
        view="files"
        files={[]}
        sources={sources}
        selectedSourceUrl={PR_URL}
        issues={issues}
        selectedIssueUrl={ISSUE_URL}
        navLinks={navLinks}
      />,
      { wrapper },
    )
    const resourceHrefs = Array.from(
      document.querySelectorAll<HTMLAnchorElement>('a[target="_blank"]'),
    ).map(a => a.getAttribute('href'))
    // Both rich-panel links are gone…
    expect(resourceHrefs).not.toContain(PR_URL)
    expect(resourceHrefs).not.toContain(`${ISSUE_URL}#issuecomment-77`)
    // …while the non-allowlisted issue host and the plain link remain reachable.
    expect(resourceHrefs).toContain(FOREIGN_ISSUE_URL)
    expect(resourceHrefs).toContain('https://example.com/design-doc')
    // The surviving issue keeps its own Resources badge, not the PR badge.
    expect(screen.getByText('Issue')).toBeInTheDocument()
  })
})
