import { describe, it, expect, vi } from 'vitest'
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
import { openActivityToTab } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'

vi.mock('../api/client', () => ({
  api: {
    browseFiles: vi.fn().mockResolvedValue({ path: '/projects/foo', parent: '/', dirs: [], files: [] }),
    pullRequestSource: vi.fn().mockImplementation(() => new Promise(() => {})),
  },
}))

import ActivityViewer from '../pages/chat/ActivityViewer'
import { api } from '../api/client'

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

describe('ActivityViewer', () => {
  const baseProps = {
    subagents: {},
    toolLog: [],
    open: true,
    onToggle: vi.fn(),
    slot: 'test-slot',
  }

  // useSortableTable persists the chosen sort to localStorage keyed by tableId,
  // so clear it between tests to keep the file-browser sort tests independent.
  beforeEach(() => localStorage.clear())

  it('renders each detected PR as a source selector in the Changes view', () => {
    render(
      <ActivityViewer
        {...baseProps}
        view="changes"
        sources={[
          { provider: 'github', owner: 'octo', repo: 'alpha', number: 42, url: 'https://github.com/octo/alpha/pull/42' },
          { provider: 'gitlab', owner: 'team', repo: 'beta', number: 7, url: 'https://gitlab.com/team/beta/-/merge_requests/7' },
        ]}
        selectedSourceUrl="https://github.com/octo/alpha/pull/42"
        onSelectSource={vi.fn()}
      />,
      { wrapper },
    )

    expect(screen.getByRole('tab', { name: 'PR #42' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'MR !7' })).toBeInTheDocument()
    expect(screen.getByText('Loading source provider…')).toBeInTheDocument()
  })

  it('Resources hides links present in the Changes tab (sources) and keeps the rest', () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    const store = configureStore({
      reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
    })
    // Files tab is the default
    store.dispatch(openActivityToTab('files'))
    const prUrl = 'https://github.com/kirodotdev/KiroCrew/pull/42'
    render(
      <Provider store={store}>
        <QueryClientProvider client={qc}>
          <ActivityViewer
            {...baseProps}
            // The Changes tab surfaces this PR, so it should NOT also appear in Resources.
            sources={[{ url: prUrl, provider: 'github', number: 42, repo: 'KiroCrew' }]}
            navLinks={[
              { url: prUrl, type: 'cr', label: 'PR #42', msgIdx: 0 },
              // Not in `sources` (a code-review host Changes can't render) — must stay reachable.
              { url: 'https://code.amazon.com/reviews/CR-1', type: 'cr', label: 'CR-1', msgIdx: 0 },
              { url: 'https://code.amazon.com/packages/KiroCrew', type: 'other', label: 'KiroCrew repo', msgIdx: 0 },
            ]}
          />
        </QueryClientProvider>
      </Provider>,
    )
    expect(screen.getByText('Resources')).toBeInTheDocument()
    // Non-Changes links stay in Resources.
    expect(screen.getByText('KiroCrew repo')).toBeInTheDocument()
    expect(screen.getByText('CR-1')).toBeInTheDocument()
    // The link already shown in the Changes tab is hidden from Resources.
    expect(screen.queryByText('PR #42')).not.toBeInTheDocument()
  })
})
