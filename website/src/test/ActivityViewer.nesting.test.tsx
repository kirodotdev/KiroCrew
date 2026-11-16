/**
 * A Crew run's own harness-native child (`native:<run id>:<child>`) draws right after
 * the run's Subagents card, indented, and is described by the run card's title.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { Provider } from 'react-redux'
import { configureStore } from '@reduxjs/toolkit'
import chatReducer, { openActivityToTab } from '../store/chatSlice'
import dashboardReducer from '../store/dashboardSlice'
import notificationsReducer from '../store/notificationsSlice'
import type { SubagentActivity } from '../types'

// jsdom polyfill: SegmentedControl uses ResizeObserver
if (typeof globalThis.ResizeObserver === 'undefined') {
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
}

vi.mock('../api/client', () => ({
  api: {
    workflowRuns: vi.fn().mockResolvedValue({ runs: [] }),
    artifacts: vi.fn().mockResolvedValue({ artifacts: [] }),
    artifactSessionDocs: vi.fn().mockResolvedValue({ docs: [] }),
  },
}))

import ActivityViewer from '../pages/chat/ActivityViewer'
import { __resetPanelTabs } from '../hooks/usePanelTabs'

const RUN = '0123456789abcdef'
const OTHER = 'fedcba9876543210'
const CHILD = `native:${RUN}:child-0001`

function sub(id: string, agent: string, extra: Partial<SubagentActivity> = {}): SubagentActivity {
  return {
    id, task: `task of ${agent}`, agent, status: 'running', streaming: '', lastTool: '',
    startedAt: 1_700_000_000_000, elapsed: 0, ...extra,
  }
}

function renderPanel(subagents: Record<string, SubagentActivity>) {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(openActivityToTab('subagents'))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ActivityViewer toolLog={[]} open onToggle={vi.fn()} slot="test-slot" subagents={subagents} />
      </QueryClientProvider>
    </Provider>,
  )
}

describe('ActivityViewer run child nesting', () => {
  beforeEach(() => { localStorage.clear(); __resetPanelTabs() })

  it('draws a run child right after its run, indented and described by its title', () => {
    // The child is listed first and the other run between: the order is the nesting's.
    renderPanel({
      [CHILD]: sub(CHILD, 'grandchild'),
      [OTHER]: sub(OTHER, 'other-run'),
      [RUN]: sub(RUN, 'the-run'),
    })
    const codes = screen.getAllByText(/^(grandchild|other-run|the-run)$/).map(el => el.textContent)
    expect(codes).toEqual(['other-run', 'the-run', 'grandchild'])
    const nested = document.querySelector(`[data-parent-id="${RUN}"]`) as HTMLElement
    expect(nested).not.toBeNull()
    expect(nested.className).toContain('ml-4')
    expect(nested.textContent).toContain('grandchild')
    const describedBy = nested.querySelector('[role="button"]')!.getAttribute('aria-describedby')!
    const title = document.getElementById(describedBy)
    expect(title).not.toBeNull()
    expect(title!.closest('[role="button"]')!.textContent).toContain('the-run')
    expect(document.querySelectorAll('[data-parent-id]')).toHaveLength(1)
  })

  it('keeps a native card whose run is not shown at the top level', () => {
    renderPanel({ [CHILD]: sub(CHILD, 'grandchild'), [OTHER]: sub(OTHER, 'other-run') })
    expect(document.querySelectorAll('[data-parent-id]')).toHaveLength(0)
    expect(screen.getByText('grandchild')).toBeTruthy()
  })
})
