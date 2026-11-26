/**
 * A Subagents card draws the agent's reasoning (`thinking`, from its
 * `subagent_thinking` frames) as a collapsed reasoning row above the card's
 * Output, never inside it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, within } from '@testing-library/react'
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

const CARD = 'a1b2c3d4'

function renderCard(extra: Partial<SubagentActivity>) {
  const store = configureStore({
    reducer: { chat: chatReducer, dashboard: dashboardReducer, notifications: notificationsReducer },
  })
  store.dispatch(openActivityToTab('subagents'))
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const card: SubagentActivity = {
    id: CARD, task: 'solve the puzzle', agent: 'general-purpose', status: 'running',
    streaming: 'Alice', lastTool: '', startedAt: 1_700_000_000_000, elapsed: 0, ...extra,
  }
  render(
    <Provider store={store}>
      <QueryClientProvider client={qc}>
        <ActivityViewer toolLog={[]} open onToggle={vi.fn()} slot="test-slot" subagents={{ [CARD]: card }} />
      </QueryClientProvider>
    </Provider>,
  )
}

describe('ActivityViewer subagent reasoning', () => {
  beforeEach(() => { localStorage.clear(); __resetPanelTabs() })

  it('draws the reasoning collapsed above Output, and Output never holds it', () => {
    renderCard({ thinking: 'Dave is in seat 1. Eve sits in seat 2.' })
    const row = screen.getByTestId('subagent-thinking')
    const output = screen.getByTestId('subagent-output-body')
    expect(row.compareDocumentPosition(output) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    expect(output.textContent).toContain('Alice')
    expect(output.textContent).not.toContain('Dave')
    expect(screen.queryByTestId('thinking-body')).toBeNull()
    fireEvent.click(within(row).getByRole('button'))
    expect(screen.getByTestId('thinking-body').textContent).toContain('Dave is in seat 1. Eve sits in seat 2.')
  })

  it('draws no reasoning row for a card without reasoning', () => {
    renderCard({})
    expect(screen.queryByTestId('subagent-thinking')).toBeNull()
    expect(screen.getByTestId('subagent-output-body').textContent).toContain('Alice')
  })

  it('renders hostile reasoning as text', () => {
    renderCard({ thinking: '<img src=x onerror="alert(1)">' })
    const row = screen.getByTestId('subagent-thinking')
    fireEvent.click(within(row).getByRole('button'))
    expect(screen.getByTestId('thinking-body').textContent).toContain('<img src=x onerror="alert(1)">')
    expect(row.querySelector('img')).toBeNull()
  })
})
